import unittest

from tests.spolecne import VZORKY, PROJEKT
import layout
import server


def roz(blok, n, sirka=0):
    _, b = server._nacti(VZORKY, PROJEKT, blok)
    x = b['networky'][n - 1]
    r, k = layout.rozloz(x['rungy'], x['prvky'], sirka)
    return r, k, {d['id']: d for d in k['prvky']}


class TestRozlozeni(unittest.TestCase):
    def test_serie_civka_vpravo(self):
        r, k, _ = roz('FB_Dopravnik', 1)
        self.assertEqual([r[p]['c'] for p in ('p1', 'p2', 'p3')], [0, 1, 2])
        self.assertEqual(len({v['r'] for v in r.values()}), 1)               # vše na jednom rungu
        self.assertEqual(r['p4']['c'], max(v['c'] for v in r.values()))      # cívka v posledním sloupci

    def test_civka_k_pravemu_okraji(self):
        _, k, d = roz('FB_Dopravnik', 1, 1000)
        self.assertGreaterEqual(k['sirka'], 1000)
        self.assertGreater(d['p4']['x'], 800)

    def test_samodrz_dve_vetve(self):
        r, k, _ = roz('FB_Dopravnik', 2)
        # větve pod hlavní, zarovnané na začátek úseku (sloupec 0)
        self.assertEqual((r['p1']['c'], r['p5']['c'], r['p6']['c']), (0, 0, 0))
        self.assertTrue(r['p1']['r'] < r['p5']['r'] < r['p6']['r'])
        self.assertEqual(r['p7']['r'], r['p6']['r'])
        # spoj za nejdelší větví: Stop až za ní, zpět na hlavním rungu
        self.assertGreater(r['p2']['c'], r['p7']['c'])
        self.assertEqual(r['p2']['r'], r['p1']['r'])
        svisle = [c for c in k['cary'] if c[0] == c[2]]
        self.assertEqual(len(svisle), 2)                                     # začátek (lišta) + konec úseku

    def test_ton_box_v_hlavni_vetvi(self):
        r, k, d = roz('FB_Dopravnik', 3)
        self.assertEqual(r['p2']['r'], r['p1']['r'])
        self.assertEqual(len(d['p2']['radky']), 2)          # IN, PT
        self.assertEqual(d['p2']['radky'][0], d['p1']['y'])  # IN na rungu

    def test_privod_k_pinu(self):
        r, k, d = roz('FB_Dopravnik', 4)
        box = r['p2']
        self.assertLess(r['p3']['c'], box['c'])
        self.assertEqual(r['p4']['c'], box['c'] - 1)                    # přívod přiléhá k boxu
        self.assertGreater(r['p3']['r'], box['r'])
        self.assertEqual(d['p2']['radky'][1], d['p3']['y'])             # pin Povoleni na lince přívodu

    def test_vnorena_vetev(self):
        r, _, _ = roz('FC_Vetve', 1)
        self.assertEqual(r['p4']['c'], r['p2']['c'])                    # Senzor_C paralelně k Senzor_B
        self.assertGreater(r['p4']['r'], r['p2']['r'])
        self.assertEqual((r['p5']['c'], r['p6']['c']), (0, 1))          # OR na začátku

    def test_vystupni_vetve(self):
        r, _, _ = roz('FC_Vetve', 2)
        self.assertEqual(r['p3']['c'], r['p4']['c'])                    # MOVE a kontakt z téhož uzlu
        self.assertEqual(r['p2']['c'], r['p5']['c'])                    # cívky zarovnané vpravo
        self.assertTrue(r['p2']['r'] < r['p3']['r'] < r['p4']['r'])

    def test_dva_rungy(self):
        r, _, _ = roz('FC_Vetve', 5)
        self.assertEqual((r['p2']['c'], r['p3']['c']), (1, 2))
        self.assertGreater(r['p4']['r'], r['p3']['r'])
        self.assertEqual(r['p4']['c'], 0)

    def test_mustek_nejde(self):
        _, b = server._nacti(VZORKY, PROJEKT, 'FC_Vetve')
        x = b['networky'][3]
        with self.assertRaises(layout.Nerozlozitelne):
            layout.rozloz(x['rungy'], x['prvky'])

    def test_prazdny(self):
        r, k, _ = roz('FB_Dopravnik', 8)
        self.assertEqual(r, {})
        self.assertEqual(k['prvky'], [])
        self.assertTrue(k['prazdny'])                        # stránka připíše „(prázdný network)“


if __name__ == '__main__':
    unittest.main()
