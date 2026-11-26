"""Round 9: safety of the Upload — D8 (a group is refused before any write), D4 (SaveProject before the first import, a TIA
crash in CompileBlock), D20b (CompileSoftware after a failed CompileBlock, only without F-blocks), D10b (a block that does
not compile after the restore is reported loudly and blocks further Uploads), D12 (Upload without CompileBlock), D10c
(what the SimaticML way cannot write is told at propose time). Fake Writer/Reader, no TIA."""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

from tests.common import ROOT, SAMPLES, PROJECT, TS0
import server

FAKE = os.path.join(ROOT, 'tests', 'fake_mcp.py')
# these tests follow the documents way (the default prefers SimaticML where it writes the change, round 11)
TIA = {'softwarePath': 'PLC_1', 'plcName': 'PLC_1', 'upload': 'documents'}
OPS = [{'type': 'insert', 'place': {'r': 0, 'k': 3}, 'element': 'NO'},
       {'type': 'change', 'id': 'n1', 'values': {'operand': '"Fuse_OK"'}}]


class TestUploadSafety(unittest.TestCase):
    def setUp(self):
        self.root, self.data, self.tia = tempfile.mkdtemp(), tempfile.mkdtemp(), tempfile.mkdtemp()
        shutil.copytree(os.path.join(SAMPLES, PROJECT, TS0), os.path.join(self.root, PROJECT, TS0))
        self.srv = None

    def start(self, *extra, f_blocks=True):
        if not f_blocks:                              # a PLC without a safety program: CompileSoftware is allowed
            shutil.rmtree(os.path.join(self.root, PROJECT, TS0, 'PLC_1', 'Blocks', 'Safety'))
        py = sys.executable
        mcp = {'writer': ([py, FAKE, 'writer', '--state', self.tia] + list(extra), None),
               'reader': ([py, FAKE, 'reader', '--state', self.tia, '--export-root', self.root, '--project', PROJECT], None)}
        self.srv = server.create_server(0, self.root, self.data, mcp=mcp, mcp_timeout=30)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.assertEqual(self.req('PUT', '/api/state/tia?project=' + PROJECT, TIA)[0], 200)

    def tearDown(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
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

    def calls(self):
        p = os.path.join(self.tia, 'calls.jsonl')
        if not os.path.exists(p):
            return []
        with open(p, encoding='utf-8') as f:
            return [json.loads(x)['tool'] for x in f]

    def proposal(self, approve=True):
        """FB_Conveyor NW1 with one more contact — goes through documents."""
        _, net = self.req('GET', '/api/network?project=%s&block=FB_Conveyor&nw=1' % PROJECT)
        s, r = self.req('POST', '/api/layout', {'project': PROJECT, 'after': {'elements': net['elements'], 'rungs': net['rungs']},
                                                'ops': OPS})
        s, u = self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'K9', 'block': 'FB_Conveyor', 'nw': 1,
                                               'after': r['after']})
        self.assertEqual(s, 201, u)
        if approve:
            self.req('POST', '/api/edits/%s/state' % u['id'], {'state': 'Approved'})
        return u['id']

    def hmi_edit(self, delete=False):
        s, u = self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'K9', 'kind': 'hmi_screen', 'screen': 'Overview'})
        after = json.loads(json.dumps(u['after']))
        if delete:                                    # the Writer has no tool to delete an item through Upload
            after['items'] = [i for i in after['items'] if i['name'] != 'Title']
        else:
            next(i for i in after['items'] if i['name'] == 'IO_Speed')['left'] += 8
        self.assertEqual(self.req('POST', '/api/edits/%s/after' % u['id'], {'after': after})[0], 200)
        return u['id']

    def steps(self, r):
        return [k['step'] for k in r['write']['steps']]

    # ---------- D8 ----------

    def test_group_refused_before_any_write(self):
        """A member Upload cannot make (an HMI item deleted) refuses the group before the first Writer call."""
        self.start()
        net, scr = self.proposal(approve=False), self.hmi_edit(delete=True)
        self.req('POST', '/api/groups', {'edit_ids': [net, scr]})
        self.req('POST', '/api/groups/G001/state', {'state': 'Approved'})
        for _ in range(2):                            # the second try writes nothing either (D8: it did write once)
            s, r = self.req('POST', '/api/groups/G001/upload', {})
            self.assertEqual(s, 409, r)
            self.assertIn('refused before any write', r['error'])
            self.assertIn(scr, r['error'])
        self.assertEqual(self.calls(), [])
        self.assertEqual({self.req('GET', '/api/edits/' + x)[1]['edit']['state'] for x in (net, scr)}, {'Approved'})

    def test_group_plc_then_hmi_all_recorded(self):
        self.start()
        net, scr = self.proposal(approve=False), self.hmi_edit()
        self.req('POST', '/api/groups', {'edit_ids': [scr, net]})
        self.req('POST', '/api/groups/G001/state', {'state': 'Approved'})
        s, r = self.req('POST', '/api/groups/G001/upload', {})
        self.assertTrue(r['ok'], r)
        self.assertEqual(([x['edit'] for x in r['results']], r['view']['state']), ([net, scr], 'Signed'))
        tools = self.calls()
        self.assertLess(tools.index('ImportFromDocuments'), tools.index('SetHmiItemAttribute'))     # PLC first, then HMI

    def test_group_hmi_not_written_after_a_plc_failure(self):
        self.start('--fail-import')
        net, scr = self.proposal(approve=False), self.hmi_edit()
        self.req('POST', '/api/groups', {'edit_ids': [net, scr]})
        self.req('POST', '/api/groups/G001/state', {'state': 'Approved'})
        s, r = self.req('POST', '/api/groups/G001/upload', {})
        self.assertEqual((s, r['ok'], [x['edit'] for x in r['results']]), (200, False, [net]))
        self.assertNotIn('SetHmiItemAttribute', self.calls())
        self.assertIn('FB_Conveyor restored from the export before the write', r['results'][0]['message'])
        u = self.req('GET', '/api/edits/' + net)[1]['edit']
        self.assertEqual((u['state'], u['writes'][-1]['ok']), ('Approved', False))                # recorded

    # ---------- D4 ----------

    def test_save_project_before_the_import_and_opt_out(self):
        self.start()
        s, r = self.req('POST', '/api/edits/%s/upload' % self.proposal())
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.steps(r)[-3:], ['SaveProject', 'ImportFromDocuments', 'CompileBlock'])
        self.assertIn('saved right before the import', r['message'])
        self.req('PUT', '/api/state/tia?project=' + PROJECT, dict(TIA, save='no'))
        self.assertEqual(self.req('GET', '/api/state?project=' + PROJECT)[1]['tia'], dict(TIA, save='no'))
        shutil.rmtree(self.tia)                       # TIA back to the sample: the same edit again
        os.makedirs(self.tia)
        s, r = self.req('POST', '/api/edits/%s/upload' % self.proposal())
        self.assertTrue(r['ok'], r)
        self.assertNotIn('SaveProject', self.steps(r))
        self.assertEqual(self.req('PUT', '/api/state/tia?project=' + PROJECT, dict(TIA, save='maybe'))[0], 400)

    def test_save_fails_nothing_written(self):
        self.start('--fail-save')
        s, r = self.req('POST', '/api/edits/%s/upload' % self.proposal())
        self.assertEqual((r['ok'], r['edit']['state']), (False, 'Approved'))
        self.assertIn('nothing was written', r['message'])
        self.assertNotIn('ImportFromDocuments', self.calls())

    def test_tia_crash_in_compile(self):
        """D4: TIA goes down in CompileBlock — said loudly, nothing restored, further Uploads refused until confirmed."""
        self.start('--crash-compile')
        uid = self.proposal()
        s, r = self.req('POST', '/api/edits/%s/upload' % uid)
        self.assertEqual((r['ok'], r['edit']['state']), (False, 'Approved'))
        self.assertTrue(r['message'].startswith('TIA PORTAL MAY HAVE CRASHED (the TIA Portal instance 4711 is gone)'), r['message'])
        self.assertIn('saved right before the first import', r['message'])
        self.assertIn('Unexpected exception', r['message'])
        self.assertFalse([x for x in self.steps(r) if x.startswith('Restore')])
        s, d = self.req('GET', '/api/edits/' + uid)
        self.assertIn('TIA Portal may have crashed', d['tia_unsafe'])
        uid2 = self.proposal()
        s, e = self.req('POST', '/api/edits/%s/upload' % uid2)
        self.assertEqual(s, 409)
        self.assertIn('unsafe to save', e['error'])
        self.req('PUT', '/api/state/tia?project=' + PROJECT, TIA)              # saving the settings confirms nothing
        self.assertIn('tia_unsafe', self.req('GET', '/api/edits/' + uid)[1])
        self.assertEqual(self.req('POST', '/api/state/tia/fixed?project=' + PROJECT, {'by': 'user'})[0], 200)
        self.assertNotIn('tia_unsafe', self.req('GET', '/api/edits/' + uid)[1])
        fixed = self.req('GET', '/api/state?project=' + PROJECT)[1]['unsafe_fixed']
        self.assertEqual((len(fixed), fixed[0]['by']), (1, 'user'))
        self.assertEqual(self.req('POST', '/api/state/tia/fixed?project=' + PROJECT, {})[0], 409)   # nothing to confirm
        self.assertEqual(self.req('POST', '/api/edits/%s/upload' % uid2)[0], 200)

    # ---------- D20b / D10b ----------

    def test_compile_software_after_a_failed_compile_block(self):
        """No F-blocks: CompileBlock fails (as after any re-import of some large FBs), CompileSoftware makes it consistent."""
        self.start('--fail-compile-always', f_blocks=False)
        s, r = self.req('POST', '/api/edits/%s/upload' % self.proposal())
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.steps(r)[-3:], ['ImportFromDocuments', 'CompileBlock', 'CompileSoftware'])
        self.assertFalse(r['write']['steps'][-2]['ok'])
        with open(os.path.join(self.tia, 'software.txt'), encoding='utf-8') as f:
            self.assertEqual(json.loads(f.readline()), {'softwarePath': 'PLC_1'})

    def test_still_inconsistent_warning(self):
        """The Writer's 'still inconsistent after the compile' fails the compile only when it names the block."""
        warn = 'ok. WARNING: still inconsistent after the compile (1): FB_Other - run Compile > Software (rebuild all)'
        self.assertEqual(server._compiled(warn, 'FB_Conveyor'), warn)
        with self.assertRaises(server.s7dcl_write.WriteError):
            server._compiled(warn.replace('FB_Other', 'FB_Conveyor'), 'FB_Conveyor')
        self.assertEqual(server._compiled(warn.replace('FB_Other', 'Group/FB_Conveyor2'), 'FB_Conveyor'),
                         warn.replace('FB_Other', 'Group/FB_Conveyor2'))          # a whole name, not a prefix
        listed = json.dumps({'message': 'ok. WARNING: still inconsistent after the compile (16): ...',
                             'stillInconsistent': ['B%d' % i for i in range(15)] + ['Group/FB_Conveyor']})
        with self.assertRaises(server.s7dcl_write.WriteError):            # the full list, not the 15 of the message
            server._compiled(listed, 'FB_Conveyor')

    def test_guard_again_under_the_lock(self):
        """An Upload that marked the project unsafe after the caller's check: the next one still writes nothing."""
        self.start()
        uid = self.proposal()
        orig, seen = server._upload_guard, []

        def first_passes(s):                                               # the caller's check ran before the mark
            seen.append(1)
            if len(seen) > 1:
                orig(s)
        server._upload_guard = first_passes
        server._set_unsafe(self.data, PROJECT, 'block(s) X inconsistent after a failed Upload')
        try:
            s, e = self.req('POST', '/api/edits/%s/upload' % uid)
        finally:
            server._upload_guard = orig
        self.assertEqual(len(seen), 2)
        self.assertEqual(s, 409, e)
        self.assertEqual(self.calls(), [])
        self.assertTrue(server.TIA_LOCK.acquire(blocking=False))          # released
        server.TIA_LOCK.release()

    def test_inconsistent_after_restore_with_f_blocks(self):
        """F-blocks in the PLC: no CompileSoftware; the restored block does not compile -> BLOCK INCONSISTENT, unsafe."""
        self.start('--fail-compile-always')
        uid = self.proposal()
        s, r = self.req('POST', '/api/edits/%s/upload' % uid)
        self.assertEqual((r['ok'], r['edit']['state']), (False, 'Approved'))
        self.assertTrue(r['message'].startswith('BLOCK INCONSISTENT: FB_Conveyor — DO NOT SAVE THE PROJECT; run Rebuild all'),
                        r['message'])
        self.assertIn('this PLC has F-blocks', r['message'])
        self.assertNotIn('CompileSoftware', self.calls())
        self.assertEqual(self.steps(r)[-2:], ['Restore: ImportFromDocuments', 'Restore: CompileBlock'])
        self.assertIn('inconsistent after a failed Upload', self.req('GET', '/api/edits/' + uid)[1]['tia_unsafe'])

    def test_inconsistent_when_compile_software_fails_too(self):
        self.start('--fail-compile-always', '--fail-compile-software', f_blocks=False)
        s, r = self.req('POST', '/api/edits/%s/upload' % self.proposal())
        self.assertFalse(r['ok'])
        self.assertTrue(r['message'].startswith('BLOCK INCONSISTENT: FB_Conveyor'), r['message'])
        self.assertIn('then CompileSoftware', r['message'])
        self.assertEqual(self.steps(r)[-3:], ['Restore: ImportFromDocuments', 'Restore: CompileBlock', 'Restore: CompileSoftware'])

    # ---------- D12 ----------

    def test_upload_without_compile(self):
        self.start('--fail-compile-always')
        uid = self.proposal()
        s, r = self.req('POST', '/api/edits/%s/upload' % uid, {'compile': False})
        self.assertTrue(r['ok'], r)
        self.assertNotIn('CompileBlock', self.calls())
        self.assertEqual((r['write']['via'], r['edit']['state']), ('SimaticML', 'Signed'))
        self.assertIn('no CompileBlock', r['message'])
        self.assertIn('NOT COMPILED: FB_Conveyor wait for Rebuild all', r['message'])
        self.assertEqual(r['edit']['history'][-1]['note'], 'not compiled — waiting for Rebuild all')

    # ---------- D10c ----------

    def test_xml_refusal_told_at_propose_time(self):
        self.start()
        _, net = self.req('GET', '/api/network?project=%s&block=FB_Conveyor&nw=1' % PROJECT)
        after = {'elements': [{'id': 'n1', 'type': 'contact', 'operand': '#Motor'}, {'id': 'n2', 'type': 'ret', 'operand': ''}],
                 'rungs': [{'from': 'powerrail', 'path': ['n1', 'n2'], 'to': None}]}
        s, u = self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'K9', 'block': 'FB_Conveyor', 'nw': 1, 'after': after})
        self.assertEqual(s, 201, u)
        d = self.req('GET', '/api/edits/' + u['id'])[1]
        self.assertTrue(d['warnings'][0].startswith('Upload through SimaticML cannot write this edit: RET'), d['warnings'])
        self.assertIn('goes through documents alone', d['warnings'][0])
        self.assertNotIn('upload_note', d)
        self.req('PUT', '/api/state/tia?project=' + PROJECT, dict(TIA, upload='xml'))
        d = self.req('GET', '/api/edits/' + u['id'])[1]
        self.assertIn('so Upload refuses it', d['warnings'][0])
        self.assertIn('RET', d['upload_note'])


if __name__ == '__main__':
    unittest.main()
