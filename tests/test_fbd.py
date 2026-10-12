"""F1 — FBD: parser, rozložení, operace, rozdíl, API (vzorek FB_Bezpecnost, smyšlená jména)."""
import copy
import http.client
import json
import shutil
import tempfile
import threading
import unittest

from tests.spolecne import VZORKY, PROJEKT
import fbd
import rozdil
import s7dcl
import server

BLOK = 'FB_Bezpecnost'


def nw(n):
    _, b = server._nacti(VZORKY, PROJEKT, BLOK)
    return b['networky'][n - 1]


def typy(n):
    return [p['typ'] for p in nw(n)['prvky']]


def vstupy(p):
    return [(v['pin'], v['operand'], v['signal'], v['neg']) for v in p['vstupy']]


def obal(text):
    return '    NETWORK\n' + text + '\n    END_NETWORK\n'


class TestParserFBD(unittest.TestCase):
    def test_vsechny_networky(self):
        _, b = server._nacti(VZORKY, PROJEKT, BLOK)
        self.assertEqual((b['druh'], b['cislo'], b['jazyk'], len(b['networky'])), ('FB', 30, 'FBD', 9))
        for n in b['networky']:
            self.assertEqual(n['jazyk'], 'FBD')
            self.assertNotIn('chyba', n, 'NW%d' % n['nw'])
            fbd.over_model(fbd.model(n['prvky']))

    def test_bitova_logika(self):
        p = nw(1)['prvky']
        self.assertEqual([x['typ'] for x in p], ['or', 'and', 'assign'])
        self.assertEqual(vstupy(p[0]), [('in1', '#Start', None, False), ('in2', '#Servis', None, False)])
        self.assertEqual(vstupy(p[1])[:2], [('in1', '', 'b1', False), ('in2', '#Porucha', None, True)])
        self.assertEqual([v['n'] for v in p[1]['vstupy']], [1, 2, 3])
        self.assertEqual((p[2]['operand'], p[2]['vstupy'][0]['signal']), ('#Povoleni', 'b2'))

    def test_wire_vstup_z_jineho_rungu(self):
        p = nw(2)['prvky']
        self.assertEqual(typy(2), ['and', 'assign', 'or'])
        self.assertEqual(p[0]['vstupy'][1]['signal'], 'b3')            # in2 := wire#w1 <- END_RUNG wire#w1
        self.assertEqual(p[2]['vstupy'][2], {'n': 3, 'pin': 'in3', 'operand': '#Blokace', 'signal': None, 'neg': True})
        self.assertEqual(fbd.signaly(p), [{'z': 'b3', 'do': 'b1', 'vstup': 2}, {'z': 'b1', 'do': 'b2', 'vstup': 1}])

    def test_odbocka_a_retez_prirazeni(self):
        p = nw(3)['prvky']
        self.assertEqual(typy(3), ['and', 'assign', 'assign', 'assign'])
        self.assertEqual(p[2]['vstupy'][0]['signal'], 'b2')            # řetěz přiřazení
        self.assertEqual(vstupy(p[3]), [('in1', '', 'b1', True)])     # odbočka wire#w2 za & + NOT Coil

    def test_volani_f_bloku(self):
        f, h = nw(4)['prvky']
        self.assertEqual((f['typ'], f['nazev'], f['instance']), ('volani', 'ESTOP1', '#NouzStop'))
        self.assertEqual(f['atributy'], {'f_user_card': '1', 'f_image_card': '0'})
        self.assertNotIn('EN', [v['pin'] for v in f['vstupy']])        # F-blok bez napájení nemá EN
        v = {x['pin']: x for x in f['vstupy']}
        self.assertTrue(v['ACK_NEC']['neg'])
        self.assertEqual((v['ACK']['signal'], v['TIME_DEL']['operand']), ('b2', ''))
        self.assertEqual([x['pin'] for x in f['vystupy']], ['Q', 'Q_DELAY', 'ACK_REQ', 'DIAG'])
        self.assertEqual((h['typ'], h['operand'], vstupy(h)), ('ptrig', '#Kvitace_Pamet', [('CLK', '#Kvitace', None, False)]))
        d = nw(5)['prvky'][0]
        self.assertEqual([x['pin'] for x in d['vstupy'] if x['neg']], ['QBAD_IN1', 'QBAD_IN2'])

    def test_s_r_sr_true_move_xor_prazdny(self):
        p = nw(6)['prvky']
        self.assertEqual(typy(6), ['s', 'r', 'sr', 'assign'])
        self.assertEqual((p[2]['operand'], [v['pin'] for v in p[2]['vstupy']]), ('#Pamet_SR', ['S', 'R1']))
        p = nw(7)['prvky']
        self.assertEqual(p[0]['vstupy'][0]['operand'], 'true')
        self.assertEqual((p[1]['typ'], p[1]['nazev'], vstupy(p[1])[0]), ('instrukce', 'MOVE', ('EN', 'TRUE', None, False)))
        self.assertEqual(typy(8), ['xor', 'ntrig', 'assign'])
        self.assertEqual(nw(9)['prvky'], [])

    def test_konstrukce_z_exportu(self):
        # NOT před prvkem = negovaný 1. vstup; RUNG bez operandu + Coil = přiřazení s otevřeným vstupem
        b = s7dcl.parse_blok('FUNCTION_BLOCK "X"\n    { S7_Language := "FBD" }\n' + obal(
            '        RUNG "a"\n            NOT A( in2 := NOT "b" )\n            Coil( "c" )\n        END_RUNG\n'
            '        RUNG\n            Coil( "d" )\n        END_RUNG wire#w1\n'
            '        RUNG "e"\n            O( in2 := wire#w1 )\n        END_RUNG') + 'END_FUNCTION_BLOCK\n')
        p = b['networky'][0]['prvky']
        self.assertEqual(vstupy(p[0]), [('in1', '"a"', None, True), ('in2', '"b"', None, True)])
        self.assertEqual(vstupy(p[2]), [('in1', '', None, False)])
        self.assertEqual(p[3]['vstupy'][1]['signal'], 'b3')

    def test_neznamy_vodic_neshodi_blok(self):
        b = s7dcl.parse_blok('FUNCTION_BLOCK "X"\n    { S7_Language := "FBD" }\n' + obal(
            '        RUNG "a"\n            A( in2 := wire#w9 )\n        END_RUNG') + 'END_FUNCTION_BLOCK\n')
        self.assertIn('w9', b['networky'][0]['chyba'])


class TestRozlozeniFBD(unittest.TestCase):
    def roz(self, n):
        return fbd.rozloz(nw(n)['prvky'], 900)

    def test_vsechny_bez_vyjimky(self):
        for n in range(1, 10):
            r, k = self.roz(n)
            self.assertTrue(k['fbd'])
            self.assertEqual(set(r), {p['id'] for p in nw(n)['prvky']})

    def test_retez_zleva_doprava_v_jedne_lince(self):
        r, k = self.roz(1)
        self.assertEqual([r[i]['c'] for i in ('b1', 'b2', 'b3')], [0, 1, 2])
        self.assertEqual(r['b1']['r'], r['b2']['r'])                    # výstup >=1 = 1. vstup &
        self.assertEqual(r['b2']['r'], r['b3']['r'])
        b = {d['id']: d for d in k['boxy']}
        self.assertEqual((b['b1']['titul'], b['b2']['titul'], b['b3']['titul'], b['b3']['nad']), ('>=1', '&', '=', '#Povoleni'))
        self.assertTrue(b['b2']['vstupy'][1]['neg'])
        self.assertIsNone(b['b2']['vstupy'][0]['operand'])               # napájený signálem: bez popisku
        self.assertEqual(k['uzly'], [])

    def test_box_na_druhem_vstupu(self):
        r, _ = self.roz(2)
        self.assertEqual((r['b3']['c'], r['b1']['c'], r['b2']['c']), (0, 1, 2))
        self.assertEqual(r['b3']['r'], r['b1']['r'] + 1)                 # výstup >=1 na lince in2 boxu &

    def test_odbocka_s_uzlem(self):
        r, k = self.roz(3)
        self.assertEqual(r['b4']['c'], r['b2']['c'])                     # větev za & ve stejném sloupci
        self.assertGreater(r['b4']['r'], r['b3']['r'])
        self.assertEqual(len(k['uzly']), 1)

    def test_prazdny_a_cyklus(self):
        _, k = self.roz(9)
        self.assertTrue(k['prazdny'])
        p = copy.deepcopy(nw(1)['prvky'])
        p[0]['vstupy'][0]['signal'] = 'b3'
        with self.assertRaises(fbd.Nerozlozitelne):
            fbd.rozloz(p)


class TestOperaceARozdilFBD(unittest.TestCase):
    def test_operace(self):
        m = fbd.model(nw(1)['prvky'])
        m, i = fbd.proved(m, {'typ': 'pridej_vstup', 'id': 'b2', 'operand': '#Zamek_OK'})
        self.assertEqual(m['prvky'][1]['vstupy'][-1], {'n': 4, 'pin': 'in4', 'operand': '#Zamek_OK', 'signal': None, 'neg': False})
        m, _ = fbd.proved(m, {'typ': 'zmen_operand', 'id': 'b3', 'operand': '#Jine'})
        m, _ = fbd.proved(m, {'typ': 'negace', 'id': 'b2', 'vstup': 2})
        self.assertEqual((m['prvky'][2]['operand'], m['prvky'][1]['vstupy'][1]['neg']), ('#Jine', False))
        m, _ = fbd.proved(m, {'typ': 'zmen_operand', 'id': 'b2', 'vstup': 1, 'operand': '#Prime'})
        self.assertEqual(vstupy(m['prvky'][1])[0], ('in1', '#Prime', None, False))      # signál nahrazen operandem
        self.assertEqual([s['do'] for s in m['signaly']], ['b3'])
        for spatna in ({'typ': 'pridej_vstup', 'id': 'b3'}, {'typ': 'negace', 'id': 'b2'}, {'typ': 'nic', 'id': 'b1'},
                       {'typ': 'negace', 'id': 'b9', 'vstup': 1}, {'typ': 'negace', 'id': 'b2', 'vstup': 7}):
            with self.assertRaises(fbd.ChybaOperace):
                fbd.proved(m, spatna)
        self.assertEqual(fbd.validuj(m), [])
        m['prvky'][2]['operand'] = ''
        self.assertEqual([c['id'] for c in fbd.validuj(m)], ['b3'])

    def test_over_model(self):
        p = copy.deepcopy(nw(1)['prvky'])
        p[2]['vstupy'][0]['signal'] = 'b9'
        with self.assertRaises(ValueError):
            fbd.over_model({'jazyk': 'FBD', 'prvky': p})
        with self.assertRaises(ValueError):
            fbd.over_model({'jazyk': 'FBD', 'prvky': [{'id': 'b1', 'typ': 'kontakt', 'vstupy': [], 'vystupy': []}]})

    def test_rozdil_a_prizrak(self):
        pred = fbd.model(nw(3)['prvky'])
        po, _ = fbd.proved(pred, {'typ': 'pridej_vstup', 'id': 'b1', 'operand': '#Novy'})
        po['prvky'] = [p for p in po['prvky'] if p['id'] != 'b4']               # smazaná červená kontrolka
        po['prvky'].append({'id': 'n1', 'typ': 'assign', 'operand': '#X', 'vystupy': [],
                            'vstupy': [{'n': 1, 'pin': 'in1', 'operand': '', 'signal': 'b2', 'neg': False}]})
        zmeny, smazane = rozdil.porovnej(pred, po)
        self.assertEqual((zmeny, smazane), ({'b1': 'zmeneny', 'n1': 'pridany'}, ['b4']))
        self.assertEqual(fbd.zmeny_pinu(pred, po, {'b1'}), {'b1': {'vstupy': [3], 'vystupy': [], 'nad': False}})
        prvky = fbd.slouc(pred, po, smazane)
        self.assertEqual(prvky[-1]['id'], 'x:b4')
        self.assertEqual(prvky[-1]['vstupy'][0]['signal'], 'b1')               # přízrak visí na odbočce za &
        r, k = fbd.rozloz(prvky)
        self.assertIn('x:b4', r)


class TestApiFBD(unittest.TestCase):
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

    def test_network_a_navrh(self):
        s, n = self.req('GET', '/api/network?projekt=%s&blok=%s&nw=1' % (PROJEKT, BLOK))
        self.assertEqual((s, n['jazyk'], n['kresba']['fbd'], len(n['signaly'])), (200, 'FBD', True, 2))
        # Claude: zkouška operací (nic neukládá) -> návrh
        s, r = self.req('POST', '/api/rozlozeni', {'projekt': PROJEKT, 'po': {'jazyk': 'FBD', 'prvky': n['prvky']},
                                                    'pred': {'jazyk': 'FBD', 'prvky': n['prvky']},
                                                    'ops': [{'typ': 'pridej_vstup', 'id': 'b2', 'operand': '#Zamek_OK'},
                                                            {'typ': 'zmen_operand', 'id': 'b3', 'operand': '#Povoleni_Pohon'}]})
        self.assertEqual((s, r['rozdil'], r['chyby'], r['posledni']), (200, {'b2': 'zmeneny', 'b3': 'zmeneny'}, [], 'b3'))
        self.assertEqual(r['rozdil_piny']['b2']['vstupy'], [4])
        s, u = self.req('POST', '/api/uprava', {'projekt': PROJEKT, 'kolo': 'F1', 'blok': BLOK, 'nw': 1,
                                                'po': {'prvky': r['po']['prvky']}, 'cislo_bloku': 999, 'titulek': 'x'})
        self.assertEqual((s, u['stav'], u['jazyk'], u['cislo_bloku'], u['titulek']),
                         (201, 'Proposal', 'FBD', 30, 'Povolení — >=1, & s negací, přiřazení'))
        self.assertEqual((u['pred']['jazyk'], u['pred']['export_ts'], u['po']['jazyk']), ('FBD', '20261012-120000', 'FBD'))
        self.assertEqual(len(u['po']['signaly']), 2)
        s, d = self.req('GET', '/api/uprava/%s' % u['id'])
        self.assertEqual((s, d['jazyk'], d['kresba']['fbd'], d['rozdil'], d['chyby']),
                         (200, 'FBD', True, {'b2': 'zmeneny', 'b3': 'zmeneny'}, []))
        # uložení „po" editorem: jazyk se nemění
        s, _ = self.req('POST', '/api/uprava/%s/po' % u['id'], {'po': {'prvky': [], 'rungy': []}})
        self.assertEqual(s, 400)
        s, x = self.req('POST', '/api/uprava/%s/po' % u['id'], {'po': {'jazyk': 'FBD', 'prvky': n['prvky']}})
        self.assertEqual(s, 200)
        # kontrola exportem pro FBD až v F2 — nesmí spadnout
        self.req('POST', '/api/uprava/%s/stav' % u['id'], {'stav': 'Approved', 'kdo': 'test'})
        s, k = self.req('POST', '/api/uprava/%s/kontrola-exportem' % u['id'], {})
        self.assertEqual((s, k['shoda']), (200, False))
        self.assertIn('F2', k['poznamka'])

    def test_spatny_navrh(self):
        p = {'jazyk': 'FBD', 'prvky': [{'id': 'b1', 'typ': 'assign', 'operand': '#X', 'vystupy': [],
                                       'vstupy': [{'pin': 'in1', 'operand': '', 'signal': 'b7'}]}]}
        s, e = self.req('POST', '/api/uprava', {'projekt': PROJEKT, 'blok': BLOK, 'nw': 1, 'po': p})
        self.assertEqual(s, 400)
        self.assertIn('b7', e['chyba'])
        s, _ = self.req('POST', '/api/rozlozeni', {'po': {'jazyk': 'FBD', 'prvky': nw(1)['prvky']},
                                                   'ops': [{'typ': 'pridej_vstup', 'id': 'b3'}]})
        self.assertEqual(s, 400)


if __name__ == '__main__':
    unittest.main()
