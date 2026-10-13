import copy
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SAMPLES = os.path.join(ROOT, 'tests', 'samples')
PROJECT = 'Demo_Linka'
TS0 = '20261012-120000'      # původní export (20261012-140000 = export po zápisu úpravy NW1)
TS1 = '20261012-140000'
TS2 = '20261012-150000'     # export po zápisu FBD úpravy (jen FB_Bezpecnost, NW1: & + in4, = #Povoleni_Pohon)


def after_nw2(net):
    """Ukázková úprava NW2 FB_Dopravnik: přidaný kontakt před cívku, změněný operand, smazaný NC ve 3. větvi."""
    after = {'elements': copy.deepcopy(net['elements']), 'rungs': copy.deepcopy(net['rungs'])}
    for p in after['elements']:
        if p['id'] == 'p3':
            p['operand'] = '#Pripraven_Auto'
    after['elements'] = [p for p in after['elements'] if p['id'] != 'p7']
    after['elements'].append({'id': 'n1', 'type': 'contact', 'operand': '"Pojistka_OK"'})
    for r in after['rungs']:
        r['path'] = [x for x in r['path'] if x != 'p7']
        if 'p4' in r['path']:
            r['path'].insert(r['path'].index('p4'), 'n1')
    return after
