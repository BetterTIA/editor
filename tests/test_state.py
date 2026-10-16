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
        state.change_state(u, 'Signed', 'user')                       # Signed without a check
        self.assertEqual(state.transitions(u), ('Checked', 'Approved'))
        with self.assertRaises(state.StateError) as e:
            state.change_state(u, 'Checked', 'user')
        self.assertEqual(e.exception.code, 409)
        u['checks'].append({'match': True})
        u['checks'].append({'match': False})                     # the last one decides
        with self.assertRaises(state.StateError):
            state.change_state(u, 'Checked', 'user')
        u['checks'].append({'match': True})
        state.change_state(u, 'Checked', 'TIAC_Reader')
        self.assertEqual((u['state'], u['history'][-1]['by']), ('Checked', 'TIAC_Reader'))
        self.assertEqual(state.transitions(u), ('Uploaded', 'Signed'))
        state.change_state(u, 'Uploaded', 'user')
        u['checks'].append({'match': False})
        state.change_state(u, 'Checked', 'user')                      # a step back to Checked is not blocked by the check

    def test_disapproved_side_state(self):
        u = self.u('Approved')
        with self.assertRaises(state.StateError) as e:
            state.change_state(u, 'Disapproved', 'user', '  ')
        self.assertEqual(e.exception.code, 400)
        state.change_state(u, 'Disapproved', 'user', ' wrong output ')
        self.assertEqual((u['state'], u['disapproved']['reason'], u['history'][-1]['reason']),
                         ('Disapproved', 'wrong output', 'wrong output'))
        self.assertEqual((state.transitions(u), state.rank('Disapproved')), ((None, 'Proposal'), 0))
        for s in ('Approved', 'Signed'):
            with self.assertRaises(state.StateError):
                state.change_state(dict(u), s, 'user')
        state.change_state(u, 'Proposal', 'AI')
        self.assertEqual((u['state'], u['disapproved']['reason']), ('Proposal', 'wrong output'))   # stays readable
        with self.assertRaises(state.StateError):
            state.change_state(self.u('Signed'), 'Disapproved', 'user', 'too late')

    def test_box_pin_changes(self):
        """E1: one changed pin of a LAD call marks that pin, not the whole box."""
        _, b = server._load(SAMPLES, PROJECT, 'FB_Conveyor', TS0)
        n = b['networks'][3]
        before = {'elements': n['elements'], 'rungs': n['rungs']}
        after = json.loads(json.dumps(before))
        bx = next(p for p in after['elements'] if p['type'] == 'box')
        bx['inputs'][3]['operand'] = '#Reset_Belt'
        bx['outputs'][1]['operand'] = '"Fault_Belt2"'
        changes, _ = diff.compare(before, after)
        self.assertEqual(diff.box_pin_changes(before, after, set(changes)),
                         {bx['id']: {'inputs': ['Reset'], 'outputs': ['Fault'], 'above': False}})
        bx['instance'] = '"DB_Belt2"'
        self.assertTrue(diff.box_pin_changes(before, after, set(changes))[bx['id']]['above'])
        self.assertEqual(server._diff_drawing(before, after)['pin_diff'][bx['id']]['inputs'], ['Reset'])

    def test_old_state_with_five_states(self):
        d = tempfile.mkdtemp()
        try:
            old = {'edits': [{'id': 'U00%d' % i, 'state': s, 'history': [], 'checks': []}
                                for i, s in enumerate(['Proposal', 'Approved', 'Signed', 'Uploaded', 'Tested'], 1)]}
            os.makedirs(os.path.join(d, PROJECT))
            with open(os.path.join(d, PROJECT, 'state.json'), 'w', encoding='utf-8') as f:
                json.dump(old, f)
            st = state.load(d, PROJECT)
            new = {'edits': [dict(u, kind='network') for u in old['edits']]}   # edits without a kind are network edits
            self.assertEqual(st, new)
            state.save(d, PROJECT, st)                                  # passes the structure check and the write
            self.assertEqual(state.load(d, PROJECT), new)
            self.assertEqual(state.transitions(st['edits'][3]), ('Tested', 'Checked'))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_kind_was_the_block_kind(self):
        st = state.migrate({'edits': [{'id': 'U001', 'kind': 'FB'}, {'id': 'U002', 'kind': 'interface', 'block_kind': 'FC'}]})
        self.assertEqual([(u['kind'], u.get('block_kind')) for u in st['edits']], [('network', 'FB'), ('interface', 'FC')])

    def test_atomic_write(self):
        d = tempfile.mkdtemp()
        try:
            state.save(d, PROJECT, {'edits': [{'id': 'U001', 'state': 'Proposal'}]})
            c = os.path.join(d, PROJECT, 'state.json')
            original = s7dcl.read(c)
            with mock.patch('state.json.dump', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    state.save(d, PROJECT, {'edits': []})
            self.assertEqual(s7dcl.read(c), original)                          # old file untouched
            self.assertEqual(os.listdir(os.path.dirname(c)), ['state.json'])   # no .tmp
            with self.assertRaises(state.StateError):
                state.save(d, PROJECT, {'edits': [{'id': 'U001'}]})           # without a state
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
        self.assertEqual(rungs[2]['path'], ['p6', 'x:p7'])                   # ghost in its original place


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
                'note': 'Fuse before the coil'}
        body.update(extra)
        return self.req('POST', '/api/edits', body)

    def test_creation_fills_from_export(self):
        s, u = self.create(title='manual', block_number=999)
        self.assertEqual(s, 201)
        self.assertEqual((u['id'], u['state'], u['author']), ('U001', 'Proposal', 'editor'))
        self.assertEqual((u['kind'], u['block_kind'], u['block_number'], u['nw'], u['title']),
                         ('network', 'FB', 10, 2, 'Motor latch'))
        self.assertEqual(u['before']['export_ts'], TS1)                       # newest export containing the block
        self.assertEqual(len(u['before']['elements']), 7)
        self.assertIsNotNone(u['after']['layout'])                         # recomputed by the server
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
                                                   'note': 'export after the write = proposal'})
        self.assertEqual((s, len(u['checks'])), (200, 1))
        self.assertEqual(self.req('POST', '/api/edits/U001/state', {'state': 'Checked'})[0], 200)
        self.assertEqual(self.req('POST', '/api/edits/U001/state', {'state': 'Tested'})[0], 409)
        self.assertEqual(self.req('POST', '/api/check', {'edit_id': 'U001', 'match': 'ano'})[0], 400)
        self.assertEqual(self.req('POST', '/api/edits/U999/state', {'state': 'Approved'})[0], 404)
        _, st = self.req('GET', '/api/state?project=%s' % PROJECT)
        self.assertEqual([h['state'] for h in st['edits'][0]['history']], ['Proposal', 'Approved', 'Signed', 'Checked'])

    def test_disapproved_via_api(self):
        _, net = self.req('GET', '/api/network?project=%s&block=FB_Conveyor&nw=2' % PROJECT)
        self.create()
        self.assertEqual(self.req('POST', '/api/edits/U001/state', {'state': 'Disapproved'})[0], 400)    # no reason
        s, u = self.req('POST', '/api/edits/U001/state', {'state': 'Disapproved', 'reason': 'use the NC contact', 'by': 'user'})
        self.assertEqual((s, u['state'], u['disapproved']['reason']), (200, 'Disapproved', 'use the NC contact'))
        _, d = self.req('GET', '/api/edits/U001')
        self.assertEqual((d['next'], d['previous']), (None, 'Proposal'))
        s, r = self.req('POST', '/api/edits/U001/after', {'after': after_nw2(net), 'note': 'NC contact'})   # a new proposal
        self.assertEqual((s, r['edit']['state'], r['edit']['history'][-1]['by']), (200, 'Proposal', 'editor'))
        _, log = self.req('GET', '/api/projects/%s/changelog' % PROJECT)
        self.assertIn('Proposal → Disapproved: use the NC contact', [x['summary'] for x in log['items']])

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

    def test_state_get_and_tia(self):
        self.create()
        s, all = self.req('GET', '/api/state')
        self.assertEqual(all['projects'][0]['project'], PROJECT)
        tia = {'softwarePath': 'PLC_1', 'plcName': 'PLC_1', 'groupPath': ''}
        self.assertEqual(self.req('PUT', '/api/state/tia?project=%s' % PROJECT, tia),
                         (200, {'project': PROJECT, 'tia': {'softwarePath': 'PLC_1', 'plcName': 'PLC_1'}}))
        st = self.req('GET', '/api/state?project=%s' % PROJECT)[1]
        self.assertEqual((st['tia']['plcName'], len(st['edits'])), ('PLC_1', 1))
        self.assertEqual(self.req('PUT', '/api/state/tia?project=%s' % PROJECT, {'x': '1'})[0], 400)
        self.assertEqual(self.req('PUT', '/api/state/tia?project=Missing', tia)[0], 404)
        self.req('PUT', '/api/state/tia?project=%s' % PROJECT, {})
        self.assertNotIn('tia', self.req('GET', '/api/state?project=%s' % PROJECT)[1])

    def test_tia_settings_and_edits_at_the_same_time(self):
        """Saving the TIA settings never loses an edit created meanwhile (it used to be GET + PUT of the whole file)."""
        threads = [threading.Thread(target=f) for i in range(6) for f in (
            lambda i=i: self.create(note=str(i)),
            lambda i=i: self.req('PUT', '/api/state/tia?project=%s' % PROJECT, {'softwarePath': 'PLC_%d' % i}))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        st = self.req('GET', '/api/state?project=%s' % PROJECT)[1]
        self.assertEqual(sorted(u['note'] for u in st['edits']), [str(i) for i in range(6)])
        self.assertRegex(st['tia']['softwarePath'], r'^PLC_\d$')

    def test_save_waits_for_a_reader(self):
        """Windows: os.replace fails while another process has state.json open — the save retries instead of failing."""
        self.create()
        f = open(os.path.join(self.data, PROJECT, 'state.json'), encoding='utf-8')
        threading.Timer(0.2, f.close).start()
        st = state.load(self.data, PROJECT)
        st['edits'][0]['note'] = 'saved'
        state.save(self.data, PROJECT, st)
        self.assertEqual(state.load(self.data, PROJECT)['edits'][0]['note'], 'saved')

    def test_invalid_input(self):
        self.assertEqual(self.create(after={'elements': 'x'})[0], 400)
        self.assertEqual(self.create(after={'elements': [], 'rungs': [{'from': 'powerrail', 'path': ['p1'], 'to': None}]})[0], 400)
        for p in ({'id': 'p1', 'type': 'contact', 'operand': 5}, {'id': 'p1', 'type': 'comparison', 'op': '=>', 'in1': 'a', 'in2': 'b'}):
            self.assertEqual(self.create(after={'elements': [p], 'rungs': [{'from': 'powerrail', 'path': ['p1'], 'to': None}]})[0], 400)
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
