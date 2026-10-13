"""D3: project changelog from state.json (history, writes, checks), export folders and snapshot jobs — no TIA."""
import http.client
import os
import shutil
import tempfile
import unittest
import urllib.parse

from tests.common import SAMPLES, PROJECT, TS0, TS1
from tests import test_upload_check
import server


class TestChangelog(unittest.TestCase):
    """The fake Writer / Reader set-up of the Upload / Check tests (borrowed, not inherited: its tests must not run twice)."""
    U = test_upload_check.TestUploadCheck
    setUp, tearDown, start, stop, req, set_tia, proposal, OPS = U.setUp, U.tearDown, U.start, U.stop, U.req, U.set_tia, U.proposal, U.OPS

    def changelog(self, **query):
        s, r = self.req('GET', '/api/projects/%s/changelog?%s' % (PROJECT, urllib.parse.urlencode(query)))
        self.assertEqual(s, 200, r)
        return r['items']

    def test_changelog_upload_check(self):
        self.set_tia()
        uid = self.proposal(ops=self.OPS)
        self.assertTrue(self.req('POST', '/api/edits/%s/upload' % uid)[1]['ok'])
        c = self.req('POST', '/api/edits/%s/check' % uid)[1]
        self.assertTrue(c['match'], c)
        items = self.changelog()
        self.assertEqual([x['time'] for x in items], sorted(x['time'] for x in items))
        ev = [(x['kind'], x.get('edit'), x['by']) for x in items]
        self.assertEqual(ev[0], ('snapshot', None, ''))                          # TS0 (2026) is older than today's edit
        self.assertEqual([e for e in ev if e[1]], [
            ('proposed', uid, 'editor'), ('state', uid, 'user'), ('upload', uid, 'TIAC_Writer'),
            ('state', uid, 'TIAC_Writer'), ('check', uid, 'TIAC_Reader'), ('state', uid, 'TIAC_Reader')])
        u = next(x for x in items if x['kind'] == 'check')
        self.assertEqual((u['block'], u['nw'], u['block_kind'], u['block_number'], u['state']),
                         ('FB_Conveyor', 1, 'FB', 10, 'Checked'))
        self.assertTrue(u['summary'].startswith('match · export %s' % c['export_ts']), u['summary'])
        snaps = [x for x in items if x['kind'] == 'snapshot']
        self.assertEqual(snaps[0]['summary'], 'snapshot: full export, 6 blocks')
        self.assertEqual((snaps[1]['summary'], snaps[1]['by']), ('partial export: FB_Conveyor (Check %s)' % uid, 'TIAC_Reader'))
        self.assertEqual([x['summary'] for x in items if x['kind'] == 'state'][-1], 'Signed → Checked')
        # filters
        self.assertEqual({x['kind'] for x in self.changelog(state='Checked')}, {'proposed', 'state', 'upload', 'check'})
        self.assertEqual(self.changelog(block='FB_Safety'), [])
        self.assertEqual([x['kind'] for x in self.changelog(until='2026-10-12')], ['snapshot'])
        self.assertEqual([x['kind'] for x in self.changelog(since=c['export_ts'])][-1], 'state')
        # Markdown: per day, every edit row with block + number + network title
        s, md = self.req_text('/api/projects/%s/changelog?format=md' % PROJECT)
        self.assertEqual(s, 200)
        self.assertTrue(md.startswith('# Changelog — %s' % PROJECT))
        self.assertIn('## 2026-10-12', md)
        self.assertIn('| 12:00:00 | snapshot |  |  | snapshot: full export, 6 blocks |', md)
        title = items[1]['title']
        self.assertTrue(title)
        self.assertIn('| %s proposed | FB10 "FB_Conveyor" · NW 1: %s | editor |' % (uid, title), md)
        self.assertEqual(md.count('FB10 "FB_Conveyor" · NW 1: %s' % title), 6)

    def req_text(self, path):
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=60)
        c.request('GET', path)
        r = c.getresponse()
        t = r.read().decode('utf-8')
        self.assertTrue(r.getheader('Content-Type').startswith('text/markdown'))
        c.close()
        return r.status, t

    def test_failed_reader_write_and_unknown_project(self):
        uid = self.proposal(ops=self.OPS, state='Proposal')
        self.assertEqual(self.req('GET', '/api/projects/Nope/changelog')[0], 404)
        items = self.changelog(block='FB_Conveyor')
        self.assertEqual([x['kind'] for x in items], ['proposed'])
        self.assertEqual(items[0]['edit'], uid)


class TestChangelogUnit(unittest.TestCase):
    def test_snapshot_job_and_partial_exports(self):
        root, data = tempfile.mkdtemp(), tempfile.mkdtemp()
        try:
            shutil.copytree(os.path.join(SAMPLES, PROJECT), os.path.join(root, PROJECT))
            jobs = {'J001': {'id': 'J001', 'kind': 'snapshot', 'project': PROJECT, 'state': 'done',
                             'result': {'export_ts': TS0}},
                    'J002': {'id': 'J002', 'kind': 'snapshot', 'project': PROJECT, 'state': 'error',
                             'error': 'TIA Portal is not running', 'started': '2026-10-12T16:00:00', 'finished': '2026-10-12T16:00:01'}}
            items = server.api_changelog(root, data, PROJECT, jobs)['items']
            self.assertEqual([(x['time'], x['summary'], x['by']) for x in items], [
                ('2026-10-12T12:00:00', 'snapshot: full export, 6 blocks (job J001)', 'TIAC_Reader'),
                ('2026-10-12T13:00:00', 'partial export: FB_Conveyor', ''),
                ('2026-10-12T14:00:00', 'partial export: FB_Conveyor', ''),
                ('2026-10-12T15:00:00', 'partial export: FB_Safety', ''),
                ('2026-10-12T16:00:01', 'snapshot failed (job J002): TIA Portal is not running', 'TIAC_Reader')])
            self.assertEqual(len(server.api_changelog(root, data, PROJECT, since=TS1, until='2026-10-12T15')['items']), 2)
            self.assertIn('Nothing recorded yet.', server.changelog_markdown(PROJECT, []))
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(data, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
