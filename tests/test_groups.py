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
import simaticml

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

    def test_delete_edit_and_group(self):
        """E3: delete an edit / a group before it is written; ids are not reused; the changelog lists it."""
        iface, new, change = self.three()
        self.req('POST', '/api/groups', {'edit_ids': [iface, new, change], 'name': 'old idea'})
        s, r = self.req('DELETE', '/api/edits/' + new)                                 # a member leaves the group
        self.assertEqual((s, r['deleted']), (200, [new]))
        _, g = self.req('GET', '/api/groups/G001')
        self.assertEqual([(m['id'], m['order']) for m in g['members']], [(iface, 1), (change, 2)])
        self.assertEqual(self.req('GET', '/api/edits/' + new)[0], 404)
        self.req('POST', '/api/groups/G001/state', {'state': 'Approved'})
        self.req('POST', '/api/edits/%s/state' % change, {'state': 'Signed'})
        s, r = self.req('DELETE', '/api/groups/G001?edits=1')                          # a written member blocks it
        self.assertEqual(s, 409)
        self.assertIn('%s (Signed)' % change, r['error'])
        self.assertEqual(self.req('DELETE', '/api/edits/' + change)[0], 409)
        s, r = self.req('DELETE', '/api/groups/G001')                                  # ungroup: the edits stay
        self.assertEqual((s, r['deleted']), (200, []))
        self.assertEqual(self.req('GET', '/api/groups/G001')[0], 404)
        self.assertEqual(self.req('DELETE', '/api/edits/' + iface)[0], 200)
        a = self.edit(nw=7, title_after='x')
        b = self.edit(nw=6, title_after='y')
        self.assertEqual(a, 'U004')                                      # deleted U002 / U003 are not reused
        s, g = self.req('POST', '/api/groups', {'edit_ids': [a, b]})
        self.assertEqual(g['id'], 'G002')
        s, r = self.req('DELETE', '/api/groups/G002?edits=1')
        self.assertEqual((s, sorted(r['deleted'])), (200, sorted([a, b])))
        _, log = self.req('GET', '/api/projects/%s/changelog' % PROJECT)
        dl = [x['summary'] for x in log['items'] if x['kind'] == 'deleted']
        self.assertEqual(len(dl), 6, dl)                                 # 4 edits + ungroup G001 + G002
        self.assertIn('deleted group G001 (edits %s, %s · old idea)' % (iface, change), dl)

    def test_task_and_mcp_delete_edit(self):
        a = self.edit(nw=7, title_after='x')
        b = self.edit(nw=6, title_after='y')
        s, g = self.req('POST', '/api/groups', {'edit_ids': [a, b], 'task': 'TASK021', 'goal': 'jam'})
        self.assertEqual(g['task'], {'id': 'TASK021', 'goal': 'jam'})
        s, g = self.req('POST', '/api/groups/G001/task', {'task': ''})
        self.assertIsNone(g['task'])
        e = mcp_server.Editor(self.url)
        r = json.loads(mcp_server.call_tool(e, 'delete_edit', {'id': a})['content'][0]['text'])
        self.assertEqual(r['deleted'], [a])
        self.assertEqual(self.req('GET', '/api/edits/' + a)[0], 404)

    def test_force_delete_written_edits(self):
        a = self.edit(nw=7, title_after='x')
        b = self.edit(nw=6, title_after='y')
        c = self.edit(nw=5, title_after='z')
        self.req('POST', '/api/groups', {'edit_ids': [a, b, c]})
        self.req('POST', '/api/groups/G001/state', {'state': 'Approved'})
        self.req('POST', '/api/edits/%s/state' % a, {'state': 'Signed'})
        self.assertEqual(self.req('DELETE', '/api/edits/' + a)[0], 409)
        self.assertIn('force=1', self.req('DELETE', '/api/edits/' + a)[1]['error'])
        self.assertEqual(self.req('DELETE', '/api/edits/%s?force=1' % a)[0], 200)
        s, r = self.req('DELETE', '/api/groups/G001?edits=1')            # b, c are Approved: no force needed
        self.assertEqual((s, sorted(r['deleted'])), (200, sorted([b, c])))
        d = self.edit(nw=7, title_after='x')
        e = self.edit(nw=6, title_after='y')
        self.req('POST', '/api/groups', {'edit_ids': [d, e]})
        self.req('POST', '/api/groups/G002/state', {'state': 'Approved'})
        self.req('POST', '/api/groups/G002/state', {'state': 'Signed'})
        self.assertEqual(self.req('DELETE', '/api/groups/G002?edits=1')[0], 409)
        self.assertEqual(self.req('DELETE', '/api/groups/G002?edits=1&force=1')[0], 200)
        self.assertEqual(self.req('GET', '/api/edits/' + d)[0], 404)
        _, log = self.req('GET', '/api/projects/%s/changelog' % PROJECT)
        self.assertTrue(any(x['kind'] == 'deleted' and a in x['summary'] for x in log['items']))
        self.assertEqual(self.edit(nw=7, title_after='q'), 'U006')      # ids are not reused
        e = mcp_server.Editor(self.url)
        t = self.edit(nw=4, title_after='w')
        self.req('POST', '/api/edits/%s/state' % t, {'state': 'Approved'})
        self.req('POST', '/api/edits/%s/state' % t, {'state': 'Signed'})
        self.assertTrue(mcp_server.call_tool(e, 'delete_edit', {'id': t}).get('isError'))
        self.assertFalse(mcp_server.call_tool(e, 'delete_edit', {'id': t, 'force': True}).get('isError'))

    def test_cleanup(self):
        ids = [self.edit(nw=n, title_after='t%d' % n) for n in (7, 6, 5, 4)]
        self.req('POST', '/api/groups', {'edit_ids': ids[:3], 'card': '#12'})
        self.req('POST', '/api/edits/%s/state' % ids[3], {'state': 'Approved'})
        self.req('POST', '/api/edits/%s/state' % ids[3], {'state': 'Signed'})
        self.assertEqual(self.req('POST', '/api/cleanup', {})[0], 400)                   # a filter is required
        s, r = self.req('POST', '/api/cleanup', {'project': PROJECT, 'all': True})       # dry run by default
        self.assertEqual((s, r['dry_run'], r['count'], r['needs_force']), (200, True, 4, [ids[3]]))
        self.assertEqual(len(self.req('GET', '/api/state?project=' + PROJECT)[1]['edits']), 4)
        self.assertEqual(self.req('POST', '/api/cleanup', {'all': True, 'dry_run': False})[0], 409)
        s, r = self.req('POST', '/api/cleanup', {'card': '#12', 'dry_run': False})
        self.assertEqual((s, r['count']), (200, 3), r)
        self.assertEqual(len(r['backups']), 1)
        with open(r['backups'][0], encoding='utf-8') as f:
            self.assertEqual(len(json.load(f)['edits']), 4)                                # the backup is the state before
        self.assertEqual([u['id'] for u in self.req('GET', '/api/state?project=' + PROJECT)[1]['edits']], [ids[3]])
        s, r = self.req('POST', '/api/cleanup', {'states': ['Signed'], 'dry_run': False, 'force': True})
        self.assertEqual((s, r['count']), (200, 1))
        self.assertEqual(self.req('GET', '/api/state?project=' + PROJECT)[1]['edits'], [])
        e = mcp_server.Editor(self.url)
        r = json.loads(mcp_server.call_tool(e, 'cleanup_edits', {'all': True})['content'][0]['text'])
        self.assertEqual((r['dry_run'], r['count']), (True, 0))

    def test_group_card(self):
        a, b, c, d = (self.edit(nw=n, title_after='t%d' % n) for n in (7, 6, 5, 4))
        s, g = self.req('POST', '/api/groups', {'edit_ids': [a, b], 'name': 'jam', 'card': '#7', 'card_title': 'Jam'})
        self.assertEqual((g['card'], g['card_title']), ('#7', 'Jam'))
        self.req('POST', '/api/groups', {'edit_ids': [c, d], 'name': 'other'})
        s, r = self.req('GET', '/api/groups?card=%23' + '7')
        self.assertEqual([x['id'] for x in r['groups']], ['G001'])
        self.assertEqual(len(self.req('GET', '/api/groups?project=' + PROJECT)[1]['groups']), 2)
        s, g = self.req('POST', '/api/groups/G002', {'card': 'https://deck.example/c/9'})
        self.assertEqual((g['card'], g['name']), ('https://deck.example/c/9', 'other'))
        e = mcp_server.Editor(self.url)
        r = json.loads(mcp_server.call_tool(e, 'update_group', {'id': 'G001', 'card': ''})['content'][0]['text'])
        self.assertEqual(r['card'], '')
        r = json.loads(mcp_server.call_tool(e, 'list_groups', {'card': 'https://deck.example/c/9'})['content'][0]['text'])
        self.assertEqual([x['id'] for x in r['groups']], ['G002'])

    def test_card_from_legacy_group_name(self):
        a, b = self.edit(nw=7, title_after='x'), self.edit(nw=6, title_after='y')
        self.req('POST', '/api/groups', {'edit_ids': [a, b], 'name': '#123 Jam detection – timer'})
        g = self.req('GET', '/api/groups/G001')[1]
        self.assertEqual((g['card'], g['card_title']), ('#123', 'Jam detection'))
        self.req('POST', '/api/groups/G001', {'card': ''})            # cleared explicitly: not derived from the name again
        self.assertEqual(self.req('GET', '/api/groups/G001')[1]['card'], '')

    def test_upload_in_order_and_check(self):
        iface, new, change = self.three()
        self.req('POST', '/api/groups', {'edit_ids': [change, new, iface]})            # any order: Upload sorts by kind
        self.assertEqual(self.req('POST', '/api/groups/G001/upload', {})[0], 409)       # not Approved yet
        self.req('POST', '/api/groups/G001/state', {'state': 'Approved'})
        s, r = self.req('POST', '/api/groups/G001/upload', {})
        self.assertEqual(s, 200, r)
        self.assertTrue(r['ok'], r)
        self.assertEqual(([x['edit'] for x in r['results']], r['via']), ([iface, new, change], 'SimaticML'))
        self.assertEqual(r['view']['state'], 'Signed')
        with open(os.path.join(self.tia, 'calls.jsonl'), encoding='utf-8') as f:     # a new network: one XML import
            tools = [json.loads(x)['tool'] for x in f]
        self.assertEqual((tools.count('ExportXmlBlock'), tools.count('ImportXmlBlock'), tools.count('ImportFromDocuments')), (1, 1, 0))
        with open(os.path.join(self.tia, 'Conveyor', 'FB_Conveyor.xml'), encoding='utf-8-sig') as f:
            t = f.read()
        self.assertIn('<Member Name="Jam_Detected" Datatype="Bool" />', t)
        nets = simaticml.networks(t)
        self.assertEqual([n['title'] for n in nets][2:4], ['Jam detection', 'Startup'])
        self.assertEqual(nets[7]['title'], 'Faults, acknowledge and jam')
        u = self.req('GET', '/api/edits/' + change)[1]['edit']
        self.assertEqual((u['writes'][-1]['edits'], u['writes'][-1]['ok']), ([iface, new, change], True))

    def test_upload_through_documents_in_order(self):
        """Without a new network, an OB or GRAPH, a group goes through documents, member by member."""
        change = self.edit(nw=7, title_after='Faults, acknowledge and jam')
        iface = self.edit(kind='interface', section='Static', members=[{'name': 'Jam_Detected', 'type': 'Bool'}])
        self.req('POST', '/api/groups', {'edit_ids': [change, iface]})
        self.req('POST', '/api/groups/G001/state', {'state': 'Approved'})
        s, r = self.req('POST', '/api/groups/G001/upload', {})
        self.assertTrue(r['ok'], r)
        self.assertEqual([x['edit'] for x in r['results']], [iface, change])
        b = s7dcl.load_block(os.path.join(self.tia, 'Conveyor', 'FB_Conveyor.s7dcl'))
        self.assertIn('Jam_Detected', b['variables'])
        self.assertEqual(b['networks'][6]['title'], 'Faults, acknowledge and jam')
        s, k = self.req('POST', '/api/edits/%s/check' % iface, {})                      # one Reader export of the block
        self.assertTrue(k['match'], k)
        s, r = self.req('POST', '/api/groups/G001/check-export', {})
        self.assertTrue(r['match'], r)

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
