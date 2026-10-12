"""Parser .s7dcl (export TIA V20+/Reader) -> model networku; titulky z .s7res.

Gramatika je odvozená z exportů V21 (nedokumentovaná). Podporuje LAD:
RUNG wire#<z> ... END_RUNG [wire#<do>], vložené `wire#wN`, atributy `{ ... }` před prvkem,
prvky `Jmeno( args )`, `NOT Coil( .. )`, volání `"Inst"( .. )`, `#inst( .. )`, `#inst.TON( .. )`.
"""
import os
import re

POROVNANI = {'EQ': '==', 'NE': '<>', 'GT': '>', 'LT': '<', 'GE': '>=', 'LE': '<='}
# zobrazované názvy instrukcí (export používá CamelCase, TIA velká písmena)
NAZVY = {
    'Move': 'MOVE', 'Add': 'ADD', 'Sub': 'SUB', 'Mul': 'MUL', 'Div': 'DIV', 'Mod': 'MOD',
    'Convert': 'CONV', 'InRange': 'IN_RANGE', 'OutRange': 'OUT_RANGE', 'Swap': 'SWAP',
    'Abs': 'ABS', 'Inc': 'INC', 'Dec': 'DEC', 'Neg': 'NEG', 'Min': 'MIN', 'Max': 'MAX',
    'Limit': 'LIMIT', 'Sel': 'SEL', 'Mux': 'MUX', 'Calculate': 'CALCULATE', 'Round': 'ROUND',
    'Trunc': 'TRUNC', 'Ceil': 'CEIL', 'Floor': 'FLOOR', 'P_Trig': 'P_TRIG', 'N_Trig': 'N_TRIG',
    'S_SR': 'SR', 'S_RS': 'RS', 'Scale_X': 'SCALE_X', 'Norm_X': 'NORM_X', 'Fill': 'FILL',
}
# výkonové piny (vstup, výstup) podle instrukce; ostatní boxy EN/ENO
NAPAJENI = {
    'TON': ('IN', 'Q'), 'TOF': ('IN', 'Q'), 'TP': ('IN', 'Q'), 'TONR': ('IN', 'Q'),
    'CTU': ('CU', 'Q'), 'CTD': ('CD', 'QD'), 'CTUD': ('CU', 'QU'),
    'R_TRIG': ('CLK', 'Q'), 'F_TRIG': ('CLK', 'Q'), 'P_TRIG': ('CLK', 'Q'), 'N_TRIG': ('CLK', 'Q'),
    'SR': ('S', 'Q'), 'RS': ('R', 'Q'), 'IN_RANGE': ('', ''), 'OUT_RANGE': ('', ''),
}
CIVKY = {'Coil': 'civka', 'S_Coil': 'civka_s', 'R_Coil': 'civka_r', 'P_Coil': 'civka_p', 'N_Coil': 'civka_n'}


def cti(cesta):
    with open(cesta, encoding='utf-8-sig') as f:
        return f.read()


class ChybaParseru(Exception):
    pass


# ---------- nízká úroveň: čtení textu s ohledem na uvozovky ----------

def _konec_retezce(t, i):
    """t[i] je " nebo ' -> index za uzavírací uvozovkou."""
    q = t[i]
    j = t.find(q, i + 1)
    if j < 0:
        raise ChybaParseru('neuzavřený řetězec')
    return j + 1


def _parovy(t, i, otv, zav):
    """t[i] == otv -> index za párovou závorkou (uvozovky se přeskakují)."""
    d = 0
    while i < len(t):
        c = t[i]
        if c in '"\'':
            i = _konec_retezce(t, i)
            continue
        if c == otv:
            d += 1
        elif c == zav:
            d -= 1
            if d == 0:
                return i + 1
        i += 1
    raise ChybaParseru('neuzavřená závorka ' + otv)


def rozdel_carkami(t):
    """Rozdělí text argumentů čárkami mimo uvozovky a závorky."""
    casti, d, z, i = [], 0, 0, 0
    while i < len(t):
        c = t[i]
        if c in '"\'':
            i = _konec_retezce(t, i)
            continue
        if c in '([':
            d += 1
        elif c in ')]':
            d -= 1
        elif c == ',' and d == 0:
            casti.append(t[z:i])
            z = i + 1
        i += 1
    casti.append(t[z:])
    return [c.strip() for c in casti]


def atributy(t):
    """'{ S7_A := "x"; S7_B := "y" }' -> {'S7_A': 'x', ...}"""
    return {m.group(1): m.group(2) for m in re.finditer(r'(S7_\w+)\s*:=\s*"([^"]*)"', t)}


# ---------- .s7res ----------

def nacti_s7res(cesta):
    """MultiLingualTexts -> {id: {jazyk: text}}. Mini-YAML: prosté, '...' / "..." hodnoty a bloky |, |-, |+."""
    if not os.path.exists(cesta):
        return {}
    radky = cti(cesta).splitlines()
    texty, akt, i = {}, None, 0
    while i < len(radky):
        r = radky[i]
        m = re.match(r'^\s*-\s+id:\s*(\S+)\s*$', r)
        if m:
            akt = texty.setdefault(m.group(1), {})
            i += 1
            continue
        m = re.match(r'^(\s+)([a-z]{2}-[A-Z]{2}):\s?(.*)$', r)
        if m and akt is not None:
            odsaz, jazyk, h = len(m.group(1)), m.group(2), m.group(3)
            if h[:1] in '|>':
                blok = []
                i += 1
                while i < len(radky) and (not radky[i].strip() or len(radky[i]) - len(radky[i].lstrip()) > odsaz):
                    blok.append(radky[i].strip())
                    i += 1
                akt[jazyk] = '\n'.join(blok).strip()
                continue
            if h.startswith("'") and h.endswith("'") and len(h) > 1:
                h = h[1:-1].replace("''", "'")
            elif h.startswith('"') and h.endswith('"') and len(h) > 1:
                h = h[1:-1].replace('\\"', '"')
            akt[jazyk] = h
        i += 1
    return texty


def text_mlc(texty, ident, jazyky=('cs-CZ', 'en-US')):
    if not ident:
        return ''
    t = texty.get(ident)
    if t is None:
        return '' if ident.startswith('MLC_') else ident
    for j in jazyky:
        if t.get(j):
            return t[j]
    return next(iter(t.values()), '')


# ---------- blok ----------

def nacti_blok(cesta_s7dcl):
    text = cti(cesta_s7dcl)
    blok = parse_blok(text)
    texty = nacti_s7res(cesta_s7dcl[:-len('.s7dcl')] + '.s7res')
    for nw in blok['networky']:
        nw['titulek'] = text_mlc(texty, nw['atributy'].get('S7_NetworkTitle'))
        nw['komentar'] = text_mlc(texty, nw['atributy'].get('S7_NetworkComment'))
    blok['titulek'] = text_mlc(texty, blok['atributy'].get('S7_BlockTitle'))
    return blok


def parse_blok(text):
    m = re.search(r'^(FUNCTION_BLOCK|FUNCTION|ORGANIZATION_BLOCK|DATA_BLOCK|TYPE)\s+("([^"]+)"|(\S+))(\s*:\s*("([^"]+)"|\S+))?',
                  text, re.M)
    if not m:
        raise ChybaParseru('nenalezena hlavička bloku')
    druh = {'FUNCTION_BLOCK': 'FB', 'FUNCTION': 'FC', 'ORGANIZATION_BLOCK': 'OB',
            'DATA_BLOCK': 'DB', 'TYPE': 'UDT'}[m.group(1)]
    hlav = text[:m.start()]
    attrs = atributy(hlav[hlav.find('{'):] if '{' in hlav else '')
    blok = {
        'druh': druh, 'nazev': m.group(3) or m.group(4), 'atributy': attrs,
        'cislo': int(attrs['S7_BlockNumber']) if attrs.get('S7_BlockNumber', '').isdigit() else None,
        'jazyk': attrs.get('S7_PreferredLanguage') or attrs.get('S7_Language') or '',
        'instance_typ': (m.group(7) or m.group(6)) if druh == 'DB' and m.group(5) else None,
        'promenne': promenne_typy(re.split(r'^\s*(?:NETWORK|BEGIN)\s*$', text[m.end():], maxsplit=1, flags=re.M)[0]),
        'networky': [],
    }
    telo = text[m.end():]
    for nw_attrs, obsah in _networky(telo):
        nw = {'nw': len(blok['networky']) + 1, 'atributy': nw_attrs,
              'jazyk': nw_attrs.get('S7_Language', ''), 'text': obsah}
        if nw['jazyk'] == 'LAD':
            nw['rungy'], nw['prvky'] = parse_lad(obsah)
        elif nw['jazyk'] == 'FBD':
            import fbd                                   # líně: fbd importuje s7dcl
            try:
                nw['prvky'] = fbd.parse(obsah)
            except ChybaParseru as e:                    # neznámá konstrukce FBD nesmí shodit celý blok
                nw['chyba'] = str(e)
        blok['networky'].append(nw)
    return blok


def promenne_typy(text):
    """Deklarace rozhraní `jmeno : Typ;` -> {jmeno: Typ} (jen pro dohledání typu multiinstance)."""
    vys = {}
    for m in re.finditer(r'^\s*("[^"]+"|\w+)\s*(\{[^}]*\})?\s*:\s*([^;:=\n]+?)\s*(:=[^;\n]*)?;', text, re.M):
        jm = m.group(1).strip('"')
        typ = m.group(3).strip()
        if typ.startswith('_.'):
            typ = typ[2:]
        vys.setdefault(jm, typ.strip('"'))
    return vys


def _networky(telo):
    """Vrací [(atributy, obsah)] pro každý NETWORK ... END_NETWORK."""
    vys, i, posl_attr = [], 0, ''
    while True:
        m = re.search(r'^[ \t]*(\{|NETWORK[ \t]*$)', telo[i:], re.M)
        if not m:
            break
        zac = i + m.start(1)
        if telo[zac] == '{':
            k = _parovy(telo, zac, '{', '}')
            posl_attr = telo[zac:k]
            i = k
            continue
        k = re.search(r'^[ \t]*END_NETWORK[ \t]*$', telo[zac:], re.M)
        if not k:
            raise ChybaParseru('NETWORK bez END_NETWORK')
        vys.append((atributy(posl_attr), telo[zac + len('NETWORK'):zac + k.start()]))
        posl_attr = ''
        i = zac + k.end()
    return vys


# ---------- LAD network ----------

def parse_lad(obsah):
    """-> (rungy, prvky). rung = {'z': uzel, 'cesta': [id prvku | 'wire#wN'], 'do': uzel|None}.
    Uzel = 'powerrail' | 'wN'; FBD-like start operandem je 'op:<text>'."""
    rungy, prvky = [], []
    akt, attrs, i, t = None, {}, 0, obsah
    while i < len(t):
        if t[i].isspace():
            i += 1
            continue
        rad_k = t.find('\n', i)
        rad_k = len(t) if rad_k < 0 else rad_k
        radek = t[i:rad_k].strip()
        if re.match(r'RUNG\b', radek):
            if akt is not None:
                raise ChybaParseru('RUNG uvnitř RUNG')
            z = radek[4:].strip()
            akt = {'z': _uzel(z) if z else 'powerrail', 'cesta': [], 'do': None}
            i = rad_k
        elif re.match(r'END_RUNG\b', radek):
            if akt is None:
                raise ChybaParseru('END_RUNG bez RUNG')
            d = radek[8:].strip()
            akt['do'] = _uzel(d) if d else None
            rungy.append(akt)
            akt = None
            i = rad_k
        elif t[i] == '{':
            k = _parovy(t, i, '{', '}')
            attrs = atributy(t[i:k])
            i = k
        elif re.match(r'wire#\w+$', radek):
            if akt is None:
                raise ChybaParseru('wire mimo RUNG')
            akt['cesta'].append(radek)
            i = rad_k
        else:
            if akt is None:
                raise ChybaParseru('prvek mimo RUNG: ' + radek[:40])
            # cíl volání = text před první '(' mimo uvozovky
            j = i
            while j < len(t) and t[j] not in '(\n':
                j = _konec_retezce(t, j) if t[j] in '"\'' else j + 1
            if j < len(t) and t[j] == '(':
                k = _parovy(t, j, '(', ')')
                cil, args = t[i:j].strip(), t[j + 1:k - 1]
            else:
                k, cil, args = j, t[i:j].strip(), None
            p = prvek(cil, args, attrs)
            p['id'] = 'p%d' % (len(prvky) + 1)
            prvky.append(p)
            akt['cesta'].append(p['id'])
            attrs = {}
            i = k
    if akt is not None:
        raise ChybaParseru('RUNG bez END_RUNG')
    return rungy, prvky


def _uzel(s):
    if s.startswith('wire#'):
        return s[5:]
    return 'op:' + s


def _argumenty(args):
    if args is None or not args.strip():
        return [], []
    poz, piny = [], []
    for c in rozdel_carkami(args):
        m = re.match(r'^("[^"]+"|[A-Za-z_]\w*)\s*(:=|=>)\s*(.*)$', c, re.S)
        if m:
            piny.append({'pin': m.group(1), 'smer': 'in' if m.group(2) == ':=' else 'out',
                         'operand': m.group(3).strip()})
        elif c:
            poz.append(c)
    return poz, piny


def _sablona(attrs):
    s = attrs.get('S7_Templates')
    if not s:
        return None
    hodnoty = re.findall(r':=\s*([\w"]+)', s)
    return ' to '.join(h.strip('"') for h in hodnoty) or s


def prvek(cil, args, attrs):
    """Klasifikace prvku podle cíle volání."""
    neg = False
    if cil.startswith('NOT '):
        neg, cil = True, cil[4:].strip()
    poz, piny = _argumenty(args)
    pin = {p['pin']: p['operand'] for p in piny}
    sabl = _sablona(attrs)
    if re.fullmatch(r'[A-Za-z_]\w*', cil):
        if cil in ('Contact', 'I_Contact'):
            return {'typ': 'kontakt_nc' if cil == 'I_Contact' else 'kontakt', 'operand': (poz or [''])[0]}
        if cil in ('P_Contact', 'N_Contact'):
            return {'typ': 'kontakt_p' if cil[0] == 'P' else 'kontakt_n',
                    'operand': pin.get('operand', (poz or [''])[0]), 'bit': pin.get('bit', '')}
        m = re.fullmatch(r'(EQ|NE|GT|LT|GE|LE)_Contact', cil)
        if m:
            return {'typ': 'porovnani', 'op': POROVNANI[m.group(1)], 'in1': pin.get('in1', ''),
                    'in2': pin.get('in2', ''), 'sablona': sabl}
        if cil in CIVKY:
            return {'typ': 'civka_neg' if neg and cil == 'Coil' else CIVKY[cil], 'operand': (poz or [''])[0]}
        if cil in ('ReturnTrue', 'ReturnFalse', 'Return'):
            return {'typ': 'ret', 'operand': {'ReturnTrue': 'TRUE', 'ReturnFalse': 'FALSE'}.get(cil, '')}
        if cil == 'NOT' and not piny:
            return {'typ': 'not'}
        nazev = NAZVY.get(cil, cil.upper() if cil.isupper() or '_' in cil else cil)
        box = {'typ': 'box', 'volani': 'instrukce', 'nazev': nazev, 'instance': None, 'sablona': sabl}
        if cil in ('P_Trig', 'N_Trig'):
            box['pod'] = (poz or [''])[0]
        elif cil in ('S_SR', 'S_RS'):
            box['instance'] = pin.pop('operand', None)
            piny = [p for p in piny if p['pin'] != 'operand']
        elif poz:
            piny = [{'pin': 'IN', 'smer': 'in', 'operand': poz[0]}] + piny
    else:
        # volání: "Inst"(..), #inst(..), #inst.TON(..), "DB".PID_Temp(..)
        m = re.fullmatch(r'(.+)\.([A-Za-z_]\w*)', cil)
        if m:
            box = {'typ': 'box', 'volani': 'instrukce', 'nazev': m.group(2), 'instance': m.group(1), 'sablona': sabl}
        else:
            box = {'typ': 'box', 'volani': 'fb', 'nazev': None, 'instance': cil, 'sablona': sabl}
    if box['volani'] == 'instrukce':
        for p in piny:
            p['pin'] = p['pin'].upper()
    box['napajeni'] = list(NAPAJENI.get(box['nazev'], ('EN', 'ENO')))
    box['vstupy'] = [p for p in piny if p['smer'] == 'in']
    box['vystupy'] = [p for p in piny if p['smer'] == 'out']
    for p in piny:
        del p['smer']
    if neg:
        box['neg'] = True
    return box


def dopln_typy_fb(blok, najdi_db):
    """Doplní název FB u volání: #inst -> typ z rozhraní bloku, "Inst" -> DATA_BLOCK "Inst" : "FB" (najdi_db)."""
    for nw in blok['networky']:
        for p in nw.get('prvky', []):
            if p.get('volani') == 'fb' and not p['nazev']:          # LAD box i FBD volani
                inst = p['instance']
                if inst.startswith('#'):
                    p['nazev'] = blok['promenne'].get(inst[1:].strip('"'))
                elif inst.startswith('"'):
                    p['nazev'] = najdi_db(inst.strip('"'))
                p['nazev'] = p['nazev'] or 'FB ?'


def vodice(nw):
    """Spoje modelu: [{z, do}] mezi uzly (powerrail, wN, id prvku, '<id>.<pin>'); do=None = otevřený konec."""
    vys = []
    for r in nw.get('rungy', []):
        pred = r['z']
        for it in r['cesta']:
            uz = it[5:] if it.startswith('wire#') else it
            vys.append({'z': pred, 'do': uz})
            pred = uz
        vys.append({'z': pred, 'do': r['do']})
    for p in nw.get('prvky', []):
        if p['typ'] == 'box':
            for pin in p['vstupy']:
                if pin['operand'].startswith('wire#'):
                    vys.append({'z': pin['operand'][5:], 'do': '%s.%s' % (p['id'], pin['pin'])})
    return vys
