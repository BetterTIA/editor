"""Layout of a LAD network from the `.s7dcl` graph (rungs + named wires) into a grid.

Steps:
1. rungs -> segments (edges) between the nodes powerrail / wN / open end,
2. series-parallel reduction of the graph to a tree: S (series), P (closed branch: starts and ends on the rung),
   PO (open fan: output branches / several rungs from the rail),
   a wire that is the value of a box pin (`pin := wire#wN`) = feed to the pin (feeder) from the rail,
3. measuring (columns, lines above/below the rung), placement, coils at the end of an open series to the right edge,
4. pixels: column width by the widest element (operand), wire lines.
What cannot be reduced (bridge, pin feed from somewhere other than the rail, FBD) returns a warning -> the page shows the rung list.
"""
import re
from collections import defaultdict

L = 18           # line height (px); elements occupy whole lines above/below the rung
GAP = 2       # free lines between stacked branches
CHAR_W = 7.2         # character width of 12 px monospace
MAX_CHARS = 30       # a longer operand is truncated (…), the full one is in the title
RAIL_X = 16
UP = 10      # margin above the first line

COILS = {'coil', 'coil_s', 'coil_r', 'coil_neg', 'coil_p', 'coil_n', 'ret'}


class LayoutError(Exception):
    pass


def _tw(s):
    s = s or '...'
    return min(len(s), MAX_CHARS) * CHAR_W


# ---------- 1+2: graph -> tree ----------

def tree(rungs, elements):
    """-> (root, feeds); feeds = {(box_id, pin): tree}"""
    byid = {p['id']: p for p in elements}
    pins_wire = {}
    for p in elements:
        if p['type'] == 'box':
            for pin in p['inputs']:
                m = re.fullmatch(r'wire#(\w+)', pin['operand'])
                if m:
                    pins_wire[m.group(1)] = (p['id'], pin['pin'])
    edges, por = [], 0
    nsink = [0]

    def sink():
        nsink[0] += 1
        return '#%d' % nsink[0]

    for ri, r in enumerate(rungs):
        if r['from'].startswith('op:'):
            raise LayoutError('rung starts with an operand (FBD shape)')
        node, el = r['from'], []
        for it in r['path']:
            if it.startswith('wire#'):
                edges.append([node, it[5:], {'k': 'S', 'children': [{'k': 'E', 'id': x} for x in el]}, por])
                por += 1
                node, el = it[5:], []
            else:
                el.append(it)
        end = r['to'] if r['to'] else sink()
        if end.startswith('op:'):
            raise LayoutError('rung ends with an operand')
        edges.append([node, end, {'k': 'S', 'children': [{'k': 'E', 'id': x} for x in el]}, por])
        por += 1

    protected = {'powerrail'} | set(pins_wire)

    def ser(a, b):
        children = []
        for t in (a, b):
            children += t['children'] if t['k'] == 'S' else [t]
        return {'k': 'S', 'children': children}

    modification = True
    while modification:
        modification = False
        sk = defaultdict(list)
        for h in edges:
            sk[(h[0], h[1])].append(h)
        for (a, b), g in sk.items():          # closed parallel branch
            if len(g) > 1 and not b.startswith('#'):
                g.sort(key=lambda h: h[3])
                for h in g:
                    edges.remove(h)
                edges.append([a, b, {'k': 'P', 'branches': [h[2] for h in g]}, g[0][3]])
                modification = True
                break
        if modification:
            continue
        nodes = {h[0] for h in edges} | {h[1] for h in edges}
        for n in sorted(nodes):                # series through a node with 1 input and 1 output
            if n in protected or n.startswith('#'):
                continue
            inp = [h for h in edges if h[1] == n]
            res = [h for h in edges if h[0] == n]
            if len(inp) == 1 and len(res) == 1:
                edges.remove(inp[0])
                edges.remove(res[0])
                edges.append([inp[0][0], res[0][1], ser(inp[0][2], res[0][2]), min(inp[0][3], res[0][3])])
                modification = True
                break
        if modification:
            continue
        for n in sorted(nodes):                # open fan (output branches / rungs from the rail)
            opened = sorted([h for h in edges if h[0] == n and h[1].startswith('#')], key=lambda h: h[3])
            if len(opened) >= 2:
                for h in opened:
                    edges.remove(h)
                branches = []
                for h in opened:
                    branches += h[2]['branches'] if h[2]['k'] == 'PO' else [h[2]]
                edges.append([n, sink(), {'k': 'PO', 'branches': branches}, opened[0][3]])
                modification = True
                break

    tree_root, feeds = None, {}
    for a, b, t, _ in edges:
        if a != 'powerrail':
            raise LayoutError('wiring is not series-parallel (node %s)' % a)
        if b.startswith('#'):
            if tree_root is not None:
                raise LayoutError('more than one root')
            tree_root = t
        elif b in pins_wire:
            feeds[pins_wire[b]] = t
        else:
            raise LayoutError('wiring is not series-parallel (node %s)' % b)
    if tree_root is None:
        raise LayoutError('network has no open rung')
    if tree_root['k'] != 'S':
        tree_root = {'k': 'S', 'children': [tree_root]}
    for (bid, pin) in feeds:
        if bid not in byid:
            raise LayoutError('feed to an unknown box')
    _mark_open(tree_root, True)
    return tree_root, feeds


def _mark_open(t, open):
    if t['k'] == 'S':
        t['open'] = open
        for i, d in enumerate(t['children']):
            _mark_open(d, open and i == len(t['children']) - 1)
    elif t['k'] == 'P':
        for v in t['branches']:
            _mark_open(v, False)
    elif t['k'] == 'PO':
        for v in t['branches']:
            _mark_open(v, True)


# ---------- 3: measuring and placement ----------

class Layout:
    def __init__(self, rungs, elements):
        self.p = {p['id']: p for p in elements}
        self.tree_root, self.feeds = tree(rungs, elements)
        self.boxpins = defaultdict(dict)       # box id -> {pin: feed tree}
        for (bid, pin), t in self.feeds.items():
            _mark_open(t, False)
            self.boxpins[bid][pin] = t
        self.lines = []                         # ('h', line, xa, xb) / ('v', x, l1, l2); x = ('c', column) | ('bo', id)
        self.position = {}                       # id -> {'c', 'r'}
        self.measure(self.tree_root)
        self.last_col = self.tree_root['w'] + 1        # coils at the right edge; column root.w = gap (stretches)
        self.place(self.tree_root, 0, 0, None)

    # element size: (above, below) in lines
    def _element(self, e):
        p = self.p[e['id']]
        t = p['type']
        e['w'] = 1
        if t == 'box':
            hl = 1 + (1 if p.get('template') else 0)
            e['up'] = hl + (1 if p.get('instance') else 0)
            nl, nr = 1 + len(p['inputs']), 1 + len(p['outputs'])
            e['pins_l'] = list(range(nl))
            e['pins_r'] = list(range(nr))
            e['down'] = max(nl, nr) - 1 + (1 if p.get('below') else 0)
        elif t == 'comparison':
            e['up'], e['down'] = 1, 2 if p.get('template') else 1
        elif t in ('contact_p', 'contact_n'):
            e['up'], e['down'] = 1, 1
        else:
            e['up'], e['down'] = 1, 0

    def measure(self, t, bottom_left=0):
        k = t['k']
        if k == 'E':
            self._element(t)
        elif k == 'S':
            cur, up, down = 0, 0, 0
            for i, d in enumerate(t['children']):
                if d['k'] == 'E' and d['id'] in self.boxpins:
                    self._element(d)
                    wf = self._feeds(d, down)
                    col = max(cur, wf)
                else:
                    self.measure(d)
                    col = cur
                d['c_rel'] = col
                d['right'] = (t.get('open') and i == len(t['children']) - 1 and d['k'] == 'E'
                               and self.p[d['id']]['type'] in COILS)
                if not d['right']:
                    cur = col + d['w']
                up, down = max(up, d['up']), max(down, d['down'])
            t['w'], t['up'], t['down'] = cur, up, down
        else:  # P / PO
            off, w = [], 0
            for i, v in enumerate(t['branches']):
                self.measure(v)
                w = max(w, v['w'])
                if i == 0:
                    off.append(0)
                else:
                    pv = t['branches'][i - 1]
                    off.append(off[-1] + pv['down'] + GAP + v['up'])
            t['w'], t['off'] = w, off
            t['up'] = t['branches'][0]['up']
            t['down'] = off[-1] + t['branches'][-1]['down']

    def _feeds(self, e, bottom):
        """Places the box pins so that the feeds (from the rail) are below everything on the left. -> width of the feeds."""
        p = self.p[e['id']]
        fed = self.boxpins[e['id']]
        tracks, last_col, wf = [0], 0, 0
        for pin in p['inputs']:
            f = fed.get(pin['pin'])
            if f is None:
                last_col += 1
            else:
                self.measure(f)
                last_col = max(last_col + 1, bottom + GAP + f['up'])
                bottom = last_col + f['down']
                wf = max(wf, f['w'])
            tracks.append(last_col)
        e['pins_l'] = tracks
        e['down'] = max(tracks[-1], e['pins_r'][-1]) + (1 if p.get('below') else 0)
        return wf

    def place(self, t, c0, r, c_end):
        k = t['k']
        if k == 'E':
            c = self.last_col if t.get('right') else c0
            self.position[t['id']] = {'c': c, 'r': r}
            p = self.p[t['id']]
            if p['type'] == 'box':
                for pin, track in zip(p['inputs'], t['pins_l'][1:]):
                    f = self.boxpins.get(t['id'], {}).get(pin['pin'])
                    if f is not None:
                        fc = c - f['w']
                        if fc > 0:
                            self.lines.append(('h', r + track, ('c', 0), ('c', fc)))
                        self.place(f, fc, r + track, c)
        elif k == 'S':
            for d in t['children']:
                self.place(d, c0 + d['c_rel'], r, None)
            if c_end is not None:
                end = ('c', c_end)
            elif not t['children']:
                end = None
            else:
                last = t['children'][-1]
                if last.get('right'):
                    end = ('ce', last['id'])   # the wire ends at the coil (TIA has no right rail)
                elif last['k'] == 'E' and self.p[last['id']]['type'] == 'box':
                    end = ('bo', last['id'])
                elif last['k'] == 'PO':
                    end = ('c', c0 + last['c_rel'])
                else:
                    end = ('c', c0 + t['w'])
            if end is not None:
                self.lines.append(('h', r, ('c', c0), end))
        else:
            for v, o in zip(t['branches'], t['off']):
                self.place(v, c0, r + o, c0 + t['w'] if k == 'P' else None)
            self.lines.append(('v', ('c', c0), r, r + t['off'][-1]))
            if k == 'P':
                self.lines.append(('v', ('c', c0 + t['w']), r, r + t['off'][-1]))


# ---------- 4: pixels ----------

def _element_width(p, e):
    t = p['type']
    if t == 'box':
        inp = [x for x, l in zip(p['inputs'], e.get('pins_l', [])[1:])]
        lw = max([_tw(x['operand']) for x in inp if not x['operand'].startswith('wire#')] + [0]) + 20
        rw = max([_tw(x['operand']) for x in p['outputs']] + [0]) + 20
        nl = max([len(x['pin']) for x in p['inputs']] + [len(p['power'][0])])
        nr = max([len(x['pin']) for x in p['outputs']] + [len(p['power'][1])])
        bw = max(76, (nl + nr) * CHAR_W + 22, len(p['name'] or '') * CHAR_W + 18, len(p.get('template') or '') * CHAR_W + 18)
        return max(lw + bw + rw, _tw(p.get('instance')) + 10), lw, bw, rw
    if t == 'comparison':
        return max(60, _tw(p['in1']) + 14, _tw(p['in2']) + 14), 0, 0, 0
    if t == 'not':
        return 50, 0, 0, 0
    return max(58, _tw(p.get('operand')) + 14, _tw(p.get('bit')) + 14 if p.get('bit') else 0), 0, 0, 0


def layout(rungs, elements, min_width=0):
    """-> (layout {id: {r, c}}, drawing dict); raises LayoutError.
    min_width (px): coils are pushed to the right edge of this width (as in TIA)."""
    R = Layout(rungs, elements)
    if not R.position:
        # empty network: rail only
        return {}, {'width': 260, 'height': 3 * L, 'rail': {'x': RAIL_X, 'y1': 4, 'y2': 3 * L}, 'lines': [], 'elements': [],
                    'empty': True}
    nodes = {}

    def walk(t):
        if t['k'] == 'E':
            nodes[t['id']] = t
        for d in t.get('children', []) + t.get('branches', []):
            walk(d)
        if t['k'] == 'E':
            for f in R.boxpins.get(t['id'], {}).values():
                walk(f)
    walk(R.tree_root)

    ncol = max(v['c'] for v in R.position.values()) + 1
    widths = [24.0] * ncol
    geo = {}
    for pid, pos in R.position.items():
        g = _element_width(R.p[pid], nodes[pid])
        geo[pid] = g
        widths[pos['c']] = max(widths[pos['c']], g[0])
    if ncol > R.last_col and RAIL_X + sum(widths) + 24 < min_width:
        widths[R.last_col - 1] += min_width - (RAIL_X + sum(widths) + 24)
    xs = [RAIL_X]
    for s in widths:
        xs.append(xs[-1] + s)
    tracks = [pos['r'] - nodes[pid]['up'] for pid, pos in R.position.items()]
    tracks_down = [pos['r'] + nodes[pid]['down'] for pid, pos in R.position.items()]
    lmin, lmax = min(tracks), max(tracks_down)

    def Y(track):
        return UP + (track - lmin) * L + L / 2

    out = []
    for pid, pos in sorted(R.position.items(), key=lambda kv: (kv[1]['r'], kv[1]['c'])):
        p, e = R.p[pid], nodes[pid]
        wid, lw, bw, rw = geo[pid]
        x0 = xs[pos['c']]
        shift = (widths[pos['c']] - wid) / 2
        d = {'id': pid, 'x': round(x0, 1), 'w': round(widths[pos['c']], 1), 'y': Y(pos['r']),
             'cx': round(x0 + widths[pos['c']] / 2, 1)}
        if p['type'] == 'box':
            bx = x0 + shift + lw
            d.update({'bx': round(bx, 1), 'bw': round(bw, 1), 'rows': [Y(pos['r'] + i) for i in e['pins_l']],
                      'rows_r': [Y(pos['r'] + i) for i in e['pins_r']],
                      'y_hl': Y(pos['r'] - (1 + (1 if p.get('template') else 0)))})
        out.append(d)
    posx = {d['id']: d for d in out}

    def X(spec):
        if spec[0] == 'c':
            return xs[spec[1]]
        if spec[0] == 'ce':
            return posx[spec[1]]['cx'] + 9
        b = posx[spec[1]]
        return b['bx'] + b['bw'] + 10

    lines = []
    for c in R.lines:
        if c[0] == 'h':
            x1, x2 = X(c[2]), X(c[3])
            if x2 > x1:
                lines.append([round(x1, 1), Y(c[1]), round(x2, 1), Y(c[1])])
        else:
            if c[3] > c[2]:
                x = round(X(c[1]), 1)
                lines.append([x, Y(c[2]), x, Y(c[3])])
    height = Y(lmax) + L
    drawing = {'width': round(xs[-1] + 24, 1), 'height': height,
              'rail': {'x': RAIL_X, 'y1': UP, 'y2': height - 4},
              'lines': lines, 'elements': out}
    lay = {pid: {'r': pos['r'] - lmin, 'c': pos['c']} for pid, pos in R.position.items()}
    return lay, drawing
