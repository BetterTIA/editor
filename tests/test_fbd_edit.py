"""F2 — FBD: editor operations, rules, insert places, export check, TIA steps, "as LAD"."""
import contextlib
import copy
import http.client
import io
import json
import shutil
import tempfile
import threading
import unittest

from tests.common import SAMPLES, PROJECT, TS0, TS2, after_nw2
import fbd
import catalog
import check
import layout
import procedure
import server

BLOCK = 'FB_Safety'
CAT = catalog.catalog(SAMPLES, PROJECT)


def nw(n, ts=TS0):
    _, b = server._load(SAMPLES, PROJECT, BLOCK, ts)
    return b['networks'][n - 1]


def m(n, ts=TS0):
    return fbd.model(copy.deepcopy(nw(n, ts)['elements']))


def op(model, *ops):
    for o in ops:
        model, _ = fbd.apply(model, o, lambda k: catalog.element(CAT, k))
    return model


def P(model):
    return {p['id']: p for p in model['elements']}


class TestOperations(unittest.TestCase):
    def test_insert_box_into_signal_and_new_input(self):
        x = op(m(1), {'type': 'insert_box', 'element': 'P_TRIG', 'to': {'id': 'b2', 'input': 1}})
        self.assertEqual(P(x)['n1']['inputs'][0]['signal'], 'b1')          # P_TRIG took over the output of >=1
        self.assertEqual(P(x)['b2']['inputs'][0]['signal'], 'n1')
        x = op(x, {'type': 'insert_box', 'element': 'OR', 'to': {'id': 'b2', 'input': 4}})
        self.assertEqual([v['pin'] for v in P(x)['b2']['inputs']], ['in1', 'in2', 'in3', 'in4'])
        self.assertEqual((P(x)['b2']['inputs'][3]['signal'], P(x)['n2']['inputs'][0]['operand']), ('n2', ''))
        x = op(x, {'type': 'insert_box', 'element': 'ASSIGN'})                      # new logic path
        self.assertEqual(P(x)['n3']['inputs'][0]['signal'], None)
        fbd.layout(x['elements'])

    def test_remove_input_negate_change_pin(self):
        x = op(m(1), {'type': 'add_input', 'id': 'b2', 'operand': '#A'}, {'type': 'remove_input', 'id': 'b2', 'input': 2})
        self.assertEqual([(v['pin'], v['operand']) for v in P(x)['b2']['inputs'][1:]], [('in2', '"DB_Machine".Safety_OK'), ('in3', '#A')])
        with self.assertRaises(fbd.OperationError):
            op(m(1), {'type': 'remove_input', 'id': 'b1', 'input': 1})       # >=1 has only 2 inputs
        x = op(m(1), {'type': 'negate', 'id': 'b1', 'output': 'main'})
        self.assertTrue(P(x)['b1']['neg_output'])
        _, k = fbd.layout(x['elements'])
        self.assertTrue(next(d for d in k['boxes'] if d['id'] == 'b1')['outputs'][0]['neg'])
        x = op(m(4), {'type': 'change_pin', 'id': 'b1', 'pin': 'TIME_DEL', 'operand': 'T#2s'},
               {'type': 'change_pin', 'id': 'b1', 'pin': 'DIAG', 'operand': '#Diag', 'neg': False})
        v = {p['pin']: p['operand'] for p in P(x)['b1']['inputs'] + P(x)['b1']['outputs']}
        self.assertEqual((v['TIME_DEL'], v['DIAG']), ('T#2s', '#Diag'))

    def test_delete_reconnects_chain(self):
        x = op(m(1), {'type': 'delete', 'id': 'b2'})
        self.assertEqual(P(x)['b3']['inputs'][0]['signal'], 'b1')           # = got the 1st input of the deleted &
        x = op(m(4), {'type': 'delete', 'id': 'b2'})                            # P_TRIG fed the ACK pin of the F-block
        self.assertEqual((P(x)['b1']['inputs'][2]['signal'], P(x)['b1']['inputs'][2]['operand']), (None, '#Ack'))   # ACK got the CLK of the edge

    def test_insert_call_from_catalog(self):
        x, i = fbd.apply(m(1), {'type': 'insert_call', 'element': 'fb:FB_Belt', 'instance': '"DB_Belt1"'},
                          lambda k: catalog.element(CAT, k), True)
        p = P(x)[i]
        self.assertEqual((p['type'], p['name'], p['instance']), ('call', 'FB_Belt', '"DB_Belt1"'))
        self.assertNotIn('EN', [v['pin'] for v in p['inputs']])               # no EN in an F-block
        x, i = fbd.apply(m(1), {'type': 'insert_box', 'element': 'MOVE'}, lambda k: catalog.element(CAT, k))
        self.assertEqual([v['pin'] for v in P(x)[i]['inputs']], ['EN', 'IN'])
        with self.assertRaises(fbd.OperationError):
            fbd.apply(m(1), {'type': 'insert_call', 'element': 'AND'})

    def test_places(self):
        x = m(1)
        mi = fbd.places(x, fbd.layout(x['elements'])[1])
        dst = [tuple(sorted(z['to'].items())) if z['to'] else None for z in mi]
        self.assertIn((('id', 'b2'), ('input', 4)), dst)                      # new input of &
        self.assertIn((('id', 'b3'), ('input', 1)), dst)
        self.assertEqual(dst[-1], None)                                        # new logic path
        self.assertEqual(len(mi), 2 + 1 + 3 + 1 + 1 + 1)


class TestRules(unittest.TestCase):
    def test_rules(self):
        x = op(m(1), {'type': 'insert_box', 'element': 'ASSIGN'}, {'type': 'insert_box', 'element': 'AND'})
        ch = fbd.validate(x)
        self.assertEqual([(c['id'], bool(c.get('warning'))) for c in ch],
                         [('n1', False), ('n1', False), ('n2', False), ('n2', False), ('n2', True)])
        self.assertIn('Missing operand', ch[0]['error'])
        self.assertIn('Unconnected input 1', ch[1]['error'])
        self.assertIn('leads nowhere', ch[4]['error'])

    def test_f_block(self):
        x = op(m(1), {'type': 'insert_call', 'element': 'fb:FB_Belt', 'instance': '"DB_Belt1"'})
        ch = fbd.validate(x, True, {'FB_Belt'})
        self.assertTrue(any('only F-instruction' in c['error'] for c in ch))
        x = op(m(7), {'type': 'change_operand', 'id': 'b2', 'input': 1, 'operand': 'TRUE'})
        self.assertTrue(any('EN cannot be connected' in c['error'] for c in fbd.validate(x, True)))
        self.assertFalse(any('EN' in c['error'] for c in fbd.validate(x, False)))


class TestCheckFBD(unittest.TestCase):
    def proposal(self):
        return op(m(1), {'type': 'add_input', 'id': 'b2', 'operand': '#Lock_OK'},
                  {'type': 'change_operand', 'id': 'b3', 'operand': '#Enable_Drive'})

    def test_match_and_mismatch(self):
        self.assertEqual(fbd.content_match(self.proposal(), nw(1, TS2)), (True, []))
        match, r = fbd.content_match(m(1), nw(1, TS2))
        self.assertFalse(match)
        self.assertEqual(len(r), 2)
        self.assertTrue(r[0].startswith('different box &') or r[0].startswith('different box ='))

    def test_commutative_order(self):
        e = copy.deepcopy(nw(1, TS2))
        b2 = e['elements'][1]
        b2['inputs'] = [b2['inputs'][3], b2['inputs'][0], b2['inputs'][2], b2['inputs'][1]]
        fbd._renumber(b2)
        self.assertEqual(fbd.content_match(self.proposal(), e), (True, []))       # & — input order does not matter
        s = m(6)
        e = copy.deepcopy(s)
        sr = P(e)['b3']
        sr['inputs'][0]['operand'], sr['inputs'][1]['operand'] = sr['inputs'][1]['operand'], sr['inputs'][0]['operand']
        self.assertFalse(fbd.content_match(s, e)[0])                           # SR — S and R1 swapped = mismatch

    def test_wiring(self):
        a = m(3)
        b = copy.deepcopy(a)
        P(b)['b4']['inputs'][0]['signal'] = 'b2'                               # tap after = instead of after &
        match, r = fbd.content_match(a, b)
        self.assertEqual((match, r), (False, ['boxes match, but signal wiring differs']))


class TestProcedure(unittest.TestCase):
    def test_fbd(self):
        before = m(1)
        after = op(before, {'type': 'add_input', 'id': 'b2', 'operand': '#Lock_OK'}, {'type': 'negate', 'id': 'b2', 'input': 4},
                {'type': 'change_operand', 'id': 'b3', 'operand': '#Enable_Drive'},
                {'type': 'insert_box', 'element': 'P_TRIG', 'to': {'id': 'b2', 'input': 1}},
                {'type': 'change_operand', 'id': 'n1', 'operand': '#Mem'})
        u = {'id': 'U009', 'block_kind': 'FB', 'block_number': 30, 'block': BLOCK, 'nw': 1, 'title': nw(1)['title'], 'f': True,
             'before': before, 'after': after}
        p = procedure.procedure(u)
        self.assertEqual(p['header'], 'FB30 "FB_Safety" · NW 1: Enable — >=1, & with negation, assignment')
        self.assertTrue(p['markdown'].startswith('### TIA steps — FB30 "FB_Safety" · NW 1: '))
        t = '\n'.join(p['steps'])
        for s in ('**Insert input** (`Ctrl+Shift+3`)', 'type `#Lock_OK`', '**Create a ones complement** (`Ctrl+Shift+4`)',
                  'change the operand above the box to `#Enable_Drive`', 'Insert box `P_TRIG` on input 1 of box `&`',
                  'type the operand `#Mem` above the box', 'Compile the safety program', 'check.py --edit U009'):
            self.assertIn(s, t)
        self.assertNotIn('Ctrl+Shift+5', t)
        u['after'] = op(before, {'type': 'delete', 'id': 'b1'})
        self.assertIn('Select box `>=1` (inputs `#Start`, `#Service`) and delete it: right-click → **Delete**', procedure.procedure(u)['markdown'])

    def test_lad(self):
        _, b = server._load(SAMPLES, PROJECT, 'FB_Conveyor', TS0)
        n = b['networks'][1]
        u = {'id': 'U010', 'block_kind': 'FB', 'block_number': 10, 'block': 'FB_Conveyor', 'nw': 2, 'title': n['title'],
             'before': {'elements': n['elements'], 'rungs': n['rungs']}, 'after': after_nw2(n)}
        p = procedure.procedure(u)
        self.assertEqual(p['header'], 'FB10 "FB_Conveyor" · NW 2: Motor latch')
        t = '\n'.join(p['steps'])
        self.assertIn('Insert `-| |- "Fuse_OK"`', t)
        self.assertIn('⟦unverified⟧', t)
        self.assertNotIn('Compile the safety', t)


class TestAsLad(unittest.TestCase):
    def test_conversion(self):
        rungs, elements = fbd.as_lad(nw(1)['elements'])
        self.assertEqual([p['type'] for p in elements], ['contact', 'contact', 'contact_nc', 'contact', 'coil'])
        layout.layout(rungs, elements)
        rungs, elements = fbd.as_lad(nw(3)['elements'])
        self.assertEqual(len([r for r in rungs if r['from'] == 'powerrail']), 3)   # 3 assignments = 3 rungs
        self.assertIn('not', [p['type'] for p in elements])
        layout.layout(rungs, elements)
        self.assertIsNone(fbd.as_lad(nw(4)['elements']))                       # F-block call
        self.assertIsNone(fbd.as_lad(nw(8)['elements']))                       # x and N_TRIG


class TestApiFbdEdit(unittest.TestCase):
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

    def test_proposal_check_signed_procedure(self):
        n = nw(1)
        mm = {'language': 'FBD', 'elements': n['elements']}
        s, r = self.req('POST', '/api/layout', {'project': PROJECT, 'before': mm, 'after': mm, 'f': True, 'ops': [
            {'type': 'add_input', 'id': 'b2', 'operand': '#Lock_OK'},
            {'type': 'change_operand', 'id': 'b3', 'operand': '#Enable_Drive'},
            {'type': 'insert_call', 'element': 'fb:FB_Belt', 'instance': '"DB_Belt1"'},
            {'type': 'delete', 'id': 'n1'}]})
        self.assertEqual((s, r['errors'], r['last']), (200, [], None))
        self.assertTrue(any(x['to'] == {'id': 'b2', 'input': 5} for x in r['places']))
        s, u = self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'F2', 'block': BLOCK, 'nw': 1, 'after': r['after']})
        self.assertEqual(s, 201)
        # before = newest export (TS2 already has the edit) -> switch before to TS0 via a state PUT as in practice (proposal before the write)
        s, st = self.req('GET', '/api/state?project=%s' % PROJECT)
        st['edits'][0]['before'] = dict(server._model(server.api_network(SAMPLES, PROJECT, BLOCK, 1, TS0)), export_ts=TS0)
        self.assertEqual(self.req('PUT', '/api/state?project=%s' % PROJECT, st)[0], 200)
        uid = u['id']
        s, p = self.req('GET', '/api/edits/%s/procedure' % uid)
        self.assertEqual(s, 200)
        self.assertIn('Insert input', p['markdown'])
        self.assertIn('FB30 "FB_Safety" · NW 1:', p['markdown'])
        self.req('POST', '/api/edits/%s/state' % uid, {'state': 'Approved', 'by': 'test'})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check.main(['--server', 'http://127.0.0.1:%d' % self.port, '--edit', uid, '--export-ts', TS0])
        self.assertEqual(code, 1)                                               # old export = mismatch
        with contextlib.redirect_stdout(out):
            code = check.main(['--server', 'http://127.0.0.1:%d' % self.port, '--edit', uid])
        self.assertEqual(code, 0, out.getvalue())                               # export after the write (TS2) = match
        s, x = self.req('POST', '/api/edits/%s/state' % uid, {'state': 'Signed', 'by': 'test'})
        self.assertEqual((s, x['state']), (200, 'Signed'))
        s, x = self.req('POST', '/api/edits/%s/state' % uid, {'state': 'Checked', 'by': 'test'})
        self.assertEqual((s, x['state']), (200, 'Checked'))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(check.main(['--server', 'http://127.0.0.1:%d' % self.port, '--procedure', uid]), 0)
        self.assertTrue(out.getvalue().startswith('### TIA steps — FB30'))

    def test_network_as_lad_and_f(self):
        s, n = self.req('GET', '/api/network?project=%s&block=%s&nw=1&ts=%s' % (PROJECT, BLOCK, TS0))
        self.assertEqual((s, n['f'], 'drawing' in n['as_lad']), (200, True, True))
        s, n = self.req('GET', '/api/network?project=%s&block=%s&nw=4' % (PROJECT, BLOCK))
        self.assertNotIn('as_lad', n)
        s, k = self.req('GET', '/api/catalog?project=%s' % PROJECT)
        self.assertEqual([x['key'] for x in k['fbd']][:3], ['AND', 'OR', 'XOR'])
        self.assertEqual({b['name']: b['f'] for b in k['blocks']}['FB_Safety'], True)


if __name__ == '__main__':
    unittest.main()
