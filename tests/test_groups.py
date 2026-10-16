"""E5: groups of edits — one change made of several edits (interface + new network + change), one state, Upload in order."""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

from tests.common import ROOT, SAMPLES, PROJECT, TS0
import mcp_server
import s7dcl
import server

FAKE = os.path.join(ROOT, 'tests', 'fake_mcp.py')


class TestGroups(unittest.TestCase):
    def setUp(self):
        self.root, self.data, self.tia = tempfile.mkdtemp(), tempfile.mkdtemp(), tempfile.mkdtemp()
        shutil.copytree(os.path.join(SAMPLES, PROJECT, TS0), os.path.join(self.root, PROJECT, TS0))
        py = sys.executable
        mcp = {'writer': ([py, FAKE, 'writer', '--state', self.tia], None),
               'reader': ([py, FAKE, 'reader', '--state', self.tia, '--export-root', self.root, '--project', PROJECT], None)}
        self.srv = server.create_server(0, self.root, self.data, mcp=mcp, mcp_timeout=30)
        self.url = 'http://127.0.0.1:%d' % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.req('PUT', '/api/state/tia?project=' + PROJECT, {'softwarePath': 'PLC_1', 'plcName': 'PLC_1'})

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        for d in (self.root, self.data, self.tia):
            shutil.rmtree(d, ignore_errors=True)

    def req(self, method, fpath, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=60)
        c.request(method, fpath, body=json.dumps(body).encode() if body is not None else None,
                  headers={'Content-Type': 'application/json'})
        r = c.getresponse()
        t = json.loads(r.read())
        c.close()
        return r.status, t

    def edit(self, **body):
        s, u = self.req('POST', '/api/edits', dict({'project': PROJECT, 'round': 'E5', 'block': 'FB_Conveyor'}, **body))
        self.assertEqual(s, 201, u)
        return u['id']

    def three(self):
        """A jam check: a new static, a new network that sets it, a renamed network — proposed in reverse order."""
        change = self.edit(nw=7, title_after='Faults, acknowledge and jam')
        new = self.edit(kind='new_network', after_nw=2, title='Jam detection',
                        after={'elements': [{'id': 'n1', 'type': 'contact', 'operand': '#Motor'},
                                            {'id': 'n2', 'type': 'contact_nc', 'operand': '#Belt_Running'},
                                            {'id': 'n3', 'type': 'coil', 'operand': '#Jam_Detected'}],
                               'rungs': [{'from': 'powerrail', 'path': ['n1', 'n2', 'n3'], 'to': None}]})
        iface = self.edit(kind='interface', section='Static', members=[{'name': 'Jam_Detected', 'type': 'Bool'}])
        return iface, new, change

    def test_create_state_procedure(self):
        iface, new, change = self.three()
        s, g = self.req('POST', '/api/groups', {'edit_ids': [iface, new, change], 'name': 'jam detection'})
        self.assertEqual(s, 201, g)
        self.assertEqual((g['id'], g['state'], g['name'], g['f'], [m['id'] for m in g['members']]),
                         ('G001', 'Proposal', 'jam detection', False, [iface, new, change]))
        self.assertEqual(self.req('POST', '/api/groups', {'edit_ids': [iface, change]})[0], 409)    # already grouped
        self.assertEqual(self.req('POST', '/api/groups', {'edit_ids': [iface]})[0], 400)
        _, d = self.req('GET', '/api/edits/' + new)                          # the operand is defined by the grouped edit
        self.assertIn('operand #Jam_Detected is not in the block interface — defined by edit %s (state Proposal)' % iface, d['warnings'])
        s, g = self.req('POST', '/api/groups/G001/state', {'state': 'Approved', 'by': 'reviewer'})
        self.assertEqual((s, {m['state'] for m in g['members']}), (200, {'Approved'}))
        self.req('POST', '/api/edits/%s/state' % change, {'state': 'Signed'})
        s, g = self.req('GET', '/api/groups/G001')
        self.assertEqual((g['state'], g['next'], g['previous']), ('Approved', 'Signed', 'Proposal'))
        s, r = self.req('POST', '/api/groups/G001/state', {'state': 'Proposal'})      # the Signed member cannot go two back
        self.assertEqual(s, 409)
        self.assertIn('%s: transition Signed → Proposal is not allowed' % change, r['error'])
        self.assertEqual({m['state'] for m in self.req('GET', '/api/groups/G001')[1]['members']}, {'Approved', 'Signed'})
        s, g = self.req('POST', '/api/groups/G001/state', {'state': 'Signed'})          # the others catch up
        self.assertEqual((s, g['state']), (200, 'Signed'))
        _, p = self.req('GET', '/api/groups/G001/procedure')
        self.assertEqual([x['edit'] for x in p['parts']], [iface, new, change])
        self.assertIn('### 2. %s — FB10 "FB_Conveyor" · NW 2+ (new): Jam detection' % new, p['markdown'])

    def test_disapproved_group_and_mcp(self):
        iface, new, change = self.three()
        self.req('POST', '/api/groups', {'edit_ids': [iface, new, change]})
        self.req('POST', '/api/edits/%s/state' % change, {'state': 'Approved'})
        s, g = self.req('POST', '/api/groups/G001/state', {'state': 'Disapproved', 'reason': 'jam needs a timer'})
        self.assertEqual((s, g['state'], g['next'], g['previous']), (200, 'Disapproved', None, 'Proposal'))
        e = mcp_server.Editor(self.url)
        x = json.loads(mcp_server.call_tool(e, 'get_edit', {'id': new})['content'][0]['text'])
        self.assertEqual((x['edit']['state'], x['edit']['disapproved']['reason']), ('Disapproved', 'jam needs a timer'))
        x = json.loads(mcp_server.call_tool(e, 'list_edits', {'state': 'Disapproved'})['content'][0]['text'])
        self.assertEqual(len(x['edits']), 3)
        self.req('POST', '/api/edits/%s/state' % iface, {'state': 'Proposal'})
        self.assertEqual(self.req('GET', '/api/groups/G001')[1]['state'], 'Disapproved')    # while one member is
        s, g = self.req('POST', '/api/groups/G001/state', {'state': 'Proposal'})
        self.assertEqual((s, g['state']), (200, 'Proposal'))

    def test_upload_in_order_and_check(self):
        iface, new, change = self.three()
        self.req('POST', '/api/groups', {'edit_ids': [change, new, iface]})            # any order: Upload sorts by kind
        self.assertEqual(self.req('POST', '/api/groups/G001/upload', {})[0], 409)       # not Approved yet
        self.req('POST', '/api/groups/G001/state', {'state': 'Approved'})
        s, r = self.req('POST', '/api/groups/G001/upload', {})
        self.assertEqual(s, 200, r)
        self.assertTrue(r['ok'], r)
        self.assertEqual([x['edit'] for x in r['results']], [iface, new, change])
        self.assertEqual(r['view']['state'], 'Signed')
        b = s7dcl.load_block(os.path.join(self.tia, 'Conveyor', 'FB_Conveyor.s7dcl'))
        self.assertIn('Jam_Detected', b['variables'])
        self.assertEqual([n['title'] for n in b['networks']][2:4], ['Jam detection', 'Startup'])
        self.assertEqual(b['networks'][7]['title'], 'Faults, acknowledge and jam')
        s, k = self.req('POST', '/api/edits/%s/check' % iface, {})                      # one Reader export of the block
        self.assertTrue(k['match'], k)
        s, r = self.req('POST', '/api/groups/G001/check-export', {})
        self.assertTrue(r['match'], r)
        self.assertIn('the network moved: NW 7 → NW 8', [x for x in r['results'] if x['edit'] == change][0]['note'])

    def test_f_block_and_mcp(self):
        a = self.edit(block='FB_Safety', kind='interface', section='Static', members=[{'name': 'Extra', 'type': 'Bool'}])
        b = self.edit(block='FB_Safety', nw=3, title_after='Tap after a box (renamed)')
        e = mcp_server.Editor(self.url)
        r = mcp_server.call_tool(e, 'create_group', {'edit_ids': [a, b], 'name': 'safety texts'})
        self.assertFalse(r.get('isError'), r)
        g = json.loads(mcp_server.call_tool(e, 'get_group', {'id': 'G001'})['content'][0]['text'])
        self.assertEqual((g['f'], g['f_note']), (True, 'one F-signature change — write all members, then compile the safety program'))
        self.assertIn('F-signature', g['procedure'])
        _, p = self.req('GET', '/api/groups/G001/procedure')
        self.assertEqual([x['edit'] for x in p['parts']], [a, b, None])     # compile once, after the last member
        self.assertEqual(sum('F-signature' in k for x in p['parts'] for k in x['steps']), 1)
        self.req('POST', '/api/groups/G001/state', {'state': 'Approved'})
        s, r = self.req('POST', '/api/groups/G001/upload', {})
        self.assertEqual(s, 409)
        self.assertIn('F-block', r['error'])


if __name__ == '__main__':
    unittest.main()
