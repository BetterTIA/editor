"""Round 12 / D5: Upload of an hmi_screen edit that deletes and adds items and sets dynamizations, through the Writer's
DeleteHmiScreenItems, CreateHmiScreenItems, SetHmiItemDynamization, SetHmiDynamizationTag and SetHmiItemAttribute — every
call as a dry run before the first real write; a group is refused before any write when a dry run fails. Fake Writer/Reader."""
import json
import unittest

import server
from tests.common import PROJECT
from tests import test_upload_safety as k9


class TestHmiUpload(unittest.TestCase):
    setUp, start, tearDown, req, proposal, hmi_edit = (k9.TestUploadSafety.__dict__[n] for n in (
        'setUp', 'start', 'tearDown', 'req', 'proposal', 'hmi_edit'))     # the round 9 helpers, not its tests

    def calls(self, full=False):
        import os
        p = os.path.join(self.tia, 'calls.jsonl')
        if not os.path.exists(p):
            return []
        with open(p, encoding='utf-8') as f:
            z = [json.loads(x) for x in f]
        return z if full else [x['tool'] for x in z]

    def screen_edit(self, change):
        s, u = self.req('POST', '/api/edits', {'project': PROJECT, 'round': 'K12', 'kind': 'hmi_screen', 'screen': 'Overview'})
        after = json.loads(json.dumps(u['after']))
        change({i['name']: i for i in after['items']}, after)
        s, r = self.req('POST', '/api/edits/%s/after' % u['id'], {'after': after})
        self.assertEqual(s, 200, r)
        return u['id']

    @staticmethod
    def big_change(x, after):
        after['items'] = [i for i in after['items'] if i['name'] != 'Separator']                     # deleted
        after['items'].append({'name': 'Btn_Reset', 'type': 'Button', 'left': 460, 'top': 720, 'width': 200, 'height': 56,
                               'text': 'Reset\nall', 'colors': {'backColor': '#2f6fa3'}, 'font': {'size': 18},
                               'bindings': [{'property': 'Visible', 'tag': 'Mode_Auto'}],
                               'events': [{'event': 'Tapped', 'script': 'HMIRuntime.Trace("x");'}]})   # added
        x['IO_Speed']['bindings'].append({'property': 'Visible', 'tag': 'Mode_Auto'})              # a new dynamization
        x['IO_Pieces']['bindings'][0]['tag'] = 'Rejects'                                             # another tag
        x['Label_Pieces']['visible'] = False
        x['Label_Speed']['font']['size'] = 22
        x['Title']['text'] = 'Line 1\noverview'

    def test_delete_add_and_dynamize(self):
        self.start()
        uid = self.screen_edit(self.big_change)
        d = self.req('GET', '/api/edits/' + uid)[1]
        self.assertNotIn('upload_note', d)
        self.req('POST', '/api/edits/%s/state' % uid, {'state': 'Approved'})
        s, r = self.req('POST', '/api/edits/%s/upload' % uid, {})
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['edit']['state'], 'Signed')
        hmi = [c for c in self.calls(True) if c['tool'] not in ('GetPortals', 'Connect')]
        dry = [c['arguments']['dryRun'] for c in hmi]
        self.assertEqual(dry, [True] * (len(hmi) // 2) + [False] * (len(hmi) // 2))      # every dry run before the first write
        real = hmi[len(hmi) // 2:]
        self.assertEqual([c['tool'] for c in real[:2]], ['DeleteHmiScreenItems', 'CreateHmiScreenItems'])
        self.assertEqual(real[0]['arguments']['names'], 'Separator')
        new = json.loads(real[1]['arguments']['items'])[0]
        self.assertEqual((new['type'], new['text'], new['properties'], new['dynamizations'], new['events']),
                         ('Button', 'Reset\nall', {'BackColor': '#FF2F6FA3', 'Font.Size': '18'}, {'Visible': 'Mode_Auto'},
                          {'Tapped': 'HMIRuntime.Trace("x");'}))
        sets = sorted((c['tool'], c['arguments']['itemName'], c['arguments'].get('attribute') or c['arguments'].get('property'),
                       c['arguments'].get('value') or c['arguments'].get('tag') or c['arguments'].get('dynamization')) for c in real[2:])
        self.assertEqual(sets, [('SetHmiDynamizationTag', 'IO_Pieces', 'ProcessValue', 'Rejects'),
                                ('SetHmiItemAttribute', 'Label_Pieces', 'Visible', 'false'),
                                ('SetHmiItemAttribute', 'Label_Speed', 'Font.Size', '22'),
                                ('SetHmiItemAttribute', 'Title', 'Text', 'Line 1\noverview'),
                                ('SetHmiItemDynamization', 'IO_Speed', 'Visible', 'Mode_Auto')])
        s, k = self.req('POST', '/api/edits/%s/check' % uid, {})              # the Reader's dump of what the fake wrote
        self.assertTrue(k['match'], k)
        self.assertEqual(k['edit']['state'], 'Checked')

    def test_refused_dry_run_writes_nothing(self):
        self.start('--fail-hmi', 'IO_Speed')
        uid = self.screen_edit(lambda x, a: (x['IO_Speed'].update(left=300), x['Title'].update(text='T')))
        self.req('POST', '/api/edits/%s/state' % uid, {'state': 'Approved'})
        s, r = self.req('POST', '/api/edits/%s/upload' % uid, {})
        self.assertFalse(r['ok'], r)
        self.assertIn('nothing was written', r['message'])
        self.assertIn('dry run SetHmiItemAttribute IO_Speed Left', r['message'])
        self.assertNotIn(False, [c['arguments'].get('dryRun', True) for c in self.calls(True)])
        u = self.req('GET', '/api/edits/' + uid)[1]['edit']
        self.assertEqual((u['state'], u['writes'][-1]['ok']), ('Approved', False))

    def test_failed_batch_item_refuses(self):
        """A batch whose dry run reports a failed item (not an error of the whole call) refuses too."""
        self.start('--fail-hmi', 'Btn_Reset')

        def two_new(x, after):
            self.big_change(x, after)
            after['items'].append(dict(after['items'][-1], name='Btn_Other', left=680, bindings=[]))
        uid = self.screen_edit(two_new)
        self.req('POST', '/api/edits/%s/state' % uid, {'state': 'Approved'})
        s, r = self.req('POST', '/api/edits/%s/upload' % uid, {})
        self.assertFalse(r['ok'], r)
        self.assertIn('the Writer refused Btn_Reset', r['message'])
        self.assertNotIn(False, [c['arguments'].get('dryRun', True) for c in self.calls(True)])

    def test_batch_that_skips_an_item_when_written_counts_as_written(self):
        """The dry run passes, the real CreateHmiScreenItems skips one item (TIA refused it) and writes the other: the
        message must not say "nothing was written" — the Delete and the Create did write."""
        self.start('--fail-hmi-write', 'Btn_Other')

        def two_new(x, after):
            self.big_change(x, after)
            after['items'].append(dict(after['items'][-1], name='Btn_Other', left=680, bindings=[]))
        uid = self.screen_edit(two_new)
        self.req('POST', '/api/edits/%s/state' % uid, {'state': 'Approved'})
        s, r = self.req('POST', '/api/edits/%s/upload' % uid, {})
        self.assertFalse(r['ok'], r)
        self.assertIn('the Writer refused Btn_Other', r['message'])
        self.assertIn('2 of 7 Writer call(s) were written', r['message'])
        self.assertNotIn('nothing was written', r['message'])
        self.assertEqual(self.req('GET', '/api/edits/' + uid)[1]['edit']['state'], 'Approved')

    def unknown_tag(self, change):
        self.start()
        uid = self.screen_edit(change)
        self.req('POST', '/api/edits/%s/state' % uid, {'state': 'Approved'})
        s, r = self.req('POST', '/api/edits/%s/upload' % uid, {})
        self.assertFalse(r['ok'], r)
        self.assertIn('no hmi tag named', r['message'].lower())
        self.assertIn('nothing was written', r['message'])
        self.assertNotIn(False, [c['arguments'].get('dryRun', True) for c in self.calls(True)])

    def test_unknown_tag_of_a_changed_binding_refused_at_dry_run(self):
        """SetHmiDynamizationTag only warns about a tag that is no HMI tag, and would write it."""
        self.unknown_tag(lambda x, a: x['IO_Pieces']['bindings'][0].update(tag='No_Such_Tag'))

    def test_unknown_tag_of_a_new_binding_refused_at_dry_run(self):
        self.unknown_tag(lambda x, a: x['IO_Speed']['bindings'].append({'property': 'Visible', 'tag': 'No_Such_Tag'}))

    def test_group_dry_run_refused_before_the_plc_write(self):
        self.start('--fail-hmi', 'IO_Speed')
        net, scr = self.proposal(approve=False), self.hmi_edit()
        self.req('POST', '/api/groups', {'edit_ids': [net, scr]})
        self.req('POST', '/api/groups/G001/state', {'state': 'Approved'})
        s, r = self.req('POST', '/api/groups/G001/upload', {})
        self.assertEqual(s, 409, r)
        self.assertIn('refused before any write', r['error'])
        self.assertIn('%s: dry run SetHmiItemAttribute IO_Speed Left' % scr, r['error'])
        tools = self.calls()
        self.assertFalse({'SaveProject', 'ImportFromDocuments', 'ExportBlocksAsDocuments'} & set(tools), tools)
        self.assertEqual({self.req('GET', '/api/edits/' + x)[1]['edit']['state'] for x in (net, scr)}, {'Approved'})

    def test_group_with_new_items(self):
        self.start()
        net, scr = self.proposal(approve=False), self.screen_edit(self.big_change)
        self.req('POST', '/api/groups', {'edit_ids': [scr, net]})
        self.req('POST', '/api/groups/G001/state', {'state': 'Approved'})
        s, r = self.req('POST', '/api/groups/G001/upload', {})
        self.assertTrue(r['ok'], r)
        tools = self.calls()
        first_import = tools.index('ImportFromDocuments')
        self.assertIn('CreateHmiScreenItems', tools[:first_import])          # the group's dry runs come first …
        self.assertGreater(len(tools) - 1 - tools[::-1].index('CreateHmiScreenItems'), first_import)   # … the write after the PLC

    def test_what_stays_refused(self):
        """No tool: a removed binding, color or font, a changed type, a new screen."""
        u = {'hmi': 'HMI_1', 'screen': 'S', 'new': False, 'before': {'screen': {'name': 'S', 'width': 100, 'height': 100, 'items': [
            {'name': 'A', 'type': 'IOField', 'left': 0, 'top': 0, 'width': 10, 'height': 10, 'colors': {'backColor': '#ffffff'},
             'bindings': [{'property': 'ProcessValue', 'tag': 'X'}]}]}}}
        u['after'] = dict(u['before']['screen'], items=[dict(u['before']['screen']['items'][0], bindings=[], colors={})])
        with self.assertRaises(server.state.StateError) as e:
            server.hmi_writes(u)
        self.assertIn('A: colors.backColor removed', str(e.exception))
        self.assertIn('A: binding ProcessValue removed', str(e.exception))
        with self.assertRaises(server.state.StateError):
            server.hmi_writes(dict(u, new=True, after=u['before']['screen']))

if __name__ == '__main__':
    unittest.main()
