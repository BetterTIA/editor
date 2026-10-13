"""Rozdíl modelu networku před / po: párování prvků podle id, smazané prvky jako přízraky ve sloučeném modelu."""
import copy
import json

import layout


def _key(p):
    return json.dumps({k: v for k, v in p.items() if k != 'id'}, sort_keys=True, ensure_ascii=False)


def compare(before, after):
    """-> (rozdil {id: 'pridany'|'zmeneny'}, smazane [id v pred])"""
    P = {p['id']: p for p in before.get('elements', [])}
    diff, deleted = {}, []
    Q = {q['id']: q for q in after.get('elements', [])}
    for i, q in Q.items():
        p = P.get(i)
        if p is None or p['type'] != q['type']:
            diff[i] = 'added'
        elif _key(p) != _key(q):
            diff[i] = 'changed'
    for i, p in P.items():
        if i not in Q or Q[i]['type'] != p['type']:
            deleted.append(i)
    return diff, deleted


def _nodes(rungs):
    u = {'powerrail'}
    for r in rungs:
        u.add(r['from'])
        if r['to']:
            u.add(r['to'])
        u.update(it[5:] for it in r['path'] if it.startswith('wire#'))
    return u


def _find(rungs, anchor):
    """Pozice kotvy (id prvku / 'wire#wN') v rungech po: -> (rung, index) ; index -1 = začátek rungu z uzlu."""
    for r in rungs:
        if anchor in r['path']:
            return r, r['path'].index(anchor)
    if anchor.startswith('wire#'):
        for r in rungs:
            if r['from'] == anchor[5:]:
                return r, -1
    return None, None


def merge(before, after, deleted):
    """Model po + smazané prvky (id 'x:<id>') vložené za nejbližšího zachovaného souseda z modelu před.
    -> (prvky, rungy, nevlozene [id])"""
    elements = copy.deepcopy(after.get('elements', []))
    rungs = copy.deepcopy(after.get('rungs', []))
    P = {p['id']: p for p in before.get('elements', [])}
    kept = {p['id'] for p in elements} - set(deleted)
    unplaced = []
    if not deleted:
        return elements, rungs, unplaced
    for i in deleted:
        g = copy.deepcopy(P[i])
        g['id'] = 'x:' + i
        elements.append(g)
    sm = set(deleted)
    nodes = _nodes(rungs)
    for pr in before.get('rungs', []):
        items = pr['path']
        if not any(it in sm for it in items):
            continue
        if all(it in sm or it.startswith('wire#') for it in items) and pr['from'] in nodes and (pr['to'] is None or pr['to'] in nodes):
            # celý rung smazán a jeho uzly v po existují: vrátí se jako přízrak
            rungs.append({'from': pr['from'], 'path': ['x:' + it if it in sm else it for it in items], 'to': pr['to']})
            continue
        for k, it in enumerate(items):
            if it not in sm:
                continue
            inserted = False
            for anchor in reversed(items[:k]):          # nejbližší zachovaný vlevo
                if anchor in kept or (anchor.startswith('wire#') and anchor[5:] in nodes):
                    r, idx = _find(rungs, anchor)
                    if r is not None:
                        pos = idx + 1
                        while pos < len(r['path']) and r['path'][pos].startswith('x:'):
                            pos += 1
                        r['path'].insert(pos, 'x:' + it)
                        inserted = True
                    break
            if not inserted:
                for anchor in items[k + 1:]:              # jinak před nejbližší zachovaný vpravo
                    if anchor in kept:
                        r, idx = _find(rungs, anchor)
                        if r is not None:
                            r['path'].insert(idx, 'x:' + it)
                            inserted = True
                        break
            if not inserted:
                unplaced.append(it)
    elements = [p for p in elements if not (p['id'].startswith('x:') and p['id'][2:] in unplaced)]
    return elements, rungs, unplaced


# ---------- M4: shoda podle obsahu (export po zápisu vs. návrh) ----------

def _norm(p):
    """Obsah prvku bez id a odvozených polí; prázdné hodnoty pryč, operand wire#wN -> 'wire#' (jména uzlů se liší)."""
    q = {}
    for k, v in p.items():
        if k in ('id', 'call', 'power'):
            continue
        if k in ('inputs', 'outputs'):
            v = [[x['pin'], 'wire#' if x['operand'].startswith('wire#') else x['operand'].strip()]
                 for x in v if x['operand'].strip()]
        elif isinstance(v, str):
            v = v.strip()
        if v not in ('', None, []):
            q[k] = v
    return q


def _j(x):
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


def canon(model):
    """Topologie networku jako strom série (S) / zavřené větve (P) / výstupní větve (PO) s obsahem prvků;
    pořadí větví nehraje roli, id ani jména uzlů také ne. None = nejde rozložit."""
    P = {p['id']: p for p in model['elements']}
    try:
        tree_root, feeds = layout.tree(model['rungs'], model['elements'])
    except layout.LayoutError:
        return None
    fed = {}
    for (bid, pin), t in feeds.items():
        fed.setdefault(bid, {})[pin] = t

    def k(t):
        if t['k'] == 'E':
            d = _norm(P[t['id']])
            if t['id'] in fed:
                d['feeds'] = {pin: k(x) for pin, x in fed[t['id']].items()}
            return d
        if t['k'] == 'S':
            flat = []
            for d in (k(x) for x in t['children']):
                flat += d['S'] if list(d) == ['S'] else [d]
            return flat[0] if len(flat) == 1 else {'S': flat}
        return {t['k']: sorted((k(v) for v in t['branches']), key=_j)}
    return k(tree_root)


def _description(p):
    if p['type'] == 'box':
        return '%s%s' % (p.get('name') or 'box', ' ' + p['instance'] if p.get('instance') else '')
    if p['type'] == 'comparison':
        return 'CMP %s %s %s' % (p.get('in1', ''), p.get('op', ''), p.get('in2', ''))
    return '%s %s' % (p['type'], p.get('operand') or '…')


def content_match(proposal, export):
    """-> (shoda, [rozdíly čitelně]) — prvky podle obsahu (typ + operand/piny) a topologie sérií a větví."""
    a = {}
    for p in proposal['elements']:
        a.setdefault(_j(_norm(p)), []).append(p)
    b = {}
    for p in export['elements']:
        b.setdefault(_j(_norm(p)), []).append(p)
    missing = [p for k, v in a.items() for p in v[len(b.get(k, [])):]]
    surplus = [p for k, v in b.items() for p in v[len(a.get(k, [])):]]
    res = []
    for p in list(missing):                           # stejný typ (a box stejného jména) = jiný operand / piny
        q = next((x for x in surplus if x['type'] == p['type'] and x.get('name') == p.get('name')), None)
        if q is None:
            continue
        missing.remove(p)
        surplus.remove(q)
        if p['type'] == 'box':
            pa, pb = dict(_norm(p).get('inputs', []) + _norm(p).get('outputs', [])), \
                dict(_norm(q).get('inputs', []) + _norm(q).get('outputs', []))
            pins = ['%s %s → %s' % (pin, pa.get(pin, '(prázdný)'), pb.get(pin, '(prázdný)'))
                    for pin in sorted(set(pa) | set(pb)) if pa.get(pin) != pb.get(pin)]
            rest = [k for k in set(_norm(p)) | set(_norm(q)) if k not in ('inputs', 'outputs')
                   and _norm(p).get(k) != _norm(q).get(k)]
            res.append('jiné pins %s: %s' % (_description(p), ', '.join(pins + ['%s %s → %s' % (k, p.get(k) or '—', q.get(k) or '—')
                                                                          for k in rest])))
        else:
            res.append('jiný operand: návrh %s, export %s' % (_description(p), _description(q)))
    res += ['chybí v export: %s' % _description(p) for p in missing]
    res += ['navíc v export: %s' % _description(p) for p in surplus]
    if not res:
        ka, kb = canon(proposal), canon(export)
        if ka is None or kb is None:
            if _j(sorted(_j(r['path']) for r in proposal['rungs'])) != _j(sorted(_j(r['path']) for r in export['rungs'])):
                res.append('zapojení fails rozložit a rungs se liší')
        elif _j(ka) != _j(kb):
            res.append('elements sedí, ale liší se zapojení (série / větve)')
    return not res, res
