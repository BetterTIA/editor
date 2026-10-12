"""Katalog prvků (instrukce + FB/FC s piny + tagy/DB z exportu). Stub — plní se v M3."""

INSTRUKCE = ['Contact', 'I_Contact', 'Coil', 'S_Coil', 'R_Coil', 'TON', 'TOF', 'TP', 'Move',
             'EQ_Contact', 'NE_Contact', 'GT_Contact', 'LT_Contact', 'GE_Contact', 'LE_Contact']


def katalog(projekt):
    return {'projekt': projekt, 'instrukce': INSTRUKCE, 'bloky': []}
