import copy
import os
import sys

KOREN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if KOREN not in sys.path:
    sys.path.insert(0, KOREN)
VZORKY = os.path.join(KOREN, 'tests', 'vzorky')
PROJEKT = 'Demo_Linka'


def po_nw2(net):
    """Ukázková úprava NW2 FB_Dopravnik: přidaný kontakt před cívku, změněný operand, smazaný NC ve 3. větvi."""
    po = {'prvky': copy.deepcopy(net['prvky']), 'rungy': copy.deepcopy(net['rungy'])}
    for p in po['prvky']:
        if p['id'] == 'p3':
            p['operand'] = '#Pripraven_Auto'
    po['prvky'] = [p for p in po['prvky'] if p['id'] != 'p7']
    po['prvky'].append({'id': 'n1', 'typ': 'kontakt', 'operand': '"Pojistka_OK"'})
    for r in po['rungy']:
        r['cesta'] = [x for x in r['cesta'] if x != 'p7']
        if 'p4' in r['cesta']:
            r['cesta'].insert(r['cesta'].index('p4'), 'n1')
    return po
