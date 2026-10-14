"""D1: project registry, TIA status and snapshots through the API with fake MCP servers — without TIA."""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import quote

from tests.common import ROOT, SAMPLES, PROJECT, TS0
import server

FAKE = os.path.join(ROOT, 'tests', 'fake_mcp.py')


class TestProjects(unittest.TestCase):
    def setUp(self):
        self.root, self.data, self.tia, self.reader_root = (tempfile.mkdtemp() for _ in range(4))
        shutil.copytree(os.path.join(SAMPLES, PROJECT, TS0), os.path.join(self.root, PROJECT, TS0))
        self.srv = None

    def start(self, writer_extra=(), reader_extra=(), autoconnect=False, reader_root=None, mcp=None):
        py = sys.executable
        if mcp is None:
            mcp = {'writer': ([py, FAKE, 'writer', '--state', self.tia] + list(writer_extra), None),
                   'reader': ([py, FAKE, 'reader', '--state', self.tia, '--export-root', reader_root or self.root,
                               '--project', PROJECT] + list(reader_extra), None)}
        self.srv = server.create_server(0, self.root, self.data, mcp=mcp, mcp_timeout=30, autoconnect=autoconnect)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
        for d in (self.root, self.data, self.tia, self.reader_root):
            shutil.rmtree(d, ignore_errors=True)

    def req(self, method, fpath, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=60)
        c.request(method, fpath, body=json.dumps(body).encode() if body is not None else None,
                  headers={'Content-Type': 'application/json'})
        r = c.getresponse()
        t = json.loads(r.read())
        c.close()
        return r.status, t

    def calls(self, tool=None):
        f = os.path.join(self.tia, 'calls.jsonl')
        if not os.path.exists(f):
            return []
        with open(f, encoding='utf-8') as x:
            res = [json.loads(r) for r in x]
        return [c for c in res if tool is None or c['tool'] == tool]

    def wait(self, job):
        for _ in range(300):
            s, j = self.req('GET', '/api/jobs/%s' % job['id'])
            self.assertEqual(s, 200)
            if j['state'] != 'running':
                return j
            time.sleep(0.1)
        self.fail('job did not end')

    # ---------- registry ----------
    def test_registry_from_default_root_and_crud(self):
        self.start()
        s, r = self.req('GET', '/api/projects')
        self.assertEqual(s, 200)
        p = r['projects'][0]
        self.assertEqual((p['name'], p['source'], p['export_root'], p['last_snapshot']), (PROJECT, 'exports', self.root, TS0))
        self.assertTrue(os.path.exists(os.path.join(self.data, 'projects.json')))
        # another export folder on disk, given as the project folder
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, True)
        shutil.copytree(os.path.join(SAMPLES, PROJECT, TS0), os.path.join(other, 'Line_B', TS0))
        s, r = self.req('POST', '/api/projects', {'source': 'exports', 'path': os.path.join(other, 'Line_B')})
        self.assertEqual(s, 201, r)
        self.assertEqual(r['projects'][0]['export_root'], other)
        s, b = self.req('GET', '/api/block?project=Line_B&block=FB_Conveyor')     # read from its own export root
        self.assertEqual(s, 200, b)
        self.assertIn('Line_B', [p['project'] for p in self.req('GET', '/api/exports')[1]['projects']])
        self.assertEqual(self.req('POST', '/api/projects', {'source': 'exports', 'path': os.path.join(other, 'nothing')})[0], 400)
        self.assertEqual(self.req('DELETE', '/api/projects/Line_B')[0], 200)
        self.assertEqual(self.req('DELETE', '/api/projects/Line_B')[0], 404)
        self.assertEqual([p['name'] for p in self.req('GET', '/api/projects')[1]['projects']], [PROJECT])

    def test_cross_origin_post_refused(self):
        self.start()
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=10)
        c.request('POST', '/api/projects', body=b'{"source": "tia"}', headers={'Origin': 'http://example.com'})
        self.assertEqual(c.getresponse().status, 403)
        c.close()
        self.assertEqual(self.calls(), [])

    def test_home_page_has_status_bar(self):
        self.start()
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=10)
        c.request('GET', '/')
        html = c.getresponse().read()
        c.close()
        self.assertIn(b'id="status"', html)
        self.assertIn(b'showHome', html)
        self.assertIn(b'Connect an AI agent', html)

    def test_edit_counts(self):
        self.start()
        _, net = self.req('GET', '/api/network?project=%s&block=FB_Conveyor&nw=1' % PROJECT)
        s, u = self.req('POST', '/api/edits', {'project': PROJECT, 'block': 'FB_Conveyor', 'nw': 1,
                                               'after': {'elements': net['elements'], 'rungs': net['rungs']}})
        self.assertEqual(s, 201, u)
        self.assertEqual(self.req('GET', '/api/projects')[1]['projects'][0]['edits'], {'Proposal': 1})

    # ---------- TIA status ----------
    def test_status_not_running_does_not_connect(self):
        self.start(['--no-portal'], ['--no-portal'])
        s, st = self.req('POST', '/api/tia/status', {})
        self.assertEqual(s, 200)
        self.assertEqual((st['tia'], st['writer'], st['reader'], st['ai']), ('not running', 'configured', 'configured', 'no client'))
        self.assertEqual(self.calls('Connect'), [])
        self.assertEqual([c['tool'] for c in self.calls()], ['GetPortals'])

    def test_status_one_instance_attached(self):
        self.start()
        s, st = self.req('POST', '/api/tia/status', {})
        self.assertEqual((st['tia'], st['project'], st['portalId']), ('attached', PROJECT, 4711))
        self.assertEqual(self.calls('Connect')[0]['arguments'], {'portalId': 4711})
        self.assertEqual(st['reader_export_root'], self.root)
        self.assertEqual(self.req('GET', '/api/tia/status')[1]['tia'], 'attached')      # cached

    def test_status_multi_instances_choose(self):
        self.start(['--multi-instances'])
        st = self.req('POST', '/api/tia/status', {})[1]
        self.assertEqual(st['tia'], 'choose')
        self.assertEqual([x['portalId'] for x in st['instances']], [4711, 4712])
        self.assertEqual(self.calls('Connect'), [])
        st = self.req('POST', '/api/tia/status', {'portalId': 4712})[1]
        self.assertEqual((st['tia'], st['portalId'], st['project']), ('attached', 4712, PROJECT))

    def test_writer_cannot_start_is_error(self):
        self.start(mcp={'writer': ([os.path.join(self.tia, 'not.exe')], None)})
        s, st = self.req('POST', '/api/tia/status', {})
        self.assertEqual(s, 200)
        self.assertTrue(st['writer'].startswith('error: '), st)
        self.assertEqual((st['tia'], st['reader']), ('unknown', 'missing'))
        s, r = self.req('POST', '/api/projects', {'source': 'tia'})
        self.assertEqual(s, 202)
        j = self.wait(r['job'])
        self.assertEqual(j['state'], 'error')
        self.assertIn('cannot be started', j['error'])

    def test_autoconnect(self):
        self.start(autoconnect=True)
        for _ in range(100):
            st = self.req('GET', '/api/tia/status')[1]
            if st.get('checked'):
                break
            time.sleep(0.1)
        self.assertEqual((st['tia'], st['autoconnect']), ('attached', True))

    def test_no_autoconnect(self):
        self.start(autoconnect=False)
        time.sleep(0.5)
        st = self.req('GET', '/api/tia/status')[1]
        self.assertEqual((st['tia'], st['checked'], st['autoconnect']), ('unknown', False, False))
        self.assertEqual(self.calls(), [])

    # ---------- snapshots ----------
    def test_snapshot_whole_plc(self):
        self.start()
        self.req('POST', '/api/tia/status', {})
        s, r = self.req('POST', '/api/projects/%s/snapshot' % PROJECT, {})
        self.assertEqual(s, 202, r)
        j = self.wait(r['job'])
        self.assertEqual(j['state'], 'done', j)
        self.assertEqual(j['chunks'], 2)
        self.assertGreater(j['exported'], 0)
        ts = j['result']['export_ts']
        self.assertNotEqual(ts, TS0)
        self.assertTrue(os.path.isdir(os.path.join(self.root, PROJECT, ts, 'PLC_1', 'Blocks')))
        ex = [c for c in self.calls('export_to_folder')]
        self.assertGreater(ex[1]['arguments']['offset'], 0)                  # nextOffset of chunk 1
        self.assertEqual(ex[1]['arguments']['runId'], ts)
        self.assertNotIn('pathPrefixes', ex[0]['arguments'])
        p = self.req('GET', '/api/projects')[1]['projects'][0]
        self.assertEqual((p['last_snapshot'], p['tia']), (ts, {'softwarePath': 'PLC_1', 'plcName': 'PLC_1'}))
        self.assertEqual(self.req('GET', '/api/block?project=%s&block=FB_Conveyor' % PROJECT)[1]['ts'], ts)

    def test_connect_to_open_tia_project_new_root(self):
        """A new project from TIA lands where the Reader exports (its own --export-root)."""
        self.start(reader_root=self.reader_root)
        self.req('DELETE', '/api/projects/%s' % PROJECT)
        s, r = self.req('POST', '/api/projects', {'source': 'tia'})
        j = self.wait(r['job'])
        self.assertEqual(j['state'], 'done', j)
        p = self.req('GET', '/api/projects')[1]['projects'][0]
        self.assertEqual((p['name'], p['source'], p['export_root']), (PROJECT, 'tia', self.reader_root))

    def test_snapshot_reader_root_mismatch(self):
        self.start(reader_root=self.reader_root)
        j = self.wait(self.req('POST', '/api/projects/%s/snapshot' % PROJECT, {})[1]['job'])
        self.assertEqual(j['state'], 'error')
        self.assertIn('--export-root', j['error'])

    def test_snapshot_tia_not_running(self):
        self.start(['--no-portal'])
        j = self.wait(self.req('POST', '/api/projects', {'source': 'tia'})[1]['job'])
        self.assertEqual(j['state'], 'error')
        self.assertIn('not running', j['error'])
        self.assertEqual(self.calls('Connect'), [])

    def test_ap21_open_snapshot_close(self):
        self.start()
        self.req('DELETE', '/api/projects/%s' % PROJECT)
        self.assertEqual(self.req('POST', '/api/projects', {'source': 'ap21', 'path': 'C:\\TIA\\x.txt'})[0], 400)
        path = 'C:\\TIA\\Demo_Line\\Demo_Line.ap21'
        s, r = self.req('POST', '/api/projects', {'source': 'ap21', 'path': path, 'close': True})
        self.assertEqual(s, 202, r)
        j = self.wait(r['job'])
        self.assertEqual(j['state'], 'done', j)
        tools = [c['tool'] for c in self.calls() if c['role'] == 'writer']
        self.assertEqual(tools[0], 'OpenTiaProject')
        self.assertEqual(tools[-1], 'CloseProject')
        self.assertEqual(self.calls('OpenTiaProject')[0]['arguments']['path'], path)
        self.assertEqual(self.calls('export_to_folder')[0]['arguments']['projectPath'], path)
        p = self.req('GET', '/api/projects')[1]['projects'][0]
        self.assertEqual((p['source'], p['project_path']), ('ap21', path))


if __name__ == '__main__':
    unittest.main()
