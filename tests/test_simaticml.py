"""E4/E5/E7/E8: SimaticML way of Upload — FlgNet from the network model, targeted edits of a block export, OB check, and the
Upload through the fake Writer (ExportXmlBlock → edit → ImportXmlBlock → CompileBlock, restore after a failure).

Fixtures (synthetic, tests/samples/simaticml): tia/ = what the fake TIA exports as XML (FB_Conveyor of Demo_Line, FB_Station =
an LAD FB with a GRAPH multi-instance, OB_Cyclic = OB30), export/Station = their documents for the editor (+ FB_Sequence.xml,
the GRAPH block), expected/ = the edited exports, checked by hand against the V21 format."""
import copy
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
import simaticml

XML = os.path.join(SAMPLES, 'simaticml')
FAKE = os.path.join(ROOT, 'tests', 'fake_mcp.py')
TIA = {'softwarePath': 'PLC_1', 'plcName': 'PLC_1', 'groupPath': 'Station'}


def read(*p):
    with open(os.path.join(XML, *p), encoding='utf-8-sig', newline='') as f:
        return f.read()


def find(nets, u):
    return server._find_network(nets, u)[0]


def lad(*elements, rungs=None):
    els = [dict(e, id=e.get('id') or 'n%d' % i) for i, e in enumerate(elements, 1)]
    return {'elements': els, 'rungs': rungs or [{'from': 'powerrail', 'path': [e['id'] for e in els], 'to': None}]}


RUN_LAMP = lad({'id': 'p1', 'type': 'contact', 'operand': '#Run'}, {'id': 'p2', 'type': 'coil', 'operand': '#Lamp'})
RUN_LAMP_AFTER = lad({'id': 'p1', 'type': 'contact', 'operand': '#Run'}, {'id': 'n1', 'type': 'contact_nc', 'operand': '#Fault'},
                     {'id': 'p2', 'type': 'coil', 'operand': '#Lamp'})
FAULT_NET = lad({'type': 'contact', 'operand': '#Enable'},
                {'type': 'box', 'call': 'instruction', 'name': 'TON', 'instance': '#T_Fault', 'template': 'Time', 'power': ['IN', 'Q'],
                 'inputs': [{'pin': 'PT', 'operand': 'T#5s'}], 'outputs': [{'pin': 'ET', 'operand': ''}]},
                {'type': 'coil_s', 'operand': '#Fault'})
STATION_EDITS = [   # one group: interface -> new network -> change (with a new title)
    {'id': 'U1', 'kind': 'interface', 'section': 'Static', 'members': [
        {'name': 'Fault', 'type': 'Bool', 'comment': 'set after 5 s'}, {'name': 'T_Fault', 'type': 'TON_TIME'}]},
    {'id': 'U2', 'kind': 'new_network', 'after_nw': 1, 'after_title': 'Sequence', 'title': 'Fault after 5 s', 'comment': '',
     'language': 'LAD', 'after': FAULT_NET},
    {'id': 'U3', 'kind': 'network', 'nw': 2, 'title': 'Run lamp', 'title_after': 'Run lamp (no fault)', 'before': RUN_LAMP,
     'after': RUN_LAMP_AFTER}]
COUNTER = lad({'id': 'p1', 'type': 'contact', 'operand': '"Count_Enable"'},
              {'id': 'p2', 'type': 'box', 'call': 'instruction', 'name': 'ADD', 'instance': None, 'template': 'Int',
               'power': ['EN', 'ENO'], 'inputs': [{'pin': 'IN1', 'operand': '"Cycle_Count"'}, {'pin': 'IN2', 'operand': '1'}],
               'outputs': [{'pin': 'OUT', 'operand': '"Cycle_Count"'}]})
COUNTER_AFTER = copy.deepcopy(COUNTER)
COUNTER_AFTER['elements'].insert(1, {'id': 'n1', 'type': 'contact_nc', 'operand': '"Count_Hold"'})
COUNTER_AFTER['rungs'][0]['path'].insert(1, 'n1')
COUNTER_AFTER['elements'][2]['inputs'][1]['operand'] = '2'
OB_EDIT = {'id': 'U4', 'kind': 'network', 'nw': 1, 'title': 'Cycle counter', 'comment_after': 'counts by two unless held',
           'before': COUNTER, 'after': COUNTER_AFTER}

# by hand: A in series, B parallel to it (a join = O with Card 2), then a coil
PARALLEL = {'elements': [{'id': 'a', 'type': 'contact', 'operand': '#A'}, {'id': 'b', 'type': 'contact', 'operand': '"B"'},
                         {'id': 'c', 'type': 'coil', 'operand': '#C'}],
            'rungs': [{'from': 'powerrail', 'path': ['a', 'wire#w1', 'c'], 'to': None},
                      {'from': 'powerrail', 'path': ['b'], 'to': 'w1'}]}
PARALLEL_XML = '''<FlgNet xmlns="http://www.siemens.com/automation/Openness/SW/NetworkSource/FlgNet/v5">
  <Parts>
    <Access Scope="LocalVariable" UId="21">
      <Symbol>
        <Component Name="A" />
      </Symbol>
    </Access>
    <Access Scope="GlobalVariable" UId="22">
      <Symbol>
        <Component Name="B" />
      </Symbol>
    </Access>
    <Access Scope="LocalVariable" UId="23">
      <Symbol>
        <Component Name="C" />
      </Symbol>
    </Access>
    <Part Name="Contact" UId="24" />
    <Part Name="Contact" UId="25" />
    <Part Name="Coil" UId="26" />
    <Part Name="O" UId="27">
      <TemplateValue Name="Card" Type="Cardinality">2</TemplateValue>
    </Part>
  </Parts>
  <Wires>
    <Wire UId="28">
      <Powerrail />
      <NameCon UId="24" Name="in" />
      <NameCon UId="25" Name="in" />
    </Wire>
    <Wire UId="29">
      <NameCon UId="27" Name="out" />
      <NameCon UId="26" Name="in" />
    </Wire>
    <Wire UId="30">
      <NameCon UId="24" Name="out" />
      <NameCon UId="27" Name="in1" />
    </Wire>
    <Wire UId="31">
      <NameCon UId="25" Name="out" />
      <NameCon UId="27" Name="in2" />
    </Wire>
    <Wire UId="32">
      <IdentCon UId="21" />
      <NameCon UId="24" Name="operand" />
    </Wire>
    <Wire UId="33">
      <IdentCon UId="22" />
      <NameCon UId="25" Name="operand" />
    </Wire>
    <Wire UId="34">
      <IdentCon UId="23" />
      <NameCon UId="26" Name="operand" />
    </Wire>
  </Wires>
</FlgNet>'''


class TestFlgNet(unittest.TestCase):
    def test_parallel_branch_by_hand(self):
        self.assertEqual(simaticml.flgnet(PARALLEL), PARALLEL_XML)
        simaticml.check_flgnet(PARALLEL_XML)

    def test_graph_ignores_uids_and_order(self):
        g = simaticml.graph(PARALLEL_XML)
        shuffled = PARALLEL_XML.replace('UId="2', 'UId="9').replace('UId="3', 'UId="8')
        self.assertEqual(simaticml.graph(shuffled), g)
        other = copy.deepcopy(PARALLEL)
        other['elements'][1]['operand'] = '"B2"'
        self.assertNotEqual(simaticml.graph(simaticml.flgnet(other)), g)
        self.assertIn(('Contact[operand=#A].out', 'O[].in1'), g)

    def test_every_network_of_the_sample_block(self):
        """The FlgNet of each Demo_Line FB_Conveyor network = the one in its XML export (calls, TON, compare, MOVE, P_TRIG)."""
        _, b = server._load(SAMPLES, PROJECT, 'FB_Conveyor', TS0)
        nets = simaticml.networks(read('tia', 'Conveyor', 'FB_Conveyor.xml'))
        cp = lambda name, kind: simaticml.call_info(read('tia', 'Conveyor', 'FB_Conveyor.xml')).get((name, kind))
        for n, x in zip(b['networks'], nets):
            self.assertEqual(simaticml.graph(simaticml.flgnet({'elements': n['elements'], 'rungs': n['rungs']}, cp)),
                             simaticml.graph(x['flgnet']), n['title'])

    def test_operands(self):
        a = simaticml._xml(simaticml.access('21', '"DB".arr[#i].x'), '')
        self.assertIn('<Component Name="arr" AccessModifier="Array">', a)
        self.assertIn('<Access Scope="LocalVariable">', a)
        a = simaticml._xml(simaticml.access('21', '#v[3]'), '')
        self.assertIn('<ConstantType>DInt</ConstantType>', a)
        self.assertIn('<ConstantType>Real</ConstantType>', simaticml._xml(simaticml.access('21', '0.5'), ''))
        self.assertIn('<ConstantType>LReal</ConstantType>', simaticml._xml(simaticml.access('21', '0.5', 'LReal'), ''))
        self.assertIn('<ConstantType>DInt</ConstantType>', simaticml._xml(simaticml.access('21', '100000'), ''))
        self.assertIn('Scope="TypedConstant"', simaticml._xml(simaticml.access('21', 'T#5s'), ''))
        for bad in ('%I0.0', '#x.%X3', 'PI', ''):
            self.assertRaises(simaticml.XmlError, simaticml.access, '21', bad)

    def test_unsupported_is_refused_by_name(self):
        for e, word in (({'type': 'ret', 'operand': ''}, 'RET'), ({'type': 'coil_neg', 'operand': '#x'}, 'negated coil'),
                        ({'type': 'box', 'call': 'instruction', 'name': 'CTU', 'instance': '#c', 'inputs': [], 'outputs': []}, 'CTU'),
                        ({'type': 'contact', 'operand': '%I0.0'}, 'operand %I0.0')):
            why = simaticml.unsupported(lad(e))
            self.assertIn(word, why or '', e)
        f = {'language': 'FBD', 'elements': [{'id': 'b1', 'type': 'sr', 'operand': '#m', 'inputs': [
            {'n': 1, 'pin': 'S', 'operand': '#a', 'signal': None, 'neg': False}], 'outputs': []}]}
        self.assertIn('SR', simaticml.unsupported(f))
        self.assertIsNone(simaticml.unsupported(RUN_LAMP_AFTER))

    def test_fbd(self):
        """FBD: & with a negated input feeding = ; the signal is a wire from out to in."""
        m = {'language': 'FBD', 'elements': [
            {'id': 'b1', 'type': 'and', 'inputs': [{'n': 1, 'pin': 'in1', 'operand': '#A', 'signal': None, 'neg': False},
                                                   {'n': 2, 'pin': 'in2', 'operand': '"B"', 'signal': None, 'neg': True}],
             'outputs': []},
            {'id': 'b2', 'type': 'assign', 'operand': '#C', 'inputs': [{'n': 1, 'pin': 'in1', 'operand': '', 'signal': 'b1',
                                                                       'neg': False}], 'outputs': []}]}
        x = simaticml.flgnet(m)
        simaticml.check_flgnet(x)
        self.assertIn('<Part Name="A" UId="24">\n      <Negated Name="in2" />\n      <TemplateValue Name="Card" Type="Cardinality">2'
                      '</TemplateValue>', x)
        self.assertIn(('A!in2[in1=#A|in2="B]', 'Coil[operand=#C]'), [(a.split('.')[0], b.split('.')[0])
                                                                      for a, b in simaticml.graph(x) if a != 'part'])


class TestEditBlock(unittest.TestCase):
    def test_fb_with_a_graph_multi_instance(self):
        """Interface + new network + changed network of an FB with a GRAPH multi-instance: one edited export, the rest
        (the multi-instance's nested sections, NW1 with the GRAPH call, the header) byte for byte."""
        t = read('tia', 'Station', 'FB_Station.xml')
        new, notes = simaticml.edit_block(t, STATION_EDITS, find, lambda n, k: simaticml.call_info(t).get((n, k)))
        self.assertEqual(new, read('expected', 'FB_Station.xml'))
        self.assertEqual(notes, ['U1: 2 member(s) added to Static', 'U2: new LAD network inserted as NW 2',
                                 'U3: NW 3 "Run lamp" replaced', 'U3: NW 3 texts set'])
        nets = simaticml.networks(new)
        self.assertEqual([n['title'] for n in nets], ['Sequence', 'Fault after 5 s', 'Run lamp (no fault)'])
        self.assertEqual(nets[0]['flgnet'], simaticml.networks(t)[0]['flgnet'])
        self.assertIn('\r\n', new)
        self.assertNotIn('\n', new.replace('\r\n', ''))

    def test_ob30(self):
        t = read('tia', 'Station', 'OB_Cyclic.xml')
        ob = simaticml.ob_attributes(t)
        self.assertEqual(ob, {'Number': '30', 'SecondaryType': 'CyclicInterrupt', 'CyclicTime': '100000', 'PhaseOffset': '0'})
        new, _ = simaticml.edit_block(t, [OB_EDIT], find)
        self.assertEqual(new, read('expected', 'OB_Cyclic.xml'))
        self.assertEqual(simaticml.ob_attributes(new), ob)
        info = {'attributes': [{'name': k, 'value': v} for k, v in ob.items()]}
        self.assertIsNone(simaticml.ob_mismatch(ob, json.dumps(info)))
        info['attributes'][0]['value'] = 123
        self.assertIn('Number 123, before the write 30', simaticml.ob_mismatch(ob, info))
        self.assertIn('SecondaryType not reported', simaticml.ob_mismatch(ob, {'Attributes': [{'Name': 'Number', 'Value': 30}]}))
        self.assertIsNone(simaticml.ob_attributes(read('tia', 'Station', 'FB_Station.xml')))

    def test_before_must_match_the_project(self):
        t = read('tia', 'Station', 'FB_Station.xml')
        u = dict(STATION_EDITS[2], before=RUN_LAMP_AFTER)          # TIA has the old network, the proposal another "before"
        with self.assertRaises(simaticml.XmlError) as e:
            simaticml.edit_block(t, [u], find)
        self.assertIn('not the proposal\'s "before" network', str(e.exception))
        self.assertIn('Contact!operand[operand=#Fault]', str(e.exception))
        u = dict(STATION_EDITS[2], after=dict(RUN_LAMP_AFTER, language='FBD'))
        self.assertRaises(simaticml.XmlError, simaticml.edit_block, t, [u], find)
        self.assertRaises(simaticml.XmlError, simaticml.edit_block, t, [dict(STATION_EDITS[0], members=[{'name': 'run', 'type': 'Bool'}])], find)
        self.assertRaises(simaticml.XmlError, simaticml.edit_block, t, [dict(STATION_EDITS[1], after_title='Other')], find)

    def test_f_block_refused(self):
        t = read('tia', 'Station', 'FB_Station.xml').replace('<ProgrammingLanguage>LAD</ProgrammingLanguage>\r\n      <SetENO',
                                                              '<ProgrammingLanguage>F_LAD</ProgrammingLanguage>\r\n      <SetENO', 1)
        self.assertIn('F-block', simaticml.f_block(t))
        self.assertRaises(simaticml.XmlError, simaticml.edit_block, t, [STATION_EDITS[2]], find)
        self.assertIsNone(simaticml.f_block(read('tia', 'Station', 'FB_Station.xml')))

    def test_block_without_networks_and_empty_network(self):
        t = read('tia', 'Station', 'OB_Cyclic.xml')
        a, b = t.index('      <SW.Blocks.CompileUnit'), t.rindex('</SW.Blocks.CompileUnit>') + len('</SW.Blocks.CompileUnit>\r\n')
        empty = t[:a] + t[b:]
        new, _ = simaticml.edit_block(empty, [dict(STATION_EDITS[1], after_nw=0, after_title='', after=RUN_LAMP)], find)
        nets = simaticml.networks(new)
        self.assertEqual([n['title'] for n in nets], ['Fault after 5 s'])
        self.assertIn('</SW.Blocks.CompileUnit>\r\n      <MultilingualText ID=', new)
        u = dict(STATION_EDITS[2], nw=1, title='Cycle counter', title_after=None, before=COUNTER, after=lad())
        new, _ = simaticml.edit_block(t, [u], find)
        self.assertEqual(simaticml.networks(new)[0]['flgnet'], '')
        self.assertIn('<NetworkSource />', new.split('Cycle counter')[0].split('CompileUnits')[1])


class TestObInfo(unittest.TestCase):
    def test_cycle_time_lost_in_the_import_is_seen(self):
        info = lambda c: json.dumps({'attributes': [{'name': 'Number', 'value': '30'}, {'name': 'CycleTime', 'value': c},
                                                      {'name': 'Name', 'value': 'X'}]})
        self.assertIsNone(simaticml.info_changed(info('100000'), info('100000')))
        self.assertIn('CycleTime 1000, before the write 100000', simaticml.info_changed(info('100000'), info('1000')))


class TestXmlUpload(unittest.TestCase):
    """Upload through the fake Writer (SimaticML of the fixtures) — the Demo_Line project with the Station blocks added."""

    def setUp(self):
        self.root, self.data, self.tia = tempfile.mkdtemp(), tempfile.mkdtemp(), tempfile.mkdtemp()
        shutil.copytree(os.path.join(SAMPLES, PROJECT, TS0), os.path.join(self.root, PROJECT, TS0))
        shutil.copytree(os.path.join(XML, 'export', 'Station'), os.path.join(self.root, PROJECT, TS0, 'PLC_1', 'Blocks', 'Station'))
        self.start()

    def start(self, *extra):
        py = sys.executable
        mcp = {'writer': ([py, FAKE, 'writer', '--state', self.tia] + list(extra), None),
               'reader': ([py, FAKE, 'reader', '--state', self.tia, '--export-root', self.root, '--project', PROJECT], None)}
        self.srv = server.create_server(0, self.root, self.data, mcp=mcp, mcp_timeout=30)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.req('PUT', '/api/state/tia?project=' + PROJECT, TIA)

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

    def edit(self, **body):
        s, u = self.req('POST', '/api/edits', dict({'project': PROJECT, 'round': 'X', 'block': 'FB_Station'}, **body))
        self.assertEqual(s, 201, u)
        return u['id']

    def approve(self, uid):
        self.assertEqual(self.req('POST', '/api/edits/%s/state' % uid, {'state': 'Approved'})[0], 200)

    def calls(self, tool=None):
        p = os.path.join(self.tia, 'calls.jsonl')
        if not os.path.exists(p):
            return []
        with open(p, encoding='utf-8') as f:
            return [c for c in map(json.loads, f) if tool is None or c['tool'] == tool]

    def written(self, block='FB_Station'):
        with open(os.path.join(self.tia, 'Station', block + '.xml'), encoding='utf-8-sig', newline='') as f:
            return f.read()

    def station_group(self):
        ids = [self.edit(nw=2, title_after='Run lamp (no fault)', after=RUN_LAMP_AFTER),
               self.edit(kind='new_network', after_nw=1, title='Fault after 5 s', after=FAULT_NET),
               self.edit(kind='interface', section='Static', members=STATION_EDITS[0]['members'])]
        self.req('POST', '/api/groups', {'edit_ids': ids})
        self.req('POST', '/api/groups/G001/state', {'state': 'Approved'})
        return ids

    def test_graph_group_one_import(self):
        """A group in an FB with a GRAPH multi-instance: one export, one edited import (= the expected file), one compile."""
        ids = self.station_group()
        _, d = self.req('GET', '/api/edits/' + ids[0])
        self.assertIn('GRAPH (Seq : "FB_Sequence")', d['upload_via'])
        s, r = self.req('POST', '/api/groups/G001/upload', {})
        self.assertTrue(r['ok'], r)
        self.assertEqual(([x['edit'] for x in r['results']], r['view']['state']), ([ids[2], ids[1], ids[0]], 'Signed'))
        self.assertEqual([c['tool'] for c in self.calls()][2:],
                         ['ExportXmlBlock', 'ImportXmlBlock', 'CompileBlock'])     # groupPath set: no ResolveObjectPath
        imp = self.calls('ImportXmlBlock')[0]['arguments']
        self.assertEqual((imp['groupPath'], imp['expectedContentHash'][:11]), ('Station', 'xml:sha256:'))
        self.assertEqual(self.written(), read('expected', 'FB_Station.xml'))
        self.assertEqual(self.req('POST', '/api/groups/G001/upload', {})[0], 409)       # Signed now

    def test_ob30_checked_after_the_import(self):
        uid = self.edit(block='OB_Cyclic', nw=1, comment_after=OB_EDIT['comment_after'], after=COUNTER_AFTER)
        self.approve(uid)
        s, r = self.req('POST', '/api/edits/%s/upload' % uid)
        self.assertTrue(r['ok'], r)
        self.assertEqual([k['step'] for k in r['write']['steps']][2:],
                         ['ExportXmlBlock', 'Edit the SimaticML', 'GetBlockInfo before', 'ImportXmlBlock', 'CompileBlock', 'GetBlockInfo'])
        self.assertIn('OB: documents would lose its event class and number', r['message'])
        self.assertEqual(self.written('OB_Cyclic'), read('expected', 'OB_Cyclic.xml'))

    def test_ob_renumbered_is_restored(self):
        """E8: the OB lost its number -> the export from before the write goes back, the edit stays Approved."""
        self.stop()
        self.start('--ob-renumbered')
        uid = self.edit(block='OB_Cyclic', nw=1, after=COUNTER_AFTER)
        self.approve(uid)
        s, r = self.req('POST', '/api/edits/%s/upload' % uid)
        self.assertEqual((r['ok'], r['edit']['state']), (False, 'Approved'), r)
        self.assertIn('Number 130, before the write 30', r['message'])
        self.assertIn('OB_Cyclic restored from the export before the write', r['message'])
        self.assertEqual([k['step'] for k in r['write']['steps']][-3:],
                         ['GetBlockInfo', 'Restore: ImportXmlBlock', 'Restore: CompileBlock'])
        self.assertEqual(self.written('OB_Cyclic'), read('tia', 'Station', 'OB_Cyclic.xml'))
        self.assertNotIn('expectedContentHash', self.calls('ImportXmlBlock')[-1]['arguments'])
        keep = r['message'].split('are kept in ')[1]
        self.assertTrue(os.path.exists(os.path.join(keep, 'import', 'OB_Cyclic.xml')))

    def test_compile_error_restores_the_group(self):
        self.stop()
        self.start('--fail-compile')
        self.station_group()
        s, r = self.req('POST', '/api/groups/G001/upload', {})
        self.assertFalse(r['ok'], r)
        self.assertEqual(r['view']['state'], 'Approved')
        self.assertIn('Invalid operand (fake)', r['results'][0]['message'])
        self.assertIn('FB_Station restored', r['results'][0]['message'])
        self.assertEqual(self.written(), read('tia', 'Station', 'FB_Station.xml'))

    def test_import_error_detail(self):
        self.stop()
        self.start('--fail-xml-import')
        uid = self.edit(block='OB_Cyclic', nw=1, after=COUNTER_AFTER)
        self.approve(uid)
        s, r = self.req('POST', '/api/edits/%s/upload' % uid)
        self.assertEqual((r['ok'], r['edit']['state']), (False, 'Approved'))
        self.assertIn("Failed importing 'OB_Cyclic'", r['message'])
        self.assertIn('detail: Network 2: the wire 37 has no target (fake)', r['message'])
        self.assertIn('OB_Cyclic restored', r['message'])

    def test_block_changed_since_the_export_is_not_restored(self):
        """expectedContentHash refused (state_changed): someone else's change stays, nothing is re-imported."""
        uid = self.edit(block='OB_Cyclic', nw=1, after=COUNTER_AFTER)
        self.approve(uid)
        orig = server.api_upload_xml
        cur = os.path.join(self.tia, 'Station', 'OB_Cyclic.xml')

        def changed_in_between(*a, **k):                          # "TIA" changes the block after the export
            real = server._content_hash
            server._content_hash = lambda r: 'xml:sha256:0'
            try:
                return orig(*a, **k)
            finally:
                server._content_hash = real
        server.api_upload_xml = changed_in_between
        try:
            s, r = self.req('POST', '/api/edits/%s/upload' % uid)
        finally:
            server.api_upload_xml = orig
        self.assertFalse(r['ok'])
        self.assertIn('state_changed', r['message'])
        self.assertNotIn('Restore', ' '.join(k['step'] for k in r['write']['steps']))
        self.assertFalse(os.path.exists(cur))

    def test_refused_before_any_write(self):
        """What the XML way cannot write is refused before the first call; the setting sends a plain FB this way too."""
        uid = self.edit(block='OB_Cyclic', nw=2, after=lad({'type': 'contact', 'operand': '"Count_Low"'}, {'type': 'ret', 'operand': ''}))
        self.approve(uid)
        _, d = self.req('GET', '/api/edits/' + uid)
        self.assertIn('RET is not written by the XML way', d['upload_note'])
        s, e = self.req('POST', '/api/edits/%s/upload' % uid)
        self.assertEqual(s, 409)
        self.assertIn('make the change by hand', e['error'])
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.req('PUT', '/api/state/tia?project=' + PROJECT, dict(TIA, upload='always'))[0], 400)
        self.req('PUT', '/api/state/tia?project=' + PROJECT, dict(TIA, groupPath='Conveyor', upload='xml'))
        s, u = self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'X', 'block': 'FB_Conveyor', 'nw': 1,
                                               'title_after': 'Conveyor ready'})
        self.approve(u['id'])
        s, r = self.req('POST', '/api/edits/%s/upload' % u['id'])
        self.assertTrue(r['ok'], r)
        self.assertIn('TIA settings: every Upload through SimaticML', r['message'])
        self.assertEqual(self.req('PUT', '/api/state/tia?project=' + PROJECT, dict(TIA, upload='auto'))[1]['tia'], TIA)


if __name__ == '__main__':
    unittest.main()
