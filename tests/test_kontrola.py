"""M4: kontrola „export po zápisu = návrh" — porovnání podle obsahu, hledání networku podle titulku, CLI, Signed."""
import contextlib
import http.client
import io
import json
import os
import re
import shutil
import tempfile
import threading
import unittest

from tests.spolecne import VZORKY, PROJEKT, TS0, TS1
import kontrola
import model
import rozdil
import server

DOP = os.path.join('PLC_1', 'Blocks', 'Dopravnik')


def _cti(ts, pripona):
    with open(os.path.join(VZORKY, PROJEKT, ts, DOP, 'FB_Dopravnik' + pripona), encoding='utf-8') as f:
        return f.read()


class TestShodaObsahu(unittest.TestCase):
    def nw(self, n, ts=TS0):
        _, b = server._nacti(VZORKY, PROJEKT, 'FB_Dopravnik', ts)
        return b['networky'][n - 1]

    def test_id_a_jmena_uzlu_nevadi(self):
        a = self.nw(2)
        b = json.loads(re.sub(r'"p(\d+)"', r'"q\1"', json.dumps(a)).replace('wire#w1', 'wire#w7').replace('"w1"', '"w7"'))
        self.assertEqual(rozdil.shoda_obsahu(a, b), (True, []))

    def test_topologie(self):
        a = self.nw(1)
        b = json.loads(json.dumps(a))
        b['rungy'][0]['cesta'] = ['p1', 'wire#w1', 'p3', 'p4']               # p2 paralelně k p1 místo do série
        b['rungy'].append({'z': 'powerrail', 'cesta': ['p2'], 'do': 'w1'})
        shoda, r = rozdil.shoda_obsahu(a, b)
        self.assertFalse(shoda)
        self.assertIn('liší se zapojení', r[0])

    def test_piny_boxu(self):
        a = self.nw(4)
        b = json.loads(json.dumps(a))
        b['prvky'][1]['vstupy'][1]['operand'] = '#Jiny'
        shoda, r = rozdil.shoda_obsahu(a, b)
        self.assertEqual(r, ['jiné piny FB_Pas "DB_Pas1": Smer #Smer → #Jiny'])

    def test_vetev_bez_prvku_v_hlavni_ceste(self):
        m = model.smaz({'prvky': self.nw(2)['prvky'], 'rungy': self.nw(2)['rungy']}, 'p1')
        ch = model.validuj(m)
        self.assertEqual([c['id'] for c in ch], ['p5', 'p6'])                # obě přemostěné větve
        self.assertTrue(all('Větev bez prvku v hlavní cestě' in c['chyba'] for c in ch))


class TestKontrolaExportem(unittest.TestCase):
    """Export root v temp: nejdřív jen původní export, „nové exporty" se přidávají během testu."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.data = tempfile.mkdtemp()
        shutil.copytree(os.path.join(VZORKY, PROJEKT, TS0), os.path.join(self.root, PROJEKT, TS0))
        self.srv = server.vytvor_server(0, self.root, self.data)
        self.url = 'http://127.0.0.1:%d' % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    def req(self, metoda, cesta, telo=None):
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=20)
        c.request(metoda, cesta, body=json.dumps(telo).encode() if telo is not None else None,
                  headers={'Content-Type': 'application/json'})
        r = c.getresponse()
        t = json.loads(r.read())
        c.close()
        return r.status, t

    def export(self, ts, s7dcl, s7res):
        d = os.path.join(self.root, PROJEKT, ts, DOP)
        os.makedirs(d)
        for pripona, text in (('.s7dcl', s7dcl), ('.s7res', s7res)):
            with open(os.path.join(d, 'FB_Dopravnik' + pripona), 'w', encoding='utf-8') as f:
                f.write(text)

    def cli(self, *arg):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            kod = kontrola.main(['--server', self.url] + list(arg))
        return kod, out.getvalue()

    def test_scenar(self):
        _, net = self.req('GET', '/api/network?projekt=%s&blok=FB_Dopravnik&nw=1' % PROJEKT)
        _, r = self.req('POST', '/api/rozlozeni', {'projekt': PROJEKT, 'po': {'prvky': net['prvky'], 'rungy': net['rungy']},
                                                   'ops': [{'typ': 'vloz', 'misto': {'r': 0, 'k': 3}, 'prvek': 'NO'},
                                                           {'typ': 'zmen', 'id': 'n1', 'hodnoty': {'operand': '"Pojistka_OK"'}}]})
        s, u = self.req('POST', '/api/uprava', {'projekt': PROJEKT, 'kolo': 'Kolo 1', 'blok': 'FB_Dopravnik', 'nw': 1,
                                                 'po': r['po']})
        uid = u['id']
        self.req('POST', '/api/uprava/%s/stav' % uid, {'stav': 'Approved'})
        # 1) tentýž export jako před úpravou -> neshoda, chybí kontakt
        kod, txt = self.cli('--uprava', uid)
        self.assertEqual(kod, 1)
        self.assertIn('chybí v exportu: kontakt "Pojistka_OK"', txt)
        self.assertIn('tentýž jako před úpravou', txt)
        # 2) export s jiným operandem -> neshoda s poznámkou
        s7dcl, s7res = _cti(TS1, '.s7dcl'), _cti(TS1, '.s7res')
        self.export('20261012-150000', s7dcl.replace('"Pojistka_OK"', '"Pojistka_XX"'), s7res)
        kod, txt = self.cli('--uprava', uid)
        self.assertEqual(kod, 1)
        self.assertIn('jiný operand: návrh kontakt "Pojistka_OK", export kontakt "Pojistka_XX"', txt)
        self.assertEqual(self.req('POST', '/api/uprava/%s/stav' % uid, {'stav': 'Signed'})[0], 409)
        # 3) export se zápisem, ale network posunutý (nový network před NW1, volá FC) -> shoda + poznámka
        novy = ('    {\n        S7_Language := "LAD";\n        S7_NetworkTitle := "MLC_0"\n    }\n    NETWORK\n'
                '        RUNG wire#powerrail\n            Contact( #Start )\n            "FC_Vetve"(  )\n'
                '        END_RUNG\n    END_NETWORK\n')
        i = s7dcl.index('    {\n        S7_Language := "LAD";\n        S7_NetworkTitle := "MLC_1"')
        self.export('20261012-160000', s7dcl[:i] + novy + s7dcl[i:],
                    s7res.replace('MultiLingualTexts:\n', 'MultiLingualTexts:\n  - id: MLC_0\n    en-US: Nový network\n', 1))
        s, k = self.req('POST', '/api/uprava/%s/kontrola-exportem' % uid, {})
        self.assertEqual((s, k['shoda'], k['export_ts'], k['nw_export']), (200, True, '20261012-160000', 2))
        self.assertIn('network se posunul: NW 1 → NW 2', k['poznamka'])
        _, n1 = self.req('GET', '/api/network?projekt=%s&blok=FB_Dopravnik&nw=1' % PROJEKT)
        self.assertEqual((n1['prvky'][1]['volani'], n1['prvky'][1]['nazev']), ('fc', 'FC_Vetve'))   # FC ze staršího exportu
        # 4) zadaný export se zápisem (vzorek TS1) -> shoda, exit 0
        self.export(TS1, s7dcl, s7res)
        kod, txt = self.cli('--uprava', uid, '--export-ts', TS1)
        self.assertEqual(kod, 0, txt)
        self.assertTrue(txt.startswith('SHODA'))
        # po shodě projde Approved -> Signed
        s, u = self.req('POST', '/api/uprava/%s/stav' % uid, {'stav': 'Signed'})
        self.assertEqual((s, u['stav']), (200, 'Signed'))
        self.assertEqual([k['shoda'] for k in u['kontroly']], [False, False, True, True])
        # chyby: neexistující export / úprava -> exit 2
        self.assertEqual(self.cli('--uprava', uid, '--export-ts', '20991231-000000')[0], 2)
        self.assertEqual(self.cli('--uprava', 'U999')[0], 2)


if __name__ == '__main__':
    unittest.main()
