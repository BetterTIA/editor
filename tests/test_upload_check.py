"""M5: Upload (TIAC_Writer) a Check (TIAC_Reader) přes API s falešnými MCP servery — bez TIA.

Export root i „projekt TIA" falešných serverů jsou v temp: Writer exportuje blok z vzorku (nebo z --stav po importu),
import zapíše do --stav; Reader exportuje z --stav do nové složky export rootu serveru."""
import contextlib
import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

from tests.common import ROOT, SAMPLES, PROJECT, TS0
import check
import s7dcl
import server

FAKE = os.path.join(ROOT, 'tests', 'fake_mcp.py')
TIA = {'softwarePath': 'PLC_1', 'plcName': 'PLC_1'}


class TestUploadCheck(unittest.TestCase):
    def setUp(self):
        self.root, self.data, self.tia = tempfile.mkdtemp(), tempfile.mkdtemp(), tempfile.mkdtemp()
        shutil.copytree(os.path.join(SAMPLES, PROJECT, TS0), os.path.join(self.root, PROJECT, TS0))
        self.start()

    def start(self, *writer_extra):
        py = sys.executable
        mcp = {'writer': ([py, FAKE, 'writer', '--state', self.tia] + list(writer_extra), None),
               'reader': ([py, FAKE, 'reader', '--state', self.tia, '--export-root', self.root, '--project', PROJECT], None)}
        self.srv = server.create_server(0, self.root, self.data, mcp=mcp, mcp_timeout=30)
        self.url = 'http://127.0.0.1:%d' % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def tearDown(self):
        self.stop()
        for d in (self.root, self.data, self.tia):
            shutil.rmtree(d, ignore_errors=True)

    def req(self, method, fpath, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=60)
        c.request(method, fpath, body=json.dumps(body).encode() if body is not None else None,
                  headers={'Content-Type': 'application/json'})
        r = c.getresponse()
        t = json.loads(r.read())
        c.close()
        return r.status, t

    def set_tia(self, tia=TIA):
        _, st = self.req('GET', '/api/state?project=%s' % PROJECT)
        st['tia'] = tia
        self.assertEqual(self.req('PUT', '/api/state?project=%s' % PROJECT, st)[0], 200)

    def proposal(self, block='FB_Conveyor', nw=1, ops=None, state='Approved'):
        _, net = self.req('GET', '/api/network?project=%s&block=%s&nw=%d' % (PROJECT, block, nw))
        after = {'language': 'FBD', 'elements': net['elements']} if net['language'] == 'FBD' else {'elements': net['elements'], 'rungs': net['rungs']}
        if ops:
            s, r = self.req('POST', '/api/layout', {'project': PROJECT, 'after': after, 'ops': ops})
            self.assertEqual(s, 200, r)
            after = r['after']
        s, u = self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'M5', 'block': block, 'nw': nw, 'after': after})
        self.assertEqual(s, 201, u)
        if state != 'Proposal':
            self.req('POST', '/api/edits/%s/state' % u['id'], {'state': 'Approved'})
        return u['id']

    def imports(self):
        with open(os.path.join(self.tia, 'imports.jsonl'), encoding='utf-8') as f:
            return [json.loads(r) for r in f]

    OPS = [{'type': 'insert', 'place': {'r': 0, 'k': 3}, 'element': 'NO'},
           {'type': 'change', 'id': 'n1', 'values': {'operand': '"Fuse_OK"'}}]

    def test_upload_f_block_and_no_tia_409(self):
        uid_f = self.proposal('FB_Safety', 1)
        uid = self.proposal(ops=self.OPS)
        s, e = self.req('POST', '/api/edits/%s/upload' % uid)
        self.assertEqual(s, 409)
        self.assertIn('TIA settings', e['error'])
        self.set_tia()
        s, e = self.req('POST', '/api/edits/%s/upload' % uid_f)
        self.assertEqual(s, 409)
        self.assertIn('F-block', e['error'])
        s, e = self.req('POST', '/api/edits/%s/check' % uid)                 # Check jen u Signed
        self.assertEqual(s, 409)
        uid_p = self.proposal(state='Proposal')
        self.assertEqual(self.req('POST', '/api/edits/%s/upload' % uid_p)[0], 409)
        self.assertFalse(os.path.exists(os.path.join(self.tia, 'imports.jsonl')))       # nic se nezapsalo
        _, st = self.req('GET', '/api/state?project=%s' % PROJECT)
        st['tia'] = {'softwarePath': 1}
        self.assertEqual(self.req('PUT', '/api/state?project=%s' % PROJECT, st)[0], 400)

    def test_upload_check_checked(self):
        self.set_tia()
        uid = self.proposal(ops=self.OPS)
        s, r = self.req('POST', '/api/edits/%s/upload' % uid)
        self.assertEqual(s, 200, r)
        self.assertTrue(r['ok'], r)
        u = r['edit']
        self.assertEqual((u['state'], u['history'][-1]['by']), ('Signed', 'TIAC_Writer'))
        self.assertEqual([k['step'] for k in u['writes'][0]['steps']],
                         ['Connect', 'ResolveObjectPath', 'ExportBlocksAsDocuments', 'Replace the network in .s7dcl',
                          'PreviewImport', 'ImportFromDocuments'])
        imp = self.imports()
        self.assertEqual((len(imp), imp[0]['groupPath'], imp[0]['importOption'], imp[0]['fileNameWithoutExtension']),
                         (1, 'Conveyor', 'Override', 'FB_Conveyor'))
        b = s7dcl.load_block(imp[0]['file'])                                  # importovaný .s7dcl má network „po"
        self.assertEqual([p.get('operand') for p in b['networks'][0]['elements']],
                         ['"Start_Povoleno"', '"DB_Machine".Safety_OK', '"DB_Machine".Mode_Auto', '"Fuse_OK"', '#Ready'])
        self.assertEqual(len(b['networks']), 8)
        # Check: Reader exportuje to, co Writer importoval -> shoda -> Checked
        s, c = self.req('POST', '/api/edits/%s/check' % uid)
        self.assertEqual(s, 200, c)
        self.assertTrue(c['match'], c)
        self.assertEqual((c['edit']['state'], c['edit']['history'][-1]['by']), ('Checked', 'TIAC_Reader'))
        self.assertEqual(c['edit']['checks'][-1]['export_ts'], c['export_ts'])
        self.assertTrue(os.path.isdir(os.path.join(self.root, PROJECT, c['export_ts'], 'PLC_1', 'Blocks', 'Conveyor')))
        self.assertEqual([z['tool'] for z in c['edit']['writes']], ['TIAC_Writer', 'TIAC_Reader'])

    def test_check_mismatch_stays_signed_and_checked_409(self):
        self.set_tia()
        uid = self.proposal(ops=self.OPS)
        self.assertTrue(self.req('POST', '/api/edits/%s/upload' % uid)[1]['ok'])
        file = self.imports()[0]['file']                                    # „někdo" v TIA změnil operand
        with open(file, encoding='utf-8') as f:
            t = f.read()
        with open(file, 'w', encoding='utf-8') as f:
            f.write(t.replace('"Fuse_OK"', '"Fuse_XX"'))
        s, c = self.req('POST', '/api/edits/%s/check' % uid)
        self.assertEqual((s, c['ok'], c['match'], c['edit']['state']), (200, False, False, 'Signed'))
        self.assertIn('Fuse_XX', c['message'])
        self.assertIs(c['edit']['checks'][-1]['match'], False)
        s, e = self.req('POST', '/api/edits/%s/state' % uid, {'state': 'Checked', 'by': 'user'})
        self.assertEqual(s, 409)                                                 # Checked bez poslední shody
        out = io.StringIO()                                                      # CLI --check: neshoda = exit 1
        with contextlib.redirect_stdout(out):
            self.assertEqual(check.main(['--server', self.url, '--check', uid]), 1)
        self.assertTrue(out.getvalue().startswith('MISMATCH %s · state Signed' % uid))
        with open(file, 'w', encoding='utf-8') as f:                           # oprava v TIA -> Check projde
            f.write(t)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(check.main(['--server', self.url, '--check', uid]), 0)
        self.assertTrue(out.getvalue().startswith('CHECKED %s · state Checked' % uid))

    def test_tia_busy(self):
        self.stop()
        self.start('--multi-instances')
        self.set_tia()
        uid = self.proposal(ops=self.OPS)
        s, r = self.req('POST', '/api/edits/%s/upload' % uid)
        self.assertEqual((s, r['ok'], r['edit']['state']), (200, False, 'Approved'))
        self.assertTrue(r['message'].startswith('TIA is busy or the Writer/Reader cannot be started: '))
        self.assertIn('More than one', r['message'])
        self.assertEqual(r['edit']['writes'][0]['steps'][0]['ok'], False)
        self.stop()                                                            # nespustitelný Writer
        self.srv = server.create_server(0, self.root, self.data, mcp={'writer': ([os.path.join(self.tia, 'not.exe')], None)})
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        s, r = self.req('POST', '/api/edits/%s/upload' % uid)
        self.assertEqual((s, r['ok']), (200, False))
        self.assertIn('fails spustit', r['message'])
        s, r = self.req('GET', '/api/edits/%s' % uid)
        self.assertEqual((r['tia'], len(r['edit']['writes'])), (TIA, 2))

    def test_lock(self):
        self.set_tia()
        uid = self.proposal(ops=self.OPS)
        self.assertTrue(server.TIA_LOCK.acquire(blocking=False))
        try:
            s, e = self.req('POST', '/api/edits/%s/upload' % uid)
        finally:
            server.TIA_LOCK.release()
        self.assertEqual(s, 409)
        self.assertIn('another Upload', e['error'])

    def test_upload_fbd_non_safety(self):
        """FBD network v ne-F bloku (FC_Branches NW3: O + Coil) — upload, import obsahuje nový vstup, Check shoda."""
        self.set_tia()
        uid = self.proposal('FC_Branches', 3, ops=[{'type': 'add_input', 'id': 'b1', 'operand': '"Sensor_C"'}])
        s, r = self.req('POST', '/api/edits/%s/upload' % uid)
        self.assertTrue(r['ok'], r)
        with open(self.imports()[0]['file'], encoding='utf-8') as f:
            self.assertIn('in3 := "Sensor_C"', f.read())
        s, c = self.req('POST', '/api/edits/%s/check' % uid)
        self.assertEqual((c['match'], c['edit']['state']), (True, 'Checked'))


if __name__ == '__main__':
    unittest.main()
