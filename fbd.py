"""FBD network (`S7_Language := "FBD"` v .s7dcl) -> vlastní model boxů a signálů, rozložení jako v TIA, operace.

Gramatika (exporty V21, 12. 10. 2026): `RUNG [<operand>|wire#wN|true]` = vstup řetězu; prvky jdou za sebou a výstup
předchozího je 1. vstup dalšího: `A( in2 := …, in3 := … )` (&), `O(…)` (>=1), `X(…)` (x), `Coil(x)` (=),
`S_Coil` / `R_Coil`, `S_SR` / `S_RS`, `P_Trig(pamet)` / `N_Trig`, instrukce `Move(…)` (EN), volání `#inst.TYP(…)`,
`"Inst"(…)`, `#inst(…)` s atributy `{ f_user_card := "1"; … }`. `NOT` před prvkem = negovaný 1. vstup,
`pin := NOT x` = negovaný vstup. `wire#wN` uvnitř rungu = odbočka za boxem, `END_RUNG wire#wN` = výstup rungu,
`in2 := wire#wN` / `RUNG wire#wN` = vstup napájený odbočkou / výstupem jiného rungu.

Model: {jazyk: 'FBD', prvky: [box], signaly: [{z, do, vstup}]} ; box = {id (b1…, nové n1…), typ, vstupy, vystupy, …}
  typ: and | or | xor | assign | s | r | sr | rs | ptrig | ntrig | volani | instrukce
  vstupy: [{n, pin, operand, signal, neg}] — signal = id boxu, jehož hlavní výstup vstup napájí (operand je pak '')
  vystupy: [{pin, operand, neg}] (piny volání / instrukcí)
  operand: přiřazení / S / R / SR / RS / bit paměti hrany ; volani: volani fb|fc|instrukce, nazev, instance, atributy
První vstup je vstup řetězu (in1, EN, CLK, S …); volání bez napájení (F-blok) EN nemá.
`signaly` jsou odvozené ze vstupů (server je dopočítá) — Claude posílá jen `prvky`.
"""
import copy
import re

import s7dcl

LOGIKA = {'A': 'and', 'O': 'or', 'X': 'xor'}
PRIRAZENI = {'Coil': 'assign', 'S_Coil': 's', 'R_Coil': 'r'}
HRANY = {'P_Trig': 'ptrig', 'N_Trig': 'ntrig'}
TYPY = {'and', 'or', 'xor', 'assign', 's', 'r', 'sr', 'rs', 'ptrig', 'ntrig', 'volani', 'instrukce'}
TITUL = {'and': '&', 'or': '>=1', 'xor': 'x', 'assign': '=', 's': 'S', 'r': 'R', 'sr': 'SR', 'rs': 'RS',
         'ptrig': 'P_TRIG', 'ntrig': 'N_TRIG'}
S_OPERANDEM = {'assign', 's', 'r', 'sr', 'rs', 'ptrig', 'ntrig'}      # operand nad boxem
PINY_VIDET = {'sr', 'rs', 'ptrig', 'ntrig', 'volani', 'instrukce'}   # u & / >=1 / = TIA jména pinů nepíše


class Nerozlozitelne(Exception):
    pass


# ---------- parser ----------

def _attrs(t):
    return {m.group(1): m.group(2) for m in re.finditer(r'(\w+)\s*:=\s*"([^"]*)"', t)}


def _hodnota(s):
    """'NOT "x"' -> ('x', neg); 'wire#w1' -> ('wire', 'w1') v operandu jako značka."""
    s = (s or '').strip()
    neg = False
    if s.startswith('NOT '):
        neg, s = True, s[4:].strip()
    return s, neg


def _rungy(obsah):
    """-> [{'z': text|None, 'polozky': [('box', cil, args, attrs) | ('wire', 'wN')], 'do': 'wN'|None}]"""
    rungy, akt, attrs, i, t = [], None, {}, 0, obsah
    while i < len(t):
        if t[i].isspace():
            i += 1
            continue
        rad_k = t.find('\n', i)
        rad_k = len(t) if rad_k < 0 else rad_k
        radek = t[i:rad_k].strip()
        if re.match(r'RUNG\b', radek):
            if akt is not None:
                raise s7dcl.ChybaParseru('RUNG uvnitř RUNG')
            akt = {'z': radek[4:].strip() or None, 'polozky': [], 'do': None}
            i = rad_k
        elif re.match(r'END_RUNG\b', radek):
            if akt is None:
                raise s7dcl.ChybaParseru('END_RUNG bez RUNG')
            d = radek[8:].strip()
            if d and not d.startswith('wire#'):
                raise s7dcl.ChybaParseru('END_RUNG s operandem')
            akt['do'] = d[5:] if d else None
            rungy.append(akt)
            akt = None
            i = rad_k
        elif t[i] == '{':
            k = s7dcl._parovy(t, i, '{', '}')
            attrs = _attrs(t[i:k])
            i = k
        elif re.match(r'wire#\w+$', radek):
            if akt is None:
                raise s7dcl.ChybaParseru('wire mimo RUNG')
            akt['polozky'].append(('wire', radek[5:]))
            i = rad_k
        else:
            if akt is None:
                raise s7dcl.ChybaParseru('prvek mimo RUNG: ' + radek[:40])
            j = i
            while j < len(t) and t[j] not in '(\n':
                j = s7dcl._konec_retezce(t, j) if t[j] in '"\'' else j + 1
            if j >= len(t) or t[j] != '(':
                raise s7dcl.ChybaParseru('FBD prvek bez závorek: ' + radek[:40])
            k = s7dcl._parovy(t, j, '(', ')')
            akt['polozky'].append(('box', t[i:j].strip(), t[j + 1:k - 1], attrs))
            attrs = {}
            i = k
    if akt is not None:
        raise s7dcl.ChybaParseru('RUNG bez END_RUNG')
    return rungy


def _vstup(pin, hodnota):
    op, neg = _hodnota(hodnota)
    return {'n': 0, 'pin': pin, 'operand': op, 'signal': None, 'neg': neg}


def _box(cil, args, attrs):
    """-> (box bez id a bez vstupu řetězu, pin vstupu řetězu | None, negace vstupu řetězu)"""
    neg = cil.startswith('NOT ')
    if neg:
        cil = cil[4:].strip()
    poz, piny = s7dcl._argumenty(args)
    sabl = s7dcl._sablona({'S7_Templates': attrs['S7_Templates']}) if 'S7_Templates' in attrs else None
    if cil in LOGIKA:
        b = {'typ': LOGIKA[cil], 'vstupy': [_vstup(p['pin'].lower(), p['operand']) for p in piny], 'vystupy': []}
        return b, 'in1', neg
    if cil in PRIRAZENI or cil in HRANY:
        b = {'typ': PRIRAZENI.get(cil) or HRANY[cil], 'operand': (poz or [''])[0], 'vstupy': [], 'vystupy': []}
        return b, 'CLK' if cil in HRANY else 'in1', neg
    if cil in ('S_SR', 'S_RS'):
        op = next((p['operand'] for p in piny if p['pin'] == 'operand'), '')
        b = {'typ': 'sr' if cil == 'S_SR' else 'rs', 'operand': op, 'vystupy': [],
             'vstupy': [_vstup(p['pin'].upper(), p['operand']) for p in piny if p['pin'] != 'operand' and p['smer'] == 'in']}
        return b, 'S' if cil == 'S_SR' else 'R', neg
    # instrukce / volání bloku: tvar z LAD parseru (název, instance, piny), jen jiný typ
    p = s7dcl.prvek(cil, args, attrs)
    b = {'typ': 'instrukce' if p['volani'] == 'instrukce' and not p.get('instance') else 'volani',
         'volani': p['volani'], 'nazev': p['nazev'], 'instance': p.get('instance'), 'sablona': sabl,
         'vstupy': [_vstup(x['pin'], x['operand']) for x in p['vstupy']],
         'vystupy': [dict(zip(('operand', 'neg'), _hodnota(x['operand'])), pin=x['pin']) for x in p['vystupy']]}
    a = {k: v for k, v in attrs.items() if not k.startswith('S7_')}
    if a:
        b['atributy'] = a
    return b, 'EN', neg


def parse(obsah):
    """FBD network -> prvky (signály ve vstupech). Vyhodí ChybaParseru."""
    prvky, wire, cekajici = [], {}, []     # wire: wN -> ('box', id) | ('op', operand, neg) | ('wire', wM)

    def zdroj(z):
        if z is None:
            return None
        if z.startswith('wire#'):
            return ('wire', z[5:])
        op, neg = _hodnota(z)
        return ('op', op, neg)

    for r in _rungy(obsah):
        pred = zdroj(r['z'])
        for pol in r['polozky']:
            if pol[0] == 'wire':
                if pred is None:
                    raise s7dcl.ChybaParseru('odbočka wire#%s bez zdroje' % pol[1])
                wire[pol[1]] = pred
                continue
            b, pin, neg = _box(*pol[1:])
            b['id'] = 'b%d' % (len(prvky) + 1)
            for v in b['vstupy']:
                if v['operand'].startswith('wire#'):
                    cekajici.append((v, ('wire', v['operand'][5:])))
                    v['operand'] = ''
            if pred is not None or b['typ'] not in ('volani', 'instrukce'):   # vstup řetězu (F-blok bez EN nemá)
                v = {'n': 0, 'pin': pin, 'operand': '', 'signal': None, 'neg': neg}
                b['vstupy'].insert(0, v)
                if pred is not None:
                    cekajici.append((v, pred))
            for k, v in enumerate(b['vstupy']):
                v['n'] = k + 1
            prvky.append(b)
            pred = ('box', b['id'])
        if r['do']:
            if pred is None:
                raise s7dcl.ChybaParseru('END_RUNG wire#%s bez zdroje' % r['do'])
            wire[r['do']] = pred
    for v, z in cekajici:
        videno = set()
        while z[0] == 'wire':
            if z[1] in videno or z[1] not in wire:
                raise s7dcl.ChybaParseru('nezapojený vodič wire#%s' % z[1])
            videno.add(z[1])
            z = wire[z[1]]
        if z[0] == 'box':
            v['signal'] = z[1]
        else:
            v['operand'], v['neg'] = z[1], v['neg'] != z[2]
    return prvky


def signaly(prvky):
    """Hrany výstup boxu -> vstup boxu: [{z, do, vstup}]"""
    return [{'z': v['signal'], 'do': p['id'], 'vstup': v['n']} for p in prvky for v in p.get('vstupy', [])
            if v.get('signal')]


def model(prvky):
    return {'jazyk': 'FBD', 'prvky': prvky, 'signaly': signaly(prvky)}


def je_fbd(m):
    return isinstance(m, dict) and m.get('jazyk') == 'FBD'


# ---------- kontrola modelu z API (hranice důvěry) ----------

def over_model(po):
    """Vyhodí ValueError, když model není FBD tvar; čísla vstupů `n` přečísluje."""
    if not isinstance(po, dict) or not isinstance(po.get('prvky'), list):
        raise ValueError('FBD po musí být {jazyk: "FBD", prvky: [{id, typ, vstupy, vystupy, …}]}')
    ids = set()
    for p in po['prvky']:
        if not isinstance(p, dict) or not isinstance(p.get('id'), str) or p['id'] in ids or p['id'].startswith('x:'):
            raise ValueError('FBD prvek bez jedinečného id: %r' % (p if not isinstance(p, dict) else p.get('id'),))
        if p.get('typ') not in TYPY:
            raise ValueError('FBD prvek %s: typ musí být jeden z %s' % (p['id'], sorted(TYPY)))
        for k, pole in (('vstupy', ('pin', 'operand')), ('vystupy', ('pin', 'operand'))):
            if not isinstance(p.get(k), list) or not all(isinstance(x, dict) and all(isinstance(x.get(f), str) for f in pole)
                                                         for x in p[k]):
                raise ValueError('FBD prvek %s: %s musí být [{pin, operand, …}]' % (p['id'], k))
        for f in ('operand', 'nazev', 'instance', 'sablona'):
            if p.get(f) is not None and not isinstance(p[f], str):
                raise ValueError('FBD prvek %s: %s musí být text' % (p['id'], f))
        ids.add(p['id'])
    for p in po['prvky']:
        for i, v in enumerate(p['vstupy']):
            v['n'] = i + 1
            v['neg'] = bool(v.get('neg'))
            if v.get('signal') is not None and v['signal'] not in ids:
                raise ValueError('FBD prvek %s: vstup %d napájí neexistující box %r' % (p['id'], i + 1, v['signal']))
            v.setdefault('signal', None)
        for v in p['vystupy']:
            v['neg'] = bool(v.get('neg'))
    if _cyklus(po['prvky']):
        raise ValueError('FBD: signály tvoří cyklus')


def _cyklus(prvky):
    P = {p['id']: p for p in prvky}
    stav = {}

    def dfs(i):
        stav[i] = 1
        for v in P[i]['vstupy']:
            s = v.get('signal')
            if s in P and (stav.get(s) == 1 or (s not in stav and dfs(s))):
                return True
        stav[i] = 2
        return False
    return any(i not in stav and dfs(i) for i in P)


# ---------- operace (F1: přes API; editor až F2) ----------

class ChybaOperace(ValueError):
    pass


def _najdi(m, pid):
    for p in m['prvky']:
        if p['id'] == pid:
            return p
    raise ChybaOperace('prvek %s neexistuje' % pid)


def _pin(p, op):
    """op.vstup = n (1…) / op.vystup = jméno pinu -> dict pinu, nebo None (box sám)."""
    if op.get('vstup') is not None:
        try:
            return p['vstupy'][int(op['vstup']) - 1]
        except (ValueError, TypeError, IndexError):
            raise ChybaOperace('box %s nemá vstup %r' % (p['id'], op['vstup']))
    if op.get('vystup') is not None:
        for v in p['vystupy']:
            if v['pin'] == op['vystup']:
                return v
        raise ChybaOperace('box %s nemá výstup %r' % (p['id'], op['vystup']))
    return None


def proved(m, op):
    """{typ: pridej_vstup, id} | {typ: zmen_operand, id, vstup?|vystup?, operand} | {typ: negace, id, vstup?|vystup?}
    -> (model, id)"""
    if not isinstance(op, dict):
        raise ChybaOperace('operace musí být objekt')
    m = copy.deepcopy(m)
    p = _najdi(m, op.get('id'))
    t = op.get('typ')
    if t == 'pridej_vstup':
        if p['typ'] not in ('and', 'or', 'xor'):
            raise ChybaOperace('vstup lze přidat jen boxu &, >=1, x')
        n = len(p['vstupy']) + 1
        p['vstupy'].append({'n': n, 'pin': 'in%d' % n, 'operand': str(op.get('operand') or '').strip(),
                            'signal': None, 'neg': bool(op.get('neg'))})
    elif t == 'zmen_operand':
        if not isinstance(op.get('operand'), str):
            raise ChybaOperace('operand musí být text')
        pin = _pin(p, op)
        if pin is None:
            if p['typ'] in S_OPERANDEM:
                p['operand'] = op['operand'].strip()
            elif p['typ'] == 'volani':
                p['instance'] = op['operand'].strip()
            else:
                raise ChybaOperace('box %s nemá operand — zadej vstup' % p['id'])
        else:
            pin['operand'] = op['operand'].strip()
            if 'signal' in pin:
                pin['signal'] = None
    elif t == 'negace':
        pin = _pin(p, op)
        if pin is None:
            raise ChybaOperace('negace: zadej vstup (n) nebo vystup (pin)')
        pin['neg'] = not pin.get('neg')
    else:
        raise ChybaOperace('neznámá FBD operace %r (pridej_vstup, zmen_operand, negace)' % t)
    m['signaly'] = signaly(m['prvky'])
    return m, p['id']


# ---------- pravidla (minimum F1) ----------

def jmeno(p):
    if p['typ'] in ('volani', 'instrukce'):
        return '%s%s' % (p.get('nazev') or 'box', ' ' + p['instance'] if p.get('instance') else '')
    return ('%s %s' % (TITUL[p['typ']], p.get('operand') or '')).strip()


def validuj(m):
    ch = []
    for p in m['prvky']:
        if p['typ'] in S_OPERANDEM and not (p.get('operand') or '').strip():
            ch.append({'id': p['id'], 'chyba': 'Chybí operand: %s' % jmeno(p)})
        if p['typ'] == 'volani' and p.get('instance') is not None and not p['instance'].strip():
            ch.append({'id': p['id'], 'chyba': 'Chybí instance: %s' % jmeno(p)})
        if p['typ'] not in ('volani', 'instrukce'):
            for v in p['vstupy']:
                # shortcut: export V21 má `RUNG` + `Coil(x)` (= bez vstupu, nejspíš „vždy 1“) — 1. vstup =/S/R se
                # nehlásí; až se v TIA ověří, jak to editor kreslí, pravidlo upřesnit
                if p['typ'] in ('assign', 's', 'r') and v['n'] == 1:
                    continue
                if not v.get('signal') and not v['operand'].strip():
                    ch.append({'id': p['id'], 'chyba': 'Nezapojený vstup %d boxu %s' % (v['n'], jmeno(p))})
    return ch


# ---------- rozdíl: smazané boxy jako přízraky ----------

def slouc(pred, po, smazane):
    """Model po + smazané boxy (id 'x:<id>') jako samostatné stromy; signály ze smazaných boxů přejmenované,
    ze zaniklých zdrojů -> prázdný operand."""
    prvky = copy.deepcopy(po['prvky'])
    zive = {p['id'] for p in prvky}
    sm = set(smazane)
    for p in pred['prvky']:
        if p['id'] not in sm:
            continue
        g = copy.deepcopy(p)
        g['id'] = 'x:' + p['id']
        for v in g['vstupy']:
            s = v.get('signal')
            if s in sm:
                v['signal'] = 'x:' + s
            elif s and s not in zive:
                v['signal'] = None
        prvky.append(g)
    return prvky


def zmeny_pinu(pred, po, zmenene):
    """U změněných boxů: které vstupy (n), výstupy (pin) a zda operand / instance -> {id: {vstupy, vystupy, nad}}"""
    P = {p['id']: p for p in pred['prvky']}
    vys = {}
    for q in po['prvky']:
        p = P.get(q['id'])
        if q['id'] not in zmenene or p is None:
            continue
        pv = {v['n']: (v['operand'], v.get('signal'), bool(v.get('neg'))) for v in p['vstupy']}
        pw = {v['pin']: (v['operand'], bool(v.get('neg'))) for v in p['vystupy']}
        vys[q['id']] = {
            'vstupy': [v['n'] for v in q['vstupy'] if pv.get(v['n']) != (v['operand'], v.get('signal'), bool(v.get('neg')))],
            'vystupy': [v['pin'] for v in q['vystupy'] if pw.get(v['pin']) != (v['operand'], bool(v.get('neg')))],
            'nad': p.get('operand') != q.get('operand') or p.get('instance') != q.get('instance')}
    return vys


# ---------- rozložení ----------

L = 18
ZN = 7.2
MAXZN = 30
KANAL = 14       # volný pruh vlevo ve sloupci pro svislé vodiče signálů
NAHORU = 8


def _tw(s):
    return min(len(s or '...'), MAXZN) * ZN


def _prave(p):
    """Piny vpravo: [{pin, operand|None, neg, hlavni}] — hlavní výstup (řetěz) první."""
    if p['typ'] in ('volani', 'instrukce'):
        hl = [{'pin': 'ENO', 'operand': None, 'neg': False, 'hlavni': True}] \
            if p['vstupy'] and p['vstupy'][0]['pin'] == 'EN' else []
        return hl + [dict(v, hlavni=False) for v in p['vystupy']]
    return [{'pin': 'Q' if p['typ'] in PINY_VIDET else '', 'operand': None, 'neg': False, 'hlavni': True}]


def _hlavicka(p):
    """(řádky nad prvním pinem: titulek + šablona + popisek nad boxem, text nad boxem)"""
    nad = p.get('operand') if p['typ'] in S_OPERANDEM else p.get('instance')
    return 1 + (1 if p.get('sablona') else 0) + (1 if nad is not None else 0), nad


def rozloz(prvky, min_sirka=0):
    """-> (rozlozeni {id: {r, c}}, kresba) ; vyhodí Nerozlozitelne."""
    P = {p['id']: p for p in prvky}
    if not prvky:
        return {}, {'fbd': True, 'sirka': 260, 'vyska': 3 * L, 'boxy': [], 'cary': [], 'uzly': [], 'prazdny': True}
    spotr = {}                                          # zdroj -> [(spotřebič, index vstupu)]
    for p in prvky:
        for i, v in enumerate(p['vstupy']):
            s = v.get('signal')
            if s:
                if s not in P:
                    raise Nerozlozitelne('signál z neexistujícího boxu %s' % s)
                spotr.setdefault(s, []).append((p['id'], i))
    if _cyklus(prvky):
        raise Nerozlozitelne('signály tvoří cyklus')
    rodic = {s: sp[0] for s, sp in spotr.items()}       # hlavní spotřebič = první v pořadí exportu
    koreny = [p['id'] for p in prvky if p['id'] not in spotr]
    if not koreny:
        raise Nerozlozitelne('network nemá výstup')

    # 1) míra podstromu: řádky pinů vlevo (relativně k 1. pinu), výška a kotva (řádek výstupu od vrchu)
    mira = {}

    def mer(b):
        p = P[b]
        hdr, _ = _hlavicka(p)
        prave = _prave(p)
        top, bottom = -hdr, max(len(p['vstupy']), len(prave), 1) - 1
        radky, dole = [], None
        for i, v in enumerate(p['vstupy']):
            r = 0 if i == 0 else radky[-1] + 1
            s = v.get('signal')
            if s and rodic.get(s) == (b, i):
                hc, ac = mer(s)
                if dole is not None:
                    r = max(r, dole + 1 + ac)
                dole = r - ac + hc - 1
                top = min(top, r - ac)
            elif s:                                     # signál odjinud: řádek vlevo musí být volný
                if dole is not None:
                    r = max(r, dole + 1)
                dole = r
            radky.append(r)
            bottom = max(bottom, r, dole if dole is not None else r)
        mira[b] = {'radky': radky, 'prave': prave, 'hdr': hdr}
        return bottom - top + 1, -top

    # 2) stromy, pořadí (zdroje signálů odjinud dřív), sloupce a řádky
    strom_boxu = {}

    def oznac(b, k, relc, rel):
        strom_boxu[b] = k
        rel[b] = relc
        for i, v in enumerate(P[b]['vstupy']):
            s = v.get('signal')
            if s and rodic.get(s) == (b, i):
                oznac(s, k, relc - 1, rel)

    rel = {}
    for k in koreny:
        oznac(k, k, 0, rel)
    zavislosti = {k: [] for k in koreny}
    for p in prvky:
        for i, v in enumerate(p['vstupy']):
            s = v.get('signal')
            if s and rodic.get(s) != (p['id'], i) and strom_boxu[s] != strom_boxu[p['id']]:
                zavislosti[strom_boxu[p['id']]].append(strom_boxu[s])
    poradi, stav = [], {}

    def navstiv(k):
        if k in stav:
            return
        stav[k] = 1
        for d in zavislosti[k]:
            navstiv(d)
        poradi.append(k)
    for k in koreny:
        navstiv(k)

    sloupec, radek = {}, {}
    r0 = 0
    for k in poradi:
        h, a = mer(k)
        cleny = [b for b in rel if strom_boxu[b] == k]
        posun = -min(rel[b] for b in cleny)
        for b in cleny:
            for i, v in enumerate(P[b]['vstupy']):
                s = v.get('signal')
                if s and s in sloupec and rodic.get(s) != (b, i):
                    posun = max(posun, sloupec[s] + 1 - rel[b])
        for b in cleny:
            sloupec[b] = rel[b] + posun

        def umisti(b, r):
            radek[b] = r
            for i, v in enumerate(P[b]['vstupy']):
                s = v.get('signal')
                if s and rodic.get(s) == (b, i):
                    umisti(s, r + mira[b]['radky'][i])
        umisti(k, r0 + a)
        r0 += h + 1

    # 3) pixely: sloupec = [kanál | popisky vstupů | box | popisky výstupů]
    ncol = max(sloupec.values()) + 1
    lw, bw, rw, nadw = [0.0] * ncol, [0.0] * ncol, [0.0] * ncol, [0.0] * ncol
    geo = {}
    for b, c in sloupec.items():
        p, m = P[b], mira[b]
        piny = p['typ'] in PINY_VIDET
        l = max([_tw(v['operand']) + 18 + (8 if v.get('neg') else 0) for v in p['vstupy'] if not v.get('signal')] + [12])
        r = max([_tw(v['operand']) + 18 + (8 if v.get('neg') else 0) for v in m['prave'] if v['operand'] is not None]
                + [14 + (8 if any(v.get('neg') for v in m['prave']) else 0)])
        nl = max([len(v['pin']) for v in p['vstupy']] + [0]) if piny else 0
        nr = max([len(v['pin']) for v in m['prave']] + [0]) if piny else 0
        titul = TITUL.get(p['typ']) or p.get('nazev') or 'FB ?'
        w = max(40 if p['typ'] in ('assign', 's', 'r') else 52, len(titul) * ZN + 18, (nl + nr) * ZN + 22 if piny else 0,
                len(p.get('sablona') or '') * ZN + 18)
        _, nad = _hlavicka(p)
        geo[b] = (titul, w, nad)
        lw[c], bw[c], rw[c] = max(lw[c], l), max(bw[c], w), max(rw[c], r)
        nadw[c] = max(nadw[c], _tw(nad) + 10 if nad is not None else 0)
    xs = [12.0]
    for c in range(ncol):
        sir = max(KANAL + lw[c] + bw[c] + rw[c], KANAL + lw[c] + nadw[c])
        xs.append(xs[-1] + sir)
    rmin = min(radek[b] - mira[b]['hdr'] for b in radek)

    def Y(r):
        return round(NAHORU + (r - rmin) * L + L / 2, 1)

    boxy, pozice = [], {}
    for b in sorted(radek, key=lambda x: (sloupec[x], radek[x])):
        p, m = P[b], mira[b]
        titul, w, nad = geo[b]
        c, r = sloupec[b], radek[b]
        bx = xs[c] + KANAL + lw[c]
        sabl = 1 if p.get('sablona') else 0
        posl = max(m['radky'][-1] if m['radky'] else 0, len(m['prave']) - 1)
        piny = p['typ'] in PINY_VIDET
        d = {'id': b, 'typ': p['typ'], 'x': round(bx, 1), 'w': round(w, 1), 'titul': titul,
             'y': round(Y(r - 1 - sabl) - L / 2 + 1, 1), 'y_titul': round(Y(r - 1 - sabl) + 5, 1),
             'sablona': p.get('sablona'), 'y_sablona': round(Y(r - 1) + 4, 1),
             'nad': nad, 'y_nad': round(Y(r - 1 - sabl) - L / 2 - 3, 1),
             'vstupy': [{'y': Y(r + rr), 'pin': v['pin'] if piny else '', 'n': v['n'],
                         'operand': None if v.get('signal') else v['operand'], 'neg': bool(v.get('neg'))}
                        for v, rr in zip(p['vstupy'], m['radky'])],
             'vystupy': [{'y': Y(r + j), 'pin': v['pin'] if piny else '', 'operand': v['operand'], 'neg': bool(v.get('neg')),
                          'pahyl': not (v['hlavni'] and (b in spotr or p['typ'] in ('assign', 's', 'r')))}
                         for j, v in enumerate(m['prave'])]}
        d['h'] = round(Y(r + posl) + L / 2 - 1 - d['y'], 1)
        boxy.append(d)
        pozice[b] = d

    cary, uzly = [], set()
    for s, sp in spotr.items():
        S = pozice[s]
        hl = next((v for v in S['vystupy'] if v['operand'] is None), S['vystupy'][0] if S['vystupy'] else None)
        sy = hl['y'] if hl else S['y'] + L
        sx = S['x'] + S['w'] + (8 if hl and hl['neg'] else 0)
        for cid, i in sp:
            C = pozice[cid]
            v = C['vstupy'][i]
            tx = C['x'] - (8 if v['neg'] else 0)
            if v['y'] == sy and rodic[s] == (cid, i):
                cary.append([round(sx, 1), sy, round(tx, 1), sy])
                continue
            chx = xs[sloupec[s] + 1] + KANAL / 2 if sloupec[cid] > sloupec[s] else sx + KANAL / 2
            cary += [[round(sx, 1), sy, round(chx, 1), sy], [round(chx, 1), sy, round(chx, 1), v['y']],
                     [round(chx, 1), v['y'], round(tx, 1), v['y']]]
            if len(sp) > 1:
                uzly.add((round(chx, 1), sy))
    sirka = round(max(xs[-1] + 12, min_sirka), 1)
    vyska = round(max(d['y'] + d['h'] for d in boxy) + L, 1)
    rozl = {b: {'r': radek[b] - rmin, 'c': sloupec[b]} for b in radek}
    return rozl, {'fbd': True, 'sirka': sirka, 'vyska': vyska, 'boxy': boxy, 'cary': cary,
                  'uzly': [list(u) for u in sorted(uzly)]}
