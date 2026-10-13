"""M3: katalog, operace modelu, pravidla LAD, API editoru a uložení „po"."""
import http.client
import json
import shutil
import tempfile
import threading
import unittest

from tests.common import SAMPLES, PROJECT, TS0
import catalog
import layout
import model
import server


def nw(n):
    _, b = server._load(SAMPLES, PROJECT, 'FB_Conveyor', TS0)
    x = b['networks'][n - 1]
    return {'elements': x['elements'], 'rungs': x['rungs']}


CAT = catalog.catalog(SAMPLES, PROJECT)


class TestCatalog(unittest.TestCase):
    def test_fb_belt_pins_and_instance(self):
        b = next(x for x in CAT['blocks'] if x['key'] == 'fb:FB_Belt')
        self.assertEqual((b['kind'], b['number'], b['instance']), ('FB', 11, ['"DB_Belt1"']))
        p = b['element']
        self.assertEqual([x['pin'] for x in p['inputs']], ['Enable', 'Direction', 'Speed', 'Reset'])
        self.assertEqual([x['pin'] for x in p['outputs']], ['Running', 'Fault', 'State'])
        self.assertEqual((p['call'], p['name'], p['instance'], p['power']), ('fb', 'FB_Belt', '"DB_Belt1"', ['EN', 'ENO']))

    def test_fc_and_interface(self):
        fc = next(x for x in CAT['blocks'] if x['key'] == 'fc:FC_Branches')
        self.assertIsNone(fc['element']['instance'])
        dop = next(x for x in CAT['blocks'] if x['name'] == 'FB_Conveyor')
        self.assertIn({'name': 'T_Startup', 'type': 'TON_TIME'}, dop['interface']['static'])   # atribut {…} přeskočen

    def test_db_and_tags(self):
        db = {d['name']: d for d in CAT['db']}
        self.assertEqual(db['DB_Belt1']['instance_type'], 'FB_Belt')
        members = {c['name']: c['type'] for c in db['DB_Machine']['members']}
        self.assertEqual((members['Speed'], members['Counter']), ('Real', 'Struct'))
        self.assertNotIn('Pieces', members)                                    # vnořené členy STRUCT ne
        self.assertIn({'name': 'Emergency_Stop', 'type': 'Bool', 'address': '%I0.1', 'table': 'Default tag table'}, CAT['tags'])

    def test_instruction(self):
        keys = {x['key'] for x in CAT['instruction']}
        for k in ('NO', 'NC', 'P', 'N', 'C', 'S', 'R', 'NEG', 'EQ', 'LE', 'TON', 'TOF', 'TP', 'CTU', 'CTD', 'MOVE',
                  'ADD', 'SUB', 'MUL', 'DIV', 'IN_RANGE', 'CONV', 'SWAP', 'P_TRIG', 'N_TRIG', 'RET'):
            self.assertIn(k, keys)
        ton = catalog.element(CAT, 'TON')
        self.assertEqual((ton['power'], ton['inputs'][0]['pin'], ton['template']), (['IN', 'Q'], 'PT', 'Time'))
        with self.assertRaises(KeyError):
            catalog.element(CAT, 'fb:Neni')


class TestOperations(unittest.TestCase):
    def test_insertion_into_series(self):
        m, i = model.insert(nw(1), {'r': 0, 'k': 3}, catalog.element(CAT, 'NO'))
        self.assertEqual(i, 'n1')
        self.assertEqual(m['rungs'][0]['path'], ['p1', 'p2', 'p3', 'n1', 'p4'])
        self.assertEqual(model.insert(m, {'r': 0, 'k': 0}, catalog.element(CAT, 'NC'))[1], 'n2')
        layout.layout(m['rungs'], m['elements'])
        with self.assertRaises(model.OperationError):
            model.insert(nw(1), {'r': 0, 'k': 9}, catalog.element(CAT, 'NO'))

    def test_output_branch_with_fb(self):
        m, i = model.branch(nw(1), {'r': 0, 'k': 3}, {'r': 0, 'k': 4}, catalog.element(CAT, 'fb:FB_Belt'))
        self.assertEqual(m['rungs'][0]['path'], ['p1', 'p2', 'p3', 'wire#w1', 'p4'])
        self.assertEqual(m['rungs'][1], {'from': 'w1', 'path': [i], 'to': None})
        self.assertEqual(model.validate(m), [])                             # box ve výstupní větvi smí
        r, _ = layout.layout(m['rungs'], m['elements'])
        self.assertGreater(r[i]['r'], r['p4']['r'])

    def test_closed_branch_and_deletion(self):
        m, i = model.branch(nw(1), {'r': 0, 'k': 1}, {'r': 0, 'k': 2}, catalog.element(CAT, 'NO'))
        self.assertEqual(m['rungs'][0]['path'], ['p1', 'wire#w2', 'p2', 'wire#w1', 'p3', 'p4'])
        self.assertEqual(m['rungs'][1], {'from': 'w2', 'path': [i], 'to': 'w1'})
        r, _ = layout.layout(m['rungs'], m['elements'])
        self.assertEqual(r[i]['c'], r['p2']['c'])                          # paralelně ke kontaktu p2
        z = model.delete(m, i)                                               # prázdná větev i uzly pryč
        self.assertEqual(z['rungs'], nw(1)['rungs'])
        self.assertEqual(len(z['elements']), 4)

    def test_delete_box_with_feed(self):
        m = model.delete(nw(4), 'p2')                                        # FB s pinem napájeným větví
        self.assertEqual(m['rungs'], [{'from': 'powerrail', 'path': ['p1'], 'to': None}])
        self.assertEqual([p['id'] for p in m['elements']], ['p1'])

    def test_change_operand(self):
        m = model.change(nw(1), 'p1', {'operand': ' "Fuse_OK" '})
        self.assertEqual(m['elements'][0]['operand'], '"Fuse_OK"')
        with self.assertRaises(model.OperationError):
            model.change(nw(1), 'p1', {'type': 'coil'})
        with self.assertRaises(model.OperationError):
            model.change(nw(1), 'p1', {'inputs': 'x'})

    def test_places(self):
        n = nw(1)
        _, k = layout.layout(n['rungs'], n['elements'], 1000)
        mi = {(x['r'], x['k']): x for x in model.places(n, k)}
        self.assertEqual(sorted(mi), [(0, 0), (0, 1), (0, 2), (0, 3), (0, 4)])
        self.assertTrue(mi[(0, 0)]['box'] and not mi[(0, 0)]['coil'])
        self.assertEqual((mi[(0, 4)]['contact'], mi[(0, 4)]['coil']), (False, False))   # za cívkou nic
        n2 = nw(2)
        _, k = layout.layout(n2['rungs'], n2['elements'], 1000)
        mi = {(x['r'], x['k']): x for x in model.places(n2, k)}
        self.assertFalse(mi[(0, 0)]['box'])                                # před uzlem w1 = uvnitř paralely
        self.assertFalse(mi[(1, 0)]['box'])                                # ve větvi
        self.assertTrue(mi[(0, 2)]['box'])                                 # za uzlem w1


class TestValidation(unittest.TestCase):
    def errors(self, m):
        return [c['error'] for c in model.validate(m)]

    def test_samples_without_errors(self):
        for n in range(1, 9):
            self.assertEqual(model.validate(nw(n)), [], 'NW%d' % n)

    def test_box_in_branch(self):
        m, i = model.branch(nw(1), {'r': 0, 'k': 1}, {'r': 0, 'k': 2}, catalog.element(CAT, 'fb:FB_Belt'))
        ch = model.validate(m)
        self.assertEqual([c['id'] for c in ch], [i])
        self.assertIn('parallel branch', ch[0]['error'])
        m, i = model.insert(nw(2), {'r': 0, 'k': 0}, catalog.element(CAT, 'MOVE'))   # hlavní větev uvnitř paralely
        self.assertTrue(any('parallel branch' in c for c in self.errors(m)))

    def test_coil_middle(self):
        m, _ = model.insert(nw(1), {'r': 0, 'k': 4}, {'type': 'contact', 'operand': '"Ack"'})
        self.assertEqual([c['id'] for c in model.validate(m)], ['p4'])
        self.assertIn('is not at the end', self.errors(m)[0])

    def test_operands_and_power(self):
        m, _ = model.insert(nw(1), {'r': 0, 'k': 0}, catalog.element(CAT, 'GT'))
        self.assertTrue(any('both operands' in c for c in self.errors(m)))
        m, _ = model.insert(nw(1), {'r': 0, 'k': 0}, catalog.element(CAT, 'TON'))
        ch = self.errors(m)
        self.assertTrue(any('Missing instance' in c for c in ch) and any('Missing PT' in c for c in ch))
        m, _ = model.insert(nw(1), {'r': 0, 'k': 0}, catalog.element(CAT, 'NO'))
        self.assertTrue(any('Missing operand' in c for c in self.errors(m)))
        m = nw(1)
        m['rungs'].append({'from': 'w9', 'path': ['p1'], 'to': None})
        self.assertTrue(any('Unpowered' in c for c in self.errors(m)))


class TestApiEditor(unittest.TestCase):
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

    def test_catalog_api(self):
        s, k = self.req('GET', '/api/catalog?project=%s' % PROJECT)
        self.assertEqual(s, 200)
        self.assertIn('fb:FB_Belt', [b['key'] for b in k['blocks']])
        self.assertEqual(self.req('GET', '/api/catalog?project=Neni')[0], 404)

    def test_layout_with_operations(self):
        before = nw(1)
        s, r = self.req('POST', '/api/layout', {
            'project': PROJECT, 'before': before, 'after': before, 'width': 900,
            'ops': [{'type': 'branch', 'from': {'r': 0, 'k': 3}, 'to': {'r': 0, 'k': 4}, 'element': 'fb:FB_Belt'},
                    {'type': 'change', 'id': 'n1', 'values': {'instance': '"DB_Belt1"'}}]})
        self.assertEqual(s, 200)
        self.assertEqual((r['last'], r['diff']), ('n1', {'n1': 'added'}))
        self.assertIsNotNone(r['drawing'])
        self.assertEqual(r['errors'], [])
        self.assertTrue(r['places'])
        s, r = self.req('POST', '/api/layout', {'project': PROJECT, 'after': before,
                                                   'ops': [{'type': 'insert', 'place': {'r': 0, 'k': 0}, 'element': 'fb:Neni'}]})
        self.assertEqual(s, 400)
        self.assertEqual(self.req('POST', '/api/layout', {'after': before, 'ops': [{'type': 'delete', 'id': 'p9'}]})[0], 400)

    def test_save_after(self):
        _, net = self.req('GET', '/api/network?project=%s&block=FB_Conveyor&nw=1' % PROJECT)
        s, u = self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'Round 1', 'block': 'FB_Conveyor', 'nw': 1,
                                                 'after': {'elements': net['elements'], 'rungs': net['rungs']}})
        self.assertEqual(s, 201)
        _, r = self.req('POST', '/api/layout', {'project': PROJECT, 'after': u['after'], 'ops': [
            {'type': 'branch', 'from': {'r': 0, 'k': 1}, 'to': {'r': 0, 'k': 2}, 'element': 'fb:FB_Belt'}]})
        s, v = self.req('POST', '/api/edits/%s/after' % u['id'], {'after': r['after']})
        self.assertEqual(s, 200)                                           # uložit lze i s chybou
        self.assertEqual(len(v['errors']), 1)
        _, d = self.req('GET', '/api/edits/%s' % u['id'])
        self.assertEqual(d['diff'], {'n1': 'added'})
        self.assertEqual(len(d['errors']), 1)                               # hlavička úpravy je označí
        self.assertIn('edited', d['edit'])
        self.assertEqual(self.req('POST', '/api/edits/%s/after' % u['id'], {'after': {'elements': 1}})[0], 400)
        self.req('POST', '/api/edits/%s/state' % u['id'], {'state': 'Approved'})
        s, e = self.req('POST', '/api/edits/%s/after' % u['id'], {'after': r['after']})
        self.assertEqual(s, 409)
        self.assertIn('Proposal', e['error'])

    def test_checked_decides_last_check(self):
        _, net = self.req('GET', '/api/network?project=%s&block=FB_Conveyor&nw=1' % PROJECT)
        _, u = self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'k', 'block': 'FB_Conveyor', 'nw': 1,
                                                 'after': {'elements': net['elements'], 'rungs': net['rungs']}})
        uid = u['id']
        self.req('POST', '/api/edits/%s/state' % uid, {'state': 'Approved'})
        self.assertEqual(self.req('POST', '/api/edits/%s/state' % uid, {'state': 'Signed'})[0], 200)   # volný
        self.req('POST', '/api/check', {'edit_id': uid, 'match': True})
        self.req('POST', '/api/check', {'edit_id': uid, 'match': False})
        self.assertEqual(self.req('POST', '/api/edits/%s/state' % uid, {'state': 'Checked'})[0], 409)
        self.req('POST', '/api/check', {'edit_id': uid, 'match': True})
        self.assertEqual(self.req('POST', '/api/edits/%s/state' % uid, {'state': 'Checked'})[0], 200)
        self.req('POST', '/api/check', {'edit_id': uid, 'match': False})
        _, d = self.req('GET', '/api/edits/%s' % uid)
        self.assertEqual((d['edit']['state'], d['checked_allowed']), ('Checked', False))   # stav se sám nevrací
        self.assertIn('MISMATCH', d['warnings'][0])


if __name__ == '__main__':
    unittest.main()
