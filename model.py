"""Operations on the "after" network model: insert in series, parallel branch, delete, change operands;
insertion places for the editor and LAD rules (validation).

Model = {elements: [{id, type, …}], rungs: [{from, path: [id | 'wire#wN'], to}]} (the /api/network shape).
Place = {r: rung index, k: index in `path`} — insert before item k (k == len(path) = at the end of the rung).
New elements get ids n1, n2, … (diff.py pairs by id), new nodes wire#wN.
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
    """'coil' | 'box' | 'contact' (contacts, comparisons, NOT) — decides the valid insertion places."""
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
    """Nodes that feed a box pin (`pin := wire#wN`)."""
    return {x['operand'][5:] for p in model['elements'] if p['type'] == 'box'
            for x in p['inputs'] if x['operand'].startswith('wire#')}


# ---------- position in the circuit ----------

def in_parallel(model, ri, k):
    """Is place k of rung ri inside a closed parallel branch? (a rung into a node, or a section that another
    rung leads to from the same start / from an earlier node of the same rung)."""
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
    """-> {contact, box, coil}: what may be inserted at the place."""
    r = model['rungs'][ri]
    coils = {p['id'] for p in model['elements'] if p['type'] in COILS}
    after_coil = any(x in coils for x in r['path'][:k])
    elements_r = [x for x in r['path'] if not _is_wire(x)]
    end = k == len(r['path']) and r['to'] is None
    return {'contact': not after_coil,
            'box': not after_coil and not in_parallel(model, ri, k),
            'coil': end and not (elements_r and elements_r[-1] in coils)}


def places(model, drawing):
    """Insertion places with geometry from the drawing (by element id): before every element and after an element
    followed by a node or the end of the rung. -> [{r, k, x, y, contact, box, coil}]"""
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
            # element body (box: rectangle, otherwise a symbol around cx) -> place = middle of the wire between cell edge and body
            l, p = (d['bx'], d['bx'] + d['bw']) if 'bx' in d else (d['cx'] - 16, d['cx'] + 16)
            add(ri, k, (d['x'] + l) / 2, d['y'])
            if k + 1 == len(c) or _is_wire(c[k + 1]):
                add(ri, k + 1, (p + d['x'] + d['w']) / 2, d['y'])
        if not [x for x in c if not _is_wire(x)] and r['from'] == 'powerrail' and len(model['rungs']) == 1:
            add(ri, len(c), drawing['rail']['x'] + 40, (drawing['rail']['y1'] + drawing['rail']['y2']) / 2)   # empty network
    return res


# ---------- operations ----------

def insert(model, place, element):
    """Insert an element in series at the place. -> (model, id)"""
    m = copy.deepcopy(model)
    r, k = _rung(m, place)
    p = _new_element(m, element)
    m['elements'].append(p)
    r['path'].insert(k, p['id'])
    return m, p['id']


def _node(m, r, k, end):
    """Node at place k of rung r (inserts a new wire# if needed). end=True and k at the end of an open rung -> None."""
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
    """Parallel branch with one element from place `src` to place `dst` of the same rung.
    `dst` at the end of an open rung = output (open) branch. -> (model, id)"""
    m = copy.deepcopy(model)
    r, a = _rung(m, src)
    r2, b = _rung(m, dst)
    if r is not r2 or a >= b:
        raise OperationError('branch: both places must be in the same rung and the start before the end')
    target = _node(m, r, b, True)                    # end first: inserting a node at b does not shift a
    start = _node(m, r, a, False)
    p = _new_element(m, element)
    m['elements'].append(p)
    m['rungs'].append({'from': start, 'path': [p['id']], 'to': target})   # appended: rung indexes (places) do not shift
    return m, p['id']


def delete(model, pid):
    """Delete an element; for a box also the rungs that fed its pins. Cleans up empty branches and unused nodes."""
    m = copy.deepcopy(model)
    P = {p['id']: p for p in m['elements']}
    if pid not in P:
        raise OperationError('element %s does not exist' % pid)
    gone = {pid}
    queue = [pid]
    while queue:                                  # feeds of the deleted box's pins (recursively)
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
    """Remove empty branches (an empty closed branch = short circuit); an empty main rung stays only if it is the
    only one; wire# nodes that connect nothing are dropped."""
    modification = True
    while modification:
        modification = False
        for r in list(m['rungs']):
            empty = not [x for x in r['path'] if not _is_wire(x)]
            if empty and (r['to'] is not None or r['from'] != 'powerrail' or len(m['rungs']) > 1):
                if any(o['from'] in {x[5:] for x in r['path']} for o in m['rungs'] if o is not r):
                    continue                       # another branch starts from its node
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
    """Change an element's operand / pins / instance (allowed fields only)."""
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
    """One operation from the API: {type: insert|branch|delete|change, …}. template(key) turns a palette key
    into an element. -> (model, id of the affected element | None)"""
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


# ---------- validation (LAD rules) ----------

def validate(model):
    """-> [{id | None, error}] — rules that TIA rejects or that leave the proposal unfinished."""
    P = {p['id']: p for p in model['elements']}
    ch = []
    produced = {'powerrail'}                       # nodes that power flows into
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
    for oi, o in enumerate(model['rungs']):      # parallel section whose main path is only a wire
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
            if p.get('call') == 'instruction':          # inputs are mandatory for an instruction (optional for an FB)
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
