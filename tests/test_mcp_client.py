"""M5: MCP klient (stdio JSON-RPC) proti falešnému Writeru / Readeru — bez TIA."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

from tests.common import ROOT, PROJECT
import mcp_client

FAKE = os.path.join(ROOT, 'tests', 'fake_mcp.py')


class TestClient(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.state, ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def writer(self, *extra):
        return [sys.executable, FAKE, 'writer', '--state', self.state] + list(extra)

    def test_writer_export_and_import(self):
        work = tempfile.mkdtemp()
        try:
            with mcp_client.Client(self.writer(), timeout=20) as k:
                self.assertIn('Connected', k.call('Connect', {}))
                fpath = json.loads(k.call('ResolveObjectPath', {'softwarePath': 'PLC_1', 'name': 'FB_Conveyor',
                                                                   'kind': 'block'}))['path']
                self.assertEqual(fpath, 'Conveyor/FB_Conveyor')
                k.call('ExportBlocksAsDocuments', {'softwarePath': 'PLC_1', 'exportPath': work,
                                                      'regexName': '^FB_Conveyor$', 'preservePath': False})
                self.assertTrue(os.path.exists(os.path.join(work, 'FB_Conveyor.s7res')))
                with open(os.path.join(work, 'FB_Conveyor.s7dcl'), 'a', encoding='utf-8') as f:
                    f.write('// změna\n')
                k.call('ImportFromDocuments', {'softwarePath': 'PLC_1', 'groupPath': 'Conveyor', 'importPath': work,
                                                  'fileNameWithoutExtension': 'FB_Conveyor', 'importOption': 'Override'})
            with open(os.path.join(self.state, 'imports.jsonl'), encoding='utf-8') as f:
                imp = json.loads(f.readline())
            self.assertEqual(imp['importOption'], 'Override')
            with open(imp['file'], encoding='utf-8') as f:
                self.assertTrue(f.read().endswith('// změna\n'))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_reader_new_folder(self):
        p = [sys.executable, FAKE, 'reader', '--state', self.state, '--export-root', self.root, '--project', PROJECT]
        with mcp_client.Client(p, timeout=20) as k:
            r = json.loads(k.call('export_to_folder', {'pathPrefixes': ['PLC_1/Blocks/Conveyor/FB_Conveyor'],
                                                          'include': ['blocks'], 'formats': ['documents']}))
            r2 = json.loads(k.call('export_to_folder', {'pathPrefixes': ['PLC_1/Blocks/Conveyor/FB_Conveyor']}))
        self.assertTrue(r['complete'])
        self.assertNotEqual(r['exportFolder'], r2['exportFolder'])          # druhý export = nové razítko
        self.assertTrue(os.path.exists(os.path.join(r['exportFolder'], 'PLC_1', 'Blocks', 'Conveyor', 'FB_Conveyor.s7dcl')))

    def test_errors(self):
        with self.assertRaises(mcp_client.MCPError) as e:
            mcp_client.Client([os.path.join(self.root, 'not.exe')]).start()
        self.assertIn('fails spustit', str(e.exception))
        with mcp_client.Client(self.writer('--multi-instances'), timeout=20) as k:
            with self.assertRaises(mcp_client.MCPError) as e:
                k.call('Connect', {})
            self.assertIn('More than one', str(e.exception))
            with self.assertRaises(mcp_client.MCPError):
                k.call('ExportBlocksAsDocuments', {})                     # isError: chybí softwarePath
        t = time.time()
        with mcp_client.Client(self.writer('--slow', '10'), timeout=20) as k:
            with self.assertRaises(mcp_client.MCPError) as e:
                k.call('Connect', {}, timeout=1)
        self.assertIn('timeout', str(e.exception))
        self.assertLess(time.time() - t, 8)
        with self.assertRaises(mcp_client.MCPError) as e:                    # proces skončí hned (špatné argumenty)
            mcp_client.Client([sys.executable, FAKE, 'nothing'], timeout=20).start()
        self.assertIn('skončil', str(e.exception))

    def test_commands(self):
        self.assertEqual(mcp_client.command_from_text(r'python "C:\a b\f.py" writer'), ['python', r'C:\a b\f.py', 'writer'])
        j = os.path.join(self.root, 'mcp.json')
        with open(j, 'w', encoding='utf-8') as f:
            json.dump({'mcpServers': {'TIAC_Writer': {'command': 'w.exe', 'args': ['--allow-write']},
                                      'tia-portal': {'command': 'r.exe', 'args': [], 'env': {'X': '1'}}}}, f)
        self.assertEqual(mcp_client.from_mcp_json(j, 'TIAC_Writer'), (['w.exe', '--allow-write'], None))
        self.assertEqual(mcp_client.from_mcp_json(j, 'tia-portal'), (['r.exe'], {'X': '1'}))
        self.assertIsNone(mcp_client.from_mcp_json(j, 'not'))


if __name__ == '__main__':
    unittest.main()
