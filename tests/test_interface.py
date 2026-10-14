"""E2: interface edits (adding members to a section of the block interface) and the #operand warning of network edits."""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

from tests.common import ROOT, SAMPLES, PROJECT, TS0, TS_IF
import catalog
import mcp_server
import s7dcl
import s7dcl_write
import server

FAKE = os.path.join(ROOT, 'tests', 'fake_mcp.py')
MEMBERS = [{'name': 'Jam_Detected', 'type': 'Bool'}, {'name': 'Jam_Count', 'type': 'Int', 'comment': 'jam counter', 'initial': '0'}]


def sample(block, folder='Conveyor', ts=TS0):
    return s7dcl.read(os.path.join(SAMPLES, PROJECT, ts, 'PLC_1', 'Blocks', folder, block + '.s7dcl'))


class TestAddMembers(unittest.TestCase):
    def test_into_an_existing_section(self):
        t = s7dcl_write.add_members(sample('FB_Conveyor'), 'Static', MEMBERS)
        st = catalog.interface(t)['section']['static']
        self.assertEqual(st[-2:], [{'name': 'Jam_Detected', 'type': 'Bool'}, {'name': 'Jam_Count', 'type': 'Int'}])
        self.assertIn('        Jam_Count : Int := 0;\n    END_VAR', t)
        self.assertEqual(len(s7dcl.parse_block(t)['networks']), 8)            # networks untouched

    def test_retain_section_is_static(self):
        text = sample('FB_Conveyor').replace('\n    VAR\n', '\n    VAR RETAIN\n', 1)
        t = s7dcl_write.add_members(text, 'Static', MEMBERS[:1])
        self.assertEqual(t.count('END_VAR'), text.count('END_VAR'))               # no second Static section
        self.assertEqual(catalog.interface(t)['section']['static'][-1], {'name': 'Jam_Detected', 'type': 'Bool'})

    def test_new_section_and_crlf(self):
        text = sample('FB_Conveyor').replace('\n', '\r\n')
        t = s7dcl_write.add_members(text, 'Temp', [{'name': 'Tmp', 'type': 'Int'}])
        self.assertNotIn('\n', t.replace('\r\n', ''))
        self.assertEqual(catalog.interface(t.replace('\r\n', '\n'))['section']['temp'], [{'name': 'Tmp', 'type': 'Int'}])
        t = s7dcl_write.add_members(sample('FC_Branches'), 'Temp', [{'name': 'Aux2', 'type': 'Real'}])
        self.assertEqual([m['name'] for m in catalog.interface(t)['section']['temp']], ['Aux', 'Aux2'])

    def test_operands_found_in_a_model(self):
        m = {'elements': [{'id': 'p1', 'type': 'contact', 'operand': '#Jam_Detected'},
                          {'id': 'p2', 'type': 'box', 'inputs': [{'pin': 'PT', 'operand': 'T#5s'}, {'pin': 'IN', 'operand': '16#FF'}],
                           'instance': '#T_Jam.Q', 'name': 'TON'},
                          {'id': 'p3', 'type': 'coil', 'operand': '"DB".x'}]}
        self.assertEqual(server._local_operands(m), {'Jam_Detected', 'T_Jam'})


class TestInterfaceApi(unittest.TestCase):
    def setUp(self):
        self.root, self.data, self.tia = tempfile.mkdtemp(), tempfile.mkdtemp(), tempfile.mkdtemp()
        shutil.copytree(os.path.join(SAMPLES, PROJECT, TS0), os.path.join(self.root, PROJECT, TS0))
        py = sys.executable
        mcp = {'writer': ([py, FAKE, 'writer', '--state', self.tia], None),
               'reader': ([py, FAKE, 'reader', '--state', self.tia, '--export-root', self.root, '--project', PROJECT], None)}
        self.srv = server.create_server(0, self.root, self.data, mcp=mcp, mcp_timeout=30)
        self.url = 'http://127.0.0.1:%d' % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

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

    def propose(self, block='FB_Conveyor', members=MEMBERS, section='Static'):
        return self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'E2', 'kind': 'interface', 'block': block,
                                                'section': section, 'members': members, 'note': 'jam detection'})

    def test_create_detail_procedure(self):
        s, u = self.propose()
        self.assertEqual(s, 201, u)
        self.assertEqual((u['kind'], u['block_kind'], u['block_number'], u['nw'], u['f'], u['state']),
                         ('interface', 'FB', 10, None, False, 'Proposal'))
        self.assertEqual(u['members'][1], {'name': 'Jam_Count', 'type': 'Int', 'comment': 'jam counter', 'initial': '0'})
        self.assertEqual(u['before']['export_ts'], TS0)
        s, d = self.req('GET', '/api/edits/' + u['id'])
        self.assertEqual(s, 200, d)
        i = d['interface']
        self.assertEqual(i['section'], 'Static')
        self.assertEqual([m['name'] for m in i['after']['Static'] if m.get('added')], ['Jam_Detected', 'Jam_Count'])
        self.assertEqual(len(i['after']['Static']), len(i['before']['Static']) + 2)
        self.assertEqual(d['errors'], [])
        _, p = self.req('GET', '/api/edits/%s/procedure' % u['id'])
        self.assertTrue(p['header'].endswith('· Interface: +2 Static'), p['header'])
        self.assertIn('Open the block interface → section **Static** → add `Jam_Count : Int := 0` (jam counter).', p['steps'])

    def test_invalid(self):
        for members, section in (([{'name': 'Ready', 'type': 'Bool'}], 'Static'),     # already in the interface
                                 ([{'name': 'ready_x', 'type': 'Bool'}, {'name': 'READY_X', 'type': 'Int'}], 'Static'),
                                 ([], 'Static'), ([{'name': '1x', 'type': 'Bool'}], 'Static'),
                                 ([{'name': 'X', 'type': 'Bool; Y : Int'}], 'Static'), ([{'name': 'X', 'type': 'Bool'}], 'Var')):
            s, r = self.propose(members=members, section=section)
            self.assertEqual(s, 400, (members, section, r))
        self.assertEqual(self.propose(block='DB_Machine')[0], 400)

    def test_update_members(self):
        _, u = self.propose()
        s, r = self.req('POST', '/api/edits/%s/after' % u['id'], {'members': [{'name': 'Jam', 'type': 'Bool'}], 'section': 'Output'})
        self.assertEqual(s, 200, r)
        self.assertEqual((r['edit']['section'], r['edit']['members']), ('Output', [{'name': 'Jam', 'type': 'Bool'}]))

    def test_check_with_export(self):
        _, u = self.propose()
        _, v = self.propose(members=[{'name': 'Jam_Count', 'type': 'DInt'}, {'name': 'Jam_X', 'type': 'Bool'}])
        shutil.copytree(os.path.join(SAMPLES, PROJECT, TS_IF), os.path.join(self.root, PROJECT, TS_IF))   # after writing
        s, k = self.req('POST', '/api/edits/%s/check-export' % u['id'], {'export_ts': TS0})
        self.assertEqual((s, k['match']), (200, False))
        self.assertIn('missing in the export: Jam_Detected : Bool (Static)', k['diffs'])
        _, k = self.req('POST', '/api/edits/%s/check-export' % u['id'], {'export_ts': TS_IF})
        self.assertTrue(k['match'], k)
        self.assertTrue(k['note'].startswith('export = proposal (2 members in Static)'))
        self.assertEqual(self.propose()[0], 400)          # the members are in the newest export now
        _, k = self.req('POST', '/api/edits/%s/check-export' % v['id'], {'export_ts': TS_IF})
        self.assertEqual(k['diffs'], ['different type: Jam_Count : Int in the export, DInt in the proposal',
                                      'missing in the export: Jam_X : Bool (Static)'])

    def test_operand_warning(self):
        _, net = self.req('GET', '/api/network?project=%s&block=FB_Conveyor&nw=1' % PROJECT)
        after = {'elements': net['elements'] + [{'id': 'n1', 'type': 'contact', 'operand': '#Jam_Detected'},
                                                {'id': 'n2', 'type': 'contact', 'operand': '#Nope'}],
                 'rungs': [dict(net['rungs'][0], path=net['rungs'][0]['path'][:-1] + ['n1', 'n2', net['rungs'][0]['path'][-1]])]}
        _, n = self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'E2', 'block': 'FB_Conveyor', 'nw': 1, 'after': after})
        _, d = self.req('GET', '/api/edits/' + n['id'])
        self.assertEqual([w for w in d['warnings'] if w.startswith('operand')],
                         ['operand #Jam_Detected is not in the block interface — propose it first (interface edit)',
                          'operand #Nope is not in the block interface — propose it first (interface edit)'])
        _, u = self.propose()
        _, d = self.req('GET', '/api/edits/' + n['id'])
        self.assertIn('operand #Jam_Detected is not in the block interface — defined by edit %s (state Proposal)' % u['id'],
                      d['warnings'])

    def test_upload_writes_the_members(self):
        self.req('PUT', '/api/state/tia?project=' + PROJECT, {'softwarePath': 'PLC_1', 'plcName': 'PLC_1'})
        _, u = self.propose()
        self.req('POST', '/api/edits/%s/state' % u['id'], {'state': 'Approved'})
        s, r = self.req('POST', '/api/edits/%s/upload' % u['id'], {})
        self.assertEqual(s, 200, r)
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['edit']['state'], 'Signed')
        self.assertIn('Add interface members in .s7dcl', [x['step'] for x in r['write']['steps']])
        with open(os.path.join(self.tia, 'Conveyor', 'FB_Conveyor.s7dcl'), encoding='utf-8-sig') as f:
            t = f.read()
        self.assertIn('        Jam_Detected : Bool;\n        Jam_Count : Int := 0;\n    END_VAR', t.replace('\r\n', '\n'))
        s, r = self.req('POST', '/api/edits/%s/check' % u['id'], {})      # the fake Reader exports what was imported
        self.assertTrue(r['match'], r)
        self.assertEqual(r['edit']['state'], 'Checked')
        _, f = self.propose(block='FB_Safety', members=[{'name': 'Extra', 'type': 'Bool'}])
        self.assertTrue(f['f'])
        self.req('POST', '/api/edits/%s/state' % f['id'], {'state': 'Approved'})
        self.assertEqual(self.req('POST', '/api/edits/%s/upload' % f['id'], {})[0], 409)

    def test_mcp_propose_and_get(self):
        e = mcp_server.Editor(self.url)
        r = mcp_server.call_tool(e, 'propose_edit', {'project': PROJECT, 'round': 'E2', 'block': 'FB_Conveyor',
                                                     'kind': 'interface', 'section': 'Static', 'members': MEMBERS})
        self.assertFalse(r.get('isError'), r)
        uid = json.loads(r['content'][0]['text'])['id']
        g = json.loads(mcp_server.call_tool(e, 'get_edit', {'id': uid})['content'][0]['text'])
        self.assertEqual((g['edit']['kind'], g['edit']['members']), ('interface', MEMBERS))
        self.assertIn('Interface: +2 Static', g['procedure'])
        r = mcp_server.call_tool(e, 'propose_edit', {'project': PROJECT, 'round': 'E2', 'block': 'FB_Conveyor', 'kind': 'interface'})
        self.assertTrue(r['isError'])
        r = json.loads(mcp_server.call_tool(e, 'list_edits', {'project': PROJECT})['content'][0]['text'])
        self.assertEqual(r['edits'][0]['where'], 'Interface: +2 Static')


if __name__ == '__main__':
    unittest.main()
