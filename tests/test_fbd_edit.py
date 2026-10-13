"""F2 — FBD: operace editoru, pravidla, místa vložení, kontrola exportem, postup pro TIA, „jako LAD"."""
import contextlib
import copy
import http.client
import io
import json
import shutil
import tempfile
import threading
import unittest

from tests.common import VZORKY, PROJEKT, TS0, TS2, po_nw2
import fbd
import catalog
import check
import layout
import procedure
import server

BLOK = 'FB_Bezpecnost'
KAT = catalog.catalog(VZORKY, PROJEKT)


def nw(n, ts=TS0):
    _, b = server._nacti(VZORKY, PROJEKT, BLOK, ts)
    return b['networky'][n - 1]


def m(n, ts=TS0):
    return fbd.model(copy.deepcopy(nw(n, ts)['prvky']))


def op(model, *ops):
    for o in ops:
        model, _ = fbd.proved(model, o, lambda k: catalog.prvek(KAT, k))
    return model


def P(model):
    return {p['id']: p for p in model['prvky']}


class TestOperace(unittest.TestCase):
    def test_vloz_box_do_signalu_a_na_novy_vstup(self):
        x = op(m(1), {'typ': 'vloz_box', 'prvek': 'P_TRIG', 'do': {'id': 'b2', 'vstup': 1}})
        self.assertEqual(P(x)['n1']['vstupy'][0]['signal'], 'b1')          # P_TRIG převzal výstup >=1
        self.assertEqual(P(x)['b2']['vstupy'][0]['signal'], 'n1')
        x = op(x, {'typ': 'vloz_box', 'prvek': 'OR', 'do': {'id': 'b2', 'vstup': 4}})
        self.assertEqual([v['pin'] for v in P(x)['b2']['vstupy']], ['in1', 'in2', 'in3', 'in4'])
        self.assertEqual((P(x)['b2']['vstupy'][3]['signal'], P(x)['n2']['vstupy'][0]['operand']), ('n2', ''))
        x = op(x, {'typ': 'vloz_box', 'prvek': 'ASSIGN'})                      # nová logická cesta
        self.assertEqual(P(x)['n3']['vstupy'][0]['signal'], None)
        fbd.rozloz(x['prvky'])

    def test_odeber_vstup_negace_zmen_pin(self):
        x = op(m(1), {'typ': 'pridej_vstup', 'id': 'b2', 'operand': '#A'}, {'typ': 'odeber_vstup', 'id': 'b2', 'vstup': 2})
        self.assertEqual([(v['pin'], v['operand']) for v in P(x)['b2']['vstupy'][1:]], [('in2', '"DB_Stroj".Bezpecnost_OK'), ('in3', '#A')])
        with self.assertRaises(fbd.ChybaOperace):
            op(m(1), {'typ': 'odeber_vstup', 'id': 'b1', 'vstup': 1})       # >=1 má jen 2 vstupy
        x = op(m(1), {'typ': 'negace', 'id': 'b1', 'vystup': 'hlavni'})
        self.assertTrue(P(x)['b1']['neg_vystup'])
        _, k = fbd.rozloz(x['prvky'])
        self.assertTrue(next(d for d in k['boxy'] if d['id'] == 'b1')['vystupy'][0]['neg'])
        x = op(m(4), {'typ': 'zmen_pin', 'id': 'b1', 'pin': 'TIME_DEL', 'operand': 'T#2s'},
               {'typ': 'zmen_pin', 'id': 'b1', 'pin': 'DIAG', 'operand': '#Diag', 'neg': False})
        v = {p['pin']: p['operand'] for p in P(x)['b1']['vstupy'] + P(x)['b1']['vystupy']}
        self.assertEqual((v['TIME_DEL'], v['DIAG']), ('T#2s', '#Diag'))

    def test_smaz_prepoji_retez(self):
        x = op(m(1), {'typ': 'smaz', 'id': 'b2'})
        self.assertEqual(P(x)['b3']['vstupy'][0]['signal'], 'b1')           # = dostal 1. vstup smazaného &
        x = op(m(4), {'typ': 'smaz', 'id': 'b2'})                            # P_TRIG napájel pin ACK F-bloku
        self.assertEqual((P(x)['b1']['vstupy'][2]['signal'], P(x)['b1']['vstupy'][2]['operand']), (None, '#Kvitace'))   # ACK dostal CLK hrany

    def test_vloz_volani_z_katalogu(self):
        x, i = fbd.proved(m(1), {'typ': 'vloz_volani', 'prvek': 'fb:FB_Pas', 'instance': '"DB_Pas1"'},
                          lambda k: catalog.prvek(KAT, k), True)
        p = P(x)[i]
        self.assertEqual((p['typ'], p['nazev'], p['instance']), ('volani', 'FB_Pas', '"DB_Pas1"'))
        self.assertNotIn('EN', [v['pin'] for v in p['vstupy']])               # v F-bloku EN není
        x, i = fbd.proved(m(1), {'typ': 'vloz_box', 'prvek': 'MOVE'}, lambda k: catalog.prvek(KAT, k))
        self.assertEqual([v['pin'] for v in P(x)[i]['vstupy']], ['EN', 'IN'])
        with self.assertRaises(fbd.ChybaOperace):
            fbd.proved(m(1), {'typ': 'vloz_volani', 'prvek': 'AND'})

    def test_mista(self):
        x = m(1)
        mi = fbd.mista(x, fbd.rozloz(x['prvky'])[1])
        do = [tuple(sorted(z['do'].items())) if z['do'] else None for z in mi]
        self.assertIn((('id', 'b2'), ('vstup', 4)), do)                      # nový vstup &
        self.assertIn((('id', 'b3'), ('vstup', 1)), do)
        self.assertEqual(do[-1], None)                                        # nová logická cesta
        self.assertEqual(len(mi), 2 + 1 + 3 + 1 + 1 + 1)


class TestPravidla(unittest.TestCase):
    def test_pravidla(self):
        x = op(m(1), {'typ': 'vloz_box', 'prvek': 'ASSIGN'}, {'typ': 'vloz_box', 'prvek': 'AND'})
        ch = fbd.validuj(x)
        self.assertEqual([(c['id'], bool(c.get('varovani'))) for c in ch],
                         [('n1', False), ('n1', False), ('n2', False), ('n2', False), ('n2', True)])
        self.assertIn('Chybí operand', ch[0]['chyba'])
        self.assertIn('Nezapojený vstup 1', ch[1]['chyba'])
        self.assertIn('nevede nikam', ch[4]['chyba'])

    def test_f_blok(self):
        x = op(m(1), {'typ': 'vloz_volani', 'prvek': 'fb:FB_Pas', 'instance': '"DB_Pas1"'})
        ch = fbd.validuj(x, True, {'FB_Pas'})
        self.assertTrue(any('jen F-instrukce' in c['chyba'] for c in ch))
        x = op(m(7), {'typ': 'zmen_operand', 'id': 'b2', 'vstup': 1, 'operand': 'TRUE'})
        self.assertTrue(any('EN zapojit' in c['chyba'] for c in fbd.validuj(x, True)))
        self.assertFalse(any('EN' in c['chyba'] for c in fbd.validuj(x, False)))


class TestKontrolaFBD(unittest.TestCase):
    def navrh(self):
        return op(m(1), {'typ': 'pridej_vstup', 'id': 'b2', 'operand': '#Zamek_OK'},
                  {'typ': 'zmen_operand', 'id': 'b3', 'operand': '#Povoleni_Pohon'})

    def test_shoda_a_neshoda(self):
        self.assertEqual(fbd.shoda_obsahu(self.navrh(), nw(1, TS2)), (True, []))
        shoda, r = fbd.shoda_obsahu(m(1), nw(1, TS2))
        self.assertFalse(shoda)
        self.assertEqual(len(r), 2)
        self.assertTrue(r[0].startswith('jiný box &') or r[0].startswith('jiný box ='))

    def test_komutativni_poradi(self):
        e = copy.deepcopy(nw(1, TS2))
        b2 = e['prvky'][1]
        b2['vstupy'] = [b2['vstupy'][3], b2['vstupy'][0], b2['vstupy'][2], b2['vstupy'][1]]
        fbd._precisluj(b2)
        self.assertEqual(fbd.shoda_obsahu(self.navrh(), e), (True, []))       # & — pořadí vstupů nevadí
        s = m(6)
        e = copy.deepcopy(s)
        sr = P(e)['b3']
        sr['vstupy'][0]['operand'], sr['vstupy'][1]['operand'] = sr['vstupy'][1]['operand'], sr['vstupy'][0]['operand']
        self.assertFalse(fbd.shoda_obsahu(s, e)[0])                           # SR — S a R1 prohozené = neshoda

    def test_zapojeni(self):
        a = m(3)
        b = copy.deepcopy(a)
        P(b)['b4']['vstupy'][0]['signal'] = 'b2'                               # odbočka za = místo za &
        shoda, r = fbd.shoda_obsahu(a, b)
        self.assertEqual((shoda, r), (False, ['boxy sedí, ale liší se zapojení signálů']))


class TestPostup(unittest.TestCase):
    def test_fbd(self):
        pred = m(1)
        po = op(pred, {'typ': 'pridej_vstup', 'id': 'b2', 'operand': '#Zamek_OK'}, {'typ': 'negace', 'id': 'b2', 'vstup': 4},
                {'typ': 'zmen_operand', 'id': 'b3', 'operand': '#Povoleni_Pohon'},
                {'typ': 'vloz_box', 'prvek': 'P_TRIG', 'do': {'id': 'b2', 'vstup': 1}},
                {'typ': 'zmen_operand', 'id': 'n1', 'operand': '#Pamet'})
        u = {'id': 'U009', 'druh': 'FB', 'cislo_bloku': 30, 'blok': BLOK, 'nw': 1, 'titulek': nw(1)['titulek'], 'f': True,
             'pred': pred, 'po': po}
        p = procedure.procedure(u)
        self.assertEqual(p['hlavicka'], 'FB30 "FB_Bezpecnost" · NW 1: Povolení — >=1, & s negací, přiřazení')
        self.assertTrue(p['markdown'].startswith('### Postup pro TIA — FB30 "FB_Bezpecnost" · NW 1: '))
        t = '\n'.join(p['kroky'])
        for s in ('**Insert input** (`Ctrl+Shift+3`)', 'napiš `#Zamek_OK`', '**Create a ones complement** (`Ctrl+Shift+4`)',
                  'přepiš operand nad boxem na `#Povoleni_Pohon`', 'Vlož box `P_TRIG` na vstup 1 boxu `&`',
                  'nad box napiš operand `#Pamet`', 'Přelož safety program', 'kontrola.py --uprava U009'):
            self.assertIn(s, t)
        self.assertNotIn('Ctrl+Shift+5', t)
        u['po'] = op(pred, {'typ': 'smaz', 'id': 'b1'})
        self.assertIn('Vyber box `>=1` (vstupy `#Start`, `#Servis`) a smaž ho: pravý klik → **Delete**', procedure.procedure(u)['markdown'])

    def test_lad(self):
        _, b = server._nacti(VZORKY, PROJEKT, 'FB_Dopravnik', TS0)
        n = b['networky'][1]
        u = {'id': 'U010', 'druh': 'FB', 'cislo_bloku': 10, 'blok': 'FB_Dopravnik', 'nw': 2, 'titulek': n['titulek'],
             'pred': {'prvky': n['prvky'], 'rungy': n['rungy']}, 'po': po_nw2(n)}
        p = procedure.procedure(u)
        self.assertEqual(p['hlavicka'], 'FB10 "FB_Dopravnik" · NW 2: Samodrž motoru')
        t = '\n'.join(p['kroky'])
        self.assertIn('Vlož `-| |- "Pojistka_OK"`', t)
        self.assertIn('⟦neověřeno⟧', t)
        self.assertNotIn('Přelož safety', t)


class TestJakoLad(unittest.TestCase):
    def test_prevod(self):
        rungy, prvky = fbd.jako_lad(nw(1)['prvky'])
        self.assertEqual([p['typ'] for p in prvky], ['kontakt', 'kontakt', 'kontakt_nc', 'kontakt', 'civka'])
        layout.rozloz(rungy, prvky)
        rungy, prvky = fbd.jako_lad(nw(3)['prvky'])
        self.assertEqual(len([r for r in rungy if r['z'] == 'powerrail']), 3)   # 3 přiřazení = 3 rungy
        self.assertIn('not', [p['typ'] for p in prvky])
        layout.rozloz(rungy, prvky)
        self.assertIsNone(fbd.jako_lad(nw(4)['prvky']))                       # volání F-bloku
        self.assertIsNone(fbd.jako_lad(nw(8)['prvky']))                       # x a N_TRIG


class TestApiF2(unittest.TestCase):
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

    def test_navrh_kontrola_signed_postup(self):
        n = nw(1)
        mm = {'jazyk': 'FBD', 'prvky': n['prvky']}
        s, r = self.req('POST', '/api/rozlozeni', {'projekt': PROJEKT, 'pred': mm, 'po': mm, 'f': True, 'ops': [
            {'typ': 'pridej_vstup', 'id': 'b2', 'operand': '#Zamek_OK'},
            {'typ': 'zmen_operand', 'id': 'b3', 'operand': '#Povoleni_Pohon'},
            {'typ': 'vloz_volani', 'prvek': 'fb:FB_Pas', 'instance': '"DB_Pas1"'},
            {'typ': 'smaz', 'id': 'n1'}]})
        self.assertEqual((s, r['chyby'], r['posledni']), (200, [], None))
        self.assertTrue(any(x['do'] == {'id': 'b2', 'vstup': 5} for x in r['mista']))
        s, u = self.req('POST', '/api/uprava', {'projekt': PROJEKT, 'kolo': 'F2', 'blok': BLOK, 'nw': 1, 'po': r['po']})
        self.assertEqual(s, 201)
        # pred = nejnovější export (TS2 už úpravu má) -> přepni pred na TS0 přes PUT stavu jako v reálu (návrh před zápisem)
        s, st = self.req('GET', '/api/stav?projekt=%s' % PROJEKT)
        st['upravy'][0]['pred'] = dict(server._model(server.api_network(VZORKY, PROJEKT, BLOK, 1, TS0)), export_ts=TS0)
        self.assertEqual(self.req('PUT', '/api/stav?projekt=%s' % PROJEKT, st)[0], 200)
        uid = u['id']
        s, p = self.req('GET', '/api/uprava/%s/postup' % uid)
        self.assertEqual(s, 200)
        self.assertIn('Insert input', p['markdown'])
        self.assertIn('FB30 "FB_Bezpecnost" · NW 1:', p['markdown'])
        self.req('POST', '/api/uprava/%s/stav' % uid, {'stav': 'Approved', 'kdo': 'test'})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kod = check.main(['--server', 'http://127.0.0.1:%d' % self.port, '--uprava', uid, '--export-ts', TS0])
        self.assertEqual(kod, 1)                                               # starý export = neshoda
        with contextlib.redirect_stdout(out):
            kod = check.main(['--server', 'http://127.0.0.1:%d' % self.port, '--uprava', uid])
        self.assertEqual(kod, 0, out.getvalue())                               # export po zápisu (TS2) = shoda
        s, x = self.req('POST', '/api/uprava/%s/stav' % uid, {'stav': 'Signed', 'kdo': 'test'})
        self.assertEqual((s, x['stav']), (200, 'Signed'))
        s, x = self.req('POST', '/api/uprava/%s/stav' % uid, {'stav': 'Checked', 'kdo': 'test'})
        self.assertEqual((s, x['stav']), (200, 'Checked'))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(check.main(['--server', 'http://127.0.0.1:%d' % self.port, '--postup', uid]), 0)
        self.assertTrue(out.getvalue().startswith('### Postup pro TIA — FB30'))

    def test_network_jako_lad_a_f(self):
        s, n = self.req('GET', '/api/network?projekt=%s&blok=%s&nw=1&ts=%s' % (PROJEKT, BLOK, TS0))
        self.assertEqual((s, n['f'], 'kresba' in n['jako_lad']), (200, True, True))
        s, n = self.req('GET', '/api/network?projekt=%s&blok=%s&nw=4' % (PROJEKT, BLOK))
        self.assertNotIn('jako_lad', n)
        s, k = self.req('GET', '/api/katalog?projekt=%s' % PROJEKT)
        self.assertEqual([x['klic'] for x in k['fbd']][:3], ['AND', 'OR', 'XOR'])
        self.assertEqual({b['nazev']: b['f'] for b in k['bloky']}['FB_Bezpecnost'], True)


if __name__ == '__main__':
    unittest.main()
