"""Operace nad modelem networku „po" (M3): vložení do série, paralelní větev, smazání, změna operandů;
místa vložení pro editor a pravidla LAD (validace).

Model = {prvky: [{id, typ, …}], rungy: [{z, cesta: [id | 'wire#wN'], do}]} (tvar /api/network).
Místo = {r: index rungu, k: index v `cesta`} — vložení před položku k (k == len(cesta) = na konec rungu).
Nové prvky dostávají id n1, n2, … (rozdil.py páruje podle id), nové uzly wire#wN.
"""
import copy
import re

CIVKY = {'civka', 'civka_s', 'civka_r', 'civka_neg', 'civka_p', 'civka_n', 'ret'}
OPERAND = {'kontakt', 'kontakt_nc', 'kontakt_p', 'kontakt_n', 'civka', 'civka_s', 'civka_r', 'civka_neg',
           'civka_p', 'civka_n'}
ZMENITELNE = {'operand', 'bit', 'in1', 'in2', 'op', 'sablona', 'instance', 'pod', 'vstupy', 'vystupy'}


class ChybaOperace(ValueError):
    pass


def druh(p):
    """'civka' | 'box' | 'kontakt' (kontakty, porovnání, NOT) — určuje platná místa vložení."""
    return 'civka' if p['typ'] in CIVKY else 'box' if p['typ'] == 'box' else 'kontakt'


def _je_wire(it):
    return it.startswith('wire#')


def _nove_id(model):
    n = [int(m.group(1)) for p in model['prvky'] for m in [re.fullmatch(r'n(\d+)', p['id'])] if m]
    return 'n%d' % (max(n, default=0) + 1)


def _nove_wire(model):
    t = []
    for r in model['rungy']:
        t += [r['z'], r['do'] or ''] + [x[5:] for x in r['cesta'] if _je_wire(x)]
    for p in model['prvky']:
        if p['typ'] == 'box':
            t += [x['operand'][5:] for x in p['vstupy'] if x['operand'].startswith('wire#')]
    n = [int(m.group(1)) for x in t for m in [re.fullmatch(r'w(\d+)', x)] if m]
    return 'w%d' % (max(n, default=0) + 1)


def _rung(model, misto):
    try:
        r = model['rungy'][int(misto['r'])]
        k = int(misto['k'])
    except (KeyError, IndexError, TypeError, ValueError):
        raise ChybaOperace('neplatné místo %r' % (misto,))
    if not 0 <= k <= len(r['cesta']):
        raise ChybaOperace('neplatné místo %r' % (misto,))
    return r, k


def _novy_prvek(model, prvek):
    if not isinstance(prvek, dict) or not isinstance(prvek.get('typ'), str):
        raise ChybaOperace('prvek musí být {typ, …}')
    p = copy.deepcopy(prvek)
    p['id'] = _nove_id(model)
    return p


def _privody(model):
    """Uzly, které napájejí pin boxu (`pin := wire#wN`)."""
    return {x['operand'][5:] for p in model['prvky'] if p['typ'] == 'box'
            for x in p['vstupy'] if x['operand'].startswith('wire#')}


# ---------- poloha v zapojení ----------

def v_paralele(model, ri, k):
    """Leží místo k rungu ri uvnitř zavřené paralelní větve? (rung do uzlu, nebo úsek, ke kterému vede
    jiný rung z téhož začátku / z dřívějšího uzlu téhož rungu)."""
    r = model['rungy'][ri]
    if r['do'] is not None:
        return True
    pred = {r['z']} | {x[5:] for x in r['cesta'][:k] if _je_wire(x)}
    for x in r['cesta'][k:]:
        if _je_wire(x):
            for j, o in enumerate(model['rungy']):
                if j != ri and o['do'] == x[5:] and o['z'] in pred:
                    return True
    return False


def platnost(model, ri, k):
    """-> {kontakt, box, civka}: co se smí vložit na místo."""
    r = model['rungy'][ri]
    civky = {p['id'] for p in model['prvky'] if p['typ'] in CIVKY}
    za_civkou = any(x in civky for x in r['cesta'][:k])
    prvky_r = [x for x in r['cesta'] if not _je_wire(x)]
    konec = k == len(r['cesta']) and r['do'] is None
    return {'kontakt': not za_civkou,
            'box': not za_civkou and not v_paralele(model, ri, k),
            'civka': konec and not (prvky_r and prvky_r[-1] in civky)}


def mista(model, kresba):
    """Místa vložení s geometrií z kresby (podle id prvků): před každým prvkem a za prvkem, po kterém
    následuje uzel nebo konec rungu. -> [{r, k, x, y, kontakt, box, civka}]"""
    if not kresba:
        return []
    geo = {d['id']: d for d in kresba['prvky']}
    vys, videno = [], set()

    def pridej(ri, k, x, y):
        if (ri, k) in videno:
            return
        videno.add((ri, k))
        vys.append(dict({'r': ri, 'k': k, 'x': round(x, 1), 'y': y}, **platnost(model, ri, k)))
    for ri, r in enumerate(model['rungy']):
        c = r['cesta']
        for k, it in enumerate(c):
            if _je_wire(it) or it not in geo:
                continue
            d = geo[it]
            # tělo prvku (box: obdélník, jinak symbol kolem cx) -> místo = střed vodiče mezi hranou buňky a tělem
            l, p = (d['bx'], d['bx'] + d['bw']) if 'bx' in d else (d['cx'] - 16, d['cx'] + 16)
            pridej(ri, k, (d['x'] + l) / 2, d['y'])
            if k + 1 == len(c) or _je_wire(c[k + 1]):
                pridej(ri, k + 1, (p + d['x'] + d['w']) / 2, d['y'])
        if not [x for x in c if not _je_wire(x)] and r['z'] == 'powerrail' and len(model['rungy']) == 1:
            pridej(ri, len(c), kresba['lista']['x'] + 40, (kresba['lista']['y1'] + kresba['lista']['y2']) / 2)   # prázdný network
    return vys


# ---------- operace ----------

def vloz(model, misto, prvek):
    """Vloží prvek do série na místo. -> (model, id)"""
    m = copy.deepcopy(model)
    r, k = _rung(m, misto)
    p = _novy_prvek(m, prvek)
    m['prvky'].append(p)
    r['cesta'].insert(k, p['id'])
    return m, p['id']


def _uzel(m, r, k, konec):
    """Uzel na místě k rungu r (případně vloží nový wire#). konec=True a k na konci otevřeného rungu -> None."""
    c = r['cesta']
    if k == 0 and not konec:
        return r['z']
    if k == len(c) and konec:
        return r['do']
    if k > 0 and _je_wire(c[k - 1]):
        return c[k - 1][5:]
    if k < len(c) and _je_wire(c[k]):
        return c[k][5:]
    w = _nove_wire(m)
    c.insert(k, 'wire#' + w)
    return w


def vetev(model, od, do, prvek):
    """Paralelní větev s jedním prvkem z místa `od` do místa `do` téhož rungu.
    `do` na konci otevřeného rungu = výstupní (otevřená) větev. -> (model, id)"""
    m = copy.deepcopy(model)
    r, a = _rung(m, od)
    r2, b = _rung(m, do)
    if r is not r2 or a >= b:
        raise ChybaOperace('větev: obě místa musí být ve stejném rungu a začátek před koncem')
    cil = _uzel(m, r, b, True)                    # nejdřív konec: vložení uzlu na b neposune a
    zac = _uzel(m, r, a, False)
    p = _novy_prvek(m, prvek)
    m['prvky'].append(p)
    m['rungy'].append({'z': zac, 'cesta': [p['id']], 'do': cil})   # na konec: indexy rungů (místa) se neposunou
    return m, p['id']


def smaz(model, pid):
    """Smaže prvek; u boxu i rungy, které napájely jeho piny. Prázdné větve a nepoužité uzly uklidí."""
    m = copy.deepcopy(model)
    P = {p['id']: p for p in m['prvky']}
    if pid not in P:
        raise ChybaOperace('prvek %s neexistuje' % pid)
    pryc = {pid}
    fronta = [pid]
    while fronta:                                  # přívody pinů smazaného boxu (rekurzivně)
        p = P[fronta.pop()]
        if p['typ'] != 'box':
            continue
        uzly = {x['operand'][5:] for x in p['vstupy'] if x['operand'].startswith('wire#')}
        for r in m['rungy']:
            if r['do'] in uzly:
                r['_pryc'] = True
                for x in r['cesta']:
                    if not _je_wire(x) and x not in pryc:
                        pryc.add(x)
                        fronta.append(x)
    m['rungy'] = [r for r in m['rungy'] if not r.pop('_pryc', False)]
    m['prvky'] = [p for p in m['prvky'] if p['id'] not in pryc]
    for r in m['rungy']:
        r['cesta'] = [x for x in r['cesta'] if x not in pryc]
    return uklid(m)


def uklid(m):
    """Prázdné větve pryč (prázdná zavřená větev = zkrat), prázdný hlavní rung zůstane jen jako jediný;
    uzly wire#, které nic nespojují, se vyhodí."""
    zmena = True
    while zmena:
        zmena = False
        for r in list(m['rungy']):
            prazdny = not [x for x in r['cesta'] if not _je_wire(x)]
            if prazdny and (r['do'] is not None or r['z'] != 'powerrail' or len(m['rungy']) > 1):
                if any(o['z'] in {x[5:] for x in r['cesta']} for o in m['rungy'] if o is not r):
                    continue                       # z jeho uzlu vede další větev
                m['rungy'].remove(r)
                zmena = True
        pouzite = _privody(m)
        for r in m['rungy']:
            pouzite |= {r['z'], r['do']}
        for r in m['rungy']:
            nova = [x for x in r['cesta'] if not _je_wire(x) or x[5:] in pouzite]
            if nova != r['cesta']:
                r['cesta'] = nova
                zmena = True
    return m


def zmen(model, pid, hodnoty):
    """Změní operand / piny / instanci prvku (jen povolená pole)."""
    if not isinstance(hodnoty, dict) or not set(hodnoty) <= ZMENITELNE:
        raise ChybaOperace('měnit lze jen %s' % sorted(ZMENITELNE))
    m = copy.deepcopy(model)
    for p in m['prvky']:
        if p['id'] == pid:
            for k, v in hodnoty.items():
                if k in ('vstupy', 'vystupy'):
                    if not isinstance(v, list) or not all(isinstance(x, dict) and isinstance(x.get('pin'), str)
                                                          and isinstance(x.get('operand'), str) for x in v):
                        raise ChybaOperace('%s musí být [{pin, operand}]' % k)
                    v = [{'pin': x['pin'], 'operand': x['operand']} for x in v]
                elif v is not None and not isinstance(v, str):
                    raise ChybaOperace('%s musí být text' % k)
                p[k] = v.strip() if isinstance(v, str) else v
            return m
    raise ChybaOperace('prvek %s neexistuje' % pid)


def proved(model, op, sablona=None):
    """Jedna operace z API: {typ: vloz|vetev|smaz|zmen, …}. sablona(prvek) převede klíč palety na prvek.
    -> (model, id dotčeného prvku | None)"""
    if not isinstance(op, dict):
        raise ChybaOperace('operace musí být objekt')
    t = op.get('typ')
    prvek = op.get('prvek')
    if isinstance(prvek, str) and sablona:
        prvek = sablona(prvek)
    if t == 'vloz':
        return vloz(model, op.get('misto'), prvek)
    if t == 'vetev':
        return vetev(model, op.get('od') or {}, op.get('do') or {}, prvek)
    if t == 'smaz':
        return smaz(model, op.get('id')), None
    if t == 'zmen':
        return zmen(model, op.get('id'), op.get('hodnoty')), op.get('id')
    raise ChybaOperace('neznámá operace %r' % t)


# ---------- validace (pravidla LAD) ----------

def validuj(model):
    """-> [{id | None, chyba}] — pravidla, která TIA odmítne nebo která návrh nedokončí."""
    P = {p['id']: p for p in model['prvky']}
    ch = []
    vyrabene = {'powerrail'}                       # uzly, do kterých vede proud
    for r in model['rungy']:
        vyrabene |= {x[5:] for x in r['cesta'] if _je_wire(x)}
        if r['do']:
            vyrabene.add(r['do'])
    privody = _privody(model)
    for ri, r in enumerate(model['rungy']):
        prvky_r = [x for x in r['cesta'] if not _je_wire(x)]
        nenapajeny = r['z'] not in vyrabene
        for k, it in enumerate(r['cesta']):
            if _je_wire(it) or it not in P:
                continue
            p = P[it]
            jm = _jmeno(p)
            if nenapajeny:
                ch.append({'id': it, 'chyba': 'Nenapájený prvek %s — rung začíná v uzlu %s, do kterého nic nevede.' % (jm, r['z'])})
            if p['typ'] == 'box' and r['do'] not in privody and v_paralele(model, ri, k):
                ch.append({'id': it, 'chyba': 'Box %s je uvnitř paralelní větve kontaktů — TIA ho odmítne. '
                                              'Dej ho do hlavní nebo výstupní větve.' % jm})
            if p['typ'] in CIVKY and (r['do'] is not None or it != prvky_r[-1]):
                ch.append({'id': it, 'chyba': 'Cívka %s není na konci rungu — za cívkou nesmí nic být.' % jm})
    for p in model['prvky']:
        jm, t = _jmeno(p), p['typ']
        if t in OPERAND and not (p.get('operand') or '').strip():
            ch.append({'id': p['id'], 'chyba': 'Chybí operand: %s' % jm})
        if t in ('kontakt_p', 'kontakt_n') and not (p.get('bit') or '').strip():
            ch.append({'id': p['id'], 'chyba': 'Chybí bit paměti hrany: %s' % jm})
        if t == 'porovnani' and not ((p.get('in1') or '').strip() and (p.get('in2') or '').strip()):
            ch.append({'id': p['id'], 'chyba': 'Porovnání %s potřebuje oba operandy (IN1 i IN2).' % jm})
        if t == 'box':
            if p.get('instance') is not None and not p['instance'].strip():
                ch.append({'id': p['id'], 'chyba': 'Chybí instance: %s' % jm})
            if 'pod' in p and not (p.get('pod') or '').strip():
                ch.append({'id': p['id'], 'chyba': 'Chybí bit paměti hrany: %s' % jm})
            if p.get('volani') == 'instrukce':          # u instrukce jsou vstupy povinné (u FB volitelné)
                prazdne = [x['pin'] for x in p['vstupy'] if not x['operand'].strip()]
                if prazdne:
                    ch.append({'id': p['id'], 'chyba': 'Chybí %s: %s' % (', '.join(prazdne), jm)})
    return ch


def _jmeno(p):
    if p['typ'] == 'box':
        return '%s%s' % (p.get('nazev') or 'box', ' ' + p['instance'] if p.get('instance') else '')
    if p['typ'] == 'porovnani':
        return 'CMP %s' % p.get('op', '')
    return '%s %s' % ({'kontakt': '-| |-', 'kontakt_nc': '-|/|-', 'kontakt_p': '-|P|-', 'kontakt_n': '-|N|-',
                       'ret': 'RET', 'not': 'NOT'}.get(p['typ'], '-( )-'), p.get('operand') or '…')
