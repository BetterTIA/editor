import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from tests.spolecne import VZORKY, PROJEKT, po_nw2
import rozdil
import s7dcl
import server
import stav


class TestAutomat(unittest.TestCase):
    def u(self, s='Proposal', kontroly=()):
        return {'id': 'U001', 'stav': s, 'historie': [], 'kontroly': list(kontroly)}

    def test_krok_vpred_a_zpet(self):
        u = self.u()
        stav.zmen_stav(u, 'Approved', 'Honza')
        self.assertEqual(u['stav'], 'Approved')
        self.assertEqual(u['historie'][-1]['stav'], 'Approved')
        self.assertEqual(u['historie'][-1]['kdo'], 'Honza')
        stav.zmen_stav(u, 'Proposal', 'Honza')
        self.assertEqual(u['stav'], 'Proposal')

    def test_preskok_nejde(self):
        u = self.u()
        with self.assertRaises(stav.ChybaStavu) as e:
            stav.zmen_stav(u, 'Signed', 'Honza')
        self.assertEqual(e.exception.kod, 409)
        with self.assertRaises(stav.ChybaStavu):
            stav.zmen_stav(self.u('Tested'), 'Approved', 'Honza')
        with self.assertRaises(stav.ChybaStavu) as e:
            stav.zmen_stav(u, 'Hotovo', 'Honza')
        self.assertEqual(e.exception.kod, 400)

    def test_signed_jen_se_shodou(self):
        u = self.u('Approved')
        with self.assertRaises(stav.ChybaStavu) as e:
            stav.zmen_stav(u, 'Signed', 'Honza')
        self.assertEqual(e.exception.kod, 409)
        u['kontroly'].append({'shoda': False})
        with self.assertRaises(stav.ChybaStavu):
            stav.zmen_stav(u, 'Signed', 'Honza')
        u['kontroly'].append({'shoda': True})
        stav.zmen_stav(u, 'Signed', 'Honza')
        self.assertEqual(u['stav'], 'Signed')
        self.assertEqual(stav.prechody(u), ('Uploaded', 'Approved'))

    def test_atomicky_zapis(self):
        d = tempfile.mkdtemp()
        try:
            stav.uloz(d, PROJEKT, {'upravy': [{'id': 'U001', 'stav': 'Proposal'}]})
            c = os.path.join(d, PROJEKT, 'stav.json')
            puvodni = s7dcl.cti(c)
            with mock.patch('stav.json.dump', side_effect=OSError('disk plný')):
                with self.assertRaises(OSError):
                    stav.uloz(d, PROJEKT, {'upravy': []})
            self.assertEqual(s7dcl.cti(c), puvodni)                          # starý soubor nedotčen
            self.assertEqual(os.listdir(os.path.dirname(c)), ['stav.json'])   # žádný .tmp
            with self.assertRaises(stav.ChybaStavu):
                stav.uloz(d, PROJEKT, {'upravy': [{'id': 'U001'}]})           # bez stavu
            with self.assertRaises(stav.ChybaStavu):
                stav.uloz(d, '..', {'upravy': []})
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_rozdil_a_prizrak(self):
        _, b = server._nacti(VZORKY, PROJEKT, 'FB_Dopravnik')
        n = b['networky'][1]
        pred = {'prvky': n['prvky'], 'rungy': n['rungy']}
        po = po_nw2(pred)
        zmeny, smazane = rozdil.porovnej(pred, po)
        self.assertEqual(zmeny, {'p3': 'zmeneny', 'n1': 'pridany'})
        self.assertEqual(smazane, ['p7'])
        prvky, rungy, nevlozene = rozdil.slouc(pred, po, smazane)
        self.assertEqual(nevlozene, [])
        self.assertEqual(rungy[2]['cesta'], ['p6', 'x:p7'])                   # přízrak na původním místě


class TestApiUpravy(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.srv = server.vytvor_server(0, VZORKY, self.data)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.data, ignore_errors=True)

    def req(self, metoda, cesta, telo=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        b = json.dumps(telo).encode('utf-8') if telo is not None else None
        c.request(metoda, cesta, body=b, headers={'Content-Type': 'application/json'} if b else {})
        r = c.getresponse()
        t = r.read()
        c.close()
        return r.status, (json.loads(t) if r.getheader('Content-Type', '').startswith('application/json') else t)

    def zaloz(self, **navic):
        _, net = self.req('GET', '/api/network?projekt=%s&blok=FB_Dopravnik&nw=2' % PROJEKT)
        telo = {'projekt': PROJEKT, 'kolo': 'Kolo 1', 'blok': 'FB_Dopravnik', 'nw': 2, 'po': po_nw2(net),
                'poznamka': 'Pojistka před cívkou'}
        telo.update(navic)
        return self.req('POST', '/api/uprava', telo)

    def test_zalozeni_doplni_z_exportu(self):
        s, u = self.zaloz(titulek='ručně', cislo_bloku=999)
        self.assertEqual(s, 201)
        self.assertEqual((u['id'], u['stav'], u['autor']), ('U001', 'Proposal', 'ANet'))
        self.assertEqual((u['druh'], u['cislo_bloku'], u['nw'], u['titulek']), ('FB', 10, 2, 'Samodrž motoru'))
        self.assertEqual(u['pred']['export_ts'], '20261012-120000')
        self.assertEqual(len(u['pred']['prvky']), 7)
        self.assertIsNotNone(u['po']['rozlozeni'])                         # přepočteno serverem
        self.assertIn({'z': 'n1', 'do': 'p4'}, u['po']['vodice'])
        self.assertTrue(os.path.exists(os.path.join(self.data, PROJEKT, 'stav.json')))
        self.assertEqual(self.zaloz()[1]['id'], 'U002')

    def test_stavy_pres_api(self):
        self.zaloz()
        s, u = self.req('POST', '/api/uprava/U001/stav', {'stav': 'Approved', 'kdo': 'Honza'})
        self.assertEqual((s, u['stav']), (200, 'Approved'))
        s, e = self.req('POST', '/api/uprava/U001/stav', {'stav': 'Signed', 'kdo': 'Honza'})
        self.assertEqual(s, 409)
        self.assertIn('kontrole', e['chyba'])
        s, u = self.req('POST', '/api/kontrola', {'uprava_id': 'U001', 'shoda': True, 'export_ts': '20261012-130000',
                                                   'poznamka': 'export po zápisu = návrh'})
        self.assertEqual((s, len(u['kontroly'])), (200, 1))
        self.assertEqual(self.req('POST', '/api/uprava/U001/stav', {'stav': 'Signed'})[0], 200)
        self.assertEqual(self.req('POST', '/api/uprava/U001/stav', {'stav': 'Tested'})[0], 409)
        self.assertEqual(self.req('POST', '/api/kontrola', {'uprava_id': 'U001', 'shoda': 'ano'})[0], 400)
        self.assertEqual(self.req('POST', '/api/uprava/U999/stav', {'stav': 'Approved'})[0], 404)
        _, st = self.req('GET', '/api/stav?projekt=%s' % PROJEKT)
        self.assertEqual([h['stav'] for h in st['upravy'][0]['historie']], ['Proposal', 'Approved', 'Signed'])

    def test_detail_pro_ui(self):
        self.zaloz()
        s, d = self.req('GET', '/api/uprava/U001?sirka=900')
        self.assertEqual(s, 200)
        self.assertEqual(d['rozdil'], {'p3': 'zmeneny', 'n1': 'pridany', 'x:p7': 'smazany'})
        self.assertIsNotNone(d['kresba'])
        self.assertIn('x:p7', d['rozlozeni'])
        self.assertEqual((d['dalsi'], d['predchozi'], d['signed_povolen']), ('Approved', None, False))
        s, html = self.req('GET', '/?uprava=U001&snimek=1')
        self.assertEqual(s, 200)
        self.assertIn(b'zobrazUpravu', html)

    def test_stav_get_put(self):
        self.zaloz()
        s, vse = self.req('GET', '/api/stav')
        self.assertEqual(vse['projekty'][0]['projekt'], PROJEKT)
        _, st = self.req('GET', '/api/stav?projekt=%s' % PROJEKT)
        st['upravy'][0]['poznamka'] = 'upraveno PUT'
        self.assertEqual(self.req('PUT', '/api/stav?projekt=%s' % PROJEKT, st)[0], 200)
        self.assertEqual(self.req('GET', '/api/stav?projekt=%s' % PROJEKT)[1]['upravy'][0]['poznamka'], 'upraveno PUT')
        self.assertEqual(self.req('PUT', '/api/stav?projekt=%s' % PROJEKT, {'x': 1})[0], 400)
        self.assertEqual(self.req('PUT', '/api/stav?projekt=Neni', st)[0], 404)

    def test_neplatny_vstup(self):
        self.assertEqual(self.zaloz(po={'prvky': 'x'})[0], 400)
        self.assertEqual(self.zaloz(po={'prvky': [], 'rungy': [{'z': 'powerrail', 'cesta': ['p1'], 'do': None}]})[0], 400)
        self.assertEqual(self.zaloz(nw=99)[0], 404)
        self.assertEqual(self.req('POST', '/api/uprava', [1, 2])[0], 400)


class TestBezDat(unittest.TestCase):
    def test_bez_data_503(self):
        srv = server.vytvor_server(0, VZORKY, '')
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=10)
            c.request('GET', '/api/stav?projekt=%s' % PROJEKT)
            self.assertEqual(c.getresponse().status, 503)
            c.close()
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == '__main__':
    unittest.main()
