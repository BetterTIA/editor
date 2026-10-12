"""Katalog prvků pro paletu editoru (M3).

- pevné instrukce LAD s piny (šablony prvků ve tvaru modelu networku, bez `id`),
- bloky projektu z exportu: FB/FC s piny z rozhraní `.s7dcl`, instanční DB (`DATA_BLOCK "X" : "FB"`),
- operandy pro našeptávání: tagy z tag tabulek (`<PLC>\\Tags\\**\\*.xml`, SimaticML) a členy globálních DB.
Čte se jen nejnovější export každého bloku / tabulky; rozhraní se čte jen po první NETWORK/BEGIN.
"""
import copy
import os
import re
import xml.etree.ElementTree as ET

import s7dcl


def _box(nazev, napajeni=('EN', 'ENO'), vstupy=(), vystupy=(), sablona=None, instance=None, **navic):
    p = {'typ': 'box', 'volani': 'instrukce', 'nazev': nazev, 'instance': instance, 'sablona': sablona,
         'napajeni': list(napajeni), 'vstupy': [{'pin': x, 'operand': ''} for x in vstupy],
         'vystupy': [{'pin': x, 'operand': ''} for x in vystupy]}
    p.update(navic)
    return p


def _instrukce():
    I = []

    def add(klic, nazev, skupina, prvek, popis=''):
        I.append({'klic': klic, 'nazev': nazev, 'skupina': skupina, 'prvek': prvek, 'popis': popis})
    add('NO', '-| |-', 'Kontakty', {'typ': 'kontakt', 'operand': ''}, 'spínací kontakt')
    add('NC', '-|/|-', 'Kontakty', {'typ': 'kontakt_nc', 'operand': ''}, 'rozpínací kontakt')
    add('P', '-|P|-', 'Kontakty', {'typ': 'kontakt_p', 'operand': '', 'bit': ''}, 'náběžná hrana')
    add('N', '-|N|-', 'Kontakty', {'typ': 'kontakt_n', 'operand': '', 'bit': ''}, 'sestupná hrana')
    add('C', '-( )-', 'Cívky', {'typ': 'civka', 'operand': ''}, 'cívka')
    add('S', '-(S)-', 'Cívky', {'typ': 'civka_s', 'operand': ''}, 'nastavit')
    add('R', '-(R)-', 'Cívky', {'typ': 'civka_r', 'operand': ''}, 'nulovat')
    add('NEG', '-(/)-', 'Cívky', {'typ': 'civka_neg', 'operand': ''}, 'negovaná cívka')
    add('RET', 'RET', 'Cívky', {'typ': 'ret', 'operand': ''}, 'návrat z bloku')
    for k, op in s7dcl.POROVNANI.items():
        add(k, 'CMP ' + op, 'Porovnání', {'typ': 'porovnani', 'op': op, 'in1': '', 'in2': '', 'sablona': 'Int'},
            k + '_Contact, S7_Templates SrcType')
    for k in ('TON', 'TOF', 'TP'):
        add(k, k, 'Časovače', _box(k, ('IN', 'Q'), ['PT'], ['ET'], 'Time', ''), 'instance: #multi nebo "DB"')
    add('CTU', 'CTU', 'Čítače', _box('CTU', ('CU', 'Q'), ['R', 'PV'], ['CV'], 'Int', ''))
    add('CTD', 'CTD', 'Čítače', _box('CTD', ('CD', 'QD'), ['LD', 'PV'], ['CV'], 'Int', ''))
    add('MOVE', 'MOVE', 'Přesun', _box('MOVE', vstupy=['IN'], vystupy=['OUT1']))
    add('SWAP', 'SWAP', 'Přesun', _box('SWAP', vstupy=['IN'], vystupy=['OUT'], sablona='Word'))
    add('CONV', 'CONV', 'Přesun', _box('CONV', vstupy=['IN'], vystupy=['OUT'], sablona='Int to DInt'))
    for k in ('ADD', 'SUB', 'MUL', 'DIV'):
        add(k, k, 'Matematika', _box(k, vstupy=['IN1', 'IN2'], vystupy=['OUT'], sablona='Int'))
    add('IN_RANGE', 'IN_RANGE', 'Porovnání', _box('IN_RANGE', ('', ''), ['MIN', 'VAL', 'MAX'], [], 'Int'))
    add('P_TRIG', 'P_TRIG', 'Hrany', _box('P_TRIG', ('CLK', 'Q'), pod=''), 'bit paměti hrany pod boxem')
    add('N_TRIG', 'N_TRIG', 'Hrany', _box('N_TRIG', ('CLK', 'Q'), pod=''))
    return I


INSTRUKCE = _instrukce()

_SEKCE = {'VAR_INPUT': 'vstupy', 'VAR_OUTPUT': 'vystupy', 'VAR_IN_OUT': 'inout', 'VAR_TEMP': 'temp'}
_cache = {}   # cesta -> (mtime, výsledek)


def rozhrani(text):
    """Hlavička + rozhraní bloku (.s7dcl) bez networků -> {druh, nazev, cislo, instance_typ, sekce: {..: [{jmeno, typ}]}}.
    Členy jen nejvyšší úrovně (STRUCT = 'Struct', jeho obsah se přeskočí)."""
    m = re.search(r'^\s*(?:NETWORK|BEGIN)\s*$', text, re.M)
    hlava = text[:m.start()] if m else text
    # shortcut: atributy `{…}` se mažou regexem bez ohledu na uvozovky — `}` uvnitř hodnoty atributu by to rozbilo
    hlava_bez = re.sub(r'\{[^{}]*\}', '', hlava)
    if not re.search(r'^(FUNCTION_BLOCK|FUNCTION|DATA_BLOCK)\b', hlava_bez, re.M):
        return None
    b = s7dcl.parse_blok(hlava_bez + '\n')
    hm = re.search(r'S7_BlockNumber\s*:=\s*"(\d+)"', hlava)
    vys = {'druh': b['druh'], 'nazev': b['nazev'], 'cislo': int(hm.group(1)) if hm else None,
           'instance_typ': b['instance_typ'], 'sekce': {}, 'f': bool(re.search(r'S7_Safety\s*:=\s*"TRUE"', hlava))}
    sekce, hloubka = None, 0
    for r in hlava_bez.splitlines():
        s = r.strip()
        if not s:
            continue
        sm = re.match(r'^(VAR_INPUT|VAR_OUTPUT|VAR_IN_OUT|VAR_TEMP|VAR)\b(?!\s*:)', s)
        if sm and hloubka == 0:
            sekce = 'konstanty' if 'CONSTANT' in s else _SEKCE.get(sm.group(1), 'staticke')
            continue
        if s.startswith('END_VAR'):
            sekce = None
            continue
        if sekce is None:
            continue
        if s.startswith('END_STRUCT'):
            hloubka -= 1
            continue
        dm = re.match(r'^("[^"]+"|[A-Za-z_]\w*)\s*:\s*([^;:=]+?)\s*(:=.*)?;?\s*$', s)
        if not dm:
            continue
        typ = dm.group(2).strip()
        struct = bool(re.search(r'\bSTRUCT$', typ))
        if struct:
            typ = typ[:-6] + 'Struct'
        if hloubka == 0:
            typ = typ[2:] if typ.startswith('_.') else typ
            vys['sekce'].setdefault(sekce, []).append({'jmeno': dm.group(1).strip('"'), 'typ': typ.strip('"')})
        if struct:
            hloubka += 1
    return vys


def _cti_cache(cesta, fce):
    try:
        mt = os.path.getmtime(cesta)
    except OSError:
        return None
    c = _cache.get(cesta)
    if c and c[0] == mt:
        return c[1]
    try:
        v = fce(cesta)
    except (s7dcl.ChybaParseru, ET.ParseError, OSError, UnicodeDecodeError, ValueError):
        v = None
    _cache[cesta] = (mt, v)
    return v


def tag_tabulka(cesta):
    """SimaticML tag tabulka -> [{jmeno, typ, adresa, tabulka}]."""
    koren = ET.parse(cesta).getroot()
    vys = []
    for tab in koren.iter('SW.Tags.PlcTagTable'):
        jm_tab = tab.findtext('AttributeList/Name') or ''
        for t in tab.iter('SW.Tags.PlcTag'):
            jm = t.findtext('AttributeList/Name')
            if jm:
                vys.append({'jmeno': jm, 'typ': t.findtext('AttributeList/DataTypeName') or '',
                            'adresa': t.findtext('AttributeList/LogicalAddress') or '', 'tabulka': jm_tab})
    return vys


def _nejnovejsi(root, projekt, pripona, podslozka=None):
    """{klíč: plná cesta} z nejnovějšího exportu, který soubor obsahuje (klíč = jméno souboru,
    s podsložkou relativní cesta pod ní)."""
    pd = os.path.join(root, projekt)
    vys = {}
    if not os.path.isdir(pd):
        return vys
    for ts in sorted(os.listdir(pd)):
        if not re.match(r'^\d{8}-\d{6}$', ts):
            continue
        for dp, _, soubory in os.walk(os.path.join(pd, ts)):
            casti = os.path.relpath(dp, os.path.join(pd, ts)).split(os.sep)
            if podslozka and podslozka not in casti:
                continue
            for f in soubory:
                if f.endswith(pripona):
                    k = os.path.join(*casti[casti.index(podslozka):], f) if podslozka else f
                    vys[k] = os.path.join(dp, f)            # novější export přepíše starší
    return vys


def sablona_bloku(b, instance=''):
    """Šablona prvku palety pro volání FB/FC z rozhraní (IN_OUT jako vstupy vlevo)."""
    s = b['sekce']
    return {'typ': 'box', 'volani': 'fb' if b['druh'] == 'FB' else 'fc', 'nazev': b['nazev'],
            'instance': instance if b['druh'] == 'FB' else None, 'sablona': None,
            'napajeni': ['EN', 'ENO'],
            'vstupy': [{'pin': x['jmeno'], 'operand': ''} for x in s.get('vstupy', []) + s.get('inout', [])],
            'vystupy': [{'pin': x['jmeno'], 'operand': ''} for x in s.get('vystupy', [])]}


def katalog(root, projekt):
    bloky, dbs = [], []
    for _, cesta in sorted(_nejnovejsi(root, projekt, '.s7dcl').items()):
        r = _cti_cache(cesta, lambda c: rozhrani(s7dcl.cti(c)))
        if r:
            (dbs if r['druh'] == 'DB' else bloky).append(r)
    inst = {}
    for d in dbs:
        if d['instance_typ']:
            inst.setdefault(d['instance_typ'], []).append('"%s"' % d['nazev'])
    vys_bloky = []
    for b in bloky:
        if b['druh'] in ('FB', 'FC'):
            instance = inst.get(b['nazev'], [])
            vys_bloky.append({'klic': '%s:%s' % (b['druh'].lower(), b['nazev']), 'nazev': b['nazev'],
                              'druh': b['druh'], 'cislo': b['cislo'], 'rozhrani': b['sekce'], 'instance': instance,
                              'f': b.get('f', False),
                              'prvek': sablona_bloku(b, instance[0] if instance else '')})
    tagy = []
    for cesta in _nejnovejsi(root, projekt, '.xml', 'Tags').values():
        tagy += _cti_cache(cesta, tag_tabulka) or []
    import fbd                                       # líně: fbd importuje s7dcl
    return {'projekt': projekt, 'instrukce': INSTRUKCE, 'bloky': vys_bloky, 'fbd': fbd.PALETA,
            'db': [{'nazev': d['nazev'], 'cislo': d['cislo'], 'instance_typ': d['instance_typ'],
                    'cleny': [] if d['instance_typ'] else [c for v in d['sekce'].values() for c in v]} for d in dbs],
            'tagy': sorted(tagy, key=lambda t: t['jmeno'].casefold())}


def prvek(kat, klic):
    """Šablona prvku podle klíče palety ('NO', 'TON', 'fb:FB_Pas', …) -> kopie ; KeyError když není."""
    for x in kat['instrukce'] + kat['bloky']:
        if x['klic'] == klic:
            return copy.deepcopy(x['prvek'])
    raise KeyError('prvek palety %s neexistuje' % klic)
