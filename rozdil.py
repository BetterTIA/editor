"""Rozdíl modelu networku před / po: párování prvků podle id, smazané prvky jako přízraky ve sloučeném modelu."""
import copy
import json


def _klic(p):
    return json.dumps({k: v for k, v in p.items() if k != 'id'}, sort_keys=True, ensure_ascii=False)


def porovnej(pred, po):
    """-> (rozdil {id: 'pridany'|'zmeneny'}, smazane [id v pred])"""
    P = {p['id']: p for p in pred.get('prvky', [])}
    rozdil, smazane = {}, []
    Q = {q['id']: q for q in po.get('prvky', [])}
    for i, q in Q.items():
        p = P.get(i)
        if p is None or p['typ'] != q['typ']:
            rozdil[i] = 'pridany'
        elif _klic(p) != _klic(q):
            rozdil[i] = 'zmeneny'
    for i, p in P.items():
        if i not in Q or Q[i]['typ'] != p['typ']:
            smazane.append(i)
    return rozdil, smazane


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
