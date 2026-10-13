import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from urllib.parse import quote

from tests.common import VZORKY, PROJEKT
import server
import screenshot


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = server.vytvor_server(0, VZORKY, '')          # volný port
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def get(self, cesta):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        c.request('GET', cesta)
        r = c.getresponse()
        telo = r.read()
        c.close()
        return r.status, r.getheader('Content-Type'), telo

    def test_exporty(self):
        s, _, b = self.get('/api/exporty')
        self.assertEqual(s, 200)
        p = json.loads(b)['projekty'][0]
        self.assertEqual(p['projekt'], PROJEKT)
        self.assertIn({'blok': 'FB_Dopravnik', 'ts': '20261012-140000'}, p['bloky'])
        self.assertIn({'blok': 'FB_Pas', 'ts': '20261012-120000'}, p['bloky'])     # dílčí export

    def test_blok(self):
        s, _, b = self.get('/api/blok?projekt=%s&blok=FB_Dopravnik' % PROJEKT)
        j = json.loads(b)
        self.assertEqual((s, j['druh'], j['cislo'], len(j['networky'])), (200, 'FB', 10, 8))
        self.assertEqual(j['networky'][1], {'nw': 2, 'jazyk': 'LAD', 'titulek': 'Samodrž motoru'})

    def test_network(self):
        s, _, b = self.get('/api/network?projekt=%s&blok=FB_Dopravnik&nw=4' % PROJEKT)
        j = json.loads(b)
        self.assertEqual(s, 200)
        for k in ('blok', 'cislo', 'nw', 'titulek', 'prvky', 'vodice', 'rozlozeni', 'kresba'):
            self.assertIn(k, j)
        self.assertEqual(j['rozlozeni']['p2'], {'r': 2, 'c': 2})
        self.assertEqual(j['varovani'], [])

    def test_network_fallbacky(self):
        _, _, b = self.get('/api/network?projekt=%s&blok=FC_Vetve&nw=4' % PROJEKT)
        j = json.loads(b)
        self.assertIsNone(j['kresba'])
        self.assertIn('seznam rungů', j['varovani'][0])
        _, _, b = self.get('/api/network?projekt=%s&blok=FC_Vetve&nw=3' % PROJEKT)
        j = json.loads(b)                                   # od F1 se FBD kreslí (vlastní model)
        self.assertEqual((j['jazyk'], j['kresba']['fbd'], j['varovani']), ('FBD', True, []))

    def test_chyby(self):
        self.assertEqual(self.get('/api/blok?projekt=%s&blok=Neni' % PROJEKT)[0], 404)
        self.assertEqual(self.get('/api/blok?projekt=%s&blok=%s' % (PROJEKT, quote('../../server')))[0], 404)
        self.assertEqual(self.get('/api/blok?projekt=%s&blok=x' % quote('..'))[0], 404)
        self.assertEqual(self.get('/api/network?projekt=%s&blok=FB_Dopravnik&nw=99' % PROJEKT)[0], 404)
        self.assertEqual(self.get('/api/network?projekt=%s&blok=FB_Dopravnik&nw=x' % PROJEKT)[0], 400)
        self.assertEqual(self.get('/nic')[0], 404)

    def test_stranka(self):
        s, t, b = self.get('/?projekt=%s&blok=FB_Dopravnik&nw=2&snimek=1' % PROJEKT)
        self.assertEqual(s, 200)
        self.assertIn('text/html', t)
        self.assertIn(b'TIAC_Editor', b)

    @unittest.skipUnless(any(os.path.exists(p) for p in screenshot.PROHLIZECE), 'chybí Chrome/Edge')
    def test_snimek(self):
        d = tempfile.mkdtemp()
        try:
            out = screenshot.screenshot('http://127.0.0.1:%d/?projekt=%s&blok=FB_Dopravnik&nw=2&snimek=1' % (self.port, PROJEKT),
                                os.path.join(d, 's.png'), 900, 400)
            with open(out, 'rb') as f:
                self.assertEqual(f.read(8), b'\x89PNG\r\n\x1a\n')
            self.assertGreater(os.path.getsize(out), 5000)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
