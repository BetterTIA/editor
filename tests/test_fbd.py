"""F1 — FBD: parser, rozložení, operace, rozdíl, API (vzorek FB_Bezpecnost, smyšlená jména)."""
import copy
import http.client
import json
import shutil
import tempfile
import threading
import unittest

from tests.common import SAMPLES, PROJECT, TS0, TS2
import fbd
import diff
import s7dcl
import server

BLOCK = 'FB_Bezpecnost'


def nw(n, ts=TS0):
    _, b = server._load(SAMPLES, PROJECT, BLOCK, ts)
    return b['networks'][n - 1]


def types(n):
    return [p['type'] for p in nw(n)['elements']]


def inputs(p):
    return [(v['pin'], v['operand'], v['signal'], v['neg']) for v in p['inputs']]


def wrap(text):
    return '    NETWORK\n' + text + '\n    END_NETWORK\n'


class TestParserFBD(unittest.TestCase):
    def test_all_networks(self):
        _, b = server._load(SAMPLES, PROJECT, BLOCK, TS0)
        self.assertEqual((b['kind'], b['number'], b['language'], len(b['networks'])), ('FB', 30, 'FBD', 9))
        for n in b['networks']:
            self.assertEqual(n['language'], 'FBD')
            self.assertNotIn('error', n, 'NW%d' % n['nw'])
            fbd.verify_model(fbd.model(n['elements']))

    def test_bit_logic(self):
        p = nw(1)['elements']
        self.assertEqual([x['type'] for x in p], ['or', 'and', 'assign'])
        self.assertEqual(inputs(p[0]), [('in1', '#Start', None, False), ('in2', '#Servis', None, False)])
        self.assertEqual(inputs(p[1])[:2], [('in1', '', 'b1', False), ('in2', '#Porucha', None, True)])
        self.assertEqual([v['n'] for v in p[1]['inputs']], [1, 2, 3])
        self.assertEqual((p[2]['operand'], p[2]['inputs'][0]['signal']), ('#Povoleni', 'b2'))

    def test_wire_input_from_other_rung(self):
        p = nw(2)['elements']
        self.assertEqual(types(2), ['and', 'assign', 'or'])
        self.assertEqual(p[0]['inputs'][1]['signal'], 'b3')            # in2 := wire#w1 <- END_RUNG wire#w1
        self.assertEqual(p[2]['inputs'][2], {'n': 3, 'pin': 'in3', 'operand': '#Blokace', 'signal': None, 'neg': True})
        self.assertEqual(fbd.signals(p), [{'from': 'b3', 'to': 'b1', 'input': 2}, {'from': 'b1', 'to': 'b2', 'input': 1}])

    def test_tap_and_assignment_chain(self):
        p = nw(3)['elements']
        self.assertEqual(types(3), ['and', 'assign', 'assign', 'assign'])
        self.assertEqual(p[2]['inputs'][0]['signal'], 'b2')            # řetěz přiřazení
        self.assertEqual(inputs(p[3]), [('in1', '', 'b1', True)])     # odbočka wire#w2 za & + NOT Coil

    def test_call_f_block(self):
        f, h = nw(4)['elements']
        self.assertEqual((f['type'], f['name'], f['instance']), ('call', 'ESTOP1', '#NouzStop'))
        self.assertEqual(f['attributes'], {'f_user_card': '1', 'f_image_card': '0'})
        self.assertNotIn('EN', [v['pin'] for v in f['inputs']])        # F-blok bez napájení nemá EN
        v = {x['pin']: x for x in f['inputs']}
        self.assertTrue(v['ACK_NEC']['neg'])
        self.assertEqual((v['ACK']['signal'], v['TIME_DEL']['operand']), ('b2', ''))
        self.assertEqual([x['pin'] for x in f['outputs']], ['Q', 'Q_DELAY', 'ACK_REQ', 'DIAG'])
        self.assertEqual((h['type'], h['operand'], inputs(h)), ('ptrig', '#Kvitace_Pamet', [('CLK', '#Kvitace', None, False)]))
        d = nw(5)['elements'][0]
        self.assertEqual([x['pin'] for x in d['inputs'] if x['neg']], ['QBAD_IN1', 'QBAD_IN2'])

    def test_s_r_sr_true_move_xor_empty(self):
        p = nw(6)['elements']
        self.assertEqual(types(6), ['s', 'r', 'sr', 'assign'])
        self.assertEqual((p[2]['operand'], [v['pin'] for v in p[2]['inputs']]), ('#Pamet_SR', ['S', 'R1']))
        p = nw(7)['elements']
        self.assertEqual(p[0]['inputs'][0]['operand'], 'true')
        self.assertEqual((p[1]['type'], p[1]['name'], inputs(p[1])[0]), ('instruction', 'MOVE', ('EN', 'TRUE', None, False)))
        self.assertEqual(types(8), ['xor', 'ntrig', 'assign'])
        self.assertEqual(nw(9)['elements'], [])

    def test_construction_from_export(self):
        # NOT před prvkem = negovaný 1. vstup; RUNG bez operandu + Coil = přiřazení s otevřeným vstupem
        b = s7dcl.parse_block('FUNCTION_BLOCK "X"\n    { S7_Language := "FBD" }\n' + wrap(
            '        RUNG "a"\n            NOT A( in2 := NOT "b" )\n            Coil( "c" )\n        END_RUNG\n'
            '        RUNG\n            Coil( "d" )\n        END_RUNG wire#w1\n'
            '        RUNG "e"\n            O( in2 := wire#w1 )\n        END_RUNG') + 'END_FUNCTION_BLOCK\n')
        p = b['networks'][0]['elements']
        self.assertEqual(inputs(p[0]), [('in1', '"a"', None, True), ('in2', '"b"', None, True)])
        self.assertEqual(inputs(p[2]), [('in1', '', None, False)])
        self.assertEqual(p[3]['inputs'][1]['signal'], 'b3')

    def test_unknown_wire_does_not_break_block(self):
        b = s7dcl.parse_block('FUNCTION_BLOCK "X"\n    { S7_Language := "FBD" }\n' + wrap(
            '        RUNG "a"\n            A( in2 := wire#w9 )\n        END_RUNG') + 'END_FUNCTION_BLOCK\n')
        self.assertIn('w9', b['networks'][0]['error'])


class TestLayoutFBD(unittest.TestCase):
    def roz(self, n):
        return fbd.layout(nw(n)['elements'], 900)

    def test_all_without_exception(self):
        for n in range(1, 10):
            r, k = self.roz(n)
            self.assertTrue(k['fbd'])
            self.assertEqual(set(r), {p['id'] for p in nw(n)['elements']})

    def test_chain_left_to_right_on_one_line(self):
        r, k = self.roz(1)
        self.assertEqual([r[i]['c'] for i in ('b1', 'b2', 'b3')], [0, 1, 2])
        self.assertEqual(r['b1']['r'], r['b2']['r'])                    # výstup >=1 = 1. vstup &
        self.assertEqual(r['b2']['r'], r['b3']['r'])
        b = {d['id']: d for d in k['boxes']}
        self.assertEqual((b['b1']['symbol'], b['b2']['symbol'], b['b3']['symbol'], b['b3']['above']), ('>=1', '&', '=', '#Povoleni'))
        self.assertTrue(b['b2']['inputs'][1]['neg'])
        self.assertIsNone(b['b2']['inputs'][0]['operand'])               # napájený signálem: bez popisku
        self.assertEqual(k['nodes'], [])

    def test_box_on_second_input(self):
        r, _ = self.roz(2)
        self.assertEqual((r['b3']['c'], r['b1']['c'], r['b2']['c']), (0, 1, 2))
        self.assertEqual(r['b3']['r'], r['b1']['r'] + 1)                 # výstup >=1 na lince in2 boxu &

    def test_tap_with_node(self):
        r, k = self.roz(3)
        self.assertEqual(r['b4']['c'], r['b2']['c'])                     # větev za & ve stejném sloupci
        self.assertGreater(r['b4']['r'], r['b3']['r'])
        self.assertEqual(len(k['nodes']), 1)

    def test_empty_and_cycle(self):
        _, k = self.roz(9)
        self.assertTrue(k['empty'])
        p = copy.deepcopy(nw(1)['elements'])
        p[0]['inputs'][0]['signal'] = 'b3'
        with self.assertRaises(fbd.LayoutError):
            fbd.layout(p)


class TestOperationsAndDiffFBD(unittest.TestCase):
    def test_operation(self):
        m = fbd.model(nw(1)['elements'])
        m, i = fbd.apply(m, {'type': 'add_input', 'id': 'b2', 'operand': '#Zamek_OK'})
        self.assertEqual(m['elements'][1]['inputs'][-1], {'n': 4, 'pin': 'in4', 'operand': '#Zamek_OK', 'signal': None, 'neg': False})
        m, _ = fbd.apply(m, {'type': 'change_operand', 'id': 'b3', 'operand': '#Jine'})
        m, _ = fbd.apply(m, {'type': 'negate', 'id': 'b2', 'input': 2})
        self.assertEqual((m['elements'][2]['operand'], m['elements'][1]['inputs'][1]['neg']), ('#Jine', False))
        m, _ = fbd.apply(m, {'type': 'change_operand', 'id': 'b2', 'input': 1, 'operand': '#Prime'})
        self.assertEqual(inputs(m['elements'][1])[0], ('in1', '#Prime', None, False))      # signál nahrazen operandem
        self.assertEqual([s['to'] for s in m['signals']], ['b3'])
        for bad in ({'type': 'add_input', 'id': 'b3'}, {'type': 'negate', 'id': 'b2'}, {'type': 'nothing', 'id': 'b1'},
                       {'type': 'negate', 'id': 'b9', 'input': 1}, {'type': 'negate', 'id': 'b2', 'input': 7}):
            with self.assertRaises(fbd.OperationError):
                fbd.apply(m, bad)
        self.assertEqual([(c['id'], c.get('warnings')) for c in fbd.validate(m)], [('b1', True)])   # >=1 odpojen (F2)
        m['elements'][2]['operand'] = ''
        self.assertEqual([c['id'] for c in fbd.validate(m)], ['b1', 'b3'])

    def test_verify_model(self):
        p = copy.deepcopy(nw(1)['elements'])
        p[2]['inputs'][0]['signal'] = 'b9'
        with self.assertRaises(ValueError):
            fbd.verify_model({'language': 'FBD', 'elements': p})
        with self.assertRaises(ValueError):
            fbd.verify_model({'language': 'FBD', 'elements': [{'id': 'b1', 'type': 'contact', 'inputs': [], 'outputs': []}]})

    def test_diff_and_ghost(self):
        before = fbd.model(nw(3)['elements'])
        after, _ = fbd.apply(before, {'type': 'add_input', 'id': 'b1', 'operand': '#Novy'})
        after['elements'] = [p for p in after['elements'] if p['id'] != 'b4']               # smazaná červená kontrolka
        after['elements'].append({'id': 'n1', 'type': 'assign', 'operand': '#X', 'outputs': [],
                            'inputs': [{'n': 1, 'pin': 'in1', 'operand': '', 'signal': 'b2', 'neg': False}]})
        changes, deleted = diff.compare(before, after)
        self.assertEqual((changes, deleted), ({'b1': 'changed', 'n1': 'added'}, ['b4']))
        self.assertEqual(fbd.pin_changes(before, after, {'b1'}), {'b1': {'inputs': [3], 'outputs': [], 'above': False}})
        elements = fbd.merge(before, after, deleted)
        self.assertEqual(elements[-1]['id'], 'x:b4')
        self.assertEqual(elements[-1]['inputs'][0]['signal'], 'b1')               # přízrak visí na odbočce za &
        r, k = fbd.layout(elements)
        self.assertIn('x:b4', r)


class TestApiFBD(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.srv = server.create_server(0, SAMPLES, self.data)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.data, ignore_errors=True)

    def req(self, method, fpath, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        c.request(method, fpath, body=json.dumps(body).encode() if body is not None else None,
                  headers={'Content-Type': 'application/json'})
        r = c.getresponse()
        t = json.loads(r.read())
        c.close()
        return r.status, t

    def test_network_and_proposal(self):
        s, n = self.req('GET', '/api/network?project=%s&block=%s&nw=1&ts=%s' % (PROJECT, BLOCK, TS0))
        self.assertEqual((s, n['language'], n['drawing']['fbd'], len(n['signals'])), (200, 'FBD', True, 2))
        # Claude: zkouška operací (nic neukládá) -> návrh
        s, r = self.req('POST', '/api/layout', {'project': PROJECT, 'after': {'language': 'FBD', 'elements': n['elements']},
                                                    'before': {'language': 'FBD', 'elements': n['elements']},
                                                    'ops': [{'type': 'add_input', 'id': 'b2', 'operand': '#Zamek_OK'},
                                                            {'type': 'change_operand', 'id': 'b3', 'operand': '#Povoleni_Pohon'}]})
        self.assertEqual((s, r['diff'], r['errors'], r['last']), (200, {'b2': 'changed', 'b3': 'changed'}, [], 'b3'))
        self.assertEqual(r['pin_diff']['b2']['inputs'], [4])
        s, u = self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'F1', 'block': BLOCK, 'nw': 1,
                                                'after': {'elements': r['after']['elements']}, 'block_number': 999, 'title': 'x'})
        self.assertEqual((s, u['state'], u['language'], u['block_number'], u['title']),
                         (201, 'Proposal', 'FBD', 30, 'Povolení — >=1, & s negací, přiřazení'))
        self.assertEqual((u['before']['language'], u['before']['export_ts'], u['after']['language'], u['f']), ('FBD', TS2, 'FBD', True))
        self.assertEqual(len(u['after']['signals']), 2)
        s, d = self.req('GET', '/api/edits/%s' % u['id'])                # „před" = nejnovější export, ten už úpravu má
        self.assertEqual((s, d['language'], d['drawing']['fbd'], d['diff'], d['errors']), (200, 'FBD', True, {}, []))
        # uložení „po" editorem: jazyk se nemění
        s, _ = self.req('POST', '/api/edits/%s/after' % u['id'], {'after': {'elements': [], 'rungs': []}})
        self.assertEqual(s, 400)
        s, x = self.req('POST', '/api/edits/%s/after' % u['id'], {'after': {'language': 'FBD', 'elements': n['elements']}})
        self.assertEqual(s, 200)
        # „po" = export před úpravou -> shoda, ale s poznámkou (F2: kontrola FBD po obsahu)
        self.req('POST', '/api/edits/%s/state' % u['id'], {'state': 'Approved', 'by': 'test'})
        s, k = self.req('POST', '/api/edits/%s/check-export' % u['id'], {'export_ts': TS0})
        self.assertEqual((s, k['match']), (200, True))

    def test_bad_proposal(self):
        p = {'language': 'FBD', 'elements': [{'id': 'b1', 'type': 'assign', 'operand': '#X', 'outputs': [],
                                       'inputs': [{'pin': 'in1', 'operand': '', 'signal': 'b7'}]}]}
        s, e = self.req('POST', '/api/edits', {'project': PROJECT, 'block': BLOCK, 'nw': 1, 'after': p})
        self.assertEqual(s, 400)
        self.assertIn('b7', e['error'])
        s, _ = self.req('POST', '/api/layout', {'after': {'language': 'FBD', 'elements': nw(1)['elements']},
                                                   'ops': [{'type': 'add_input', 'id': 'b3'}]})
        self.assertEqual(s, 400)


if __name__ == '__main__':
    unittest.main()
