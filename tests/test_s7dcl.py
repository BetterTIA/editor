import glob
import os
import tempfile
import unittest

from tests.spolecne import VZORKY, PROJEKT, TS0
import s7dcl
import server


def nw(blok, n):
    _, b = server._nacti(VZORKY, PROJEKT, blok, TS0)
    return b['networky'][n - 1]


class TestParser(unittest.TestCase):
    def test_vsechny_vzorky(self):
        soubory = glob.glob(os.path.join(VZORKY, '**', '*.s7dcl'), recursive=True)
        self.assertGreaterEqual(len(soubory), 4)
        for f in soubory:
            b = s7dcl.nacti_blok(f)
            self.assertIn(b['druh'], ('FB', 'FC', 'DB'))

    def test_hlavicka_a_titulky(self):
        _, b = server._nacti(VZORKY, PROJEKT, 'FB_Dopravnik', TS0)
        self.assertEqual((b['druh'], b['nazev'], b['cislo']), ('FB', 'FB_Dopravnik', 10))
        self.assertEqual(len(b['networky']), 8)
        self.assertEqual(b['networky'][0]['titulek'], 'Připravenost dopravníku')
        self.assertEqual(b['networky'][3]['titulek'], 'Volání pásu (instance DB_Pas1)')   # hodnota v '...'
        self.assertEqual(b['networky'][1]['komentar'], 'Start nebo servisní start, Stop rozpíná.\nPřídržná větev přes Motor.')
        self.assertEqual(b['networky'][7]['titulek'], '')

    def test_serie(self):
        n = nw('FB_Dopravnik', 1)
        self.assertEqual([p['typ'] for p in n['prvky']], ['kontakt'] * 3 + ['civka'])
        self.assertEqual(n['prvky'][1]['operand'], '"DB_Stroj".Bezpecnost_OK')
        self.assertEqual(n['rungy'], [{'z': 'powerrail', 'cesta': ['p1', 'p2', 'p3', 'p4'], 'do': None}])

    def test_vetve_wire(self):
        n = nw('FB_Dopravnik', 2)
        self.assertEqual(len(n['rungy']), 3)
        self.assertEqual(n['rungy'][0]['cesta'], ['p1', 'wire#w1', 'p2', 'p3', 'p4'])
        self.assertEqual([r['do'] for r in n['rungy']], [None, 'w1', 'w1'])
        self.assertEqual(n['prvky'][1]['typ'], 'kontakt_nc')
        v = s7dcl.vodice(n)
        self.assertIn({'z': 'p5', 'do': 'w1'}, v)
        self.assertIn({'z': 'w1', 'do': 'p2'}, v)

    def test_ton(self):
        p = nw('FB_Dopravnik', 3)['prvky'][1]
        self.assertEqual((p['typ'], p['nazev'], p['instance'], p['sablona']), ('box', 'TON', '#T_Rozbeh', 'Time'))
        self.assertEqual(p['napajeni'], ['IN', 'Q'])
        self.assertEqual(p['vstupy'], [{'pin': 'PT', 'operand': 'T#2s'}])
        self.assertEqual(p['vystupy'], [{'pin': 'ET', 'operand': '#Rozbeh_Cas'}])

    def test_volani_fb(self):
        n = nw('FB_Dopravnik', 4)
        p = n['prvky'][1]
        self.assertEqual((p['volani'], p['instance'], p['nazev']), ('fb', '"DB_Pas1"', 'FB_Pas'))
        self.assertEqual([x['pin'] for x in p['vstupy']], ['Povoleni', 'Smer', 'Rychlost', 'Reset'])
        self.assertEqual(p['vstupy'][0]['operand'], 'wire#w1')
        self.assertEqual(p['vstupy'][3]['operand'], '')             # prázdný pin
        self.assertEqual(p['vystupy'][2], {'pin': 'Stav', 'operand': ''})
        self.assertIn({'z': 'w1', 'do': 'p2.Povoleni'}, s7dcl.vodice(n))

    def test_porovnani(self):
        a, b, _ = nw('FB_Dopravnik', 5)['prvky']
        self.assertEqual((a['typ'], a['op'], a['in1'], a['in2'], a['sablona']),
                         ('porovnani', '<>', '"DB_Stroj".Rezim', '0', 'Int'))
        self.assertEqual((b['op'], b['sablona']), ('>', 'Real'))

    def test_move(self):
        p = nw('FB_Dopravnik', 6)['prvky'][1]
        self.assertEqual((p['nazev'], p['napajeni']), ('MOVE', ['EN', 'ENO']))
        self.assertEqual(p['vstupy'], [{'pin': 'IN', 'operand': '1500'}])
        self.assertEqual(p['vystupy'], [{'pin': 'OUT1', 'operand': '"DB_Stroj".Zadana_Rychlost'}])

    def test_civky_a_trig(self):
        prvky = nw('FB_Dopravnik', 7)['prvky']
        self.assertEqual([p['typ'] for p in prvky],
                         ['kontakt', 'civka_s', 'kontakt', 'civka', 'kontakt', 'box', 'civka_r'])
        self.assertEqual((prvky[5]['nazev'], prvky[5]['pod'], prvky[5]['napajeni']), ('P_TRIG', '#Kvitace_P', ['CLK', 'Q']))

    def test_fbd_vlastni_model(self):
        n = nw('FC_Vetve', 3)
        self.assertEqual(n['jazyk'], 'FBD')
        self.assertNotIn('rungy', n)                                     # FBD nemá LAD rungy, ale boxy (F1)
        self.assertEqual([p['typ'] for p in n['prvky']], ['or', 'assign'])

    def test_carky_v_uvozovkach(self):
        self.assertEqual(s7dcl.rozdel_carkami('a := "x,y", b := c[1,2], d =>  '), ['a := "x,y"', 'b := c[1,2]', 'd =>'])

    def test_s7res(self):
        with tempfile.NamedTemporaryFile('w', suffix='.s7res', delete=False, encoding='utf-8') as f:
            f.write("MultiLingualTexts:\n  - id: MLC_a\n    en-US: 'It''s'\n"
                    "  - id: MLC_b\n    en-US: |+\n      radek 1\n      radek 2\n"
                    "  - id: MLC_c\n    cs-CZ: česky\n    en-US: anglicky\n")
        try:
            t = s7dcl.nacti_s7res(f.name)
        finally:
            os.remove(f.name)
        self.assertEqual(t['MLC_a']['en-US'], "It's")
        self.assertEqual(t['MLC_b']['en-US'], 'radek 1\nradek 2')
        self.assertEqual(s7dcl.text_mlc(t, 'MLC_c'), 'česky')
        self.assertEqual(s7dcl.text_mlc(t, 'MLC_neni'), '')


if __name__ == '__main__':
    unittest.main()
