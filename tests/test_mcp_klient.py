"""M5: MCP klient (stdio JSON-RPC) proti falešnému Writeru / Readeru — bez TIA."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

from tests.spolecne import KOREN, PROJEKT
import mcp_klient

FALESNY = os.path.join(KOREN, 'tests', 'falesny_mcp.py')


class TestKlient(unittest.TestCase):
    def setUp(self):
        self.stav = tempfile.mkdtemp()
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.stav, ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def writer(self, *navic):
        return [sys.executable, FALESNY, 'writer', '--stav', self.stav] + list(navic)

    def test_writer_export_a_import(self):
        prac = tempfile.mkdtemp()
        try:
            with mcp_klient.Klient(self.writer(), timeout=20) as k:
                self.assertIn('Connected', k.zavolej('Connect', {}))
                cesta = json.loads(k.zavolej('ResolveObjectPath', {'softwarePath': 'PLC_1', 'name': 'FB_Dopravnik',
                                                                   'kind': 'block'}))['path']
                self.assertEqual(cesta, 'Dopravnik/FB_Dopravnik')
                k.zavolej('ExportBlocksAsDocuments', {'softwarePath': 'PLC_1', 'exportPath': prac,
                                                      'regexName': '^FB_Dopravnik$', 'preservePath': False})
                self.assertTrue(os.path.exists(os.path.join(prac, 'FB_Dopravnik.s7res')))
                with open(os.path.join(prac, 'FB_Dopravnik.s7dcl'), 'a', encoding='utf-8') as f:
                    f.write('// změna\n')
                k.zavolej('ImportFromDocuments', {'softwarePath': 'PLC_1', 'groupPath': 'Dopravnik', 'importPath': prac,
                                                  'fileNameWithoutExtension': 'FB_Dopravnik', 'importOption': 'Override'})
            with open(os.path.join(self.stav, 'importy.jsonl'), encoding='utf-8') as f:
                imp = json.loads(f.readline())
            self.assertEqual(imp['importOption'], 'Override')
            with open(imp['soubor'], encoding='utf-8') as f:
                self.assertTrue(f.read().endswith('// změna\n'))
        finally:
            shutil.rmtree(prac, ignore_errors=True)

    def test_reader_nova_slozka(self):
        p = [sys.executable, FALESNY, 'reader', '--stav', self.stav, '--export-root', self.root, '--projekt', PROJEKT]
        with mcp_klient.Klient(p, timeout=20) as k:
            r = json.loads(k.zavolej('export_to_folder', {'pathPrefixes': ['PLC_1/Blocks/Dopravnik/FB_Dopravnik'],
                                                          'include': ['blocks'], 'formats': ['documents']}))
            r2 = json.loads(k.zavolej('export_to_folder', {'pathPrefixes': ['PLC_1/Blocks/Dopravnik/FB_Dopravnik']}))
        self.assertTrue(r['complete'])
        self.assertNotEqual(r['exportFolder'], r2['exportFolder'])          # druhý export = nové razítko
        self.assertTrue(os.path.exists(os.path.join(r['exportFolder'], 'PLC_1', 'Blocks', 'Dopravnik', 'FB_Dopravnik.s7dcl')))

    def test_chyby(self):
        with self.assertRaises(mcp_klient.ChybaMCP) as e:
            mcp_klient.Klient([os.path.join(self.root, 'neni.exe')]).spust()
        self.assertIn('nejde spustit', str(e.exception))
        with mcp_klient.Klient(self.writer('--vice-instanci'), timeout=20) as k:
            with self.assertRaises(mcp_klient.ChybaMCP) as e:
                k.zavolej('Connect', {})
            self.assertIn('More than one', str(e.exception))
            with self.assertRaises(mcp_klient.ChybaMCP):
                k.zavolej('ExportBlocksAsDocuments', {})                     # isError: chybí softwarePath
        t = time.time()
        with mcp_klient.Klient(self.writer('--pomalu', '10'), timeout=20) as k:
            with self.assertRaises(mcp_klient.ChybaMCP) as e:
                k.zavolej('Connect', {}, timeout=1)
        self.assertIn('timeout', str(e.exception))
        self.assertLess(time.time() - t, 8)
        with self.assertRaises(mcp_klient.ChybaMCP) as e:                    # proces skončí hned (špatné argumenty)
            mcp_klient.Klient([sys.executable, FALESNY, 'nic'], timeout=20).spust()
        self.assertIn('skončil', str(e.exception))

    def test_prikazy(self):
        self.assertEqual(mcp_klient.prikaz_z_textu(r'python "C:\a b\f.py" writer'), ['python', r'C:\a b\f.py', 'writer'])
        j = os.path.join(self.root, 'mcp.json')
        with open(j, 'w', encoding='utf-8') as f:
            json.dump({'mcpServers': {'TIAC_Writer': {'command': 'w.exe', 'args': ['--allow-write']},
                                      'tia-portal': {'command': 'r.exe', 'args': [], 'env': {'X': '1'}}}}, f)
        self.assertEqual(mcp_klient.z_mcp_json(j, 'TIAC_Writer'), (['w.exe', '--allow-write'], None))
        self.assertEqual(mcp_klient.z_mcp_json(j, 'tia-portal'), (['r.exe'], {'X': '1'}))
        self.assertIsNone(mcp_klient.z_mcp_json(j, 'neni'))


if __name__ == '__main__':
    unittest.main()
