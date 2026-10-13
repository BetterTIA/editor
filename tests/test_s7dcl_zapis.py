"""M5: serializace modelu do .s7dcl — roundtrip parse(serialize(parse(x))) na všech networcích vzorků,
náhrada networku v souboru bloku, modely z editoru (LAD/FBD operace)."""
import copy
import glob
import os
import unittest

from tests.spolecne import VZORKY, PROJEKT, TS0
import fbd
import model
import rozdil
import s7dcl
import s7dcl_zapis as z
import server


def shoda(a, b):
    return fbd.shoda_obsahu(a, b) if a['jazyk'] == 'FBD' else rozdil.shoda_obsahu(a, b)


def reparse(nw):
    """model networku -> text -> model (stejný jazyk)"""
    obsah = z.text_networku(nw)
    if nw.get('jazyk') == 'FBD':
        return {'jazyk': 'FBD', 'prvky': fbd.parse(obsah)}
    r, p = s7dcl.parse_lad(obsah)
    return {'jazyk': 'LAD', 'prvky': p, 'rungy': r}


class TestRoundtrip(unittest.TestCase):
    def test_vsechny_networky_vzorku(self):
        n = 0
        for f in glob.glob(os.path.join(VZORKY, '**', '*.s7dcl'), recursive=True):
            text = s7dcl.cti(f)
            b = s7dcl.parse_blok(text)
            for nw in b['networky']:
                if 'prvky' not in nw:
                    continue
                with self.subTest(blok=os.path.basename(f), nw=nw['nw']):
                    self.assertEqual(shoda(nw, reparse(nw)), (True, []))
                    # nahrazení networku sám sebou: celý blok se čte stejně (ostatní networky i atributy beze změny)
                    b2 = s7dcl.parse_blok(z.nahrad_network(text, nw['nw'], z.text_networku(nw)))
                    self.assertEqual(len(b2['networky']), len(b['networky']))
                    for x, y in zip(b['networky'], b2['networky']):
                        self.assertEqual(x['atributy'], y['atributy'])
                        if x['nw'] != nw['nw']:
                            self.assertEqual(x['text'], y['text'])
                        elif 'prvky' in x:
                            self.assertEqual(shoda(x, y), (True, []))
                    n += 1
        self.assertGreaterEqual(n, 30)

    def test_text_jako_export(self):
        f = os.path.join(VZORKY, PROJEKT, TS0, 'PLC_1', 'Blocks', 'Dopravnik', 'FB_Dopravnik.s7dcl')
        text = s7dcl.cti(f)
        nw4 = s7dcl.parse_blok(text)['networky'][3]
        self.assertEqual(z.nahrad_network(text, 4, z.text_networku(nw4)), text)    # NW4 znak po znaku jako export

    def test_crlf_a_chyby(self):
        text = s7dcl.cti(os.path.join(VZORKY, PROJEKT, TS0, 'PLC_1', 'Blocks', 'Dopravnik', 'FC_Vetve.s7dcl'))
        crlf = text.replace('\n', '\r\n')
        vys = z.nahrad_network(crlf, 1, '        RUNG wire#powerrail\n        END_RUNG\n')
        self.assertNotIn('\n', vys.replace('\r\n', ''))
        self.assertEqual(s7dcl.parse_blok(vys.replace('\r\n', '\n'))['networky'][0]['prvky'], [])
        with self.assertRaises(z.ChybaZapisu):
            z.nahrad_network(text, 99, '')


class TestModelyZEditoru(unittest.TestCase):
    def nw(self, blok, n):
        _, b = server._nacti(VZORKY, PROJEKT, blok, TS0)
        return b['networky'][n - 1]

    def test_lad_operace(self):
        n = self.nw('FB_Dopravnik', 2)
        m = {'prvky': n['prvky'], 'rungy': n['rungy']}
        m = model.vloz(m, {'r': 0, 'k': 3}, {'typ': 'kontakt', 'operand': '"Pojistka_OK"'})[0]
        m = model.vetev(m, {'r': 0, 'k': 4}, {'r': 0, 'k': 5}, {'typ': 'kontakt_nc', 'operand': '#X'})[0]
        m = model.proved(m, {'typ': 'vloz', 'misto': {'r': 0, 'k': 0},
                             'prvek': {'typ': 'porovnani', 'op': '>=', 'in1': '#A', 'in2': '5', 'sablona': 'Int'}})[0]
        self.assertEqual(shoda(dict(m, jazyk='LAD'), reparse(m)), (True, []))
        self.assertIn('{ S7_Templates := "SrcType := Int" }\n            GE_Contact(', z.lad_text(m))

    def test_lad_volani_fc_a_instrukce(self):
        m = {'prvky': [{'id': 'p1', 'typ': 'box', 'volani': 'fc', 'nazev': 'FC_Vetve', 'instance': None, 'sablona': None,
                        'napajeni': ['EN', 'ENO'], 'vstupy': [], 'vystupy': []},
                       {'id': 'p2', 'typ': 'box', 'volani': 'instrukce', 'nazev': 'CONV', 'instance': None,
                        'sablona': 'Int to DInt', 'napajeni': ['EN', 'ENO'],
                        'vstupy': [{'pin': 'IN', 'operand': '#A'}], 'vystupy': [{'pin': 'OUT', 'operand': '#B'}]}],
             'rungy': [{'z': 'powerrail', 'cesta': ['p1', 'p2'], 'do': None}]}
        t = z.lad_text(m)
        self.assertIn('"FC_Vetve"(  )', t)
        self.assertIn('Convert(\n                in := #A,\n                out => #B', t)
        r = reparse(m)
        self.assertEqual(r['prvky'][0]['instance'], '"FC_Vetve"')          # parser: volání; server ho dohledá jako FC
        self.assertEqual(rozdil.shoda_obsahu({'prvky': m['prvky'][1:], 'rungy': []},
                                             {'prvky': r['prvky'][1:], 'rungy': []}), (True, []))

    def test_fbd_operace(self):
        n = self.nw('FB_Bezpecnost', 3)
        m = {'jazyk': 'FBD', 'prvky': n['prvky']}
        for op in ({'typ': 'pridej_vstup', 'id': 'b1', 'operand': '#Novy'},
                   {'typ': 'negace', 'id': 'b1', 'vstup': 3},
                   {'typ': 'vloz_box', 'prvek': 'OR', 'do': {'id': 'b1', 'vstup': 2}},
                   {'typ': 'zmen_operand', 'id': 'n1', 'vstup': 2, 'operand': '#Servis'},
                   {'typ': 'vloz_box', 'prvek': 'SR'},
                   {'typ': 'zmen_operand', 'id': 'n2', 'operand': '#Pamet'},
                   {'typ': 'zmen_operand', 'id': 'n2', 'vstup': 1, 'operand': '#Set'},
                   {'typ': 'zmen_operand', 'id': 'n2', 'vstup': 2, 'operand': '#Reset'}):
            m = fbd.proved(m, op)[0]
        self.assertEqual(shoda(m, reparse(m)), (True, []))

    def test_fbd_negace_hlavniho_vystupu(self):
        n = self.nw('FB_Bezpecnost', 2)                     # O -> wire#w1 -> A in2
        m = fbd.proved({'jazyk': 'FBD', 'prvky': n['prvky']}, {'typ': 'negace', 'id': 'b3', 'vystup': 'hlavni'})[0]
        r = reparse(m)
        self.assertIn('in2 := NOT wire#w1', z.fbd_text(m))   # zapíše se jako negace vstupu spotřebiče
        a = next(p for p in r['prvky'] if p['typ'] == 'and')
        self.assertTrue(a['vstupy'][1]['neg'])

    def test_fbd_cyklus(self):
        m = {'jazyk': 'FBD', 'prvky': [
            {'id': 'b1', 'typ': 'and', 'vstupy': [{'n': 1, 'pin': 'in1', 'operand': '', 'signal': 'b2', 'neg': False}],
             'vystupy': []},
            {'id': 'b2', 'typ': 'and', 'vstupy': [{'n': 1, 'pin': 'in1', 'operand': '', 'signal': 'b1', 'neg': False}],
             'vystupy': []}]}
        with self.assertRaises(z.ChybaZapisu):
            z.fbd_text(copy.deepcopy(m))


if __name__ == '__main__':
    unittest.main()
