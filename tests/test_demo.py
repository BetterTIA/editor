"""E1: a demo instance (fake Writer/Reader) refuses real projects: adding one outside the export root, Upload, Check."""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

from tests.common import ROOT, SAMPLES, PROJECT, TS0
import server

FAKE = os.path.join(ROOT, 'tests', 'fake_mcp.py')


class TestDemo(unittest.TestCase):
    def setUp(self):
        self.root, self.data, self.tia, self.other = (tempfile.mkdtemp() for _ in range(4))
        shutil.copytree(os.path.join(SAMPLES, PROJECT, TS0), os.path.join(self.root, PROJECT, TS0))
        shutil.copytree(os.path.join(SAMPLES, PROJECT, TS0), os.path.join(self.other, 'Real_Line', TS0))
        py = sys.executable
        self.mcp = {'writer': ([py, FAKE, 'writer', '--state', self.tia], None),
                    'reader': ([py, FAKE, 'reader', '--state', self.tia, '--export-root', self.root, '--project', PROJECT], None)}
        self.srv = server.create_server(0, self.root, self.data, mcp=self.mcp, mcp_timeout=30, demo=True)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        for d in (self.root, self.data, self.tia, self.other):
            shutil.rmtree(d, ignore_errors=True)

    def req(self, method, fpath, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=60)
        c.request(method, fpath, body=json.dumps(body).encode() if body is not None else None,
                  headers={'Content-Type': 'application/json'})
        r = c.getresponse()
        t = json.loads(r.read())
        c.close()
        return r.status, t

    def test_detected_from_the_commands(self):
        self.assertTrue(server.is_fake(self.mcp))
        self.assertFalse(server.is_fake({'writer': (['TIAC_Writer.exe'], None), 'reader': None}))
        self.assertFalse(server.is_fake(None))

    def test_status_says_demo(self):
        s, st = self.req('GET', '/api/tia/status')
        self.assertEqual((s, st['demo']), (200, True))

    def test_real_project_refused_unless_forced(self):
        path = os.path.join(self.other, 'Real_Line')
        s, r = self.req('POST', '/api/projects', {'source': 'exports', 'path': path})
        self.assertEqual(s, 409)
        self.assertIn('demo instance', r['error'])
        s, r = self.req('POST', '/api/projects', {'source': 'exports', 'path': os.path.join(self.root, PROJECT)})
        self.assertEqual(s, 201, r)                       # inside the demo export root: fine
        s, r = self.req('POST', '/api/projects', {'source': 'exports', 'path': path, 'force': True})
        self.assertEqual((s, r['projects'][0]['export_root']), (201, self.other))   # a project folder -> its parent

        for project in ('Real_Line', PROJECT):
            _, net = self.req('GET', '/api/network?project=%s&block=FB_Conveyor&nw=1' % project)
            s, u = self.req('POST', '/api/edits', {'project': project, 'round': 'R', 'block': 'FB_Conveyor', 'nw': 1,
                                                    'after': {'elements': net['elements'], 'rungs': net['rungs']}})
            self.assertEqual(s, 201, u)
            self.req('POST', '/api/edits/%s/state' % u['id'], {'state': 'Approved'})
            s, r = self.req('POST', '/api/edits/%s/upload' % u['id'], {})
            s2, r2 = self.req('POST', '/api/edits/%s/check' % u['id'], {})
            if project == 'Real_Line':
                self.assertEqual((s, s2), (409, 409))
                self.assertIn('demo instance', r['error'] + r2['error'])
            else:                                         # demo project: the usual rules (no TIA settings -> 409 with that reason)
                self.assertEqual(s, 409)
                self.assertIn('TIA settings', r['error'])


if __name__ == '__main__':
    unittest.main()
