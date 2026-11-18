"""Compare two exports of a block network by network (file level): netdiff, /api/compare-exports, MCP tool."""
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest

import netdiff
import server
from tests.common import SAMPLES, PROJECT, TS0, TS1

HEAD = '{\n    S7_BlockNumber := "5";\n    S7_PreferredLanguage := "LAD"\n}\nFUNCTION_BLOCK "FB_T"\n    VAR_INPUT\n        A : Bool;\n    END_VAR\n\n'


def nw(title, body, comment=None):
    a = 'S7_Language := "LAD";\n        S7_NetworkTitle := "%s"' % title
    if comment:
        a += ';\n        S7_NetworkComment := "%s"' % comment
    return '    {\n        %s\n    }\n    NETWORK\n        RUNG wire#powerrail\n            %s\n        END_RUNG\n    END_NETWORK\n' % (a, body)


def block(nets, texts):
    return HEAD + ''.join(nets) + 'END_FUNCTION_BLOCK\n', 'MultiLingualTexts:\n' + ''.join(
        '  - id: %s\n    en-US: %s\n' % kv for kv in texts)


class TestNetdiff(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        a = block([nw('MLC_1', 'Contact( #A )\n            Coil( #X )', 'MLC_9'), nw('MLC_2', 'Contact( #A )\n            Coil( #Y )'),
                   nw('MLC_3', 'Coil( #Z )')], [('MLC_1', 'First'), ('MLC_2', 'Second'), ('MLC_3', 'Third'), ('MLC_9', 'note')])
        # B: new network first, NW 1 unchanged code but new title + comment, NW 2 code changed, Third removed;
        # text ids renumbered (MLC_7...) must not count as a difference
        b = block([nw('MLC_4', 'Coil( #N )'), nw('MLC_5', 'Contact( #A )\n            Coil( #X )', 'MLC_6'),
                   nw('MLC_7', 'Contact( #A )\n            I_Contact( #B )\n            Coil( #Y )')],
                  [('MLC_4', 'New'), ('MLC_5', 'First renamed'), ('MLC_6', 'other note'), ('MLC_7', 'Second')])
        cls.a, cls.b = (os.path.join(cls.dir, n) for n in ('a.s7dcl', 'b.s7dcl'))
        for p, (code, res) in ((cls.a, a), (cls.b, b)):
            open(p, 'w', encoding='utf-8').write(code)
            open(p[:-6] + '.s7res', 'w', encoding='utf-8').write(res)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_networks(self):
        r = netdiff.compare(self.a, self.b, [(2, 3)])
        self.assertEqual(r['summary'], {'same': 0, 'changed': 2, 'added': 1, 'removed': 1, 'networks_a': 3, 'networks_b': 3})
        got = [(n['nw_a'], n['nw_b'], n['status']) for n in r['networks']]
        self.assertEqual(got, [(None, 1, 'added'), (1, 2, 'changed'), (2, 3, 'changed'), (3, None, 'removed')])
        t = r['networks'][1]                      # code equal (ids masked), only the texts differ
        self.assertFalse(t['code_changed'])
        self.assertEqual((t['title_change'], t['comment_change']), (['First', 'First renamed'], ['note', 'other note']))
        self.assertNotIn('diff', t)
        d = r['networks'][2]
        self.assertTrue(d['code_changed'])
        self.assertIn('+I_Contact( #B )', d['diff'].replace('            ', ''))

    def test_same_and_masking(self):
        r = netdiff.compare(self.a, self.a)
        self.assertEqual(r['summary']['same'], 3)
        self.assertEqual(r['networks'], [])

    def test_api(self):
        srv = server.create_server(0, self.dir, tempfile.mkdtemp())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            def get(q):
                c = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=30)
                c.request('GET', '/api/compare-exports?' + q)
                x = c.getresponse()
                return x.status, json.loads(x.read())
            s, r = get('a=%s&b=%s&detail=2:3' % (self.a, self.b))
            self.assertEqual((s, r['summary']['changed'], 'diff' in r['networks'][2]), (200, 2, True))
            self.assertEqual(get('a=%s&b=%s' % (self.a, self.dir + '/nope.s7dcl'))[0], 404)
            self.assertEqual(get('a=%s&b=%s' % (self.a, os.path.abspath(__file__)[:-3] + '.s7dcl'))[0], 404)
            outside = os.path.join(tempfile.gettempdir(), 'outside_x.s7dcl')
            shutil.copy(self.a, outside)
            try:
                self.assertEqual(get('a=%s&b=%s' % (outside, self.b))[0], 404)      # outside the export roots
            finally:
                os.remove(outside)
            self.assertEqual(get('a=%s&b=%s&detail=x' % (self.a, self.b))[0], 400)
            self.assertEqual(get('a=20261012-120000&b=20261012-130000')[0], 404)     # snapshot needs project + block
        finally:
            srv.shutdown()
            srv.server_close()

    def test_snapshots(self):
        root = tempfile.mkdtemp()
        shutil.copytree(os.path.join(SAMPLES, PROJECT), os.path.join(root, PROJECT))
        try:
            r = server.api_compare_exports(root, PROJECT, TS0, TS1, 'FB_Conveyor', '1:1')
            self.assertEqual([(n['status'], n['code_changed']) for n in r['networks']], [('changed', True)])
            self.assertIn('Fuse_OK', r['networks'][0]['diff'])
            with self.assertRaises(KeyError):
                server.api_compare_exports(root, PROJECT, TS0, TS1, 'FB_Nope')
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
