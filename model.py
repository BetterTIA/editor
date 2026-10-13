"""Operace nad modelem networku „po" (M3): vložení do série, paralelní větev, smazání, změna operandů;
místa vložení pro editor a pravidla LAD (validace).

Model = {prvky: [{id, typ, …}], rungy: [{z, cesta: [id | 'wire#wN'], do}]} (tvar /api/network).
Místo = {r: index rungu, k: index v `cesta`} — vložení před položku k (k == len(cesta) = na konec rungu).
Nové prvky dostávají id n1, n2, … (rozdil.py páruje podle id), nové uzly wire#wN.
"""
import copy
import re


COILS = {'coil', 'coil_s', 'coil_r', 'coil_neg', 'coil_p', 'coil_n', 'ret'}
OPERAND = {'contact', 'contact_nc', 'contact_p', 'contact_n', 'coil', 'coil_s', 'coil_r', 'coil_neg',
           'coil_p', 'coil_n'}
CHANGEABLE = {'operand', 'bit', 'in1', 'in2', 'op', 'template', 'instance', 'below', 'inputs', 'outputs'}


class OperationError(ValueError):
    pass


def kind(p):
    """'civka' | 'box' | 'kontakt' (kontakty, porovnání, NOT) — určuje platná místa vložení."""
    return 'coil' if p['type'] in COILS else 'box' if p['type'] == 'box' else 'contact'


def _is_wire(it):
    return it.startswith('wire#')


def _new_id(model):
    n = [int(m.group(1)) for p in model['elements'] for m in [re.fullmatch(r'n(\d+)', p['id'])] if m]
    return 'n%d' % (max(n, default=0) + 1)


def _new_wire(model):
    t = []
    for r in model['rungs']:
        t += [r['from'], r['to'] or ''] + [x[5:] for x in r['path'] if _is_wire(x)]
    for p in model['elements']:
        if p['type'] == 'box':
            t += [x['operand'][5:] for x in p['inputs'] if x['operand'].startswith('wire#')]
    n = [int(m.group(1)) for x in t for m in [re.fullmatch(r'w(\d+)', x)] if m]
    return 'w%d' % (max(n, default=0) + 1)


def _rung(model, place):
    try:
        r = model['rungs'][int(place['r'])]
        k = int(place['k'])
    except (KeyError, IndexError, TypeError, ValueError):
        raise OperationError('invalid place %r' % (place,))
    if not 0 <= k <= len(r['path']):
        raise OperationError('invalid place %r' % (place,))
    return r, k


def _new_element(model, element):
    if not isinstance(element, dict) or not isinstance(element.get('type'), str):
        raise OperationError('element must be {type, …}')
    p = copy.deepcopy(element)
    p['id'] = _new_id(model)
    return p


def _feeds(model):
    """Uzly, které napájejí pin boxu (`pin := wire#wN`)."""
    return {x['operand'][5:] for p in model['elements'] if p['type'] == 'box'
            for x in p['inputs'] if x['operand'].startswith('wire#')}


# ---------- poloha v zapojení ----------

def in_parallel(model, ri, k):
    """Leží místo k rungu ri uvnitř zavřené paralelní větve? (rung do uzlu, nebo úsek, ke kterému vede
    jiný rung z téhož začátku / z dřívějšího uzlu téhož rungu)."""
    r = model['rungs'][ri]
    if r['to'] is not None:
        return True
    before = {r['from']} | {x[5:] for x in r['path'][:k] if _is_wire(x)}
    for x in r['path'][k:]:
        if _is_wire(x):
            for j, o in enumerate(model['rungs']):
                if j != ri and o['to'] == x[5:] and o['from'] in before:
                    return True
    return False


def validity(model, ri, k):
    """-> {kontakt, box, civka}: co se smí vložit na místo."""
    r = model['rungs'][ri]
    coils = {p['id'] for p in model['elements'] if p['type'] in COILS}
    after_coil = any(x in coils for x in r['path'][:k])
    elements_r = [x for x in r['path'] if not _is_wire(x)]
    end = k == len(r['path']) and r['to'] is None
    return {'contact': not after_coil,
            'box': not after_coil and not in_parallel(model, ri, k),
            'coil': end and not (elements_r and elements_r[-1] in coils)}


def places(model, drawing):
    """Místa vložení s geometrií z kresby (podle id prvků): před každým prvkem a za prvkem, po kterém
    následuje uzel nebo konec rungu. -> [{r, k, x, y, kontakt, box, civka}]"""
    if not drawing:
        return []
    geo = {d['id']: d for d in drawing['elements']}
    res, seen = [], set()

    def add(ri, k, x, y):
        if (ri, k) in seen:
            return
        seen.add((ri, k))
        res.append(dict({'r': ri, 'k': k, 'x': round(x, 1), 'y': y}, **validity(model, ri, k)))
    for ri, r in enumerate(model['rungs']):
        c = r['path']
        for k, it in enumerate(c):
            if _is_wire(it) or it not in geo:
                continue
            d = geo[it]
            # tělo prvku (box: obdélník, jinak symbol kolem cx) -> místo = střed vodiče mezi hranou buňky a tělem
            l, p = (d['bx'], d['bx'] + d['bw']) if 'bx' in d else (d['cx'] - 16, d['cx'] + 16)
            add(ri, k, (d['x'] + l) / 2, d['y'])
            if k + 1 == len(c) or _is_wire(c[k + 1]):
                add(ri, k + 1, (p + d['x'] + d['w']) / 2, d['y'])
        if not [x for x in c if not _is_wire(x)] and r['from'] == 'powerrail' and len(model['rungs']) == 1:
            add(ri, len(c), drawing['rail']['x'] + 40, (drawing['rail']['y1'] + drawing['rail']['y2']) / 2)   # prázdný network
    return res


# ---------- operace ----------

def insert(model, place, element):
    """Vloží prvek do série na místo. -> (model, id)"""
    m = copy.deepcopy(model)
    r, k = _rung(m, place)
    p = _new_element(m, element)
    m['elements'].append(p)
    r['path'].insert(k, p['id'])
    return m, p['id']


def _node(m, r, k, end):
    """Uzel na místě k rungu r (případně vloží nový wire#). konec=True a k na konci otevřeného rungu -> None."""
    c = r['path']
    if k == 0 and not end:
        return r['from']
    if k == len(c) and end:
        return r['to']
    if k > 0 and _is_wire(c[k - 1]):
        return c[k - 1][5:]
    if k < len(c) and _is_wire(c[k]):
        return c[k][5:]
    w = _new_wire(m)
    c.insert(k, 'wire#' + w)
    return w


def branch(model, src, dst, element):
    """Paralelní větev s jedním prvkem z místa `od` do místa `do` téhož rungu.
    `do` na konci otevřeného rungu = výstupní (otevřená) větev. -> (model, id)"""
    m = copy.deepcopy(model)
    r, a = _rung(m, src)
    r2, b = _rung(m, dst)
    if r is not r2 or a >= b:
        raise OperationError('branch: both places must be in the same rung and the start before the end')
    target = _node(m, r, b, True)                    # nejdřív konec: vložení uzlu na b neposune a
    start = _node(m, r, a, False)
    p = _new_element(m, element)
    m['elements'].append(p)
    m['rungs'].append({'from': start, 'path': [p['id']], 'to': target})   # na konec: indexy rungů (místa) se neposunou
    return m, p['id']


def delete(model, pid):
    """Smaže prvek; u boxu i rungy, které napájely jeho piny. Prázdné větve a nepoužité uzly uklidí."""
    m = copy.deepcopy(model)
    P = {p['id']: p for p in m['elements']}
    if pid not in P:
        raise OperationError('element %s does not exist' % pid)
    gone = {pid}
    queue = [pid]
    while queue:                                  # přívody pinů smazaného boxu (rekurzivně)
        p = P[queue.pop()]
        if p['type'] != 'box':
            continue
        nodes = {x['operand'][5:] for x in p['inputs'] if x['operand'].startswith('wire#')}
        for r in m['rungs']:
            if r['to'] in nodes:
                r['_gone'] = True
                for x in r['path']:
                    if not _is_wire(x) and x not in gone:
                        gone.add(x)
                        queue.append(x)
    m['rungs'] = [r for r in m['rungs'] if not r.pop('_gone', False)]
    m['elements'] = [p for p in m['elements'] if p['id'] not in gone]
    for r in m['rungs']:
        r['path'] = [x for x in r['path'] if x not in gone]
    return cleanup(m)


def cleanup(m):
    """Prázdné větve pryč (prázdná zavřená větev = zkrat), prázdný hlavní rung zůstane jen jako jediný;
    uzly wire#, které nic nespojují, se vyhodí."""
    modification = True
    while modification:
        modification = False
        for r in list(m['rungs']):
            empty = not [x for x in r['path'] if not _is_wire(x)]
            if empty and (r['to'] is not None or r['from'] != 'powerrail' or len(m['rungs']) > 1):
                if any(o['from'] in {x[5:] for x in r['path']} for o in m['rungs'] if o is not r):
                    continue                       # z jeho uzlu vede další větev
                m['rungs'].remove(r)
                modification = True
        used = _feeds(m)
        for r in m['rungs']:
            used |= {r['from'], r['to']}
        for r in m['rungs']:
            new_entry = [x for x in r['path'] if not _is_wire(x) or x[5:] in used]
            if new_entry != r['path']:
                r['path'] = new_entry
                modification = True
    return m


def change(model, pid, values):
    """Změní operand / piny / instanci prvku (jen povolená pole)."""
    if not isinstance(values, dict) or not set(values) <= CHANGEABLE:
        raise OperationError('only %s can be changed' % sorted(CHANGEABLE))
    m = copy.deepcopy(model)
    for p in m['elements']:
        if p['id'] == pid:
            for k, v in values.items():
                if k in ('inputs', 'outputs'):
                    if not isinstance(v, list) or not all(isinstance(x, dict) and isinstance(x.get('pin'), str)
                                                          and isinstance(x.get('operand'), str) for x in v):
                        raise OperationError('%s must be [{pin, operand}]' % k)
                    v = [{'pin': x['pin'], 'operand': x['operand']} for x in v]
                elif v is not None and not isinstance(v, str):
                    raise OperationError('%s must be a string' % k)
                p[k] = v.strip() if isinstance(v, str) else v
            return m
    raise OperationError('element %s does not exist' % pid)


def apply(model, op, template=None):
    """Jedna operace z API: {typ: vloz|vetev|smaz|zmen, …}. sablona(prvek) převede klíč palety na prvek.
    -> (model, id dotčeného prvku | None)"""
    if not isinstance(op, dict):
        raise OperationError('an operation must be an object')
    t = op.get('type')
    element = op.get('element')
    if isinstance(element, str) and template:
        element = template(element)
    if t == 'insert':
        return insert(model, op.get('place'), element)
    if t == 'branch':
        return branch(model, op.get('from') or {}, op.get('to') or {}, element)
    if t == 'delete':
        return delete(model, op.get('id')), None
    if t == 'change':
        return change(model, op.get('id'), op.get('values')), op.get('id')
    raise OperationError('unknown operation %r' % (t,))


# ---------- validace (pravidla LAD) ----------

def validate(model):
    """-> [{id | None, chyba}] — pravidla, která TIA odmítne nebo která návrh nedokončí."""
    P = {p['id']: p for p in model['elements']}
    ch = []
    produced = {'powerrail'}                       # uzly, do kterých vede proud
    for r in model['rungs']:
        produced |= {x[5:] for x in r['path'] if _is_wire(x)}
        if r['to']:
            produced.add(r['to'])
    feeds = _feeds(model)
    for ri, r in enumerate(model['rungs']):
        elements_r = [x for x in r['path'] if not _is_wire(x)]
        unpowered = r['from'] not in produced
        for k, it in enumerate(r['path']):
            if _is_wire(it) or it not in P:
                continue
            p = P[it]
            nm = _display_name(p)
            if unpowered:
                ch.append({'id': it, 'error': 'Unpowered element %s — the rung starts in node %s, which nothing leads into.' % (nm, r['from'])})
            if p['type'] == 'box' and r['to'] not in feeds and in_parallel(model, ri, k):
                ch.append({'id': it, 'error': 'Box %s is inside a parallel branch of contacts — TIA rejects it. '
                                                 'Put it in the main or the output branch.' % nm})
            if p['type'] in COILS and (r['to'] is not None or it != elements_r[-1]):
                ch.append({'id': it, 'error': 'Coil %s is not at the end of the rung — nothing may follow a coil.' % nm})
    for oi, o in enumerate(model['rungs']):      # paralelní úsek, jehož hlavní cesta je jen vodič
        if o['to'] is None or o['to'] in feeds:
            continue
        for ri, r in enumerate(model['rungs']):
            c = r['path']
            if ri == oi or 'wire#' + o['to'] not in c:
                continue
            j = c.index('wire#' + o['to'])
            if o['from'] == r['from']:
                i = 0
            elif 'wire#' + o['from'] in c[:j]:
                i = c.index('wire#' + o['from']) + 1
            else:
                continue
            if not [x for x in c[i:j] if not _is_wire(x)]:
                first = next((x for x in o['path'] if not _is_wire(x)), None)
                ch.append({'id': first, 'error': 'Branch without an element in the main path — the main path of the section is only a wire, '
                                                    'branch %s is bypassed. Add an element to the main path or delete the branch.'
                                                 % (_display_name(P[first]) if first in P else '')})
                break
    for p in model['elements']:
        nm, t = _display_name(p), p['type']
        if t in OPERAND and not (p.get('operand') or '').strip():
            ch.append({'id': p['id'], 'error': 'Missing operand: %s' % nm})
        if t in ('contact_p', 'contact_n') and not (p.get('bit') or '').strip():
            ch.append({'id': p['id'], 'error': 'Missing edge memory bit: %s' % nm})
        if t == 'comparison' and not ((p.get('in1') or '').strip() and (p.get('in2') or '').strip()):
            ch.append({'id': p['id'], 'error': 'Comparison %s needs both operands (IN1 and IN2).' % nm})
        if t == 'box':
            if p.get('instance') is not None and not p['instance'].strip():
                ch.append({'id': p['id'], 'error': 'Missing instance: %s' % nm})
            if 'below' in p and not (p.get('below') or '').strip():
                ch.append({'id': p['id'], 'error': 'Missing edge memory bit: %s' % nm})
            if p.get('call') == 'instruction':          # u instrukce jsou vstupy povinné (u FB volitelné)
                empty_pins = [x['pin'] for x in p['inputs'] if not x['operand'].strip()]
                if empty_pins:
                    ch.append({'id': p['id'], 'error': 'Missing %s: %s' % (', '.join(empty_pins), nm)})
    return ch


def _display_name(p):
    if p['type'] == 'box':
        return '%s%s' % (p.get('name') or 'box', ' ' + p['instance'] if p.get('instance') else '')
    if p['type'] == 'comparison':
        return 'CMP %s' % p.get('op', '')
    return '%s %s' % ({'contact': '-| |-', 'contact_nc': '-|/|-', 'contact_p': '-|P|-', 'contact_n': '-|N|-',
                       'ret': 'RET', 'not': 'NOT'}.get(p['type'], '-( )-'), p.get('operand') or '…')
