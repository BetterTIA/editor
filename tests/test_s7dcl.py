import glob
import os
import tempfile
import unittest

from tests.common import SAMPLES, PROJECT, TS0
import s7dcl
import server


def nw(block, n):
    _, b = server._load(SAMPLES, PROJECT, block, TS0)
    return b['networks'][n - 1]


class TestParser(unittest.TestCase):
    def test_all_samples(self):
        files = glob.glob(os.path.join(SAMPLES, '**', '*.s7dcl'), recursive=True)
        self.assertGreaterEqual(len(files), 4)
        for f in files:
            b = s7dcl.load_block(f)
            self.assertIn(b['kind'], ('FB', 'FC', 'DB'))

    def test_header_and_titles(self):
        _, b = server._load(SAMPLES, PROJECT, 'FB_Dopravnik', TS0)
        self.assertEqual((b['kind'], b['name'], b['number']), ('FB', 'FB_Dopravnik', 10))
        self.assertEqual(len(b['networks']), 8)
        self.assertEqual(b['networks'][0]['title'], 'Připravenost dopravníku')
        self.assertEqual(b['networks'][3]['title'], 'Volání pásu (instance DB_Pas1)')   # hodnota v '...'
        self.assertEqual(b['networks'][1]['comment'], 'Start nebo servisní start, Stop rozpíná.\nPřídržná větev přes Motor.')
        self.assertEqual(b['networks'][7]['title'], '')

    def test_series(self):
        n = nw('FB_Dopravnik', 1)
        self.assertEqual([p['type'] for p in n['elements']], ['contact'] * 3 + ['coil'])
        self.assertEqual(n['elements'][1]['operand'], '"DB_Stroj".Bezpecnost_OK')
        self.assertEqual(n['rungs'], [{'from': 'powerrail', 'path': ['p1', 'p2', 'p3', 'p4'], 'to': None}])

    def test_branches_wire(self):
        n = nw('FB_Dopravnik', 2)
        self.assertEqual(len(n['rungs']), 3)
        self.assertEqual(n['rungs'][0]['path'], ['p1', 'wire#w1', 'p2', 'p3', 'p4'])
        self.assertEqual([r['to'] for r in n['rungs']], [None, 'w1', 'w1'])
        self.assertEqual(n['elements'][1]['type'], 'contact_nc')
        v = s7dcl.wires(n)
        self.assertIn({'from': 'p5', 'to': 'w1'}, v)
        self.assertIn({'from': 'w1', 'to': 'p2'}, v)

    def test_ton(self):
        p = nw('FB_Dopravnik', 3)['elements'][1]
        self.assertEqual((p['type'], p['name'], p['instance'], p['template']), ('box', 'TON', '#T_Rozbeh', 'Time'))
        self.assertEqual(p['power'], ['IN', 'Q'])
        self.assertEqual(p['inputs'], [{'pin': 'PT', 'operand': 'T#2s'}])
        self.assertEqual(p['outputs'], [{'pin': 'ET', 'operand': '#Rozbeh_Cas'}])

    def test_call_fb(self):
        n = nw('FB_Dopravnik', 4)
        p = n['elements'][1]
        self.assertEqual((p['call'], p['instance'], p['name']), ('fb', '"DB_Pas1"', 'FB_Pas'))
        self.assertEqual([x['pin'] for x in p['inputs']], ['Povoleni', 'Smer', 'Rychlost', 'Reset'])
        self.assertEqual(p['inputs'][0]['operand'], 'wire#w1')
        self.assertEqual(p['inputs'][3]['operand'], '')             # prázdný pin
        self.assertEqual(p['outputs'][2], {'pin': 'Stav', 'operand': ''})
        self.assertIn({'from': 'w1', 'to': 'p2.Povoleni'}, s7dcl.wires(n))

    def test_comparison(self):
        a, b, _ = nw('FB_Dopravnik', 5)['elements']
        self.assertEqual((a['type'], a['op'], a['in1'], a['in2'], a['template']),
                         ('comparison', '<>', '"DB_Stroj".Rezim', '0', 'Int'))
        self.assertEqual((b['op'], b['template']), ('>', 'Real'))

    def test_move(self):
        p = nw('FB_Dopravnik', 6)['elements'][1]
        self.assertEqual((p['name'], p['power']), ('MOVE', ['EN', 'ENO']))
        self.assertEqual(p['inputs'], [{'pin': 'IN', 'operand': '1500'}])
        self.assertEqual(p['outputs'], [{'pin': 'OUT1', 'operand': '"DB_Stroj".Zadana_Rychlost'}])

    def test_coils_and_trig(self):
        elements = nw('FB_Dopravnik', 7)['elements']
        self.assertEqual([p['type'] for p in elements],
                         ['contact', 'coil_s', 'contact', 'coil', 'contact', 'box', 'coil_r'])
        self.assertEqual((elements[5]['name'], elements[5]['below'], elements[5]['power']), ('P_TRIG', '#Kvitace_P', ['CLK', 'Q']))

    def test_fbd_own_model(self):
        n = nw('FC_Vetve', 3)
        self.assertEqual(n['language'], 'FBD')
        self.assertNotIn('rungs', n)                                     # FBD nemá LAD rungy, ale boxy (F1)
        self.assertEqual([p['type'] for p in n['elements']], ['or', 'assign'])

    def test_commas_in_quotes(self):
        self.assertEqual(s7dcl.split_commas('a := "x,y", b := c[1,2], d =>  '), ['a := "x,y"', 'b := c[1,2]', 'd =>'])

    def test_s7res(self):
        with tempfile.NamedTemporaryFile('w', suffix='.s7res', delete=False, encoding='utf-8') as f:
            f.write("MultiLingualTexts:\n  - id: MLC_a\n    en-US: 'It''s'\n"
                    "  - id: MLC_b\n    en-US: |+\n      line 1\n      line 2\n"
                    "  - id: MLC_c\n    cs-CZ: česky\n    en-US: anglicky\n")
        try:
            t = s7dcl.load_s7res(f.name)
        finally:
            os.remove(f.name)
        self.assertEqual(t['MLC_a']['en-US'], "It's")
        self.assertEqual(t['MLC_b']['en-US'], 'line 1\nline 2')
        self.assertEqual(s7dcl.text_mlc(t, 'MLC_c'), 'česky')
        self.assertEqual(s7dcl.text_mlc(t, 'MLC_neni'), '')


if __name__ == '__main__':
    unittest.main()
