"""E3: new network edits — insertion into .s7dcl, API, TIA steps, Upload (+ SetNetworkText for the title) and Check."""
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
import s7dcl_write
import server

FAKE = os.path.join(ROOT, 'tests', 'fake_mcp.py')
AFTER = {'elements': [{'id': 'n1', 'type': 'contact', 'operand': '#Start'}, {'id': 'n2', 'type': 'coil', 'operand': '"Horn"'}],
         'rungs': [{'from': 'powerrail', 'path': ['n1', 'n2'], 'to': None}]}


def sample(block='FB_Conveyor', folder='Conveyor'):
    return s7dcl.read(os.path.join(SAMPLES, PROJECT, TS0, 'PLC_1', 'Blocks', folder, block + '.s7dcl'))


class TestInsertNetwork(unittest.TestCase):
    def test_after_a_network_and_at_the_start(self):
        text = sample()
        before = s7dcl.parse_block(text)['networks']
        for after_nw in (0, 2, 8):
            t = s7dcl_write.insert_network(text, after_nw, s7dcl_write.network_text(AFTER))
            nets = s7dcl.parse_block(t)['networks']
            self.assertEqual(len(nets), 9)
            self.assertEqual([p['operand'] for p in nets[after_nw]['elements']], ['#Start', '"Horn"'])
            self.assertEqual([n['text'] for n in nets[:after_nw] + nets[after_nw + 1:]], [n['text'] for n in before])
            self.assertEqual([n['attributes'] for n in nets[:after_nw] + nets[after_nw + 1:]], [n['attributes'] for n in before])
        self.assertRaises(s7dcl_write.WriteError, s7dcl_write.insert_network, text, 9, '')

    def test_fbd_crlf_and_a_block_without_networks(self):
        t = s7dcl_write.insert_network(sample().replace('\n', '\r\n'), 1, '        RUNG #A\n            Coil( #B )\n        END_RUNG\n', 'FBD')
        self.assertNotIn('\n', t.replace('\r\n', ''))
        n = s7dcl.parse_block(t.replace('\r\n', '\n'))['networks'][1]
        self.assertEqual((n['language'], n['elements'][0]['type']), ('FBD', 'assign'))
        head = sample().split('    {\n        S7_Language')[0] + 'END_FUNCTION_BLOCK\n'
        t = s7dcl_write.insert_network(head, 0, s7dcl_write.network_text(AFTER))
        self.assertEqual(len(s7dcl.parse_block(t)['networks']), 1)


class TestNewNetworkApi(unittest.TestCase):
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

    def propose(self, **kw):
        body = dict({'project': PROJECT, 'round': 'E3', 'kind': 'new_network', 'block': 'FB_Conveyor', 'after_nw': 2,
                     'title': 'Horn while starting', 'comment': 'sounds while Start is pressed', 'after': AFTER}, **kw)
        return self.req('POST', '/api/edits', body)

    def test_create_detail_procedure(self):
        s, u = self.propose()
        self.assertEqual(s, 201, u)
        self.assertEqual((u['kind'], u['nw'], u['after_nw'], u['after_title'], u['language'], u['title']),
                         ('new_network', 3, 2, 'Motor latch', 'LAD', 'Horn while starting'))
        _, d = self.req('GET', '/api/edits/' + u['id'])
        self.assertEqual(d['diff'], {'n1': 'added', 'n2': 'added'})
        self.assertEqual(d['errors'], [])
        _, p = self.req('GET', '/api/edits/%s/procedure' % u['id'])
        self.assertTrue(p['header'].endswith('· NW 2+ (new): Horn while starting'), p['header'])
        self.assertIn('Insert a network after it: right-click → **Insert network** (`Ctrl+R`) ⟦unverified⟧.', p['steps'])
        self.assertIn('Set the network title to **Horn while starting** and the comment to “sounds while Start is pressed”.', p['steps'])
        self.assertTrue(any(x.startswith('Insert `-| |- #Start` at the start of rung 1') for x in p['steps']), p['steps'])
        s, r = self.req('POST', '/api/edits/%s/after' % u['id'], {'title': 'Horn on start'})
        self.assertEqual((s, r['edit']['title']), (200, 'Horn on start'))

    def test_invalid(self):
        for kw in ({'after_nw': 9}, {'after_nw': -1}, {'title': ''}, {'title': 'Motor latch'}, {'language': 'SCL'},
                   {'after': {'elements': 'x'}}, {'title': 'a\nb'}):
            s, r = self.propose(**kw)
            self.assertEqual(s, 400, (kw, r))

    def test_fbd_in_an_f_block(self):
        s, u = self.propose(block='FB_Safety', after_nw=0, title='Lamp test',
                            after={'language': 'FBD', 'elements': [{'id': 'n1', 'type': 'assign', 'operand': '#Lamp_Green',
                                                                   'inputs': [{'n': 1, 'pin': 'in', 'operand': '#Start'}], 'outputs': []}]})
        self.assertEqual(s, 201, u)
        self.assertEqual((u['language'], u['f'], u['after_title']), ('FBD', True, ''))
        _, p = self.req('GET', '/api/edits/%s/procedure' % u['id'])
        self.assertIn('F-signature', p['markdown'])
        self.req('POST', '/api/edits/%s/state' % u['id'], {'state': 'Approved'})
        self.assertEqual(self.req('POST', '/api/edits/%s/upload' % u['id'], {})[0], 409)

    def test_upload_title_and_check(self):
        self.req('PUT', '/api/state/tia?project=' + PROJECT, {'softwarePath': 'PLC_1', 'plcName': 'PLC_1'})
        _, net = self.req('GET', '/api/network?project=%s&block=FB_Conveyor&nw=4' % PROJECT)
        _, old = self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'E3', 'block': 'FB_Conveyor', 'nw': 4,
                                                 'after': {'elements': net['elements'], 'rungs': net['rungs']}})
        _, u = self.propose()
        self.req('POST', '/api/edits/%s/state' % u['id'], {'state': 'Approved'})
        s, r = self.req('POST', '/api/edits/%s/upload' % u['id'], {})
        self.assertTrue(r['ok'], r)
        self.assertEqual([x['step'] for x in r['write']['steps']][-6:],
                         ['Insert the network in .s7dcl', 'PreviewImport', 'ImportFromDocuments', 'CompileBlock', 'SetNetworkText',
                          'CompileBlock'])
        with open(os.path.join(self.tia, 'calls.jsonl'), encoding='utf-8') as f:
            call = [json.loads(x) for x in f if '"SetNetworkText"' in x][0]['arguments']
        self.assertEqual(call, {'softwarePath': 'PLC_1', 'blockPath': 'Conveyor/FB_Conveyor', 'network': 3, 'dryRun': False,
                                'title': 'Horn while starting', 'comment': 'sounds while Start is pressed'})
        b = s7dcl.load_block(os.path.join(self.tia, 'Conveyor', 'FB_Conveyor.s7dcl'))
        self.assertEqual([n['title'] for n in b['networks']][1:4], ['Motor latch', 'Horn while starting', 'Startup'])
        self.assertEqual(b['networks'][2]['comment'], 'sounds while Start is pressed')
        s, r = self.req('POST', '/api/edits/%s/check' % u['id'], {})
        self.assertTrue(r['match'], r)
        self.assertIn('the new network is NW 3 in the export', r['edit']['checks'][-1]['note'])
        _, k = self.req('POST', '/api/edits/%s/check-export' % old['id'], {})      # the networks after it moved
        self.assertTrue(k['match'], k)
        self.assertIn('the network moved: NW 4 → NW 5', k['note'])
        _, c = self.req('GET', '/api/projects/%s/compare?from=%s&to=%s' % (PROJECT, TS0, k['export_ts']))
        nets = c['blocks'][0]['networks']
        self.assertIn((None, 3, 'added', [u['id']]), [(n['nw_from'], n['nw_to'], n['status'], n['matches']) for n in nets])

    def test_mcp(self):
        e = mcp_server.Editor(self.url)
        r = mcp_server.call_tool(e, 'propose_edit', {'project': PROJECT, 'round': 'E3', 'block': 'FB_Conveyor',
                                                     'kind': 'new_network', 'after_nw': 0, 'title': 'First', 'after': AFTER})
        self.assertFalse(r.get('isError'), r)
        uid = json.loads(r['content'][0]['text'])['id']
        g = json.loads(mcp_server.call_tool(e, 'get_edit', {'id': uid})['content'][0]['text'])
        self.assertEqual((g['edit']['kind'], g['edit']['after_nw'], g['diff']), ('new_network', 0, {'n1': 'added', 'n2': 'added'}))
        r = mcp_server.call_tool(e, 'propose_edit', {'project': PROJECT, 'round': 'E3', 'block': 'FB_Conveyor', 'kind': 'new_network',
                                                     'after_nw': 0, 'after': AFTER})
        self.assertIn('needs: title', r['content'][0]['text'])


if __name__ == '__main__':
    unittest.main()
