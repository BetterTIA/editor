"""Rozložení LAD networku z grafu `.s7dcl` (rungy + pojmenované vodiče) do mřížky.

Postup:
1. rungy -> úseky (hrany) mezi uzly powerrail / wN / otevřený konec,
2. sériově-paralelní redukce grafu na strom: S (série), P (zavřená větev: začátek i konec na rungu),
   PO (otevřený vějíř: výstupní větve / víc rungů od lišty),
   vodič, který je hodnotou pinu boxu (`pin := wire#wN`), = přívod k pinu (feeder) od lišty,
3. měření (sloupce, řádky nad/pod rungem), umístění, cívky na konci otevřené série k pravému okraji,
4. pixely: šířka sloupce podle nejširšího prvku (operand), čáry vodičů.
Co se nedá redukovat (můstek, přívod pinu odjinud než z lišty, FBD), vrací varování -> stránka ukáže seznam rungů.
"""
import re
from collections import defaultdict

L = 18           # výška linky (px); prvky zabírají celé linky nad/pod rungem
MEZERA = 2       # volné linky mezi větvemi pod sebou
ZN = 7.2         # šířka znaku monospace 12 px
MAXZN = 30       # delší operand se zkrátí (…), plný je v title
LISTA_X = 16
NAHORU = 10      # okraj nad první linkou

CIVKY = {'civka', 'civka_s', 'civka_r', 'civka_neg', 'civka_p', 'civka_n', 'ret'}


class Nerozlozitelne(Exception):
    pass


def _tw(s):
    s = s or '...'
    return min(len(s), MAXZN) * ZN


# ---------- 1+2: graf -> strom ----------

def strom(rungy, prvky):
    """-> (koren, privody) ; privody = {(id_boxu, pin): strom}"""
    byid = {p['id']: p for p in prvky}
    piny_vodic = {}
    for p in prvky:
        if p['typ'] == 'box':
            for pin in p['vstupy']:
                m = re.fullmatch(r'wire#(\w+)', pin['operand'])
                if m:
                    piny_vodic[m.group(1)] = (p['id'], pin['pin'])
    hrany, por = [], 0
    nsink = [0]

    def sink():
        nsink[0] += 1
        return '#%d' % nsink[0]

    for ri, r in enumerate(rungy):
        if r['z'].startswith('op:'):
            raise Nerozlozitelne('rung začíná operandem (FBD tvar)')
        uzel, prv = r['z'], []
        for it in r['cesta']:
            if it.startswith('wire#'):
                hrany.append([uzel, it[5:], {'k': 'S', 'deti': [{'k': 'E', 'id': x} for x in prv]}, por])
                por += 1
                uzel, prv = it[5:], []
            else:
                prv.append(it)
        konec = r['do'] if r['do'] else sink()
        if konec.startswith('op:'):
            raise Nerozlozitelne('rung končí operandem')
        hrany.append([uzel, konec, {'k': 'S', 'deti': [{'k': 'E', 'id': x} for x in prv]}, por])
        por += 1

    chranene = {'powerrail'} | set(piny_vodic)

    def ser(a, b):
        deti = []
        for t in (a, b):
            deti += t['deti'] if t['k'] == 'S' else [t]
        return {'k': 'S', 'deti': deti}

    zmena = True
    while zmena:
        zmena = False
        sk = defaultdict(list)
        for h in hrany:
            sk[(h[0], h[1])].append(h)
        for (a, b), g in sk.items():          # zavřená paralelní větev
            if len(g) > 1 and not b.startswith('#'):
                g.sort(key=lambda h: h[3])
                for h in g:
                    hrany.remove(h)
                hrany.append([a, b, {'k': 'P', 'vetve': [h[2] for h in g]}, g[0][3]])
                zmena = True
                break
        if zmena:
            continue
        uzly = {h[0] for h in hrany} | {h[1] for h in hrany}
        for n in sorted(uzly):                # série přes uzel s 1 vstupem a 1 výstupem
            if n in chranene or n.startswith('#'):
                continue
            vst = [h for h in hrany if h[1] == n]
            vys = [h for h in hrany if h[0] == n]
            if len(vst) == 1 and len(vys) == 1:
                hrany.remove(vst[0])
                hrany.remove(vys[0])
                hrany.append([vst[0][0], vys[0][1], ser(vst[0][2], vys[0][2]), min(vst[0][3], vys[0][3])])
                zmena = True
                break
        if zmena:
            continue
        for n in sorted(uzly):                # otevřený vějíř (výstupní větve / rungy od lišty)
            otev = sorted([h for h in hrany if h[0] == n and h[1].startswith('#')], key=lambda h: h[3])
            if len(otev) >= 2:
                for h in otev:
                    hrany.remove(h)
                vetve = []
                for h in otev:
                    vetve += h[2]['vetve'] if h[2]['k'] == 'PO' else [h[2]]
                hrany.append([n, sink(), {'k': 'PO', 'vetve': vetve}, otev[0][3]])
                zmena = True
                break

    koren, privody = None, {}
    for a, b, t, _ in hrany:
        if a != 'powerrail':
            raise Nerozlozitelne('zapojení není sériově-paralelní (uzel %s)' % a)
        if b.startswith('#'):
            if koren is not None:
                raise Nerozlozitelne('víc kořenů')
            koren = t
        elif b in piny_vodic:
            privody[piny_vodic[b]] = t
        else:
            raise Nerozlozitelne('zapojení není sériově-paralelní (uzel %s)' % b)
    if koren is None:
        raise Nerozlozitelne('network nemá otevřený rung')
    if koren['k'] != 'S':
        koren = {'k': 'S', 'deti': [koren]}
    for (bid, pin) in privody:
        if bid not in byid:
            raise Nerozlozitelne('přívod k neznámému boxu')
    _oznac_otevrene(koren, True)
    return koren, privody


def _oznac_otevrene(t, otevrena):
    if t['k'] == 'S':
        t['otevrena'] = otevrena
        for i, d in enumerate(t['deti']):
            _oznac_otevrene(d, otevrena and i == len(t['deti']) - 1)
    elif t['k'] == 'P':
        for v in t['vetve']:
            _oznac_otevrene(v, False)
    elif t['k'] == 'PO':
        for v in t['vetve']:
            _oznac_otevrene(v, True)


# ---------- 3: měření a umístění ----------

class Rozlozeni:
    def __init__(self, rungy, prvky):
        self.p = {p['id']: p for p in prvky}
        self.koren, self.privody = strom(rungy, prvky)
        self.boxpiny = defaultdict(dict)       # id boxu -> {pin: strom přívodu}
        for (bid, pin), t in self.privody.items():
            _oznac_otevrene(t, False)
            self.boxpiny[bid][pin] = t
        self.cary = []                         # ('h', linka, xa, xb) / ('v', x, l1, l2); x = ('c', sloupec) | ('bo', id)
        self.pozice = {}                       # id -> {'c', 'r'}
        self.meř(self.koren)
        self.posl = self.koren['w'] + 1        # cívky u pravého okraje; sloupec koren.w = mezera (roztahuje se)
        self.umisti(self.koren, 0, 0, None)

    # rozměry prvku: (nad, pod) v linkách
    def _prvek(self, e):
        p = self.p[e['id']]
        t = p['typ']
        e['w'] = 1
        if t == 'box':
            hl = 1 + (1 if p.get('sablona') else 0)
            e['up'] = hl + (1 if p.get('instance') else 0)
            nl, nr = 1 + len(p['vstupy']), 1 + len(p['vystupy'])
            e['piny_l'] = list(range(nl))
            e['piny_r'] = list(range(nr))
            e['down'] = max(nl, nr) - 1 + (1 if p.get('pod') else 0)
        elif t == 'porovnani':
            e['up'], e['down'] = 1, 2 if p.get('sablona') else 1
        elif t in ('kontakt_p', 'kontakt_n'):
            e['up'], e['down'] = 1, 1
        else:
            e['up'], e['down'] = 1, 0

    def meř(self, t, spodek_vlevo=0):
        k = t['k']
        if k == 'E':
            self._prvek(t)
        elif k == 'S':
            cur, up, down = 0, 0, 0
            for i, d in enumerate(t['deti']):
                if d['k'] == 'E' and d['id'] in self.boxpiny:
                    self._prvek(d)
                    wf = self._privody(d, down)
                    col = max(cur, wf)
                else:
                    self.meř(d)
                    col = cur
                d['c_rel'] = col
                d['vpravo'] = (t.get('otevrena') and i == len(t['deti']) - 1 and d['k'] == 'E'
                               and self.p[d['id']]['typ'] in CIVKY)
                if not d['vpravo']:
                    cur = col + d['w']
                up, down = max(up, d['up']), max(down, d['down'])
            t['w'], t['up'], t['down'] = cur, up, down
        else:  # P / PO
            off, w = [], 0
            for i, v in enumerate(t['vetve']):
                self.meř(v)
                w = max(w, v['w'])
                if i == 0:
                    off.append(0)
                else:
                    pv = t['vetve'][i - 1]
                    off.append(off[-1] + pv['down'] + MEZERA + v['up'])
            t['w'], t['off'] = w, off
            t['up'] = t['vetve'][0]['up']
            t['down'] = off[-1] + t['vetve'][-1]['down']

    def _privody(self, e, spodek):
        """Rozmístí piny boxu tak, aby přívody (od lišty) byly pod vším vlevo. -> šířka přívodů."""
        p = self.p[e['id']]
        fed = self.boxpiny[e['id']]
        linky, posl, wf = [0], 0, 0
        for pin in p['vstupy']:
            f = fed.get(pin['pin'])
            if f is None:
                posl += 1
            else:
                self.meř(f)
                posl = max(posl + 1, spodek + MEZERA + f['up'])
                spodek = posl + f['down']
                wf = max(wf, f['w'])
            linky.append(posl)
        e['piny_l'] = linky
        e['down'] = max(linky[-1], e['piny_r'][-1]) + (1 if p.get('pod') else 0)
        return wf

    def umisti(self, t, c0, r, c_konec):
        k = t['k']
        if k == 'E':
            c = self.posl if t.get('vpravo') else c0
            self.pozice[t['id']] = {'c': c, 'r': r}
            p = self.p[t['id']]
            if p['typ'] == 'box':
                for pin, linka in zip(p['vstupy'], t['piny_l'][1:]):
                    f = self.boxpiny.get(t['id'], {}).get(pin['pin'])
                    if f is not None:
                        fc = c - f['w']
                        if fc > 0:
                            self.cary.append(('h', r + linka, ('c', 0), ('c', fc)))
                        self.umisti(f, fc, r + linka, c)
        elif k == 'S':
            for d in t['deti']:
                self.umisti(d, c0 + d['c_rel'], r, None)
            if c_konec is not None:
                konec = ('c', c_konec)
            elif not t['deti']:
                konec = None
            else:
                last = t['deti'][-1]
                if last.get('vpravo'):
                    konec = ('ce', last['id'])   # vodič končí na cívce (TIA nemá pravou lištu)
                elif last['k'] == 'E' and self.p[last['id']]['typ'] == 'box':
                    konec = ('bo', last['id'])
                elif last['k'] == 'PO':
                    konec = ('c', c0 + last['c_rel'])
                else:
                    konec = ('c', c0 + t['w'])
            if konec is not None:
                self.cary.append(('h', r, ('c', c0), konec))
        else:
            for v, o in zip(t['vetve'], t['off']):
                self.umisti(v, c0, r + o, c0 + t['w'] if k == 'P' else None)
            self.cary.append(('v', ('c', c0), r, r + t['off'][-1]))
            if k == 'P':
                self.cary.append(('v', ('c', c0 + t['w']), r, r + t['off'][-1]))


# ---------- 4: pixely ----------

def _sirka_prvku(p, e):
    t = p['typ']
    if t == 'box':
        vst = [x for x, l in zip(p['vstupy'], e.get('piny_l', [])[1:])]
        lw = max([_tw(x['operand']) for x in vst if not x['operand'].startswith('wire#')] + [0]) + 20
        rw = max([_tw(x['operand']) for x in p['vystupy']] + [0]) + 20
        nl = max([len(x['pin']) for x in p['vstupy']] + [len(p['napajeni'][0])])
        nr = max([len(x['pin']) for x in p['vystupy']] + [len(p['napajeni'][1])])
        bw = max(76, (nl + nr) * ZN + 22, len(p['nazev'] or '') * ZN + 18, len(p.get('sablona') or '') * ZN + 18)
        return max(lw + bw + rw, _tw(p.get('instance')) + 10), lw, bw, rw
    if t == 'porovnani':
        return max(60, _tw(p['in1']) + 14, _tw(p['in2']) + 14), 0, 0, 0
    if t == 'not':
        return 50, 0, 0, 0
    return max(58, _tw(p.get('operand')) + 14, _tw(p.get('bit')) + 14 if p.get('bit') else 0), 0, 0, 0


def rozloz(rungy, prvky, min_sirka=0):
    """-> (rozlozeni {id: {r, c}}, kresba dict) ; vyhodí Nerozlozitelne.
    min_sirka (px): cívky se odsunou k pravému okraji této šířky (jako v TIA)."""
    R = Rozlozeni(rungy, prvky)
    if not R.pozice:
        # prázdný network: jen lišta
        return {}, {'sirka': 260, 'vyska': 3 * L, 'lista': {'x': LISTA_X, 'y1': 4, 'y2': 3 * L}, 'cary': [], 'prvky': [],
                    'prazdny': True}
    uzly = {}

    def projdi(t):
        if t['k'] == 'E':
            uzly[t['id']] = t
        for d in t.get('deti', []) + t.get('vetve', []):
            projdi(d)
        if t['k'] == 'E':
            for f in R.boxpiny.get(t['id'], {}).values():
                projdi(f)
    projdi(R.koren)

    ncol = max(v['c'] for v in R.pozice.values()) + 1
    sirky = [24.0] * ncol
    geo = {}
    for pid, poz in R.pozice.items():
        g = _sirka_prvku(R.p[pid], uzly[pid])
        geo[pid] = g
        sirky[poz['c']] = max(sirky[poz['c']], g[0])
    if ncol > R.posl and LISTA_X + sum(sirky) + 24 < min_sirka:
        sirky[R.posl - 1] += min_sirka - (LISTA_X + sum(sirky) + 24)
    xs = [LISTA_X]
    for s in sirky:
        xs.append(xs[-1] + s)
    linky = [poz['r'] - uzly[pid]['up'] for pid, poz in R.pozice.items()]
    linky_dol = [poz['r'] + uzly[pid]['down'] for pid, poz in R.pozice.items()]
    lmin, lmax = min(linky), max(linky_dol)

    def Y(linka):
        return NAHORU + (linka - lmin) * L + L / 2

    vyst = []
    for pid, poz in sorted(R.pozice.items(), key=lambda kv: (kv[1]['r'], kv[1]['c'])):
        p, e = R.p[pid], uzly[pid]
        sir, lw, bw, rw = geo[pid]
        x0 = xs[poz['c']]
        posun = (sirky[poz['c']] - sir) / 2
        d = {'id': pid, 'x': round(x0, 1), 'w': round(sirky[poz['c']], 1), 'y': Y(poz['r']),
             'cx': round(x0 + sirky[poz['c']] / 2, 1)}
        if p['typ'] == 'box':
            bx = x0 + posun + lw
            d.update({'bx': round(bx, 1), 'bw': round(bw, 1), 'radky': [Y(poz['r'] + i) for i in e['piny_l']],
                      'radky_r': [Y(poz['r'] + i) for i in e['piny_r']],
                      'y_hl': Y(poz['r'] - (1 + (1 if p.get('sablona') else 0)))})
        vyst.append(d)
    pozx = {d['id']: d for d in vyst}

    def X(spec):
        if spec[0] == 'c':
            return xs[spec[1]]
        if spec[0] == 'ce':
            return pozx[spec[1]]['cx'] + 9
        b = pozx[spec[1]]
        return b['bx'] + b['bw'] + 10

    cary = []
    for c in R.cary:
        if c[0] == 'h':
            x1, x2 = X(c[2]), X(c[3])
            if x2 > x1:
                cary.append([round(x1, 1), Y(c[1]), round(x2, 1), Y(c[1])])
        else:
            if c[3] > c[2]:
                x = round(X(c[1]), 1)
                cary.append([x, Y(c[2]), x, Y(c[3])])
    vyska = Y(lmax) + L
    kresba = {'sirka': round(xs[-1] + 24, 1), 'vyska': vyska,
              'lista': {'x': LISTA_X, 'y1': NAHORU, 'y2': vyska - 4},
              'cary': cary, 'prvky': vyst}
    rozl = {pid: {'r': poz['r'] - lmin, 'c': poz['c']} for pid, poz in R.pozice.items()}
    return rozl, kresba
