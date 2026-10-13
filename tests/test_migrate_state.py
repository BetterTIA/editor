"""tools/migrate_state.py: an old stav.json (Czech keys) becomes a state.json the current code accepts."""
import json
import os
import sys
import tempfile
import unittest

from tests.common import ROOT
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import migrate_state
import model
import state

OLD = {'upravy': [{
    'id': 'U001', 'projekt': 'Demo_Line', 'kolo': 'Round 1', 'blok': 'FB_Conveyor', 'druh': 'FB', 'cislo_bloku': 10,
    'nw': 2, 'titulek': 'Motor latch', 'jazyk': 'LAD', 'f': False, 'autor': 'editor', 'cas': '2026-10-12T10:00:00',
    'stav': 'Approved', 'poznamka': 'fuse before the coil',
    'historie': [{'stav': 'Proposal', 'cas': '2026-10-12T10:00:00', 'kdo': 'editor'}],
    'pred': {'prvky': [{'id': 'p1', 'typ': 'kontakt', 'operand': '#Start'},
                       {'id': 'p2', 'typ': 'civka', 'operand': '#Motor'}],
             'rungy': [{'z': 'powerrail', 'cesta': ['p1', 'p2'], 'do': None}], 'export_ts': '20261012-120000'},
    'po': {'prvky': [{'id': 'p1', 'typ': 'kontakt', 'operand': '#Start'},
                     {'id': 'n1', 'typ': 'box', 'volani': 'instrukce', 'nazev': 'MOVE', 'instance': None,
                      'sablona': None, 'napajeni': ['EN', 'ENO'], 'vstupy': [{'pin': 'IN', 'operand': '1'}],
                      'vystupy': [{'pin': 'OUT1', 'operand': '#X'}]}],
           'rungy': [{'z': 'powerrail', 'cesta': ['p1', 'n1'], 'do': None}]},
    'kontroly': [{'cas': '2026-10-12T11:00:00', 'shoda': True, 'export_ts': '20261012-140000', 'poznamka': 'ok'}],
    'zapisy': [{'cas': '2026-10-12T11:00:00', 'nastroj': 'TIAC_Writer', 'ok': True, 'zprava': 'ok',
                'kroky': [{'krok': 'Connect', 'ok': True, 'zprava': ''}]}],
}], 'tia': {'softwarePath': 'PLC_1'}}


class TestMigrateState(unittest.TestCase):
    def test_migrate(self):
        new = migrate_state.migrate(OLD)
        state.verify_structure(new)                                     # the server accepts it
        u = new['edits'][0]
        self.assertEqual((u['project'], u['round'], u['block'], u['block_number'], u['title'], u['state']),
                         ('Demo_Line', 'Round 1', 'FB_Conveyor', 10, 'Motor latch', 'Approved'))
        self.assertEqual(u['history'][0]['by'], 'editor')
        self.assertEqual(u['before']['rungs'][0], {'from': 'powerrail', 'path': ['p1', 'p2'], 'to': None})
        self.assertEqual([p['type'] for p in u['after']['elements']], ['contact', 'box'])
        self.assertEqual(u['after']['elements'][1]['call'], 'instruction')
        self.assertEqual(u['checks'][0]['match'], True)
        self.assertEqual(u['writes'][0]['steps'][0]['step'], 'Connect')
        self.assertEqual(new['tia'], {'softwarePath': 'PLC_1'})
        self.assertEqual(model.validate(u['after']), [])                # the model is usable by the editor

    def test_cli_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.path.join(d, 'stav.json')
            with open(old, 'w', encoding='utf-8') as f:
                json.dump(OLD, f)
            self.assertEqual(migrate_state.main([old]), 0)
            with open(os.path.join(d, 'state.json'), encoding='utf-8') as f:
                self.assertEqual(len(json.load(f)['edits']), 1)
            self.assertEqual(migrate_state.main([old]), 2)              # state.json exists -> refused


if __name__ == '__main__':
    unittest.main()
