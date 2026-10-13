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
        _, b = server._load(SAMPLES, PROJECT, 'FB_Conveyor', TS0)
        self.assertEqual((b['kind'], b['name'], b['number']), ('FB', 'FB_Conveyor', 10))
        self.assertEqual(len(b['networks']), 8)
        self.assertEqual(b['networks'][0]['title'], 'Připravenost dopravníku')
        self.assertEqual(b['networks'][3]['title'], 'Belt call (instance DB_Belt1)')   # value in '...'
        self.assertEqual(b['networks'][1]['comment'], 'Start or service start, Stop opens the circuit.\nHolding branch through Motor.')
        self.assertEqual(b['networks'][7]['title'], '')

    def test_series(self):
        n = nw('FB_Conveyor', 1)
        self.assertEqual([p['type'] for p in n['elements']], ['contact'] * 3 + ['coil'])
        self.assertEqual(n['elements'][1]['operand'], '"DB_Machine".Safety_OK')
        self.assertEqual(n['rungs'], [{'from': 'powerrail', 'path': ['p1', 'p2', 'p3', 'p4'], 'to': None}])

    def test_branches_wire(self):
        n = nw('FB_Conveyor', 2)
        self.assertEqual(len(n['rungs']), 3)
        self.assertEqual(n['rungs'][0]['path'], ['p1', 'wire#w1', 'p2', 'p3', 'p4'])
        self.assertEqual([r['to'] for r in n['rungs']], [None, 'w1', 'w1'])
        self.assertEqual(n['elements'][1]['type'], 'contact_nc')
        v = s7dcl.wires(n)
        self.assertIn({'from': 'p5', 'to': 'w1'}, v)
        self.assertIn({'from': 'w1', 'to': 'p2'}, v)

    def test_ton(self):
        p = nw('FB_Conveyor', 3)['elements'][1]
        self.assertEqual((p['type'], p['name'], p['instance'], p['template']), ('box', 'TON', '#T_Startup', 'Time'))
        self.assertEqual(p['power'], ['IN', 'Q'])
        self.assertEqual(p['inputs'], [{'pin': 'PT', 'operand': 'T#2s'}])
        self.assertEqual(p['outputs'], [{'pin': 'ET', 'operand': '#Startup_Time'}])

    def test_call_fb(self):
        n = nw('FB_Conveyor', 4)
        p = n['elements'][1]
        self.assertEqual((p['call'], p['instance'], p['name']), ('fb', '"DB_Belt1"', 'FB_Belt'))
        self.assertEqual([x['pin'] for x in p['inputs']], ['Enable', 'Direction', 'Speed', 'Reset'])
        self.assertEqual(p['inputs'][0]['operand'], 'wire#w1')
        self.assertEqual(p['inputs'][3]['operand'], '')             # empty pin
        self.assertEqual(p['outputs'][2], {'pin': 'State', 'operand': ''})
        self.assertIn({'from': 'w1', 'to': 'p2.Enable'}, s7dcl.wires(n))

    def test_comparison(self):
        a, b, _ = nw('FB_Conveyor', 5)['elements']
        self.assertEqual((a['type'], a['op'], a['in1'], a['in2'], a['template']),
                         ('comparison', '<>', '"DB_Machine".Mode', '0', 'Int'))
        self.assertEqual((b['op'], b['template']), ('>', 'Real'))

    def test_move(self):
        p = nw('FB_Conveyor', 6)['elements'][1]
        self.assertEqual((p['name'], p['power']), ('MOVE', ['EN', 'ENO']))
        self.assertEqual(p['inputs'], [{'pin': 'IN', 'operand': '1500'}])
        self.assertEqual(p['outputs'], [{'pin': 'OUT1', 'operand': '"DB_Machine".Speed_Setpoint'}])

    def test_coils_and_trig(self):
        elements = nw('FB_Conveyor', 7)['elements']
        self.assertEqual([p['type'] for p in elements],
                         ['contact', 'coil_s', 'contact', 'coil', 'contact', 'box', 'coil_r'])
        self.assertEqual((elements[5]['name'], elements[5]['below'], elements[5]['power']), ('P_TRIG', '#Ack_P', ['CLK', 'Q']))

    def test_fbd_own_model(self):
        n = nw('FC_Branches', 3)
        self.assertEqual(n['language'], 'FBD')
        self.assertNotIn('rungs', n)                                     # FBD has no LAD rungs but boxes (F1)
        self.assertEqual([p['type'] for p in n['elements']], ['or', 'assign'])

    def test_commas_in_quotes(self):
        self.assertEqual(s7dcl.split_commas('a := "x,y", b := c[1,2], d =>  '), ['a := "x,y"', 'b := c[1,2]', 'd =>'])

    def test_s7res(self):
        with tempfile.NamedTemporaryFile('w', suffix='.s7res', delete=False, encoding='utf-8') as f:
            f.write("MultiLingualTexts:\n  - id: MLC_a\n    en-US: 'It''s'\n"
                    "  - id: MLC_b\n    en-US: |+\n      line 1\n      line 2\n"
                    "  - id: MLC_c\n    cs-CZ: česky\n    en-US: English\n")
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
