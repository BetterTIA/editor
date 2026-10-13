"""Rozdíl modelu networku před / po: párování prvků podle id, smazané prvky jako přízraky ve sloučeném modelu."""
import copy
import json

import layout


def _klic(p):
    return json.dumps({k: v for k, v in p.items() if k != 'id'}, sort_keys=True, ensure_ascii=False)


def porovnej(pred, po):
    """-> (rozdil {id: 'pridany'|'zmeneny'}, smazane [id v pred])"""
    P = {p['id']: p for p in pred.get('prvky', [])}
    diff, smazane = {}, []
    Q = {q['id']: q for q in po.get('prvky', [])}
    for i, q in Q.items():
        p = P.get(i)
        if p is None or p['typ'] != q['typ']:
            diff[i] = 'pridany'
        elif _klic(p) != _klic(q):
            diff[i] = 'zmeneny'
    for i, p in P.items():
        if i not in Q or Q[i]['typ'] != p['typ']:
            smazane.append(i)
    return diff, smazane


def _uzly(rungy):
    u = {'powerrail'}
    for r in rungy:
        u.add(r['z'])
        if r['do']:
            u.add(r['do'])
        u.update(it[5:] for it in r['cesta'] if it.startswith('wire#'))
    return u


def _najdi(rungy, kotva):
    """Pozice kotvy (id prvku / 'wire#wN') v rungech po: -> (rung, index) ; index -1 = začátek rungu z uzlu."""
    for r in rungy:
        if kotva in r['cesta']:
            return r, r['cesta'].index(kotva)
    if kotva.startswith('wire#'):
        for r in rungy:
            if r['z'] == kotva[5:]:
                return r, -1
    return None, None


def slouc(pred, po, smazane):
    """Model po + smazané prvky (id 'x:<id>') vložené za nejbližšího zachovaného souseda z modelu před.
    -> (prvky, rungy, nevlozene [id])"""
    prvky = copy.deepcopy(po.get('prvky', []))
    rungy = copy.deepcopy(po.get('rungy', []))
    P = {p['id']: p for p in pred.get('prvky', [])}
    zachovane = {p['id'] for p in prvky} - set(smazane)
    nevlozene = []
    if not smazane:
        return prvky, rungy, nevlozene
    for i in smazane:
        g = copy.deepcopy(P[i])
        g['id'] = 'x:' + i
        prvky.append(g)
    sm = set(smazane)
    uzly = _uzly(rungy)
    for pr in pred.get('rungy', []):
        polozky = pr['cesta']
        if not any(it in sm for it in polozky):
            continue
        if all(it in sm or it.startswith('wire#') for it in polozky) and pr['z'] in uzly and (pr['do'] is None or pr['do'] in uzly):
            # celý rung smazán a jeho uzly v po existují: vrátí se jako přízrak
            rungy.append({'z': pr['z'], 'cesta': ['x:' + it if it in sm else it for it in polozky], 'do': pr['do']})
            continue
        for k, it in enumerate(polozky):
            if it not in sm:
                continue
            vlozeno = False
            for kotva in reversed(polozky[:k]):          # nejbližší zachovaný vlevo
                if kotva in zachovane or (kotva.startswith('wire#') and kotva[5:] in uzly):
                    r, idx = _najdi(rungy, kotva)
                    if r is not None:
                        pos = idx + 1
                        while pos < len(r['cesta']) and r['cesta'][pos].startswith('x:'):
                            pos += 1
                        r['cesta'].insert(pos, 'x:' + it)
                        vlozeno = True
                    break
            if not vlozeno:
                for kotva in polozky[k + 1:]:              # jinak před nejbližší zachovaný vpravo
                    if kotva in zachovane:
                        r, idx = _najdi(rungy, kotva)
                        if r is not None:
                            r['cesta'].insert(idx, 'x:' + it)
                            vlozeno = True
                        break
            if not vlozeno:
                nevlozene.append(it)
    prvky = [p for p in prvky if not (p['id'].startswith('x:') and p['id'][2:] in nevlozene)]
    return prvky, rungy, nevlozene


# ---------- M4: shoda podle obsahu (export po zápisu vs. návrh) ----------

def _norm(p):
    """Obsah prvku bez id a odvozených polí; prázdné hodnoty pryč, operand wire#wN -> 'wire#' (jména uzlů se liší)."""
    q = {}
    for k, v in p.items():
        if k in ('id', 'volani', 'napajeni'):
            continue
        if k in ('vstupy', 'vystupy'):
            v = [[x['pin'], 'wire#' if x['operand'].startswith('wire#') else x['operand'].strip()]
                 for x in v if x['operand'].strip()]
        elif isinstance(v, str):
            v = v.strip()
        if v not in ('', None, []):
            q[k] = v
    return q


def _j(x):
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


def kanon(model):
    """Topologie networku jako strom série (S) / zavřené větve (P) / výstupní větve (PO) s obsahem prvků;
    pořadí větví nehraje roli, id ani jména uzlů také ne. None = nejde rozložit."""
    P = {p['id']: p for p in model['prvky']}
    try:
        koren, privody = layout.strom(model['rungy'], model['prvky'])
    except layout.Nerozlozitelne:
        return None
    fed = {}
    for (bid, pin), t in privody.items():
        fed.setdefault(bid, {})[pin] = t

    def k(t):
        if t['k'] == 'E':
            d = _norm(P[t['id']])
            if t['id'] in fed:
                d['privody'] = {pin: k(x) for pin, x in fed[t['id']].items()}
            return d
        if t['k'] == 'S':
            plosne = []
            for d in (k(x) for x in t['deti']):
                plosne += d['S'] if list(d) == ['S'] else [d]
            return plosne[0] if len(plosne) == 1 else {'S': plosne}
        return {t['k']: sorted((k(v) for v in t['vetve']), key=_j)}
    return k(koren)


def _popis(p):
    if p['typ'] == 'box':
        return '%s%s' % (p.get('nazev') or 'box', ' ' + p['instance'] if p.get('instance') else '')
    if p['typ'] == 'porovnani':
        return 'CMP %s %s %s' % (p.get('in1', ''), p.get('op', ''), p.get('in2', ''))
    return '%s %s' % (p['typ'], p.get('operand') or '…')


def shoda_obsahu(navrh, export):
    """-> (shoda, [rozdíly čitelně]) — prvky podle obsahu (typ + operand/piny) a topologie sérií a větví."""
    a = {}
    for p in navrh['prvky']:
        a.setdefault(_j(_norm(p)), []).append(p)
    b = {}
    for p in export['prvky']:
        b.setdefault(_j(_norm(p)), []).append(p)
    chybi = [p for k, v in a.items() for p in v[len(b.get(k, [])):]]
    prebyva = [p for k, v in b.items() for p in v[len(a.get(k, [])):]]
    vys = []
    for p in list(chybi):                           # stejný typ (a box stejného jména) = jiný operand / piny
        q = next((x for x in prebyva if x['typ'] == p['typ'] and x.get('nazev') == p.get('nazev')), None)
        if q is None:
            continue
        chybi.remove(p)
        prebyva.remove(q)
        if p['typ'] == 'box':
            pa, pb = dict(_norm(p).get('vstupy', []) + _norm(p).get('vystupy', [])), \
                dict(_norm(q).get('vstupy', []) + _norm(q).get('vystupy', []))
            piny = ['%s %s → %s' % (pin, pa.get(pin, '(prázdný)'), pb.get(pin, '(prázdný)'))
                    for pin in sorted(set(pa) | set(pb)) if pa.get(pin) != pb.get(pin)]
            ost = [k for k in set(_norm(p)) | set(_norm(q)) if k not in ('vstupy', 'vystupy')
                   and _norm(p).get(k) != _norm(q).get(k)]
            vys.append('jiné piny %s: %s' % (_popis(p), ', '.join(piny + ['%s %s → %s' % (k, p.get(k) or '—', q.get(k) or '—')
                                                                          for k in ost])))
        else:
            vys.append('jiný operand: návrh %s, export %s' % (_popis(p), _popis(q)))
    vys += ['chybí v exportu: %s' % _popis(p) for p in chybi]
    vys += ['navíc v exportu: %s' % _popis(p) for p in prebyva]
    if not vys:
        ka, kb = kanon(navrh), kanon(export)
        if ka is None or kb is None:
            if _j(sorted(_j(r['cesta']) for r in navrh['rungy'])) != _j(sorted(_j(r['cesta']) for r in export['rungy'])):
                vys.append('zapojení nejde rozložit a rungy se liší')
        elif _j(ka) != _j(kb):
            vys.append('prvky sedí, ale liší se zapojení (série / větve)')
    return not vys, vys
