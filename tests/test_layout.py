import unittest

from tests.common import SAMPLES, PROJECT, TS0
import layout
import server


def roz(block, n, width=0):
    _, b = server._load(SAMPLES, PROJECT, block, TS0)
    x = b['networks'][n - 1]
    r, k = layout.layout(x['rungs'], x['elements'], width)
    return r, k, {d['id']: d for d in k['elements']}


class TestLayout(unittest.TestCase):
    def test_series_coil_right(self):
        r, k, _ = roz('FB_Conveyor', 1)
        self.assertEqual([r[p]['c'] for p in ('p1', 'p2', 'p3')], [0, 1, 2])
        self.assertEqual(len({v['r'] for v in r.values()}), 1)               # everything on one rung
        self.assertEqual(r['p4']['c'], max(v['c'] for v in r.values()))      # coil in the last column

    def test_coil_at_right_edge(self):
        _, k, d = roz('FB_Conveyor', 1, 1000)
        self.assertGreaterEqual(k['width'], 1000)
        self.assertGreater(d['p4']['x'], 800)

    def test_latch_two_branches(self):
        r, k, _ = roz('FB_Conveyor', 2)
        # branches below the main one, aligned to the section start (column 0)
        self.assertEqual((r['p1']['c'], r['p5']['c'], r['p6']['c']), (0, 0, 0))
        self.assertTrue(r['p1']['r'] < r['p5']['r'] < r['p6']['r'])
        self.assertEqual(r['p7']['r'], r['p6']['r'])
        # join after the longest branch: Stop only after it, back on the main rung
        self.assertGreater(r['p2']['c'], r['p7']['c'])
        self.assertEqual(r['p2']['r'], r['p1']['r'])
        vertical = [c for c in k['lines'] if c[0] == c[2]]
        self.assertEqual(len(vertical), 2)                                     # section start (rail) + end

    def test_ton_box_in_main_branch(self):
        r, k, d = roz('FB_Conveyor', 3)
        self.assertEqual(r['p2']['r'], r['p1']['r'])
        self.assertEqual(len(d['p2']['rows']), 2)          # IN, PT
        self.assertEqual(d['p2']['rows'][0], d['p1']['y'])  # IN on the rung

    def test_feed_to_pin(self):
        r, k, d = roz('FB_Conveyor', 4)
        box = r['p2']
        self.assertLess(r['p3']['c'], box['c'])
        self.assertEqual(r['p4']['c'], box['c'] - 1)                    # the feed is adjacent to the box
        self.assertGreater(r['p3']['r'], box['r'])
        self.assertEqual(d['p2']['rows'][1], d['p3']['y'])             # pin Enable on the feed line

    def test_nested_branch(self):
        r, _, _ = roz('FC_Branches', 1)
        self.assertEqual(r['p4']['c'], r['p2']['c'])                    # Sensor_C parallel to Sensor_B
        self.assertGreater(r['p4']['r'], r['p2']['r'])
        self.assertEqual((r['p5']['c'], r['p6']['c']), (0, 1))          # OR at the start

    def test_output_branches(self):
        r, _, _ = roz('FC_Branches', 2)
        self.assertEqual(r['p3']['c'], r['p4']['c'])                    # MOVE and contact from the same node
        self.assertEqual(r['p2']['c'], r['p5']['c'])                    # coils aligned right
        self.assertTrue(r['p2']['r'] < r['p3']['r'] < r['p4']['r'])

    def test_two_rungs(self):
        r, _, _ = roz('FC_Branches', 5)
        self.assertEqual((r['p2']['c'], r['p3']['c']), (1, 2))
        self.assertGreater(r['p4']['r'], r['p3']['r'])
        self.assertEqual(r['p4']['c'], 0)

    def test_bridge_fails(self):
        _, b = server._load(SAMPLES, PROJECT, 'FC_Branches')
        x = b['networks'][3]
        with self.assertRaises(layout.LayoutError):
            layout.layout(x['rungs'], x['elements'])

    def test_empty(self):
        r, k, _ = roz('FB_Conveyor', 8)
        self.assertEqual(r, {})
        self.assertEqual(k['elements'], [])
        self.assertTrue(k['empty'])                        # the page appends "(empty network)"


if __name__ == '__main__':
    unittest.main()
