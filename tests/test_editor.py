"""M3: katalog, operace modelu, pravidla LAD, API editoru a uložení „po"."""
import http.client
import json
import shutil
import tempfile
import threading
import unittest

from tests.spolecne import VZORKY, PROJEKT, TS0
import katalog
import layout
import model
import server


def nw(n):
    _, b = server._nacti(VZORKY, PROJEKT, 'FB_Dopravnik', TS0)
    x = b['networky'][n - 1]
    return {'prvky': x['prvky'], 'rungy': x['rungy']}


KAT = katalog.katalog(VZORKY, PROJEKT)


class TestKatalog(unittest.TestCase):
    def test_fb_pas_piny_a_instance(self):
        b = next(x for x in KAT['bloky'] if x['klic'] == 'fb:FB_Pas')
        self.assertEqual((b['druh'], b['cislo'], b['instance']), ('FB', 11, ['"DB_Pas1"']))
        p = b['prvek']
        self.assertEqual([x['pin'] for x in p['vstupy']], ['Povoleni', 'Smer', 'Rychlost', 'Reset'])
        self.assertEqual([x['pin'] for x in p['vystupy']], ['Bezi', 'Porucha', 'Stav'])
        self.assertEqual((p['volani'], p['nazev'], p['instance'], p['napajeni']), ('fb', 'FB_Pas', '"DB_Pas1"', ['EN', 'ENO']))

    def test_fc_a_rozhrani(self):
        fc = next(x for x in KAT['bloky'] if x['klic'] == 'fc:FC_Vetve')
        self.assertIsNone(fc['prvek']['instance'])
        dop = next(x for x in KAT['bloky'] if x['nazev'] == 'FB_Dopravnik')
        self.assertIn({'jmeno': 'T_Rozbeh', 'typ': 'TON_TIME'}, dop['rozhrani']['staticke'])   # atribut {…} přeskočen

    def test_db_a_tagy(self):
        db = {d['nazev']: d for d in KAT['db']}
        self.assertEqual(db['DB_Pas1']['instance_typ'], 'FB_Pas')
        cleny = {c['jmeno']: c['typ'] for c in db['DB_Stroj']['cleny']}
        self.assertEqual((cleny['Rychlost'], cleny['Pocitadlo']), ('Real', 'Struct'))
        self.assertNotIn('Kusy', cleny)                                    # vnořené členy STRUCT ne
        self.assertIn({'jmeno': 'Nouzovy_Stop', 'typ': 'Bool', 'adresa': '%I0.1', 'tabulka': 'Default tag table'}, KAT['tagy'])

    def test_instrukce(self):
        klice = {x['klic'] for x in KAT['instrukce']}
        for k in ('NO', 'NC', 'P', 'N', 'C', 'S', 'R', 'NEG', 'EQ', 'LE', 'TON', 'TOF', 'TP', 'CTU', 'CTD', 'MOVE',
                  'ADD', 'SUB', 'MUL', 'DIV', 'IN_RANGE', 'CONV', 'SWAP', 'P_TRIG', 'N_TRIG', 'RET'):
            self.assertIn(k, klice)
        ton = katalog.prvek(KAT, 'TON')
        self.assertEqual((ton['napajeni'], ton['vstupy'][0]['pin'], ton['sablona']), (['IN', 'Q'], 'PT', 'Time'))
        with self.assertRaises(KeyError):
            katalog.prvek(KAT, 'fb:Neni')


class TestOperace(unittest.TestCase):
    def test_vlozeni_do_serie(self):
        m, i = model.vloz(nw(1), {'r': 0, 'k': 3}, katalog.prvek(KAT, 'NO'))
        self.assertEqual(i, 'n1')
        self.assertEqual(m['rungy'][0]['cesta'], ['p1', 'p2', 'p3', 'n1', 'p4'])
        self.assertEqual(model.vloz(m, {'r': 0, 'k': 0}, katalog.prvek(KAT, 'NC'))[1], 'n2')
        layout.rozloz(m['rungy'], m['prvky'])
        with self.assertRaises(model.ChybaOperace):
            model.vloz(nw(1), {'r': 0, 'k': 9}, katalog.prvek(KAT, 'NO'))

    def test_vystupni_vetev_s_fb(self):
        m, i = model.vetev(nw(1), {'r': 0, 'k': 3}, {'r': 0, 'k': 4}, katalog.prvek(KAT, 'fb:FB_Pas'))
        self.assertEqual(m['rungy'][0]['cesta'], ['p1', 'p2', 'p3', 'wire#w1', 'p4'])
        self.assertEqual(m['rungy'][1], {'z': 'w1', 'cesta': [i], 'do': None})
        self.assertEqual(model.validuj(m), [])                             # box ve výstupní větvi smí
        r, _ = layout.rozloz(m['rungy'], m['prvky'])
        self.assertGreater(r[i]['r'], r['p4']['r'])

    def test_zavrena_vetev_a_smazani(self):
        m, i = model.vetev(nw(1), {'r': 0, 'k': 1}, {'r': 0, 'k': 2}, katalog.prvek(KAT, 'NO'))
        self.assertEqual(m['rungy'][0]['cesta'], ['p1', 'wire#w2', 'p2', 'wire#w1', 'p3', 'p4'])
        self.assertEqual(m['rungy'][1], {'z': 'w2', 'cesta': [i], 'do': 'w1'})
        r, _ = layout.rozloz(m['rungy'], m['prvky'])
        self.assertEqual(r[i]['c'], r['p2']['c'])                          # paralelně ke kontaktu p2
        z = model.smaz(m, i)                                               # prázdná větev i uzly pryč
        self.assertEqual(z['rungy'], nw(1)['rungy'])
        self.assertEqual(len(z['prvky']), 4)

    def test_smazani_boxu_s_privodem(self):
        m = model.smaz(nw(4), 'p2')                                        # FB s pinem napájeným větví
        self.assertEqual(m['rungy'], [{'z': 'powerrail', 'cesta': ['p1'], 'do': None}])
        self.assertEqual([p['id'] for p in m['prvky']], ['p1'])

    def test_zmena_operandu(self):
        m = model.zmen(nw(1), 'p1', {'operand': ' "Pojistka_OK" '})
        self.assertEqual(m['prvky'][0]['operand'], '"Pojistka_OK"')
        with self.assertRaises(model.ChybaOperace):
            model.zmen(nw(1), 'p1', {'typ': 'civka'})
        with self.assertRaises(model.ChybaOperace):
            model.zmen(nw(1), 'p1', {'vstupy': 'x'})

    def test_mista(self):
        n = nw(1)
        _, k = layout.rozloz(n['rungy'], n['prvky'], 1000)
        mi = {(x['r'], x['k']): x for x in model.mista(n, k)}
        self.assertEqual(sorted(mi), [(0, 0), (0, 1), (0, 2), (0, 3), (0, 4)])
        self.assertTrue(mi[(0, 0)]['box'] and not mi[(0, 0)]['civka'])
        self.assertEqual((mi[(0, 4)]['kontakt'], mi[(0, 4)]['civka']), (False, False))   # za cívkou nic
        n2 = nw(2)
        _, k = layout.rozloz(n2['rungy'], n2['prvky'], 1000)
        mi = {(x['r'], x['k']): x for x in model.mista(n2, k)}
        self.assertFalse(mi[(0, 0)]['box'])                                # před uzlem w1 = uvnitř paralely
        self.assertFalse(mi[(1, 0)]['box'])                                # ve větvi
        self.assertTrue(mi[(0, 2)]['box'])                                 # za uzlem w1


class TestValidace(unittest.TestCase):
    def chyby(self, m):
        return [c['chyba'] for c in model.validuj(m)]

    def test_vzorky_bez_chyb(self):
        for n in range(1, 9):
            self.assertEqual(model.validuj(nw(n)), [], 'NW%d' % n)

    def test_box_ve_vetvi(self):
        m, i = model.vetev(nw(1), {'r': 0, 'k': 1}, {'r': 0, 'k': 2}, katalog.prvek(KAT, 'fb:FB_Pas'))
        ch = model.validuj(m)
        self.assertEqual([c['id'] for c in ch], [i])
        self.assertIn('paralelní větve', ch[0]['chyba'])
        m, i = model.vloz(nw(2), {'r': 0, 'k': 0}, katalog.prvek(KAT, 'MOVE'))   # hlavní větev uvnitř paralely
        self.assertTrue(any('paralelní větve' in c for c in self.chyby(m)))

    def test_civka_uprostred(self):
        m, _ = model.vloz(nw(1), {'r': 0, 'k': 4}, {'typ': 'kontakt', 'operand': '"Kvitace"'})
        self.assertEqual([c['id'] for c in model.validuj(m)], ['p4'])
        self.assertIn('není na konci', self.chyby(m)[0])

    def test_operandy_a_napajeni(self):
        m, _ = model.vloz(nw(1), {'r': 0, 'k': 0}, katalog.prvek(KAT, 'GT'))
        self.assertTrue(any('oba operandy' in c for c in self.chyby(m)))
        m, _ = model.vloz(nw(1), {'r': 0, 'k': 0}, katalog.prvek(KAT, 'TON'))
        ch = self.chyby(m)
        self.assertTrue(any('Chybí instance' in c for c in ch) and any('Chybí PT' in c for c in ch))
        m, _ = model.vloz(nw(1), {'r': 0, 'k': 0}, katalog.prvek(KAT, 'NO'))
        self.assertTrue(any('Chybí operand' in c for c in self.chyby(m)))
        m = nw(1)
        m['rungy'].append({'z': 'w9', 'cesta': ['p1'], 'do': None})
        self.assertTrue(any('Nenapájený' in c for c in self.chyby(m)))


class TestApiEditor(unittest.TestCase):
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
        c.request(metoda, cesta, body=json.dumps(telo).encode() if telo is not None else None,
                  headers={'Content-Type': 'application/json'})
        r = c.getresponse()
        t = json.loads(r.read())
        c.close()
        return r.status, t

    def test_katalog_api(self):
        s, k = self.req('GET', '/api/katalog?projekt=%s' % PROJEKT)
        self.assertEqual(s, 200)
        self.assertIn('fb:FB_Pas', [b['klic'] for b in k['bloky']])
        self.assertEqual(self.req('GET', '/api/katalog?projekt=Neni')[0], 404)

    def test_rozlozeni_s_operacemi(self):
        pred = nw(1)
        s, r = self.req('POST', '/api/rozlozeni', {
            'projekt': PROJEKT, 'pred': pred, 'po': pred, 'sirka': 900,
            'ops': [{'typ': 'vetev', 'od': {'r': 0, 'k': 3}, 'do': {'r': 0, 'k': 4}, 'prvek': 'fb:FB_Pas'},
                    {'typ': 'zmen', 'id': 'n1', 'hodnoty': {'instance': '"DB_Pas1"'}}]})
        self.assertEqual(s, 200)
        self.assertEqual((r['posledni'], r['rozdil']), ('n1', {'n1': 'pridany'}))
        self.assertIsNotNone(r['kresba'])
        self.assertEqual(r['chyby'], [])
        self.assertTrue(r['mista'])
        s, r = self.req('POST', '/api/rozlozeni', {'projekt': PROJEKT, 'po': pred,
                                                   'ops': [{'typ': 'vloz', 'misto': {'r': 0, 'k': 0}, 'prvek': 'fb:Neni'}]})
        self.assertEqual(s, 400)
        self.assertEqual(self.req('POST', '/api/rozlozeni', {'po': pred, 'ops': [{'typ': 'smaz', 'id': 'p9'}]})[0], 400)

    def test_ulozeni_po(self):
        _, net = self.req('GET', '/api/network?projekt=%s&blok=FB_Dopravnik&nw=1' % PROJEKT)
        s, u = self.req('POST', '/api/uprava', {'projekt': PROJEKT, 'kolo': 'Kolo 1', 'blok': 'FB_Dopravnik', 'nw': 1,
                                                 'po': {'prvky': net['prvky'], 'rungy': net['rungy']}})
        self.assertEqual(s, 201)
        _, r = self.req('POST', '/api/rozlozeni', {'projekt': PROJEKT, 'po': u['po'], 'ops': [
            {'typ': 'vetev', 'od': {'r': 0, 'k': 1}, 'do': {'r': 0, 'k': 2}, 'prvek': 'fb:FB_Pas'}]})
        s, v = self.req('POST', '/api/uprava/%s/po' % u['id'], {'po': r['po']})
        self.assertEqual(s, 200)                                           # uložit lze i s chybou
        self.assertEqual(len(v['chyby']), 1)
        _, d = self.req('GET', '/api/uprava/%s' % u['id'])
        self.assertEqual(d['rozdil'], {'n1': 'pridany'})
        self.assertEqual(len(d['chyby']), 1)                               # hlavička úpravy je označí
        self.assertIn('upraveno', d['uprava'])
        self.assertEqual(self.req('POST', '/api/uprava/%s/po' % u['id'], {'po': {'prvky': 1}})[0], 400)
        self.req('POST', '/api/uprava/%s/stav' % u['id'], {'stav': 'Approved'})
        s, e = self.req('POST', '/api/uprava/%s/po' % u['id'], {'po': r['po']})
        self.assertEqual(s, 409)
        self.assertIn('Proposal', e['chyba'])

    def test_signed_rozhoduje_posledni_kontrola(self):
        _, net = self.req('GET', '/api/network?projekt=%s&blok=FB_Dopravnik&nw=1' % PROJEKT)
        _, u = self.req('POST', '/api/uprava', {'projekt': PROJEKT, 'kolo': 'k', 'blok': 'FB_Dopravnik', 'nw': 1,
                                                 'po': {'prvky': net['prvky'], 'rungy': net['rungy']}})
        uid = u['id']
        self.req('POST', '/api/uprava/%s/stav' % uid, {'stav': 'Approved'})
        self.req('POST', '/api/kontrola', {'uprava_id': uid, 'shoda': True})
        self.req('POST', '/api/kontrola', {'uprava_id': uid, 'shoda': False})
        self.assertEqual(self.req('POST', '/api/uprava/%s/stav' % uid, {'stav': 'Signed'})[0], 409)
        self.req('POST', '/api/kontrola', {'uprava_id': uid, 'shoda': True})
        self.assertEqual(self.req('POST', '/api/uprava/%s/stav' % uid, {'stav': 'Signed'})[0], 200)
        self.req('POST', '/api/kontrola', {'uprava_id': uid, 'shoda': False})
        _, d = self.req('GET', '/api/uprava/%s' % uid)
        self.assertEqual((d['uprava']['stav'], d['signed_povolen']), ('Signed', False))   # stav se sám nevrací
        self.assertIn('NESHODA', d['varovani'][0])


if __name__ == '__main__':
    unittest.main()
