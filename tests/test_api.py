import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from urllib.parse import quote

from tests.common import SAMPLES, PROJECT
import server
import screenshot


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = server.create_server(0, SAMPLES, '')          # free port
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def get(self, fpath):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        c.request('GET', fpath)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, r.getheader('Content-Type'), body

    def test_exports(self):
        s, _, b = self.get('/api/exports')
        self.assertEqual(s, 200)
        p = json.loads(b)['projects'][0]
        self.assertEqual(p['project'], PROJECT)
        self.assertIn({'block': 'FB_Conveyor', 'ts': '20261012-140000'}, p['blocks'])
        self.assertIn({'block': 'FB_Belt', 'ts': '20261012-120000'}, p['blocks'])     # partial export

    def test_block(self):
        s, _, b = self.get('/api/block?project=%s&block=FB_Conveyor' % PROJECT)
        j = json.loads(b)
        self.assertEqual((s, j['kind'], j['number'], len(j['networks'])), (200, 'FB', 10, 8))
        self.assertEqual(j['networks'][1], {'nw': 2, 'language': 'LAD', 'title': 'Motor latch'})

    def test_network(self):
        s, _, b = self.get('/api/network?project=%s&block=FB_Conveyor&nw=4' % PROJECT)
        j = json.loads(b)
        self.assertEqual(s, 200)
        for k in ('block', 'number', 'nw', 'title', 'elements', 'wires', 'layout', 'drawing'):
            self.assertIn(k, j)
        self.assertEqual(j['layout']['p2'], {'r': 2, 'c': 2})
        self.assertEqual(j['warnings'], [])

    def test_network_fallbacks(self):
        _, _, b = self.get('/api/network?project=%s&block=FC_Branches&nw=4' % PROJECT)
        j = json.loads(b)
        self.assertIsNone(j['drawing'])
        self.assertIn('list of rungs', j['warnings'][0])
        _, _, b = self.get('/api/network?project=%s&block=FC_Branches&nw=3' % PROJECT)
        j = json.loads(b)                                   # since F1 FBD is drawn (own model)
        self.assertEqual((j['language'], j['drawing']['fbd'], j['warnings']), ('FBD', True, []))

    def test_errors(self):
        self.assertEqual(self.get('/api/block?project=%s&block=Missing' % PROJECT)[0], 404)
        self.assertEqual(self.get('/api/block?project=%s&block=%s' % (PROJECT, quote('../../server')))[0], 404)
        self.assertEqual(self.get('/api/block?project=%s&block=x' % quote('..'))[0], 404)
        self.assertEqual(self.get('/api/network?project=%s&block=FB_Conveyor&nw=99' % PROJECT)[0], 404)
        self.assertEqual(self.get('/api/network?project=%s&block=FB_Conveyor&nw=x' % PROJECT)[0], 400)
        self.assertEqual(self.get('/nothing')[0], 404)

    def test_page(self):
        s, t, b = self.get('/?project=%s&block=FB_Conveyor&nw=2&screenshot=1' % PROJECT)
        self.assertEqual(s, 200)
        self.assertIn('text/html', t)
        self.assertIn(b'BetterTIA Editor', b)

    @unittest.skipUnless(any(os.path.exists(p) for p in screenshot.BROWSERS), 'Chrome/Edge missing')
    def test_screenshot(self):
        d = tempfile.mkdtemp()
        try:
            out = screenshot.screenshot('http://127.0.0.1:%d/?project=%s&block=FB_Conveyor&nw=2&screenshot=1' % (self.port, PROJECT),
                                os.path.join(d, 's.png'), 900, 400)
            with open(out, 'rb') as f:
                self.assertEqual(f.read(8), b'\x89PNG\r\n\x1a\n')
            self.assertGreater(os.path.getsize(out), 5000)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
