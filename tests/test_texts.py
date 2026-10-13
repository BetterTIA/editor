"""E4: a new title / comment of a network (title_after, comment_after) — alone or with a model change."""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

from tests.common import ROOT, SAMPLES, PROJECT, TS0
import mcp_server
import s7dcl
import server

FAKE = os.path.join(ROOT, 'tests', 'fake_mcp.py')


class TestNetworkTexts(unittest.TestCase):
    def setUp(self):
        self.root, self.data, self.tia = tempfile.mkdtemp(), tempfile.mkdtemp(), tempfile.mkdtemp()
        shutil.copytree(os.path.join(SAMPLES, PROJECT, TS0), os.path.join(self.root, PROJECT, TS0))
        py = sys.executable
        mcp = {'writer': ([py, FAKE, 'writer', '--state', self.tia], None),
               'reader': ([py, FAKE, 'reader', '--state', self.tia, '--export-root', self.root, '--project', PROJECT], None)}
        self.srv = server.create_server(0, self.root, self.data, mcp=mcp, mcp_timeout=30)
        self.url = 'http://127.0.0.1:%d' % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        st = self.req('GET', '/api/state?project=' + PROJECT)[1]
        st['tia'] = {'softwarePath': 'PLC_1', 'plcName': 'PLC_1'}
        self.req('PUT', '/api/state?project=' + PROJECT, st)

    def tearDown(self):
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

    def propose(self, nw=3, **kw):
        s, u = self.req('POST', '/api/edits', dict({'project': PROJECT, 'round': 'E4', 'block': 'FB_Conveyor', 'nw': nw}, **kw))
        self.assertEqual(s, 201, u)
        return u

    def upload_and_check(self, u):
        self.req('POST', '/api/edits/%s/state' % u['id'], {'state': 'Approved'})
        _, r = self.req('POST', '/api/edits/%s/upload' % u['id'], {})
        self.assertTrue(r['ok'], r)
        with open(os.path.join(self.tia, 'calls.jsonl'), encoding='utf-8') as f:
            call = [json.loads(x) for x in f if '"SetNetworkText"' in x][-1]['arguments']
        _, k = self.req('POST', '/api/edits/%s/check' % u['id'], {})
        return call, k

    def test_title_only(self):
        u = self.propose(title_after='Startup delay', comment_after='')
        self.assertEqual((u['title'], u['title_after'], 'comment_after' in u), ('Startup', 'Startup delay', False))
        self.assertEqual(u['after']['elements'], u['before']['elements'])
        _, d = self.req('GET', '/api/edits/' + u['id'])
        self.assertEqual((d['texts'], d['diff']), ({'title_after': ['Startup', 'Startup delay']}, {}))
        _, p = self.req('GET', '/api/edits/%s/procedure' % u['id'])
        self.assertTrue(p['header'].endswith('NW 3: Startup → Startup delay'), p['header'])
        self.assertEqual(p['steps'][1], 'Rename the network title to **Startup delay** (click the title line of the network and type).')
        _, k = self.req('POST', '/api/edits/%s/check-export' % u['id'], {})
        self.assertIn('title in the export "Startup", the proposal "Startup delay"', k['diffs'])
        call, k = self.upload_and_check(u)
        self.assertEqual(call, {'softwarePath': 'PLC_1', 'blockPath': 'Conveyor/FB_Conveyor', 'network': 3, 'dryRun': False,
                                'title': 'Startup delay'})
        self.assertTrue(k['match'], k)
        self.assertEqual(k['edit']['state'], 'Checked')

    def test_model_title_and_comment(self):
        _, net = self.req('GET', '/api/network?project=%s&block=FB_Conveyor&nw=2' % PROJECT)
        after = {'elements': net['elements'] + [{'id': 'n1', 'type': 'contact', 'operand': '"Fuse_OK"'}],
                 'rungs': [dict(net['rungs'][0], path=['n1'] + net['rungs'][0]['path'])] + net['rungs'][1:]}
        u = self.propose(nw=2, after=after, title_after='Motor latch with fuse')
        s, r = self.req('POST', '/api/edits/%s/after' % u['id'], {'comment_after': 'Fuse first.'})
        self.assertEqual(s, 200, r)
        self.assertEqual((r['edit']['title_after'], r['edit']['comment_after']), ('Motor latch with fuse', 'Fuse first.'))
        self.assertEqual(len(r['edit']['after']['elements']), len(net['elements']) + 1)      # the model stays
        call, k = self.upload_and_check(r['edit'])
        self.assertEqual((call['network'], call['title'], call['comment']), (2, 'Motor latch with fuse', 'Fuse first.'))
        self.assertTrue(k['match'], k)
        b = s7dcl.load_block(os.path.join(self.tia, 'Conveyor', 'FB_Conveyor.s7dcl'))
        self.assertEqual((b['networks'][1]['title'], b['networks'][1]['comment']), ('Motor latch with fuse', 'Fuse first.'))

    def test_invalid_and_mcp(self):
        self.assertEqual(self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'E4', 'block': 'FB_Conveyor', 'nw': 3,
                                                         'title_after': 'a\nb'})[0], 400)
        self.assertEqual(self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'E4', 'block': 'FB_Conveyor', 'nw': 3})[0], 400)
        s, r = self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'E4', 'block': 'FB_Conveyor', 'nw': 3,
                                               'title_after': 'Startup'})   # the same title = nothing to change
        self.assertEqual(s, 400, r)
        e = mcp_server.Editor(self.url)
        r = json.loads(mcp_server.call_tool(e, 'propose_edit', {'project': PROJECT, 'round': 'E4', 'block': 'FB_Conveyor', 'nw': 1,
                                                                'title_after': 'Conveyor ready'})['content'][0]['text'])
        r = json.loads(mcp_server.call_tool(e, 'update_edit', {'id': r['id'], 'comment_after': 'all three conditions'})['content'][0]['text'])
        g = json.loads(mcp_server.call_tool(e, 'get_edit', {'id': r['id']})['content'][0]['text'])
        self.assertEqual((g['edit']['title_after'], g['edit']['comment_after']), ('Conveyor ready', 'all three conditions'))
        self.assertIn('Rename the network title to **Conveyor ready**', g['procedure'])


if __name__ == '__main__':
    unittest.main()
