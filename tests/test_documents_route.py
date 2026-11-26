"""Round 11: the documents way and the route choice of Upload — D17a rung order, D17b texts in the .s7res, D15 nested OR,
D16 SimaticML preferred (documents only where SimaticML refuses), D1 repeated MLC ids, D11 several new networks in one block.
Synthetic fixtures; fake Writer/Reader, no TIA."""
import copy
import glob
import json
import os
import shutil
import tempfile
import unittest

from tests.common import SAMPLES, PROJECT, TS0
from tests import test_simaticml, test_upload_check
import s7dcl
import s7dcl_write
import server

CONVEYOR = os.path.join(SAMPLES, PROJECT, TS0, 'PLC_1', 'Blocks', 'Conveyor')
AUTO = {'softwarePath': 'PLC_1', 'plcName': 'PLC_1'}


def c(i, op):
    return {'id': i, 'type': 'contact', 'operand': op}


# NW1 of FB_Conveyor turned into A ∨ (B ∧ (C ∨ D)) → coil: the branch D opens inside B's rung and closes where B's rung closes
NESTED = {'elements': [c('p1', '"Start_Povoleno"'), c('p2', '"DB_Machine".Safety_OK'), c('p3', '"DB_Machine".Mode_Auto'),
                       {'id': 'p4', 'type': 'coil', 'operand': '#Ready'}, c('n1', '"Fuse_OK"')],
          'rungs': [{'from': 'powerrail', 'path': ['p1', 'wire#w1', 'p4'], 'to': None},
                    {'from': 'powerrail', 'path': ['p2', 'wire#w2', 'p3'], 'to': 'w1'},
                    {'from': 'w2', 'path': ['n1'], 'to': 'w1'}]}


class TestRungOrder(unittest.TestCase):
    def test_branches_right_after_their_main_rung(self):
        """D17a: two self-holdings with a branch from the middle wire, the branches appended at the end of the model (as the
        editor adds them) -> each branch right after its main rung, closing branches before opening ones (TIA's order)."""
        els = [c('a%d' % i, '#A%d' % i) for i in range(1, 9)] + [{'id': 'q1', 'type': 'coil', 'operand': '#Q1'},
                                                                 {'id': 'q2', 'type': 'coil', 'operand': '#Q2'}]
        m = {'elements': els, 'rungs': [
            {'from': 'powerrail', 'path': ['a1', 'wire#w5', 'a2', 'wire#w1', 'a3', 'wire#w2', 'q1'], 'to': None},
            {'from': 'w1', 'path': ['a4'], 'to': 'w2'},
            {'from': 'powerrail', 'path': ['a5', 'wire#w6', 'a6', 'wire#w3', 'a7', 'wire#w4', 'q2'], 'to': None},
            {'from': 'w3', 'path': ['a8'], 'to': 'w4'},
            {'from': 'powerrail', 'path': [], 'to': 'w5'},
            {'from': 'powerrail', 'path': [], 'to': 'w6'}]}
        rungs = [(r['from'], r['to']) for r in s7dcl_write.rung_order(m['rungs'])]
        self.assertEqual(rungs, [('powerrail', None), ('powerrail', 'w5'), ('w1', 'w2'),
                                 ('powerrail', None), ('powerrail', 'w6'), ('w3', 'w4')])
        text = s7dcl_write.lad_text(m)
        self.assertEqual([x.strip() for x in text.splitlines() if 'RUNG' in x][:6],
                         ['RUNG wire#powerrail', 'END_RUNG', 'RUNG wire#powerrail', 'END_RUNG wire#w5', 'RUNG wire#w1',
                          'END_RUNG wire#w2'])
        back = s7dcl.parse_block('FUNCTION_BLOCK "X"\n    { S7_Language := "LAD" }\n    NETWORK\n%s    END_NETWORK\n'
                                 'END_FUNCTION_BLOCK\n' % text)['networks'][0]
        self.assertTrue(server.diff.content_match(m, back)[0])

    def test_nested_branches_depth_first(self):
        """A branch with its own branch (TIA: main, the closing branch, its sub-branch, then the opening branch)."""
        rungs = [{'from': 'w3', 'path': ['x'], 'to': 'w4'}, {'from': 'powerrail', 'path': [], 'to': 'w2'},
                 {'from': 'powerrail', 'path': ['a', 'wire#w1', 'b', 'wire#w3', 'c', 'wire#w4', 'q'], 'to': None},
                 {'from': 'powerrail', 'path': ['d', 'wire#w2', 'e'], 'to': 'w1'}]
        self.assertEqual([(r['from'], r['to']) for r in s7dcl_write.rung_order(rungs)],
                         [('powerrail', None), ('powerrail', 'w1'), ('powerrail', 'w2'), ('w3', 'w4')])

    def test_nested_or(self):
        self.assertIn('wire#w2 closes at wire#w1', s7dcl_write.nested_or(NESTED))
        simple = s7dcl.load_block(os.path.join(SAMPLES, PROJECT, TS0, 'PLC_1', 'Blocks', 'Conveyor', 'FC_Branches.s7dcl'))
        self.assertIsNone(s7dcl_write.nested_or(simple['networks'][0]))
        self.assertIsNone(s7dcl_write.nested_or({'language': 'FBD', 'elements': []}))


class TestTextsInRes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for f in ('FB_Conveyor.s7dcl', 'FB_Conveyor.s7res'):
            shutil.copy(os.path.join(CONVEYOR, f), self.dir)
        self.file = os.path.join(self.dir, 'FB_Conveyor.s7dcl')
        self.nets = s7dcl.load_block(self.file)['networks']

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def edit(self, nw, after=None, **texts):
        n = self.nets[nw - 1]
        before = {'elements': n['elements'], 'rungs': n['rungs']}
        return dict({'kind': 'network', 'nw': nw, 'title': n['title'], 'before': before, 'after': after or before}, **texts)

    def test_title_and_comment_in_the_documents(self):
        """D17b: comment_after (and title_after) are in the written .s7dcl / .s7res — a network without a comment gets a new
        record right after its title record, the old title record goes; the record order stays the order of the uses."""
        server._replace_in_file(self.file, self.edit(3, title_after='Startup horn', comment_after='two lines\nof comment'))
        after = s7dcl.load_block(self.file)['networks']
        self.assertEqual((after[2]['title'], after[2]['comment']), ('Startup horn', 'two lines\nof comment'))
        self.assertEqual([n['title'] for n in after[:2] + after[3:]], [n['title'] for n in self.nets[:2] + self.nets[3:]])
        with open(self.file[:-6] + '.s7res', encoding='utf-8') as f:
            res = f.read()
        uses = [i for _, i in s7dcl._mlc_uses(s7dcl.read(self.file))]
        self.assertEqual(uses, s7dcl_write.res_ids(res))
        self.assertNotIn('MLC_3\n', res)

    def test_logic_and_comment(self):
        n = self.nets[1]
        after = copy.deepcopy({'elements': n['elements'], 'rungs': n['rungs']})
        after['elements'].append(c('n1', '"Fuse_OK"'))
        after['rungs'].append({'from': 'powerrail', 'path': ['n1'], 'to': 'w1'})
        server._replace_in_file(self.file, self.edit(2, after, comment_after='Start, service start or fuse'))
        got = s7dcl.load_block(self.file)['networks'][1]
        self.assertEqual((got['title'], got['comment']), ('Motor latch', 'Start, service start or fuse'))
        self.assertTrue(server.diff.content_match(after, got)[0])

    def test_repeated_ids(self):
        self.assertEqual(s7dcl_write.repeated_ids('MultiLingualTexts:\n  - id: MLC_a\n    en-US: x\n  - id: MLC_b\n'
                                                  '    en-US: y\n  - id: MLC_a\n    en-US: z\n'), ['MLC_a'])


class TestRoute(unittest.TestCase):
    """Upload route through the API (Demo_Line; the fake Writer has SimaticML of FB_Conveyor only)."""
    U = test_upload_check.TestUploadCheck
    setUp, tearDown, start, stop, req, proposal, imports, calls, OPS = (U.setUp, U.tearDown, U.start, U.stop, U.req, U.proposal,
                                                                        U.imports, U.calls, U.OPS)

    def set_tia(self, **kw):
        self.assertEqual(self.req('PUT', '/api/state/tia?project=%s' % PROJECT, dict(AUTO, **kw))[0], 200)

    def nested(self, extra=None):
        after = copy.deepcopy(NESTED)
        if extra:
            after['elements'].append(extra)
            after['rungs'].append({'from': 'w2', 'path': [extra['id']], 'to': None})
        s, u = self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'X', 'block': 'FB_Conveyor', 'nw': 1, 'after': after})
        self.assertEqual(s, 201, u)
        self.req('POST', '/api/edits/%s/state' % u['id'], {'state': 'Approved'})
        return u['id']

    def test_preferred_simaticml(self):
        """D16: a network change goes through SimaticML when it writes it — no documents re-read of the other networks."""
        self.set_tia()
        uid = self.proposal(ops=self.OPS)
        _, d = self.req('GET', '/api/edits/' + uid)
        self.assertEqual(d['upload_via'], 'SimaticML (%s)' % server.PREFER_XML)
        s, r = self.req('POST', '/api/edits/%s/upload' % uid)
        self.assertTrue(r['ok'], r)
        self.assertEqual(([k['step'] for k in r['write']['steps']][3:], r['write']['via']),
                         (['ExportXmlBlock', 'Edit the SimaticML', 'SaveProject', 'ImportXmlBlock', 'CompileBlock'], 'SimaticML'))
        self.assertNotIn('ImportFromDocuments', [x['tool'] for x in self.calls()])
        self.set_tia(upload='documents')                                     # the setting keeps the documents way
        _, d = self.req('GET', '/api/edits/' + self.proposal(ops=self.OPS))
        self.assertNotIn('upload_via', d)

    def test_documents_when_simaticml_refuses_the_block(self):
        """A block SimaticML cannot prepare (here: no XML export) falls back to documents before any write of it; a group
        writes FB_Conveyor through SimaticML and FC_Branches through documents."""
        self.set_tia()
        a = self.proposal(ops=self.OPS)
        b = self.proposal('FC_Branches', 1, ops=[{'type': 'change', 'id': 'p4', 'values': {'operand': '"Sensor_D"'}}])
        self.req('POST', '/api/groups', {'edit_ids': [a, b]})
        s, r = self.req('POST', '/api/groups/G001/upload', {})
        self.assertTrue(r['ok'], r)
        msg = r['results'][0]['message']
        self.assertIn('through SimaticML (%s' % server.PREFER_XML, msg)
        self.assertIn('through documents: FC_Branches (SimaticML: ', msg)
        self.assertEqual([x['tool'] for x in self.calls() if x['tool'] in ('ImportXmlBlock', 'ImportFromDocuments')],
                         ['ImportXmlBlock', 'ImportFromDocuments'])
        self.assertEqual(r['via'], 'SimaticML')

    def test_nested_or_goes_through_simaticml(self):
        """D15: a nested OR goes through SimaticML even with the documents setting."""
        self.set_tia(upload='documents')
        uid = self.nested()
        _, d = self.req('GET', '/api/edits/' + uid)
        self.assertIn('nested OR', d['upload_via'])
        s, r = self.req('POST', '/api/edits/%s/upload' % uid)
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['write']['via'], 'SimaticML')
        self.assertNotIn('ImportFromDocuments', [x['tool'] for x in self.calls()])

    def test_nested_or_refused_when_simaticml_cannot_write_it(self):
        """D15: nested OR + what SimaticML does not write (a negated coil) — refused before any write, both reasons named."""
        self.set_tia()
        uid = self.nested({'id': 'n2', 'type': 'coil_neg', 'operand': '#Not_Ready'})
        _, d = self.req('GET', '/api/edits/' + uid)
        self.assertIn('nested OR', d['upload_note'])
        s, e = self.req('POST', '/api/edits/%s/upload' % uid)
        self.assertEqual(s, 409, e)
        self.assertIn('nested OR', e['error'])
        self.assertIn('make the change by hand', e['error'])
        self.assertFalse(os.path.exists(os.path.join(self.tia, 'calls.jsonl')))

    def test_repeated_mlc_ids_refused_before_the_import(self):
        """D1: documents whose .s7res repeats an MLC id are not imported (TIA refuses them as corrupted); SimaticML offered."""
        self.set_tia(upload='documents')
        uid = self.proposal(ops=self.OPS)
        os.makedirs(os.path.join(self.tia, 'Conveyor'))
        shutil.copy(os.path.join(CONVEYOR, 'FB_Conveyor.s7dcl'), os.path.join(self.tia, 'Conveyor'))
        with open(os.path.join(CONVEYOR, 'FB_Conveyor.s7res'), encoding='utf-8-sig') as f, \
                open(os.path.join(self.tia, 'Conveyor', 'FB_Conveyor.s7res'), 'w', encoding='utf-8') as g:
            g.write(f.read().rstrip('\n') + '\n  - id: MLC_2\n    en-US: Motor latch\n')
        s, r = self.req('POST', '/api/edits/%s/upload' % uid)
        self.assertEqual((r['ok'], r['edit']['state']), (False, 'Approved'), r)
        self.assertIn('repeats the MLC id(s) MLC_2', r['message'])
        self.assertIn('through SimaticML', r['message'])
        self.assertNotIn('ImportFromDocuments', [x['tool'] for x in self.calls()])
        self.assertNotIn('BLOCK INCONSISTENT', r['message'])


class TestNewNetworksInOneBlock(unittest.TestCase):
    """D11 through SimaticML (FB_Station, a block with GRAPH)."""
    X = test_simaticml.TestXmlUpload
    setUp, tearDown, start, stop, req, edit, calls, written = X.setUp, X.tearDown, X.start, X.stop, X.req, X.edit, X.calls, X.written

    def test_group_of_new_networks_at_different_places(self):
        """Two after NW1 and one after NW2, all counted in the same export, the later place last in the group: no "the
        networks moved", and the two after NW1 keep the group order."""
        net = lambda op: test_simaticml.lad({'type': 'contact', 'operand': '#Run'}, {'type': 'coil', 'operand': op})
        ids = [self.edit(kind='new_network', after_nw=1, title='A', after=net('#Lamp')),
               self.edit(kind='new_network', after_nw=1, title='B', after=net('#Lamp')),
               self.edit(kind='new_network', after_nw=2, title='C', after=net('#Lamp'))]
        self.req('POST', '/api/groups', {'edit_ids': ids})
        self.req('POST', '/api/groups/G001/state', {'state': 'Approved'})
        s, r = self.req('POST', '/api/groups/G001/upload', {})
        self.assertTrue(r['ok'], r)
        nets = server.simaticml.networks(self.written())
        self.assertEqual([n['title'] for n in nets], ['Sequence', 'A', 'B', 'Run lamp', 'C'])
        self.assertEqual(len(self.calls('ImportXmlBlock')), 1)


if __name__ == '__main__':
    unittest.main()
