"""H: WinCC Unified HMI screens — the Reader dump parsed into screen models, the API, the snapshot with the HMI dump."""
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
import hmi
import server

FAKE = os.path.join(ROOT, 'tests', 'fake_mcp.py')


class TestHmiModel(unittest.TestCase):
    def test_sample_screens(self):
        r = hmi.api_screens(SAMPLES, PROJECT)
        self.assertEqual((r['export_ts'], r['hmi']), (TS0, 'HMI_1'))
        self.assertEqual([s['name'] for s in r['screens']], ['Alarms', 'Manual', 'Overview'])
        self.assertIn({'name': 'Machine_Speed', 'type': 'Real', 'plc_tag': '"DB_Machine".Speed', 'connection': 'HMI_Connection_1',
                       'table': 'Default tag table'}, r['tags'])
        s = hmi.api_screen(SAMPLES, PROJECT, 'Overview')
        x = {i['name']: i for i in s['items']}
        self.assertEqual((s['width'], s['height'], s['background']), (1280, 800, '#eef1f4'))
        self.assertEqual(x['IO_Speed']['type'], 'IOField')
        self.assertEqual(x['IO_Speed']['bindings'], [{'property': 'ProcessValue', 'tag': 'Machine_Speed'}])
        self.assertEqual(x['Label_Speed']['text'], 'Belt speed')                   # en-US, rich-text markup dropped
        self.assertEqual(x['Title']['colors']['backColor'], '#00000000')           # transparent keeps its alpha
        self.assertEqual(x['Btn_Alarms']['events'][0]['event'], 'Tapped')
        self.assertEqual(x['Clock']['type'], 'Clock')                              # unknown class: a generic box
        self.assertEqual(hmi.api_screen(SAMPLES, PROJECT, 'Manual')['items'][3]['type'], 'Switch')   # HmiToggleSwitch
        with self.assertRaises(KeyError):
            hmi.api_screen(SAMPLES, PROJECT, 'Nope')

    def test_tolerant_item(self):
        self.assertEqual(hmi.item({'$type': 'HmiSomethingNew'}),
                         {'name': '', 'type': 'SomethingNew', 'left': 0, 'top': 0, 'width': 0, 'height': 0, 'bindings': []})
        self.assertEqual(hmi.screen({})['width'], 1280)
        self.assertEqual((hmi.color('#80112233'), hmi.color('red'), hmi.tia_color('#112233')), ('#11223380', None, '#FF112233'))


class TestHmiApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = server.create_server(0, SAMPLES, '')
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def get(self, path):
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=10)
        c.request('GET', path)
        r = c.getresponse()
        t = json.loads(r.read())
        c.close()
        return r.status, t

    def test_screens_and_screen(self):
        s, r = self.get('/api/hmi/screens?project=%s' % PROJECT)
        self.assertEqual((s, len(r['screens'])), (200, 3))
        s, r = self.get('/api/hmi/screen?project=%s&screen=Alarms&export_ts=%s' % (PROJECT, TS0))
        self.assertEqual((s, r['hmi'], r['items'][2]['type']), (200, 'HMI_1', 'AlarmControl'))
        self.assertEqual(self.get('/api/hmi/screen?project=%s&screen=Nope' % PROJECT)[0], 404)
        self.assertEqual(self.get('/api/hmi/screens?project=%s&export_ts=20200101-000000' % PROJECT)[0], 404)
        self.assertEqual(self.get('/api/hmi/screens?project=%s' % quote('..\\x'))[0], 400)


class TestHmiSnapshot(unittest.TestCase):
    def setUp(self):
        self.root, self.data, self.tia = (tempfile.mkdtemp() for _ in range(3))
        shutil.copytree(os.path.join(SAMPLES, PROJECT, TS0, 'PLC_1'), os.path.join(self.root, PROJECT, TS0, 'PLC_1'))
        self.srv = None

    def tearDown(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
        for d in (self.root, self.data, self.tia):
            shutil.rmtree(d, ignore_errors=True)

    def snapshot(self, *reader_extra):
        py = sys.executable
        mcp = {'writer': ([py, FAKE, 'writer', '--state', self.tia], None),
               'reader': ([py, FAKE, 'reader', '--state', self.tia, '--export-root', self.root, '--project', PROJECT]
                          + list(reader_extra), None)}
        self.srv = server.create_server(0, self.root, self.data, mcp=mcp, mcp_timeout=30)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=60)
        c.request('POST', '/api/projects/%s/snapshot' % PROJECT, b'{}', {'Content-Type': 'application/json'})
        job = json.loads(c.getresponse().read())['job']
        for _ in range(300):
            j = self.srv.RequestHandlerClass.jobs[job['id']]
            if j['state'] != 'running':
                return j
            time.sleep(0.1)
        self.fail('job did not end')

    def test_snapshot_exports_the_hmi_too(self):
        self.assertRaises(KeyError, hmi.find, self.root, PROJECT)
        j = self.snapshot()
        self.assertEqual(j['state'], 'done', j)
        ts = j['result']['export_ts']
        self.assertEqual(j['hmi_chunks'], 2)                                      # 6 files, 3 per chunk
        self.assertNotIn('warnings', j)
        self.assertTrue(os.path.exists(os.path.join(self.root, PROJECT, ts, 'manifest-hmi.jsonl')))
        self.assertEqual(hmi.api_screens(self.root, PROJECT)['export_ts'], ts)

    def test_hmi_failure_is_a_warning(self):
        j = self.snapshot('--no-hmi')
        self.assertEqual(j['state'], 'done', j)
        self.assertIn('No WinCC Unified HMI', j['warnings'][0])


if __name__ == '__main__':
    unittest.main()
