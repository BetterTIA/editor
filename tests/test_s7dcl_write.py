"""M5: model serialization to .s7dcl — roundtrip parse(serialize(parse(x))) on all sample networks,
replacing a network in the block file, models from the editor (LAD/FBD operations)."""
import copy
import glob
import os
import unittest

from tests.common import SAMPLES, PROJECT, TS0
import fbd
import model
import diff
import s7dcl
import s7dcl_write as z
import server


def match(a, b):
    return fbd.content_match(a, b) if a['language'] == 'FBD' else diff.content_match(a, b)


def reparse(nw):
    """network model -> text -> model (same language)"""
    content = z.network_text(nw)
    if nw.get('language') == 'FBD':
        return {'language': 'FBD', 'elements': fbd.parse(content)}
    r, p = s7dcl.parse_lad(content)
    return {'language': 'LAD', 'elements': p, 'rungs': r}


class TestRoundtrip(unittest.TestCase):
    def test_all_sample_networks(self):
        n = 0
        for f in glob.glob(os.path.join(SAMPLES, '**', '*.s7dcl'), recursive=True):
            text = s7dcl.read(f)
            b = s7dcl.parse_block(text)
            for nw in b['networks']:
                if 'elements' not in nw:
                    continue
                with self.subTest(block=os.path.basename(f), nw=nw['nw']):
                    self.assertEqual(match(nw, reparse(nw)), (True, []))
                    # replacing a network with itself: the whole block reads the same (other networks and attributes unchanged)
                    b2 = s7dcl.parse_block(z.replace_network(text, nw['nw'], z.network_text(nw)))
                    self.assertEqual(len(b2['networks']), len(b['networks']))
                    for x, y in zip(b['networks'], b2['networks']):
                        self.assertEqual(x['attributes'], y['attributes'])
                        if x['nw'] != nw['nw']:
                            self.assertEqual(x['text'], y['text'])
                        elif 'elements' in x:
                            self.assertEqual(match(x, y), (True, []))
                    n += 1
        self.assertGreaterEqual(n, 30)

    def test_text_as_export(self):
        f = os.path.join(SAMPLES, PROJECT, TS0, 'PLC_1', 'Blocks', 'Conveyor', 'FB_Conveyor.s7dcl')
        text = s7dcl.read(f)
        nw4 = s7dcl.parse_block(text)['networks'][3]
        self.assertEqual(z.replace_network(text, 4, z.network_text(nw4)), text)    # NW4 character for character as exported

    def test_crlf_and_errors(self):
        text = s7dcl.read(os.path.join(SAMPLES, PROJECT, TS0, 'PLC_1', 'Blocks', 'Conveyor', 'FC_Branches.s7dcl'))
        crlf = text.replace('\n', '\r\n')
        res = z.replace_network(crlf, 1, '        RUNG wire#powerrail\n        END_RUNG\n')
        self.assertNotIn('\n', res.replace('\r\n', ''))
        self.assertEqual(s7dcl.parse_block(res.replace('\r\n', '\n'))['networks'][0]['elements'], [])
        with self.assertRaises(z.WriteError):
            z.replace_network(text, 99, '')


class TestModelsFromEditor(unittest.TestCase):
    def nw(self, block, n):
        _, b = server._load(SAMPLES, PROJECT, block, TS0)
        return b['networks'][n - 1]

    def test_lad_operation(self):
        n = self.nw('FB_Conveyor', 2)
        m = {'elements': n['elements'], 'rungs': n['rungs']}
        m = model.insert(m, {'r': 0, 'k': 3}, {'type': 'contact', 'operand': '"Fuse_OK"'})[0]
        m = model.branch(m, {'r': 0, 'k': 4}, {'r': 0, 'k': 5}, {'type': 'contact_nc', 'operand': '#X'})[0]
        m = model.apply(m, {'type': 'insert', 'place': {'r': 0, 'k': 0},
                             'element': {'type': 'comparison', 'op': '>=', 'in1': '#A', 'in2': '5', 'template': 'Int'}})[0]
        self.assertEqual(match(dict(m, language='LAD'), reparse(m)), (True, []))
        self.assertIn('{ S7_Templates := "SrcType := Int" }\n            GE_Contact(', z.lad_text(m))

    def test_lad_call_fc_and_instruction(self):
        m = {'elements': [{'id': 'p1', 'type': 'box', 'call': 'fc', 'name': 'FC_Branches', 'instance': None, 'template': None,
                        'power': ['EN', 'ENO'], 'inputs': [], 'outputs': []},
                       {'id': 'p2', 'type': 'box', 'call': 'instruction', 'name': 'CONV', 'instance': None,
                        'template': 'Int to DInt', 'power': ['EN', 'ENO'],
                        'inputs': [{'pin': 'IN', 'operand': '#A'}], 'outputs': [{'pin': 'OUT', 'operand': '#B'}]}],
             'rungs': [{'from': 'powerrail', 'path': ['p1', 'p2'], 'to': None}]}
        t = z.lad_text(m)
        self.assertIn('"FC_Branches"(  )', t)
        self.assertIn('Convert(\n                in := #A,\n                out => #B', t)
        r = reparse(m)
        self.assertEqual(r['elements'][0]['instance'], '"FC_Branches"')          # parser: a call; the server resolves it as an FC
        self.assertEqual(diff.content_match({'elements': m['elements'][1:], 'rungs': []},
                                             {'elements': r['elements'][1:], 'rungs': []}), (True, []))

    def test_fbd_operation(self):
        n = self.nw('FB_Safety', 3)
        m = {'language': 'FBD', 'elements': n['elements']}
        for op in ({'type': 'add_input', 'id': 'b1', 'operand': '#New'},
                   {'type': 'negate', 'id': 'b1', 'input': 3},
                   {'type': 'insert_box', 'element': 'OR', 'to': {'id': 'b1', 'input': 2}},
                   {'type': 'change_operand', 'id': 'n1', 'input': 2, 'operand': '#Service'},
                   {'type': 'insert_box', 'element': 'SR'},
                   {'type': 'change_operand', 'id': 'n2', 'operand': '#Mem'},
                   {'type': 'change_operand', 'id': 'n2', 'input': 1, 'operand': '#Set'},
                   {'type': 'change_operand', 'id': 'n2', 'input': 2, 'operand': '#Reset'}):
            m = fbd.apply(m, op)[0]
        self.assertEqual(match(m, reparse(m)), (True, []))

    def test_fbd_negate_main_output(self):
        n = self.nw('FB_Safety', 2)                     # O -> wire#w1 -> A in2
        m = fbd.apply({'language': 'FBD', 'elements': n['elements']}, {'type': 'negate', 'id': 'b3', 'output': 'main'})[0]
        r = reparse(m)
        self.assertIn('in2 := NOT wire#w1', z.fbd_text(m))   # written as a negation of the consumer's input
        a = next(p for p in r['elements'] if p['type'] == 'and')
        self.assertTrue(a['inputs'][1]['neg'])

    def test_fbd_cycle(self):
        m = {'language': 'FBD', 'elements': [
            {'id': 'b1', 'type': 'and', 'inputs': [{'n': 1, 'pin': 'in1', 'operand': '', 'signal': 'b2', 'neg': False}],
             'outputs': []},
            {'id': 'b2', 'type': 'and', 'inputs': [{'n': 1, 'pin': 'in1', 'operand': '', 'signal': 'b1', 'neg': False}],
             'outputs': []}]}
        with self.assertRaises(z.WriteError):
            z.fbd_text(copy.deepcopy(m))


if __name__ == '__main__':
    unittest.main()
