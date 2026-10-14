"""D2: the editor as an MCP server — mcp_server.py driven by mcp_client.py against a test HTTP server, no TIA."""
import base64
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

from tests.common import ROOT, SAMPLES, PROJECT, TS0, TS1
import mcp_client
import screenshot
import server

MCP_SERVER = os.path.join(ROOT, 'mcp_server.py')
TOOLS = {'list_projects', 'list_blocks', 'get_network', 'list_edits', 'get_edit', 'propose_edit', 'update_edit',
         'create_group', 'get_group', 'run_check', 'get_procedure', 'tia_status', 'take_snapshot', 'get_job', 'screenshot', 'get_changelog',
         'list_snapshots', 'compare_snapshots', 'list_screens', 'get_screen'}
OPS = [{'type': 'insert', 'place': {'r': 0, 'k': 3}, 'element': 'NO'},
       {'type': 'change', 'id': 'n1', 'values': {'operand': '"Fuse_OK"'}}]


class TestMcpServer(unittest.TestCase):
    def setUp(self):
        self.root, self.data = tempfile.mkdtemp(), tempfile.mkdtemp()
        shutil.copytree(os.path.join(SAMPLES, PROJECT, TS0), os.path.join(self.root, PROJECT, TS0))
        self.srv = server.create_server(0, self.root, self.data, ai_timeout=1.0)
        self.url = 'http://127.0.0.1:%d' % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.clients = []

    def tearDown(self):
        for k in self.clients:
            k.close()
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    def client(self, name='test-agent', url=None):
        k = mcp_client.Client([sys.executable, MCP_SERVER, '--server', url or self.url, '--heartbeat', '0.3'],
                              timeout=60, display_name='mcp_server', client_info={'name': name, 'version': '1.0'})
        k.start()
        self.clients.append(k)
        return k

    def ai(self):
        return server.ai_status(self.srv.RequestHandlerClass)['ai']

    def call(self, k, tool, **a):
        return json.loads(k.call(tool, a))

    def test_tools_and_edit_workflow(self):
        k = self.client()
        self.assertEqual(self.ai(), 'connected: test-agent')
        self.assertEqual({t['name'] for t in k.list_tools()}, TOOLS)
        st = self.call(k, 'tia_status')
        self.assertEqual(st['ai'], 'connected: test-agent')
        self.assertEqual(st['ai_clients'][0]['name'], 'test-agent')

        self.assertIn(PROJECT, [p['name'] for p in self.call(k, 'list_projects')['projects']])
        blocks = {b['block']: b for b in self.call(k, 'list_blocks', project=PROJECT)['blocks']}
        self.assertEqual((blocks['FB_Conveyor']['kind'], blocks['FB_Conveyor']['networks']), ('FB', 8))
        net = self.call(k, 'get_network', project=PROJECT, block='FB_Conveyor', nw=1)
        self.assertEqual(net['language'], 'LAD')
        self.assertNotIn('drawing', net)
        self.assertNotIn('drawing', self.call(k, 'get_network', project=PROJECT, block='FB_Safety', nw=1)['as_lad'])

        after = server.api_layout(self.root, {'project': PROJECT, 'ops': OPS,
                                              'after': {'elements': net['elements'], 'rungs': net['rungs']}})['after']
        r = self.call(k, 'propose_edit', project=PROJECT, round='AI', block='FB_Conveyor', nw=1, after=after,
                      note='add a fuse contact')
        uid = r['id']
        self.assertEqual((r['state'], r['errors']), ('Proposal', []))
        with open(os.path.join(self.data, PROJECT, 'state.json'), encoding='utf-8') as f:
            u = next(x for x in json.load(f)['edits'] if x['id'] == uid)
        self.assertEqual((u['state'], u['author'], u['note']), ('Proposal', 'test-agent', 'add a fuse contact'))

        self.assertEqual(self.call(k, 'update_edit', id=uid, after=after, note='fuse')['state'], 'Proposal')
        e = self.call(k, 'get_edit', id=uid)
        self.assertEqual((e['edit']['note'], e['edit']['before']['export_ts']), ('fuse', TS0))
        self.assertIn('added', e['diff'].values())
        self.assertIn('NW 1', e['procedure'])
        self.assertIn('NW 1', k.call('get_procedure', {'id': uid}))
        self.assertEqual([x['id'] for x in self.call(k, 'list_edits', state='Proposal')['edits']], [uid])
        self.assertEqual(self.call(k, 'list_edits', project=PROJECT, state='Approved')['edits'], [])

        self.assertFalse(self.call(k, 'run_check', id=uid)['match'])          # newest export = before the change
        shutil.copytree(os.path.join(SAMPLES, PROJECT, TS1), os.path.join(self.root, PROJECT, TS1))   # "export after writing"
        c = self.call(k, 'run_check', id=uid)
        self.assertEqual((c['match'], c['export_ts'], c['state']), (True, TS1, 'Proposal'))
        self.assertEqual(len(self.call(k, 'list_blocks', project=PROJECT, export_ts=TS1)['blocks']), 1)

        job = self.call(k, 'take_snapshot', project=PROJECT)                # no Writer configured -> the job fails
        for _ in range(50):
            job = self.call(k, 'get_job', id=job['id'])
            if job['state'] != 'running':
                break
            time.sleep(0.1)
        self.assertEqual(job['state'], 'error')
        for tool, a in (('get_edit', {'id': 'U999'}), ('list_blocks', {'project': 'Nope'}), ('get_edit', {}),
                        ('take_snapshot', {'project': 'Nope'}), ('get_job', {'id': 'J999'}), ('nope', {})):
            r = k.call_raw(tool, a)
            self.assertTrue(r.get('isError'), (tool, r))
        self.assertIn('HTTP 404', k.call_raw('get_edit', {'id': 'U999'})['content'][0]['text'])

    def test_disconnect_and_timeout(self):
        k = self.client()
        k.close()                                                   # EOF on stdin -> DELETE
        self.assertEqual(self.ai(), 'no client')
        k = self.client('second')
        self.assertEqual(self.ai(), 'connected: second')
        k.p.kill()                                                  # no DELETE -> forgotten after ai_timeout
        k.p.wait()
        self.assertEqual(self.ai(), 'connected: second')
        time.sleep(1.5)
        self.assertEqual(self.ai(), 'no client')

    def test_editor_not_running(self):
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        k = self.client(url='http://127.0.0.1:%d' % port)          # initialize must not fail without the editor
        r = k.call_raw('list_projects', {})
        self.assertTrue(r['isError'])
        self.assertIn('not reachable', r['content'][0]['text'])

    @unittest.skipUnless(any(os.path.exists(p) for p in screenshot.BROWSERS), 'Chrome / Edge not installed')
    def test_screenshot(self):
        k = self.client()
        r = k.call_raw('screenshot', {'project': PROJECT, 'block': 'FB_Conveyor', 'nw': 2, 'width': 900, 'height': 500})
        self.assertFalse(r.get('isError'), r)
        self.assertEqual(r['content'][0]['mimeType'], 'image/png')
        self.assertTrue(base64.b64decode(r['content'][0]['data']).startswith(b'\x89PNG'))
        self.assertTrue(k.call_raw('screenshot', {'project': PROJECT})['isError'])


if __name__ == '__main__':
    unittest.main()
