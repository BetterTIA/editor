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

from tests.common import SAMPLES, PROJECT, TS0, TS1
import check
import model
import diff
import server

EXTRA = os.path.join('PLC_1', 'Blocks', 'Conveyor')


def _read(ts, suffix):
    with open(os.path.join(SAMPLES, PROJECT, ts, EXTRA, 'FB_Conveyor' + suffix), encoding='utf-8') as f:
        return f.read()


class TestContentMatch(unittest.TestCase):
    def nw(self, n, ts=TS0):
        _, b = server._load(SAMPLES, PROJECT, 'FB_Conveyor', ts)
        return b['networks'][n - 1]

    def test_ids_and_node_names_do_not_matter(self):
        a = self.nw(2)
        b = json.loads(re.sub(r'"p(\d+)"', r'"q\1"', json.dumps(a)).replace('wire#w1', 'wire#w7').replace('"w1"', '"w7"'))
        self.assertEqual(diff.content_match(a, b), (True, []))

    def test_topology(self):
        a = self.nw(1)
        b = json.loads(json.dumps(a))
        b['rungs'][0]['path'] = ['p1', 'wire#w1', 'p3', 'p4']               # p2 paralelně k p1 místo do série
        b['rungs'].append({'from': 'powerrail', 'path': ['p2'], 'to': 'w1'})
        match, r = diff.content_match(a, b)
        self.assertFalse(match)
        self.assertIn('liší se zapojení', r[0])

    def test_box_pins(self):
        a = self.nw(4)
        b = json.loads(json.dumps(a))
        b['elements'][1]['inputs'][1]['operand'] = '#Jiny'
        match, r = diff.content_match(a, b)
        self.assertEqual(r, ['jiné pins FB_Belt "DB_Belt1": Direction #Direction → #Jiny'])

    def test_branch_without_element_in_main_path(self):
        m = model.delete({'elements': self.nw(2)['elements'], 'rungs': self.nw(2)['rungs']}, 'p1')
        ch = model.validate(m)
        self.assertEqual([c['id'] for c in ch], ['p5', 'p6'])                # obě přemostěné větve
        self.assertTrue(all('Branch without an element in the main path' in c['error'] for c in ch))


class TestExportCheck(unittest.TestCase):
    """Export root v temp: nejdřív jen původní export, „nové exporty" se přidávají během testu."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.data = tempfile.mkdtemp()
        shutil.copytree(os.path.join(SAMPLES, PROJECT, TS0), os.path.join(self.root, PROJECT, TS0))
        self.srv = server.create_server(0, self.root, self.data)
        self.url = 'http://127.0.0.1:%d' % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    def req(self, method, fpath, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=20)
        c.request(method, fpath, body=json.dumps(body).encode() if body is not None else None,
                  headers={'Content-Type': 'application/json'})
        r = c.getresponse()
        t = json.loads(r.read())
        c.close()
        return r.status, t

    def export(self, ts, s7dcl, s7res):
        d = os.path.join(self.root, PROJECT, ts, EXTRA)
        os.makedirs(d)
        for suffix, text in (('.s7dcl', s7dcl), ('.s7res', s7res)):
            with open(os.path.join(d, 'FB_Conveyor' + suffix), 'w', encoding='utf-8') as f:
                f.write(text)

    def cli(self, *arg):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = check.main(['--server', self.url] + list(arg))
        return code, out.getvalue()

    def test_scenario(self):
        _, net = self.req('GET', '/api/network?project=%s&block=FB_Conveyor&nw=1' % PROJECT)
        _, r = self.req('POST', '/api/layout', {'project': PROJECT, 'after': {'elements': net['elements'], 'rungs': net['rungs']},
                                                   'ops': [{'type': 'insert', 'place': {'r': 0, 'k': 3}, 'element': 'NO'},
                                                           {'type': 'change', 'id': 'n1', 'values': {'operand': '"Fuse_OK"'}}]})
        s, u = self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'Round 1', 'block': 'FB_Conveyor', 'nw': 1,
                                                 'after': r['after']})
        uid = u['id']
        self.req('POST', '/api/edits/%s/state' % uid, {'state': 'Approved'})
        self.req('POST', '/api/edits/%s/state' % uid, {'state': 'Signed'})          # zapsáno ručně (Signed je volný)
        # 1) tentýž export jako před úpravou -> neshoda, chybí kontakt
        code, txt = self.cli('--edit', uid)
        self.assertEqual(code, 1)
        self.assertIn('chybí v export: contact "Fuse_OK"', txt)
        self.assertIn('the same as before the change', txt)
        # 2) export s jiným operandem -> neshoda s poznámkou
        s7dcl, s7res = _read(TS1, '.s7dcl'), _read(TS1, '.s7res')
        self.export('20261012-150000', s7dcl.replace('"Fuse_OK"', '"Fuse_XX"'), s7res)
        code, txt = self.cli('--edit', uid)
        self.assertEqual(code, 1)
        self.assertIn('jiný operand: návrh contact "Fuse_OK", export contact "Fuse_XX"', txt)
        self.assertEqual(self.req('POST', '/api/edits/%s/state' % uid, {'state': 'Checked'})[0], 409)
        # 3) export se zápisem, ale network posunutý (nový network před NW1, volá FC) -> shoda + poznámka
        new_value = ('    {\n        S7_Language := "LAD";\n        S7_NetworkTitle := "MLC_0"\n    }\n    NETWORK\n'
                '        RUNG wire#powerrail\n            Contact( #Start )\n            "FC_Branches"(  )\n'
                '        END_RUNG\n    END_NETWORK\n')
        i = s7dcl.index('    {\n        S7_Language := "LAD";\n        S7_NetworkTitle := "MLC_1"')
        self.export('20261012-160000', s7dcl[:i] + new_value + s7dcl[i:],
                    s7res.replace('MultiLingualTexts:\n', 'MultiLingualTexts:\n  - id: MLC_0\n    en-US: Nový network\n', 1))
        s, k = self.req('POST', '/api/edits/%s/check-export' % uid, {})
        self.assertEqual((s, k['match'], k['export_ts'], k['nw_export']), (200, True, '20261012-160000', 2))
        self.assertIn('the network moved: NW 1 → NW 2', k['note'])
        _, n1 = self.req('GET', '/api/network?project=%s&block=FB_Conveyor&nw=1' % PROJECT)
        self.assertEqual((n1['elements'][1]['call'], n1['elements'][1]['name']), ('fc', 'FC_Branches'))   # FC ze staršího exportu
        # 4) zadaný export se zápisem (vzorek TS1) -> shoda, exit 0
        self.export(TS1, s7dcl, s7res)
        code, txt = self.cli('--edit', uid, '--export-ts', TS1)
        self.assertEqual(code, 0, txt)
        self.assertTrue(txt.startswith('MATCH'))
        # po shodě projde Signed -> Checked
        s, u = self.req('POST', '/api/edits/%s/state' % uid, {'state': 'Checked'})
        self.assertEqual((s, u['state']), (200, 'Checked'))
        self.assertEqual([k['match'] for k in u['checks']], [False, False, True, True])
        # chyby: neexistující export / úprava -> exit 2
        self.assertEqual(self.cli('--edit', uid, '--export-ts', '20991231-000000')[0], 2)
        self.assertEqual(self.cli('--edit', 'U999')[0], 2)


if __name__ == '__main__':
    unittest.main()
