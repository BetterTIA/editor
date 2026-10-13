"""Serializace modelu networku („po", LAD z model.py / FBD z fbd.py) zpět do gramatiky `.s7dcl` (TIA V21)
a náhrada textu jednoho networku v souboru bloku (M5, Upload přes TIAC_Writer).

Tvar textu kopíruje exporty V21 ve vzorcích (odsazení 4 mezery, RUNG/END_RUNG, `wire#wN`, `Contact( x )`,
volání s `:=` / `=>`, `{ S7_Templates := … }`, FBD `A( in2 := … )`, `NOT` před prvkem). Ověřené je jen to, že
vlastní parser (`s7dcl.py`, `fbd.py`) text přečte na shodný obsah — ne že ho TIA importuje (viz ANET.md, M5).
"""
import re

import s7dcl

ODS = ' ' * 8                                   # RUNG v těle networku
ODS_P = ' ' * 12                                # prvek v rungu
ODS_A = ' ' * 16                                # argument prvku

# zobrazovaný název instrukce -> jméno v exportu (inverze s7dcl.NAZVY)
EXPORT_NAZEV = {v: k for k, v in s7dcl.NAZVY.items()}
CIVKY = {v: k for k, v in s7dcl.CIVKY.items()}
POROVNANI = {v: k for k, v in s7dcl.POROVNANI.items()}
# systémové instrukce: piny v exportu malými písmeny (parser je zvětšuje); F-instrukce / FB piny ponechá
SYSTEMOVE = set(s7dcl.NAZVY.values()) | set(s7dcl.NAPAJENI) | {'MOVE'}


class ChybaZapisu(ValueError):
    pass


# ---------- společné ----------

def _sablona(nazev, sabl):
    """'Int' / 'Int to DInt' -> text atributu `{ S7_Templates := "…" }`.
    shortcut: klíče šablon odhadnuté (SrcType, time_type, DestType), ověřené jen porovnání a TON — doplnit z exportu"""
    if not sabl:
        return None
    casti = [x.strip() for x in str(sabl).split(' to ')]
    if nazev in ('TON', 'TOF', 'TP', 'TONR'):
        klice = ['time_type']
    elif len(casti) == 2:
        klice = ['SrcType', 'DestType']
    else:
        klice = ['SrcType']
    return '{ S7_Templates := "%s" }' % '; '.join('%s := %s' % kv for kv in zip(klice, casti))


def _atributy(a):
    if not a:
        return []
    polozky = ['%s := "%s"' % (k, v) for k, v in a.items()]
    return [ODS_P + '{'] + [ODS_A + x + (';' if i < len(polozky) - 1 else '') for i, x in enumerate(polozky)] \
        + [ODS_P + '}']


def _volani(cil, args):
    """cil( a, b ) — jeden argument na řádku, jeden argument na jednom řádku (jako export)."""
    if not args:
        return [ODS_P + cil + '(  )']
    if len(args) == 1 and '\n' not in args[0]:
        return [ODS_P + '%s( %s )' % (cil, args[0])]
    return [ODS_P + cil + '('] + [ODS_A + a + (',' if i < len(args) - 1 else '') for i, a in enumerate(args)] \
        + [ODS_P + ')']


def _pin(pin, smer, hodnota, neg=False, male=False):
    h = (('NOT ' if neg else '') + (hodnota or '').strip()).strip()
    return '%s %s %s' % (pin.lower() if male else pin, smer, h) if h else '%s %s  ' % (pin.lower() if male else pin, smer)   # prázdný pin jako export: `pin :=  ,`


def _cil_boxu(p):
    """-> (text cíle volání, piny malými?)"""
    nazev, inst = p.get('nazev'), p.get('instance')
    if p.get('volani') == 'fc' or (p.get('volani') == 'fb' and not inst):
        return '"%s"' % (nazev or '').strip('"'), False
    if p.get('volani') == 'fb':
        return inst, False
    if inst:                                           # #T.TON(…), "DB".TP(…), #NouzStop.ESTOP1(…)
        return '%s.%s' % (inst, nazev), nazev in SYSTEMOVE
    return EXPORT_NAZEV.get(nazev, nazev), True


# ---------- LAD ----------

def _lad_prvek(p):
    t = p['typ']
    if t in ('kontakt', 'kontakt_nc'):
        return [ODS_P + '%s( %s )' % ('Contact' if t == 'kontakt' else 'I_Contact', p.get('operand', ''))]
    if t in ('kontakt_p', 'kontakt_n'):
        # shortcut: tvar P_Contact v exportu V21 neověřen (ve vzorcích není) — parser čte pojmenované i poziční
        return _volani('P_Contact' if t == 'kontakt_p' else 'N_Contact',
                       ['operand := %s' % p.get('operand', ''), 'bit := %s' % p.get('bit', '')])
    if t == 'porovnani':
        s = _sablona(None, p.get('sablona'))
        return ([ODS_P + s] if s else []) + _volani('%s_Contact' % POROVNANI[p['op']],
                                                     ['in1 := %s' % p.get('in1', ''), 'in2 := %s' % p.get('in2', '')])
    if t == 'civka_neg':
        return [ODS_P + 'NOT Coil( %s )' % p.get('operand', '')]
    if t in CIVKY:
        return [ODS_P + '%s( %s )' % (CIVKY[t], p.get('operand', ''))]
    if t == 'ret':
        return [ODS_P + {'TRUE': 'ReturnTrue', 'FALSE': 'ReturnFalse'}.get(p.get('operand'), 'Return') + '(  )']
    if t == 'not':
        return [ODS_P + 'NOT']
    if t != 'box':
        raise ChybaZapisu('neznámý typ prvku %r (%s)' % (t, p.get('id')))
    cil, male = _cil_boxu(p)
    if p.get('nazev') in ('P_TRIG', 'N_TRIG') and not p.get('instance'):
        radky = [ODS_P + '%s( %s )' % (cil, p.get('pod', ''))]
    else:
        args = [_pin(x['pin'], ':=', x['operand'], male=male) for x in p.get('vstupy', [])]
        if p.get('nazev') in ('SR', 'RS') and p.get('volani') == 'instrukce':
            args.append('operand := %s' % (p.get('instance') or ''))
            cil = EXPORT_NAZEV[p['nazev']]
        args += [_pin(x['pin'], '=>', x['operand'], male=male) for x in p.get('vystupy', [])]
        radky = _volani(cil, args)
    if p.get('neg'):
        radky[0] = ODS_P + 'NOT ' + radky[0].lstrip()
    s = _sablona(p.get('nazev'), p.get('sablona'))
    return ([ODS_P + s] if s else []) + radky


def _uzel(u):
    return u[3:] if u.startswith('op:') else 'wire#' + u


def lad_text(m):
    """LAD model {prvky, rungy} -> text těla networku (řádky mezi NETWORK a END_NETWORK)."""
    P = {p['id']: p for p in m['prvky']}
    radky = []
    for r in m.get('rungy') or [{'z': 'powerrail', 'cesta': [], 'do': None}]:
        radky.append(ODS + 'RUNG ' + _uzel(r['z']))
        for it in r['cesta']:
            if it.startswith('wire#'):
                radky.append(ODS_P + it)
            elif it in P:
                radky += _lad_prvek(P[it])
            else:
                raise ChybaZapisu('rung odkazuje na neexistující prvek %r' % it)
        radky.append(ODS + 'END_RUNG' + (' wire#' + r['do'] if r.get('do') else ''))
    return '\n'.join(radky) + '\n'


# ---------- FBD ----------

def _ma_retez(p):
    """Má box vstup řetězu (1. vstup = výstup předchozího / operand RUNG)? Volání v F-bloku EN nemá."""
    return bool(p['vstupy']) and (p['typ'] not in ('volani', 'instrukce') or p['vstupy'][0]['pin'] == 'EN')


def _fbd_box(p, wire_of):
    t, pred = p['typ'], []                             # pred = atributy / šablona nad prvkem
    vst = p['vstupy'][1:] if _ma_retez(p) else p['vstupy']

    def hodnota(v):
        return 'wire#' + wire_of[v['signal']] if v.get('signal') else v.get('operand', '')
    if t in ('and', 'or', 'xor'):
        radky = _volani({'and': 'A', 'or': 'O', 'xor': 'X'}[t], [_pin(v['pin'], ':=', hodnota(v), v.get('neg')) for v in vst])
    elif t in ('assign', 's', 'r'):
        radky = [ODS_P + '%s( %s )' % ({'assign': 'Coil', 's': 'S_Coil', 'r': 'R_Coil'}[t], p.get('operand', ''))]
    elif t in ('ptrig', 'ntrig'):
        radky = [ODS_P + '%s( %s )' % ('P_Trig' if t == 'ptrig' else 'N_Trig', p.get('operand', ''))]
    elif t in ('sr', 'rs'):
        radky = _volani('S_SR' if t == 'sr' else 'S_RS', [_pin(v['pin'], ':=', hodnota(v), v.get('neg'), male=True)
                                                          for v in vst] + ['operand := %s' % p.get('operand', '')])
    else:
        cil, male = _cil_boxu(p)
        args = [_pin(v['pin'], ':=', hodnota(v), v.get('neg'), male) for v in vst]
        args += [_pin(v['pin'], '=>', v.get('operand', ''), v.get('neg'), male) for v in p.get('vystupy', [])]
        s = _sablona(p.get('nazev'), p.get('sablona'))
        pred = ([ODS_P + s] if s else []) + _atributy(p.get('atributy'))
        radky = _volani(cil, args)
    if _ma_retez(p) and p['vstupy'][0].get('neg'):
        radky[0] = ODS_P + 'NOT ' + radky[0].lstrip()
    return pred + radky


def fbd_text(m):
    """FBD model {prvky} -> text těla networku. Řetěz = box, jehož 1. vstup je hlavní výstup předchozího;
    další spotřebiče výstupu dostanou `wire#wN` (odbočka v rungu / END_RUNG wire#wN). Negace hlavního výstupu
    (`neg_vystup`, jen z editoru) se zapíše jako negace vstupů spotřebičů."""
    prvky = [dict(p, vstupy=[dict(v) for v in p['vstupy']]) for p in m['prvky']]
    P = {p['id']: p for p in prvky}
    spotr = {p['id']: [] for p in prvky}
    for c in prvky:
        for v in c['vstupy']:
            if v.get('signal'):
                if v['signal'] not in P:
                    raise ChybaZapisu('signál z neexistujícího boxu %s' % v['signal'])
                spotr[v['signal']].append((c, v))
    for p in prvky:
        if p.get('neg_vystup'):
            for _, v in spotr[p['id']]:
                v['neg'] = not v.get('neg')
    nxt = {}
    for p in prvky:
        k = next((c for c, v in spotr[p['id']] if _ma_retez(c) and v is c['vstupy'][0]), None)
        if k is not None:
            nxt[p['id']] = k
    pokracovani = {c['id'] for c in nxt.values()}
    wire_of = {}
    for p in prvky:
        if any(c is not nxt.get(p['id']) or v is not c['vstupy'][0] for c, v in spotr[p['id']]):
            wire_of[p['id']] = 'w%d' % (len(wire_of) + 1)
    radky, hotovo = [], set()
    for start in prvky:
        if start['id'] in pokracovani:
            continue
        v0 = start['vstupy'][0] if _ma_retez(start) else None
        if v0 is None:
            z = ''
        elif v0.get('signal'):
            z = ' wire#' + wire_of[v0['signal']]
        else:
            z = (' ' + v0['operand'].strip()) if v0.get('operand', '').strip() else ''
        radky.append(ODS + 'RUNG' + z)
        p = start
        while True:
            hotovo.add(p['id'])
            radky += _fbd_box(p, wire_of)
            dalsi = nxt.get(p['id'])
            if dalsi is None or dalsi['id'] in hotovo:
                break
            if p['id'] in wire_of:
                radky.append(ODS_P + 'wire#' + wire_of[p['id']])
            p = dalsi
        radky.append(ODS + 'END_RUNG' + (' wire#' + wire_of[p['id']] if p['id'] in wire_of else ''))
    if len(hotovo) != len(prvky):
        raise ChybaZapisu('FBD zapojení má cyklus (%s)' % ', '.join(sorted(set(P) - hotovo)))
    if not prvky:
        radky = [ODS + 'RUNG', ODS + 'END_RUNG']
    return '\n'.join(radky) + '\n'


def text_networku(m):
    """Model „po" (LAD nebo FBD) -> text těla networku pro `.s7dcl`."""
    if m.get('jazyk') == 'FBD':
        return fbd_text(m)
    return lad_text(m)


# ---------- náhrada networku v souboru bloku ----------

def nahrad_network(text_s7dcl, cislo_nw, novy_text):
    """Nahradí tělo N-tého NETWORK … END_NETWORK (1 = první) textem `novy_text`; hlavička bloku, atributy networku
    (titulek, komentář, jazyk) a ostatní networky zůstanou beze změny. Konce řádků CRLF se zachovají."""
    crlf = '\r\n' in text_s7dcl
    t = text_s7dcl.replace('\r\n', '\n')
    zacatky = list(re.finditer(r'^[ \t]*NETWORK[ \t]*\n', t, re.M))
    if not 1 <= cislo_nw <= len(zacatky):
        raise ChybaZapisu('network %d v bloku není (bloků networků %d)' % (cislo_nw, len(zacatky)))
    z = zacatky[cislo_nw - 1].end()
    k = re.compile(r'^[ \t]*END_NETWORK[ \t]*$', re.M).search(t, z)
    if not k:
        raise ChybaZapisu('NETWORK bez END_NETWORK')
    novy = novy_text.replace('\r\n', '\n')
    if not novy.endswith('\n'):
        novy += '\n'
    t = t[:z] + novy + t[k.start():]
    return t.replace('\n', '\r\n') if crlf else t
