import os
import sys

KOREN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if KOREN not in sys.path:
    sys.path.insert(0, KOREN)
VZORKY = os.path.join(KOREN, 'tests', 'vzorky')
PROJEKT = 'Demo_Linka'
