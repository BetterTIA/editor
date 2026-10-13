"""D3: snapshots of a project and their comparison (blocks -> networks), the drawn difference, "matches edit"."""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

from tests.common import ROOT, SAMPLES, PROJECT, TS0, TS1, TS2, TS_IF
import mcp_client
import server

TS3 = '20261012-160000'      # FB_Conveyor of TS1 with a new network inserted before NW 1 (partial)
TS4 = '20261012-170000'      # full export = TS0 without FB_Belt
NEW_NETWORK = '''    NETWORK
        RUNG wire#powerrail
            Contact( #Start )
            Coil( #Moving )
        END_RUNG
    END_NETWORK
'''


def blocks(r):
    return {b['block']: b for b in r['blocks']}


class TestCompare(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        p = os.path.join(cls.root, PROJECT)
        shutil.copytree(os.path.join(SAMPLES, PROJECT), p)
        conv = os.path.join(p, TS3, 'PLC_1', 'Blocks', 'Conveyor')
        shutil.copytree(os.path.join(p, TS1, 'PLC_1', 'Blocks', 'Conveyor'), conv)
        f = os.path.join(conv, 'FB_Conveyor.s7dcl')
        with open(f, encoding='utf-8-sig') as x:
            t = x.read()
        i = t.index('    {\n        S7_Language')
        with open(f, 'w', encoding='utf-8') as x:
            x.write(t[:i] + NEW_NETWORK + t[i:])
        shutil.copytree(os.path.join(p, TS0), os.path.join(p, TS4))
        for e in ('.s7dcl', '.s7res'):
            os.remove(os.path.join(p, TS4, 'PLC_1', 'Blocks', 'Conveyor', 'FB_Belt' + e))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def compare(self, a, b, block=None):
        return server.api_compare(self.root, '', PROJECT, a, b, block)

    def test_snapshots(self):
        s = server.api_snapshots(self.root, PROJECT)['snapshots']
        self.assertEqual([(x['ts'], x['full'], x['block_count']) for x in s],
                         [(TS4, True, 5), (TS3, False, 1), (TS2, False, 1), (TS1, False, 1), (TS_IF, False, 1), (TS0, True, 6)])
        self.assertEqual(s[1]['blocks'], ['FB_Conveyor'])
        self.assertNotIn('blocks', s[0])

    def test_conveyor_nw1_changed(self):
        r = self.compare(TS0, TS1)
        self.assertEqual(list(blocks(r)), ['FB_Conveyor'])
        b = blocks(r)['FB_Conveyor']
        self.assertEqual((b['status'], b['from_ts'], b['to_ts'], b['kind'], b['number'], b['notes']),
                         ('changed', TS0, TS1, 'FB', 10, []))
        self.assertEqual([(n['nw_from'], n['nw_to'], n['status'], n['diffs']) for n in b['networks']],
                         [(1, 1, 'changed', ['added: contact "Fuse_OK"'])])

    def test_safety_and_overlay(self):
        b = blocks(self.compare(TS1, TS2))
        self.assertEqual(list(b), ['FB_Safety'])                       # FB_Conveyor is the same copy (TS1) in both
        s = b['FB_Safety']
        self.assertEqual(s['notes'], ['interface: + Enable_Drive : Bool'])
        self.assertEqual([(n['nw_from'], n['status'], n['language']) for n in s['networks']], [(1, 'changed', 'FBD')])
        self.assertEqual(len(s['networks'][0]['diffs']), 2)
        self.assertIn('#Lock_OK', s['networks'][0]['diffs'][0])
        self.assertIn('#Enable_Drive', s['networks'][0]['diffs'][1])
        self.assertEqual(sorted(blocks(self.compare(TS0, TS2))), ['FB_Conveyor', 'FB_Safety'])
        self.assertEqual(list(blocks(self.compare(TS0, TS2, 'FB_Safety'))), ['FB_Safety'])
        self.assertEqual(self.compare(TS2, TS2)['blocks'], [])

    def test_network_moved(self):
        n = blocks(self.compare(TS1, TS3))['FB_Conveyor']['networks']
        self.assertEqual(n[0], {'nw_from': None, 'nw_to': 1, 'title': '', 'language': '', 'notes': [], 'diffs': [],
                                'matches': [], 'status': 'added'})
        self.assertEqual([(x['nw_from'], x['nw_to'], x['status']) for x in n[1:]], [(k, k + 1, 'moved') for k in range(1, 9)])
        self.assertEqual(n[1]['notes'], ['the network moved: NW 1 → NW 2'])
        self.assertEqual(n[8]['title'], '')                              # untitled NW 8 paired by content, not by number

    def test_full_export_removes_block(self):
        b = blocks(self.compare(TS0, TS4))
        self.assertEqual({k: v['status'] for k, v in b.items()}, {'FB_Belt': 'removed'})
        self.assertEqual((b['FB_Belt']['from_ts'], b['FB_Belt']['to_ts'], b['FB_Belt']['kind']), (TS0, None, 'FB'))
        self.assertEqual(blocks(self.compare(TS4, TS0))['FB_Belt']['status'], 'added')

    def test_errors(self):
        with self.assertRaises(KeyError):
            self.compare(TS0, '20990101-000000')
        with self.assertRaises(KeyError):
            self.compare(TS0, TS1, 'FB_Nope')
        with self.assertRaises(KeyError):
            server.api_snapshots(self.root, 'Nope')


class TestCompareApi(unittest.TestCase):
    """Through HTTP: matches edit, the drawn difference with align, 404s."""

    def setUp(self):
        self.root, self.data = tempfile.mkdtemp(), tempfile.mkdtemp()
        shutil.copytree(os.path.join(SAMPLES, PROJECT), os.path.join(self.root, PROJECT))
        self.srv = server.create_server(0, self.root, self.data)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=60)
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                  headers={'Content-Type': 'application/json'})
        r = c.getresponse()
        t = json.loads(r.read())
        c.close()
        return r.status, t

    def net(self, ts, block='FB_Conveyor'):
        return self.req('GET', '/api/network?project=%s&block=%s&nw=1&ts=%s' % (PROJECT, block, ts))[1]

    def test_matches_edit(self):
        n = self.net(TS1)
        s, u = self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'R', 'block': 'FB_Conveyor', 'nw': 1,
                                               'after': {'elements': n['elements'], 'rungs': n['rungs']}})
        self.assertEqual(s, 201, u)
        f = self.net(TS2, 'FB_Safety')
        s, v = self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'R', 'block': 'FB_Safety', 'nw': 1,
                                               'after': {'language': 'FBD', 'elements': f['elements']}})
        self.assertEqual(s, 201, v)
        s, r = self.req('GET', '/api/projects/%s/compare?from=%s&to=%s' % (PROJECT, TS0, TS2))
        self.assertEqual(s, 200, r)
        self.assertEqual({k: b['networks'][0]['matches'] for k, b in blocks(r).items()},
                         {'FB_Conveyor': [u['id']], 'FB_Safety': [v['id']]})
        s, r = self.req('GET', '/api/projects/%s/snapshots' % PROJECT)
        self.assertEqual((s, len(r['snapshots'])), (200, 4))
        self.assertEqual(self.req('GET', '/api/projects/%s/compare?from=%s&to=x' % (PROJECT, TS0))[0], 404)
        self.assertEqual(self.req('GET', '/api/projects/Nope/snapshots')[0], 404)

    def test_layout_align(self):
        a, b = self.net(TS0), self.net(TS1)
        m = lambda x: {'elements': x['elements'], 'rungs': x['rungs']}
        s, d = self.req('POST', '/api/layout', {'project': PROJECT, 'before': m(a), 'after': m(b), 'align': True})
        self.assertEqual((s, d['diff']), (200, {'n1': 'added'}))
        s, d = self.req('POST', '/api/layout', {'project': PROJECT, 'before': m(a), 'after': m(b)})
        self.assertEqual(d['diff'], {'p4': 'added', 'p5': 'added', 'x:p4': 'deleted'})   # without align the ids shift
        a, b = self.net(TS0, 'FB_Safety'), self.net(TS2, 'FB_Safety')
        f = lambda x: {'language': 'FBD', 'elements': x['elements']}
        s, d = self.req('POST', '/api/layout', {'project': PROJECT, 'before': f(a), 'after': f(b), 'align': True})
        self.assertEqual((s, d['diff']), (200, {'b2': 'changed', 'b3': 'changed'}))


    def test_mcp_tools(self):
        """get_changelog, list_snapshots, compare_snapshots through mcp_server.py driven by mcp_client.py."""
        n = self.net(TS1)
        uid = self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'R', 'block': 'FB_Conveyor', 'nw': 1,
                                              'after': {'elements': n['elements'], 'rungs': n['rungs']}})[1]['id']
        k = mcp_client.Client([sys.executable, os.path.join(ROOT, 'mcp_server.py'), '--server',
                               'http://127.0.0.1:%d' % self.srv.server_address[1]], timeout=60, display_name='mcp_server',
                              client_info={'name': 'test-agent', 'version': '1.0'})
        k.start()
        try:
            s = json.loads(k.call('list_snapshots', {'project': PROJECT}))['snapshots']
            self.assertEqual([x['ts'] for x in s], [TS2, TS1, TS_IF, TS0])
            r = json.loads(k.call('compare_snapshots', {'project': PROJECT, 'from': TS0, 'to': TS1}))
            self.assertEqual([(b['block'], b['networks'][0]['nw_to'], b['networks'][0]['matches']) for b in r['blocks']],
                             [('FB_Conveyor', 1, [uid])])
            r = json.loads(k.call('compare_snapshots', {'project': PROJECT, 'from': TS0, 'to': TS2, 'block': 'FB_Safety'}))
            self.assertEqual([b['block'] for b in r['blocks']], ['FB_Safety'])
            items = json.loads(k.call('get_changelog', {'project': PROJECT}))['items']
            self.assertEqual([x['kind'] for x in items], ['snapshot'] * 4 + ['proposed'])
            self.assertEqual(len(json.loads(k.call('get_changelog', {'project': PROJECT, 'until': TS1}))['items']), 3)
            md = k.call('get_changelog', {'project': PROJECT, 'format': 'md'})
            self.assertIn('| %s proposed | FB10 "FB_Conveyor" · NW 1: ' % uid, md)
            with self.assertRaises(mcp_client.MCPError) as e:
                k.call('compare_snapshots', {'project': PROJECT, 'from': TS0, 'to': '20990101-000000'})
            self.assertIn('HTTP 404', str(e.exception))
        finally:
            k.close()


if __name__ == '__main__':
    unittest.main()
