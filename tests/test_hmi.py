"""H: WinCC Unified HMI screens — the Reader dump parsed into screen models, the API, the snapshot with the HMI dump."""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import quote

from tests.common import ROOT, SAMPLES, PROJECT, TS0
import hmi
import server

FAKE = os.path.join(ROOT, 'tests', 'fake_mcp.py')


class TestHmiModel(unittest.TestCase):
    def test_sample_screens(self):
        r = hmi.api_screens(SAMPLES, PROJECT)
        self.assertEqual((r['export_ts'], r['hmi']), (TS0, 'HMI_1'))
        self.assertEqual([s['name'] for s in r['screens']], ['Alarms', 'Manual', 'Overview'])
        self.assertIn({'name': 'Machine_Speed', 'type': 'Real', 'plc_tag': '"DB_Machine".Speed', 'connection': 'HMI_Connection_1',
                       'table': 'Default tag table'}, r['tags'])
        s = hmi.api_screen(SAMPLES, PROJECT, 'Overview')
        x = {i['name']: i for i in s['items']}
        self.assertEqual((s['width'], s['height'], s['background']), (1280, 800, '#eef1f4'))
        self.assertEqual(x['IO_Speed']['type'], 'IOField')
        self.assertEqual(x['IO_Speed']['bindings'], [{'property': 'ProcessValue', 'tag': 'Machine_Speed'}])
        self.assertEqual(x['Label_Speed']['text'], 'Belt speed')                   # en-US, rich-text markup dropped
        self.assertEqual(x['Title']['colors']['backColor'], '#00000000')           # transparent keeps its alpha
        self.assertEqual(x['Btn_Alarms']['events'][0]['event'], 'Tapped')
        self.assertEqual(x['Clock']['type'], 'Clock')                              # unknown class: a generic box
        self.assertEqual(hmi.api_screen(SAMPLES, PROJECT, 'Manual')['items'][3]['type'], 'Switch')   # HmiToggleSwitch
        with self.assertRaises(KeyError):
            hmi.api_screen(SAMPLES, PROJECT, 'Nope')

    def test_tolerant_item(self):
        self.assertEqual(hmi.item({'$type': 'HmiSomethingNew'}),
                         {'name': '', 'type': 'SomethingNew', 'left': 0, 'top': 0, 'width': 0, 'height': 0, 'bindings': []})
        self.assertEqual(hmi.screen({})['width'], 1280)
        self.assertEqual((hmi.color('#80112233'), hmi.color('red'), hmi.tia_color('#112233')), ('#11223380', None, '#FF112233'))


class TestHmiApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = server.create_server(0, SAMPLES, '')
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def get(self, path):
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=10)
        c.request('GET', path)
        r = c.getresponse()
        t = json.loads(r.read())
        c.close()
        return r.status, t

    def test_screens_and_screen(self):
        s, r = self.get('/api/hmi/screens?project=%s' % PROJECT)
        self.assertEqual((s, len(r['screens'])), (200, 3))
        s, r = self.get('/api/hmi/screen?project=%s&screen=Alarms&export_ts=%s' % (PROJECT, TS0))
        self.assertEqual((s, r['hmi'], r['items'][2]['type']), (200, 'HMI_1', 'AlarmControl'))
        self.assertEqual(self.get('/api/hmi/screen?project=%s&screen=Nope' % PROJECT)[0], 404)
        self.assertEqual(self.get('/api/hmi/screens?project=%s&export_ts=20200101-000000' % PROJECT)[0], 404)
        self.assertEqual(self.get('/api/hmi/screens?project=%s' % quote('..\\x'))[0], 400)


class TestHmiSnapshot(unittest.TestCase):
    def setUp(self):
        self.root, self.data, self.tia = (tempfile.mkdtemp() for _ in range(3))
        shutil.copytree(os.path.join(SAMPLES, PROJECT, TS0, 'PLC_1'), os.path.join(self.root, PROJECT, TS0, 'PLC_1'))
        self.srv = None

    def tearDown(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
        for d in (self.root, self.data, self.tia):
            shutil.rmtree(d, ignore_errors=True)

    def snapshot(self, *reader_extra):
        py = sys.executable
        mcp = {'writer': ([py, FAKE, 'writer', '--state', self.tia], None),
               'reader': ([py, FAKE, 'reader', '--state', self.tia, '--export-root', self.root, '--project', PROJECT]
                          + list(reader_extra), None)}
        self.srv = server.create_server(0, self.root, self.data, mcp=mcp, mcp_timeout=30)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=60)
        c.request('POST', '/api/projects/%s/snapshot' % PROJECT, b'{}', {'Content-Type': 'application/json'})
        job = json.loads(c.getresponse().read())['job']
        for _ in range(300):
            j = self.srv.RequestHandlerClass.jobs[job['id']]
            if j['state'] != 'running':
                return j
            time.sleep(0.1)
        self.fail('job did not end')

    def test_snapshot_exports_the_hmi_too(self):
        self.assertRaises(KeyError, hmi.find, self.root, PROJECT)
        j = self.snapshot()
        self.assertEqual(j['state'], 'done', j)
        ts = j['result']['export_ts']
        self.assertEqual(j['hmi_chunks'], 2)                                      # 6 files, 3 per chunk
        self.assertNotIn('warnings', j)
        self.assertTrue(os.path.exists(os.path.join(self.root, PROJECT, ts, 'manifest-hmi.jsonl')))
        self.assertEqual(hmi.api_screens(self.root, PROJECT)['export_ts'], ts)

    def test_hmi_failure_is_a_warning(self):
        j = self.snapshot('--no-hmi')
        self.assertEqual(j['state'], 'done', j)
        self.assertIn('No WinCC Unified HMI', j['warnings'][0])


class TestHmiEdits(unittest.TestCase):
    """H2: hmi_screen edits — proposal, diff, TIA steps, Upload (fake Writer), Check (fake Reader), presets, MCP."""
    def setUp(self):
        self.root, self.data, self.tia = (tempfile.mkdtemp() for _ in range(3))
        shutil.copytree(os.path.join(SAMPLES, PROJECT, TS0), os.path.join(self.root, PROJECT, TS0))
        py = sys.executable
        mcp = {'writer': ([py, FAKE, 'writer', '--state', self.tia], None),
               'reader': ([py, FAKE, 'reader', '--state', self.tia, '--export-root', self.root, '--project', PROJECT], None)}
        self.srv = server.create_server(0, self.root, self.data, mcp=mcp, mcp_timeout=30)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        for d in (self.root, self.data, self.tia):
            shutil.rmtree(d, ignore_errors=True)

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=60)
        c.request(method, path, json.dumps(body).encode() if body is not None else None, {'Content-Type': 'application/json'})
        r = c.getresponse()
        t = json.loads(r.read())
        c.close()
        return r.status, t

    def ok(self, method, path, body=None, status=200):
        s, r = self.req(method, path, body)
        self.assertEqual(s, status, r)
        return r

    def changed_overview(self):
        u = self.ok('POST', '/api/edits', {'project': PROJECT, 'round': 'HMI', 'kind': 'hmi_screen', 'screen': 'Overview'}, 201)
        self.assertEqual((u['hmi'], u['new'], u['before']['export_ts'], u['after'], u['f']),
                         ('HMI_1', False, TS0, u['before']['screen'], False))
        after = json.loads(json.dumps(u['after']))
        x = {i['name']: i for i in after['items']}
        x['IO_Speed']['left'] += 8
        x['Title']['text'] = 'Line 1 — overview'
        x['IO_Pieces']['bindings'][0]['tag'] = 'Rejects'
        x['Header']['colors']['backColor'] = '#203040'
        return u['id'], after

    def test_change_upload_check(self):
        uid, after = self.changed_overview()
        r = self.ok('POST', '/api/edits/%s/after' % uid, {'after': after})
        self.assertEqual(r['errors'], [])
        d = self.ok('GET', '/api/edits/' + uid)
        c = d['screen_diff']
        self.assertEqual((c['added'], c['deleted'], sorted(c['changed'])), ([], [], ['Header', 'IO_Pieces', 'IO_Speed', 'Title']))
        self.assertEqual(c['changed']['IO_Speed'], [{'attr': 'left', 'before': 260, 'after': 268}])
        self.assertNotIn('upload_note', d)
        p = self.ok('GET', '/api/edits/%s/procedure' % uid)
        self.assertIn('"HMI_1" · Screen: Overview', p['header'])
        self.assertTrue(any('`IO_Pieces`' in k and 'dynamization ProcessValue `Pieces` → `Rejects`' in k for k in p['steps']), p['steps'])
        self.assertFalse(self.ok('POST', '/api/edits/%s/check-export' % uid, {})['match'])      # the export is still the old one
        self.ok('POST', '/api/edits/%s/state' % uid, {'state': 'Approved', 'by': 'user'})
        r = self.ok('POST', '/api/edits/%s/upload' % uid, {})
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['edit']['state'], 'Signed')
        with open(os.path.join(self.tia, 'calls.jsonl'), encoding='utf-8') as f:
            calls = [json.loads(z) for z in f]
        sets = [(z['tool'], z['arguments'].get('itemName'), z['arguments'].get('attribute') or z['arguments'].get('property'),
                 z['arguments'].get('value') or z['arguments'].get('tag')) for z in calls if z['tool'].startswith('SetHmi')]
        self.assertEqual(sorted(sets), [('SetHmiDynamizationTag', 'IO_Pieces', 'ProcessValue', 'Rejects'),
                                        ('SetHmiItemAttribute', 'Header', 'BackColor', '#FF203040'),
                                        ('SetHmiItemAttribute', 'IO_Speed', 'Left', '268'),
                                        ('SetHmiItemAttribute', 'Title', 'Text', 'Line 1 — overview')])
        r = self.ok('POST', '/api/edits/%s/check' % uid, {})
        self.assertTrue(r['match'], r)
        self.assertEqual(r['edit']['state'], 'Checked')

    def test_new_screen_from_preset(self):
        self.assertEqual(self.ok('GET', '/api/hmi/presets')['presets'][0]['key'], 'machine_overview')
        m = self.ok('POST', '/api/hmi/preset', {'project': PROJECT, 'preset': 'manual_control', 'screen': 'Belt',
                                                'params': {'machine': 'Belt 2', 'tags': ['Mode_Auto', 'Speed_Setpoint']}})
        types = {i['name']: i['type'] for i in m['items']}
        self.assertEqual((types['Switch_Mode_Auto'], types['IO_Speed_Setpoint']), ('Switch', 'IOField'))   # Bool -> switch
        body = {'project': PROJECT, 'round': 'HMI', 'kind': 'hmi_screen', 'screen': 'Belt', 'new': True,
                'preset': 'machine_overview', 'params': {'machine': 'Belt 2', 'tags': ['Machine_Speed', 'Horn', 'Nope']}}
        u = self.ok('POST', '/api/edits', body, 201)
        d = self.ok('GET', '/api/edits/' + u['id'])
        self.assertEqual(d['screen_diff']['added'][:2], ['Header', 'Title'])
        self.assertTrue(any(e.get('warning') and 'Nope' in e['error'] for e in d['errors']), d['errors'])
        self.assertIn('ImportHmiScreen', d['upload_note'])
        p = self.ok('GET', '/api/edits/%s/procedure' % u['id'])
        self.assertIn('Add new screen', p['steps'][0])
        self.assertIn('dynamization `ProcessValue` → tag `Machine_Speed`', p['markdown'])
        self.ok('POST', '/api/edits/%s/state' % u['id'], {'state': 'Approved', 'by': 'user'})
        self.assertEqual(self.req('POST', '/api/edits/%s/upload' % u['id'], {})[0], 409)
        k = self.ok('POST', '/api/edits/%s/check-export' % u['id'], {})
        self.assertEqual((k['match'], k['diffs']), (False, ['screen Belt is not in the export']))
        # the same name again, an exported name as new, a duplicate item, a broken model
        self.assertEqual(self.req('POST', '/api/edits', dict(body, screen='Overview'))[0], 400)
        after = json.loads(json.dumps(d['edit']['after']))
        after['items'].append(dict(after['items'][0]))
        self.assertEqual(self.req('POST', '/api/edits/%s/after' % u['id'], {'after': after})[0], 409)   # Approved: no changes
        self.ok('POST', '/api/edits/%s/state' % u['id'], {'state': 'Proposal', 'by': 'user'})
        s, r = self.req('POST', '/api/edits/%s/after' % u['id'], {'after': after})
        self.assertEqual(s, 400)
        self.assertIn('unique', r['error'])
        after['items'].pop()
        after['items'][0]['width'] = 5000
        r = self.ok('POST', '/api/edits/%s/after' % u['id'], {'after': after})
        self.assertTrue(any('outside the screen' in e['error'] for e in r['errors']), r['errors'])
        self.assertEqual(self.req('POST', '/api/edits/%s/after' % u['id'], {'after': {'items': 'x'}})[0], 400)

    def test_mcp_propose_screen(self):
        import mcp_server
        e = mcp_server.Editor('http://127.0.0.1:%d' % self.srv.server_address[1])
        r = mcp_server.call_tool(e, 'propose_screen', {'project': PROJECT, 'round': 'AI', 'screen': 'Alarms_2',
                                                       'preset': 'alarm_page', 'params': {'machine': 'Line'}}, {'name': 'agent'})
        v = json.loads(r['content'][0]['text'])
        self.assertEqual((v['new'], v['state']), (True, 'Proposal'))
        self.assertIn('Alarm_View', v['changes']['added'])
        r = mcp_server.call_tool(e, 'propose_screen', {'project': PROJECT, 'round': 'AI', 'screen': 'Manual'})
        self.assertFalse(json.loads(r['content'][0]['text'])['new'])
        self.assertTrue(mcp_server.call_tool(e, 'propose_screen', {'project': PROJECT, 'round': 'AI', 'screen': 'X'})['isError'])
        self.assertEqual(len(json.loads(mcp_server.call_tool(e, 'list_presets', {})['content'][0]['text'])['presets']), 4)


if __name__ == '__main__':
    unittest.main()
