import copy
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SAMPLES = os.path.join(ROOT, 'tests', 'samples')
PROJECT = 'Demo_Line'
TS0 = '20261012-120000'      # original export (20261012-140000 = export after writing the NW1 edit)
TS1 = '20261012-140000'
TS_IF = '20261012-130000'   # export after writing an interface edit (FB_Conveyor only: + Jam_Detected, Jam_Count)
TS2 = '20261012-150000'     # export after writing the FBD edit (FB_Safety only, NW1: & + in4, = #Enable_Drive)


def after_nw2(net):
    """Sample edit of FB_Conveyor NW2: contact added before the coil, operand changed, NC contact in the 3rd branch deleted."""
    after = {'elements': copy.deepcopy(net['elements']), 'rungs': copy.deepcopy(net['rungs'])}
    for p in after['elements']:
        if p['id'] == 'p3':
            p['operand'] = '#Ready_Auto'
    after['elements'] = [p for p in after['elements'] if p['id'] != 'p7']
    after['elements'].append({'id': 'n1', 'type': 'contact', 'operand': '"Fuse_OK"'})
    for r in after['rungs']:
        r['path'] = [x for x in r['path'] if x != 'p7']
        if 'p4' in r['path']:
            r['path'].insert(r['path'].index('p4'), 'n1')
    return after
