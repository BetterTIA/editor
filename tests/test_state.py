import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from tests.common import SAMPLES, PROJECT, TS1, TS0, after_nw2
import diff
import s7dcl
import server
import state


class TestStateMachine(unittest.TestCase):
    def u(self, s='Proposal', checks=()):
        return {'id': 'U001', 'state': s, 'history': [], 'checks': list(checks)}

    def test_step_forward_and_back(self):
        u = self.u()
        state.change_state(u, 'Approved', 'user')
        self.assertEqual(u['state'], 'Approved')
        self.assertEqual(u['history'][-1]['state'], 'Approved')
        self.assertEqual(u['history'][-1]['by'], 'user')
        state.change_state(u, 'Proposal', 'user')
        self.assertEqual(u['state'], 'Proposal')

    def test_skip_fails(self):
        u = self.u()
        with self.assertRaises(state.StateError) as e:
            state.change_state(u, 'Signed', 'user')
        self.assertEqual(e.exception.code, 409)
        with self.assertRaises(state.StateError):
            state.change_state(self.u('Tested'), 'Approved', 'user')
        with self.assertRaises(state.StateError) as e:
            state.change_state(u, 'Hotovo', 'user')
        self.assertEqual(e.exception.code, 400)

    def test_signed_free_checked_only_with_match(self):
        u = self.u('Approved')
        state.change_state(u, 'Signed', 'user')                       # od 13. 10. Signed bez kontroly
        self.assertEqual(state.transitions(u), ('Checked', 'Approved'))
        with self.assertRaises(state.StateError) as e:
            state.change_state(u, 'Checked', 'user')
        self.assertEqual(e.exception.code, 409)
        u['checks'].append({'match': True})
        u['checks'].append({'match': False})                     # rozhoduje poslední
        with self.assertRaises(state.StateError):
            state.change_state(u, 'Checked', 'user')
        u['checks'].append({'match': True})
        state.change_state(u, 'Checked', 'TIAC_Reader')
        self.assertEqual((u['state'], u['history'][-1]['by']), ('Checked', 'TIAC_Reader'))
        self.assertEqual(state.transitions(u), ('Uploaded', 'Signed'))
        state.change_state(u, 'Uploaded', 'user')
        u['checks'].append({'match': False})
        state.change_state(u, 'Checked', 'user')                      # krok zpět do Checked kontrola neblokuje

    def test_old_state_with_five_states(self):
        d = tempfile.mkdtemp()
        try:
            old = {'edits': [{'id': 'U00%d' % i, 'state': s, 'history': [], 'checks': []}
                                for i, s in enumerate(['Proposal', 'Approved', 'Signed', 'Uploaded', 'Tested'], 1)]}
            os.makedirs(os.path.join(d, PROJECT))
            with open(os.path.join(d, PROJECT, 'state.json'), 'w', encoding='utf-8') as f:
                json.dump(old, f)
            st = state.load(d, PROJECT)
            self.assertEqual(st, old)
            state.save(d, PROJECT, st)                                  # projde kontrolou struktury i zápisem
            self.assertEqual(state.load(d, PROJECT), old)
            self.assertEqual(state.transitions(st['edits'][3]), ('Tested', 'Checked'))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_atomic_write(self):
        d = tempfile.mkdtemp()
        try:
            state.save(d, PROJECT, {'edits': [{'id': 'U001', 'state': 'Proposal'}]})
            c = os.path.join(d, PROJECT, 'state.json')
            original = s7dcl.read(c)
            with mock.patch('state.json.dump', side_effect=OSError('disk plný')):
                with self.assertRaises(OSError):
                    state.save(d, PROJECT, {'edits': []})
            self.assertEqual(s7dcl.read(c), original)                          # starý soubor nedotčen
            self.assertEqual(os.listdir(os.path.dirname(c)), ['state.json'])   # žádný .tmp
            with self.assertRaises(state.StateError):
                state.save(d, PROJECT, {'edits': [{'id': 'U001'}]})           # bez stavu
            with self.assertRaises(state.StateError):
                state.save(d, '..', {'edits': []})
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_diff_and_ghost(self):
        _, b = server._load(SAMPLES, PROJECT, 'FB_Conveyor', TS0)
        n = b['networks'][1]
        before = {'elements': n['elements'], 'rungs': n['rungs']}
        after = after_nw2(before)
        changes, deleted = diff.compare(before, after)
        self.assertEqual(changes, {'p3': 'changed', 'n1': 'added'})
        self.assertEqual(deleted, ['p7'])
        elements, rungs, unplaced = diff.merge(before, after, deleted)
        self.assertEqual(unplaced, [])
        self.assertEqual(rungs[2]['path'], ['p6', 'x:p7'])                   # přízrak na původním místě


class TestApiEdits(unittest.TestCase):
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
        b = json.dumps(body).encode('utf-8') if body is not None else None
        c.request(method, fpath, body=b, headers={'Content-Type': 'application/json'} if b else {})
        r = c.getresponse()
        t = r.read()
        c.close()
        return r.status, (json.loads(t) if r.getheader('Content-Type', '').startswith('application/json') else t)

    def create(self, **extra):
        _, net = self.req('GET', '/api/network?project=%s&block=FB_Conveyor&nw=2' % PROJECT)
        body = {'project': PROJECT, 'round': 'Round 1', 'block': 'FB_Conveyor', 'nw': 2, 'after': after_nw2(net),
                'note': 'Pojistka před cívkou'}
        body.update(extra)
        return self.req('POST', '/api/edits', body)

    def test_creation_fills_from_export(self):
        s, u = self.create(title='ručně', block_number=999)
        self.assertEqual(s, 201)
        self.assertEqual((u['id'], u['state'], u['author']), ('U001', 'Proposal', 'editor'))
        self.assertEqual((u['kind'], u['block_number'], u['nw'], u['title']), ('FB', 10, 2, 'Motor latch'))
        self.assertEqual(u['before']['export_ts'], TS1)                       # nejnovější export s blokem
        self.assertEqual(len(u['before']['elements']), 7)
        self.assertIsNotNone(u['after']['layout'])                         # přepočteno serverem
        self.assertIn({'from': 'n1', 'to': 'p4'}, u['after']['wires'])
        self.assertTrue(os.path.exists(os.path.join(self.data, PROJECT, 'state.json')))
        self.assertEqual(self.create()[1]['id'], 'U002')

    def test_states_via_api(self):
        self.create()
        s, u = self.req('POST', '/api/edits/U001/state', {'state': 'Approved', 'by': 'user'})
        self.assertEqual((s, u['state']), (200, 'Approved'))
        self.assertEqual(self.req('POST', '/api/edits/U001/state', {'state': 'Signed', 'by': 'user'})[0], 200)
        s, e = self.req('POST', '/api/edits/U001/state', {'state': 'Checked', 'by': 'user'})
        self.assertEqual(s, 409)
        self.assertIn('check', e['error'])
        s, u = self.req('POST', '/api/check', {'edit_id': 'U001', 'match': True, 'export_ts': '20261012-130000',
                                                   'note': 'export after zápisu = návrh'})
        self.assertEqual((s, len(u['checks'])), (200, 1))
        self.assertEqual(self.req('POST', '/api/edits/U001/state', {'state': 'Checked'})[0], 200)
        self.assertEqual(self.req('POST', '/api/edits/U001/state', {'state': 'Tested'})[0], 409)
        self.assertEqual(self.req('POST', '/api/check', {'edit_id': 'U001', 'match': 'ano'})[0], 400)
        self.assertEqual(self.req('POST', '/api/edits/U999/state', {'state': 'Approved'})[0], 404)
        _, st = self.req('GET', '/api/state?project=%s' % PROJECT)
        self.assertEqual([h['state'] for h in st['edits'][0]['history']], ['Proposal', 'Approved', 'Signed', 'Checked'])

    def test_detail_for_ui(self):
        self.create()
        s, d = self.req('GET', '/api/edits/U001?width=900')
        self.assertEqual(s, 200)
        self.assertEqual(d['diff'], {'p3': 'changed', 'n1': 'added', 'x:p7': 'deleted'})
        self.assertIsNotNone(d['drawing'])
        self.assertIn('x:p7', d['layout'])
        self.assertEqual((d['next'], d['previous'], d['checked_allowed']), ('Approved', None, False))
        s, html = self.req('GET', '/?edit=U001&screenshot=1')
        self.assertEqual(s, 200)
        self.assertIn(b'showEdit', html)

    def test_state_get_put(self):
        self.create()
        s, all = self.req('GET', '/api/state')
        self.assertEqual(all['projects'][0]['project'], PROJECT)
        _, st = self.req('GET', '/api/state?project=%s' % PROJECT)
        st['edits'][0]['note'] = 'edited PUT'
        self.assertEqual(self.req('PUT', '/api/state?project=%s' % PROJECT, st)[0], 200)
        self.assertEqual(self.req('GET', '/api/state?project=%s' % PROJECT)[1]['edits'][0]['note'], 'edited PUT')
        self.assertEqual(self.req('PUT', '/api/state?project=%s' % PROJECT, {'x': 1})[0], 400)
        self.assertEqual(self.req('PUT', '/api/state?project=Neni', st)[0], 404)

    def test_invalid_input(self):
        self.assertEqual(self.create(after={'elements': 'x'})[0], 400)
        self.assertEqual(self.create(after={'elements': [], 'rungs': [{'from': 'powerrail', 'path': ['p1'], 'to': None}]})[0], 400)
        self.assertEqual(self.create(nw=99)[0], 404)
        self.assertEqual(self.req('POST', '/api/edits', [1, 2])[0], 400)


class TestWithoutData(unittest.TestCase):
    def test_without_data_503(self):
        srv = server.create_server(0, SAMPLES, '')
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=10)
            c.request('GET', '/api/state?project=%s' % PROJECT)
            self.assertEqual(c.getresponse().status, 503)
            c.close()
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == '__main__':
    unittest.main()
