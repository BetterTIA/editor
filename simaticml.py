"""SimaticML (the TIA Portal V21 XML export of a block) for the XML way of Upload (E4/E5/E7).

ImportFromDocuments fails for an FB with GRAPH (also as a multi-instance), loses an OB's event class and number and fails
for a new network. This module edits the block's own export from ExportXmlBlock instead and leaves everything it does not
change byte for byte:

- `flgnet(model)` — the editor's "after" network model (LAD rungs or FBD boxes) -> `<FlgNet>` (FlgNet v5: Parts with
  Access / Part / Call, Wires with Powerrail / NameCon / IdentCon / OpenCon); a join of parallel LAD branches is an `O`
  part, the way TIA exports it;
- `edit_block(xml, edits, …)` — interface members into the `Interface/Sections` (v5), new networks as new
  `SW.Blocks.CompileUnit`s, a changed network's FlgNet replaced, titles and comments into the network's
  `MultilingualText` in the culture of the export;
- `graph(flgnet)` — the wiring as a multiset of edges independent of UIds and order: the safeguard that the network in
  TIA is still the proposal's "before" network;
- `ob_attributes` / `ob_mismatch` — number, event class and cycle of an OB, compared with GetBlockInfo after the import.

The shapes follow V21 exports (Contact / Coil / SCoil / RCoil / PContact / NContact, Eq…Le, Move, Add…Div, Inc, InRange,
TON / TP with Instance and time_type, PBox, Call with CallInfo and an OpenCon on each unconnected parameter, O with Card),
and so does the order (`_Net._flow`): parts by the current flow (a part after the parts feeding it), the power rail's
connections in the order that walk meets them, accesses, OpenCons and wires in the order of the parts. OutRange, TOF,
NBox and the FBD parts A / X are written by analogy and are not verified in an export. Everything else is refused before any write (`unsupported`).
"""
import collections
import re
import xml.etree.ElementTree as ET

FLG_NS = 'http://www.siemens.com/automation/Openness/SW/NetworkSource/FlgNet/v5'
NL = '\n'

CONTACTS = {'contact': 'Contact', 'contact_nc': 'Contact', 'contact_p': 'PContact', 'contact_n': 'NContact'}
COILS = {'coil': 'Coil', 'coil_s': 'SCoil', 'coil_r': 'RCoil'}
FBD_COILS = {'assign': 'Coil', 's': 'SCoil', 'r': 'RCoil'}
LOGIC = {'and': 'A', 'or': 'O', 'xor': 'X'}
COMPARE = {'==': 'Eq', '<>': 'Ne', '<': 'Lt', '<=': 'Le', '>': 'Gt', '>=': 'Ge'}
MATH = {'ADD': 'Add', 'SUB': 'Sub', 'MUL': 'Mul', 'DIV': 'Div'}
RANGE = {'IN_RANGE': 'InRange', 'OUT_RANGE': 'OutRange'}
TIMERS = {'TON', 'TOF', 'TP'}
INTEGER = {'SInt', 'Int', 'DInt', 'LInt', 'USInt', 'UInt', 'UDInt', 'ULInt', 'Byte', 'Word', 'DWord', 'LWord'}
REAL = {'Real', 'LReal'}
# system types of a multi-instance member: TIA writes them with Version="1.0"
# shortcut: only the IEC timers / counters / edges are listed; add a type when an import asks for its version
VERSIONED = {'TON_TIME', 'TOF_TIME', 'TP_TIME', 'TONR_TIME', 'IEC_TIMER', 'IEC_COUNTER', 'CTU_INT', 'CTD_INT', 'CTUD_INT',
             'R_TRIG', 'F_TRIG'}
SUPPORTED = ('contacts (NO, NC, P, N), coils (=, S, R), parallel branches, comparisons (==, <>, <, >, <=, >=), MOVE, '
             'ADD, SUB, MUL, DIV, INC, IN_RANGE, OUT_RANGE, TON, TOF, TP, P_TRIG, N_TRIG and FB / FC calls; FBD &, >=1, x, =, S, R')


class XmlError(ValueError):
    pass


# ---------- writing XML text the way TIA indents it ----------

def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _xml(node, ind, nl=NL):
    """(tag, [(attribute, value)], children | text | None) -> text; two spaces per level, `<x a="v" />` like TIA."""
    tag, attrs, body = node
    a = ''.join(' %s="%s"' % (k, esc(v)) for k, v in attrs)
    if body is None or body == []:
        return '%s<%s%s />' % (ind, tag, a)
    if isinstance(body, str):
        return '%s<%s%s>%s</%s>' % (ind, tag, a, esc(body).replace('&quot;', '"'), tag)
    return '%s<%s%s>%s%s%s%s</%s>' % (ind, tag, a, nl, nl.join(_xml(c, ind + '  ', nl) for c in body), nl, ind, tag)


# ---------- operands -> Access ----------

def _split(s, sep='.'):
    """Split at `sep` outside quotes and brackets."""
    out, cur, q, d = [], '', False, 0
    for c in s:
        if c == '"':
            q = not q
        elif not q and c == '[':
            d += 1
        elif not q and c == ']':
            d -= 1
        if c == sep and not q and d == 0:
            out.append(cur)
            cur = ''
        else:
            cur += c
    return out + [cur]


def _constant(text, hint):
    """-> (scope, Constant children) or None when the text is not a constant."""
    t = text.strip()
    if re.fullmatch(r'[A-Za-z0-9_]+#\S+', t):                        # T#2s, 16#FF, DINT#5: TIA keeps them typed
        # shortcut: every `prefix#value` is a TypedConstant (verified for T#); a refused import names the one to fix
        return 'TypedConstant', [('ConstantValue', [], t)]
    if re.fullmatch(r'(?i)true|false', t):
        return 'LiteralConstant', [('ConstantType', [], 'Bool'), ('ConstantValue', [], t)]
    if re.fullmatch(r'[+-]?\d+', t):
        typ = hint if hint in INTEGER else 'Int' if -32768 <= int(t) <= 32767 else 'DInt'
        return 'LiteralConstant', [('ConstantType', [], typ), ('ConstantValue', [], t)]
    if re.fullmatch(r'[+-]?\d+\.\d*([eE][+-]?\d+)?', t):
        return 'LiteralConstant', [('ConstantType', [], hint if hint in REAL else 'Real'), ('ConstantValue', [], t)]
    return None


def _component(c, what):
    m = re.fullmatch(r'("[^"]+"|[A-Za-z_]\w*)((?:\[[^\]]*\])?)', c.strip())
    if not m:
        raise XmlError('operand %s: the part "%s" is not supported by the XML way (absolute addresses, slices and '
                       'indirect access are not)' % (what, c))
    name = m.group(1).strip('"')
    if not m.group(2):
        return ('Component', [('Name', name)], None)
    idx = [_index(x, what) for x in _split(m.group(2)[1:-1], ',')]
    return ('Component', [('Name', name), ('AccessModifier', 'Array')], idx)


def _index(x, what):
    k = _constant(x, 'DInt')
    if k and k[0] == 'LiteralConstant':
        return ('Access', [('Scope', 'LiteralConstant')], [('Constant', [], k[1])])
    return ('Access', [('Scope', _scope(x, what))], [('Symbol', [], _symbol(x, what))])


def _scope(x, what):
    x = x.strip()
    if x.startswith('#'):
        return 'LocalVariable'
    if x.startswith('"'):
        return 'GlobalVariable'
    raise XmlError('operand %s is not supported by the XML way (only #local, "global", constants)' % what)


def _symbol(x, what):
    return [_component(c, what) for c in _split(x.strip().lstrip('#'))]


def access(uid, text, hint=None):
    """Operand text -> <Access> node (LocalVariable, GlobalVariable, LiteralConstant, TypedConstant)."""
    t = (text or '').strip()
    k = _constant(t, hint)
    if k:
        return ('Access', [('Scope', k[0]), ('UId', uid)], [('Constant', [], k[1])])
    return ('Access', [('Scope', _scope(t, t)), ('UId', uid)], [('Symbol', [], _symbol(t, t))])


def _instance(uid, text):
    t = (text or '').strip()
    if not t:
        raise XmlError('a timer / FB call without an instance')
    return ('Instance', [('Scope', _scope(t, t)), ('UId', uid)], _symbol(t, t))


# ---------- network model -> FlgNet ----------

class _Net:
    """Collects Access / Part nodes and the wires of one FlgNet."""

    def __init__(self):
        self.uid = 20
        self.accesses, self.parts, self.wires = [], [], []
        self.sinks = collections.OrderedDict()                 # source -> [targets]  (source = ('P',) | (uid, pin))
        self.operands = []                                     # (access uid, part uid, pin, out?)
        self.open = []                                         # (part uid, pin, out?)
        self.pins = {}                                         # part key -> its xml pins in declaration order

    def next(self):
        self.uid += 1
        return str(self.uid)

    def operand(self, part, pin, text, hint=None, out=False):
        self.operands.append((text, hint, part, pin, out))

    def connect(self, src, dst):
        self.sinks.setdefault(src, []).append(dst)

    def _number(self, node):
        """Fill the empty UIds of a part and its children in document order."""
        tag, attrs, body = node
        attrs = [(k, self.next() if k == 'UId' and v is None else v) for k, v in attrs]
        return tag, attrs, [self._number(c) for c in body] if isinstance(body, list) else body

    def _rank(self, part, pin):
        """Order of a part's pins: the power pins (en, in, pre…) first, then the box pins as declared (in1 < in2 < … for O)."""
        p = self.pins.get(part, [])
        return p.index(pin) + 1 if pin in p else int(re.sub(r'\D', '', pin) or 0)

    def _flow(self):
        """-> (part keys, power rail connections) in the order of the current flow, the way TIA exports a network: depth
        first from the parts that feed no other part (in model order), each part after the parts feeding its inputs (in
        pin order); a power rail connection where this walk meets it. TIA refuses other orders ("The elements must be sorted according to the
        current flow"; "… the connections in the power rail must be located in the same sequence")."""
        feeds = collections.defaultdict(list)
        for s, ds in self.sinks.items():
            for d in ds:
                feeds[d[0]].append((self._rank(*d), s, d))
        seen, order, rail = set(), [], []

        def visit(k):
            if k not in seen:
                seen.add(k)
                for _, s, d in sorted(feeds[k], key=lambda x: x[0]):
                    if s == ('P',):
                        rail.append(d)
                    else:
                        visit(s[0])
                order.append(k)
        fed = {s[0] for s in self.sinks if s != ('P',)}
        for k in [k for k, _, _ in self.parts if k not in fed] + [k for k, _, _ in self.parts]:
            visit(k)                                           # from the ends of the rungs (coils, boxes), in model order
        return order, rail

    def build(self):
        """Everything in the order of the parts, as TIA exports it: Access UIds first (in the order of the parts they feed),
        then the parts, the OpenCons and the wires (power rail first, then per part its inputs and its outputs); the
        connections of a branching wire in the order of the parts."""
        order, rail = self._flow()
        at = {k: i for i, k in enumerate(order)}
        node_of = {k: n for k, _, n in self.parts}
        rank = self._rank
        acc = []
        for text, hint, part, pin, out in sorted(self.operands, key=lambda o: (at[o[2]], o[4])):
            uid = self.next()
            acc.append((uid, part, pin, out))
            self.accesses.append(access(uid, text, hint))
        ids, parts = {}, []
        for key in order:                                      # parts after the accesses; an Instance right after its part
            node = self._number(node_of[key])
            ids[key] = dict(node[1])['UId']
            parts.append(node)
        opens = [(self.next(), part, pin, out) for part, pin, out in self.open]
        wires = []

        def con(c):
            return ('Powerrail', [], None) if c == ('P',) else ('NameCon', [('UId', ids[c[0]]), ('Name', c[1])], None)
        for s, ds in self.sinks.items():
            k = (-1, 0, 0) if s == ('P',) else (at[s[0]], 1, rank(*s))
            ds = rail if s == ('P',) else sorted(ds, key=lambda d: at[d[0]])
            wires.append((k, [con(s)] + [con(d) for d in ds]))
        for uid, part, pin, out in acc:
            ident = ('IdentCon', [('UId', uid)], None)
            wires.append(((at[part], int(out), rank(part, pin)), [con((part, pin)), ident] if out else [ident, con((part, pin))]))
        for uid, part, pin, out in opens:
            o = ('OpenCon', [('UId', uid)], None)
            wires.append(((at[part], int(out), rank(part, pin)), [con((part, pin)), o] if out else [o, con((part, pin))]))
        wires = [('Wire', [('UId', self.next())], w) for _, w in sorted(wires, key=lambda w: w[0])]
        return ('FlgNet', [('xmlns', FLG_NS)], [('Parts', [], self.accesses + parts or None), ('Wires', [], wires or None)])


def _part_node(name, extra=(), version=None, disabled_eno=False, children=None):
    attrs = [('Name', name)] + ([('Version', version)] if version else []) + [('UId', None)] + \
        ([('DisabledENO', 'true')] if disabled_eno else [])
    return ('Part', attrs, list(children or []))


def _template(name, typ):
    return ('TemplateValue', [('Name', name), ('Type', 'Type')], typ)


def _card(n):
    return ('TemplateValue', [('Name', 'Card'), ('Type', 'Cardinality')], str(n))


def _src_type(t):
    return _template('SrcType', t) if t else ('AutomaticTyped', [('Name', 'SrcType')], None)


def _box(p, call_params):
    """Box element (LAD box / FBD call or instruction) -> (Part | Call node, power in, power out, {model pin: (xml pin, hint)},
    open outputs?). Raises XmlError for what the XML way does not write."""
    name, inst, tmpl = p.get('name'), p.get('instance'), p.get('template')
    pins = {}
    if p.get('call') in ('fb', 'fc'):
        kind = 'FB' if p['call'] == 'fb' and inst else 'FC'
        if not name or name == 'FB ?':
            raise XmlError('a call of an unknown block (instance %s) — the XML way needs the called block' % inst)
        params = call_params(name, kind) if call_params else None
        if params == 'any':                                    # a check before the export: the pins are taken as they are
            params = [(x['pin'], 'Input', '') for x in p.get('inputs', [])] + \
                [(x['pin'], 'Output', '') for x in p.get('outputs', [])]
        if params is None:
            raise XmlError('the interface of %s "%s" is not in the export — the XML way cannot write its call' % (kind, name))
        info = [('Parameter', [('Name', n), ('Section', s), ('Type', t)], None) for n, s, t in params]
        pins = {n: (n, t) for n, s, t in params}
        for x in p.get('inputs', []) + p.get('outputs', []):
            if x['pin'] not in pins:
                raise XmlError('%s "%s" has no parameter %s' % (kind, name, x['pin']))
        ci = [('Instance', [('Scope', _scope(inst, inst)), ('UId', None)], _symbol(inst, inst))] if kind == 'FB' else []
        node = ('Call', [('UId', None)], [('CallInfo', [('Name', name), ('BlockType', kind)], ci + info)])
        return node, 'en', 'eno', pins, {n: s for n, s, t in params}
    if name == 'INC':
        if not tmpl:
            raise XmlError('INC without its data type — the XML way needs it (DestType)')
        pins.update({'IN': ('operand', tmpl), 'OPERAND': ('operand', tmpl)})  # positional / named in the documents
        return _part_node('Inc', disabled_eno=True, children=[_template('DestType', tmpl)]), 'en', 'eno', pins, True
    if name == 'MOVE':
        outs = p.get('outputs', [])
        for i, x in enumerate(outs, 1):
            pins[x['pin']] = ('out%d' % i, None)
        pins['IN'] = ('in', None)
        return _part_node('Move', disabled_eno=True, children=[_card(max(len(outs), 1))]), 'en', 'eno', pins, True
    if name in MATH:
        ins = [x for x in p.get('inputs', []) if re.fullmatch(r'IN\d+', x['pin'])]
        for x in ins:
            pins[x['pin']] = (x['pin'].lower(), tmpl)
        pins['OUT'] = ('out', tmpl)
        body = ([_card(len(ins))] if name in ('ADD', 'MUL') else []) + [_src_type(tmpl)]
        return _part_node(MATH[name], disabled_eno=True, children=body), 'en', 'eno', pins, True
    if name in RANGE:
        pins.update({'MIN': ('min', tmpl), 'VAL': ('in', tmpl), 'IN': ('in', tmpl), 'MAX': ('max', tmpl)})
        return _part_node(RANGE[name], children=[_src_type(tmpl)]), 'pre', 'out', pins, True
    if name in TIMERS:
        if not inst:
            raise XmlError('%s without an instance' % name)
        pins.update({'PT': ('PT', 'Time'), 'ET': ('ET', 'Time')})
        body = [('Instance', [('Scope', _scope(inst, inst)), ('UId', None)], _symbol(inst, inst)),
                _template('time_type', tmpl or 'Time')]
        return _part_node(name, version='1.0', children=body), 'IN', 'Q', pins, True
    raise XmlError('%s is not written by the XML way (it writes %s)' % (name or 'this box', SUPPORTED))


def _element(net, p, call_params):
    """LAD element -> its part in net; -> (part key, power in pin, power out pin)."""
    key, t = p['id'], p['type']
    if t in CONTACTS:
        neg = [('Negated', [('Name', 'operand')], None)] if t == 'contact_nc' else []
        net.parts.append((key, None, _part_node(CONTACTS[t], children=neg)))
        net.operand(key, 'operand', _required(p, 'operand'))
        if t in ('contact_p', 'contact_n'):
            net.operand(key, 'bit', _required(p, 'bit'))
            net.pins[key] = ['bit', 'operand']                # TIA wires the edge bit first (its accesses: operand first)
            return key, 'pre', 'out'
        return key, 'in', 'out'
    if t in COILS:
        net.parts.append((key, None, _part_node(COILS[t])))
        net.operand(key, 'operand', _required(p, 'operand'))
        return key, 'in', 'out'
    if t == 'comparison':
        if p.get('op') not in COMPARE:
            raise XmlError('comparison %r is not written by the XML way' % p.get('op'))
        net.parts.append((key, None, _part_node(COMPARE[p['op']], children=[_src_type(p.get('template'))])))
        net.operand(key, 'in1', _required(p, 'in1'), p.get('template'))
        net.operand(key, 'in2', _required(p, 'in2'), p.get('template'))
        return key, 'pre', 'out'
    if t == 'box' and p.get('name') in ('P_TRIG', 'N_TRIG') and not p.get('instance'):
        net.parts.append((key, None, _part_node('PBox' if p['name'] == 'P_TRIG' else 'NBox')))
        net.operand(key, 'bit', _required(p, 'below'))
        return key, 'in', 'out'
    if t == 'box':
        if p.get('neg'):
            raise XmlError('a negated box (%s) is not written by the XML way' % (p.get('name') or p.get('instance')))
        node, pin_in, pin_out, pins, open_out = _box(p, call_params)
        net.parts.append((key, None, node))
        _box_pins(net, key, p, pins, open_out)
        return key, pin_in, pin_out
    raise XmlError('%s is not written by the XML way (it writes %s)' % ({'coil_neg': 'a negated coil', 'ret': 'RET',
                                                                           'not': 'NOT', 'coil_p': '-(P)-',
                                                                           'coil_n': '-(N)-'}.get(t, t), SUPPORTED))


def _required(p, k):
    v = (p.get(k) or '').strip()
    if not v:
        raise XmlError('element %s has no %s' % (p.get('id'), k))
    return v


def _box_pins(net, key, p, pins, open_out, skip=()):
    """Operands of a box's pins (inputs fed by a node are wired by the caller). Pins the XML way does not know -> error.
    open_out: True = open outputs get an OpenCon; a call's {parameter: section} = every parameter without a connection
    gets an OpenCon, as in TIA's export (without it TIA refuses: "The connection with the name 'n' is not connected")."""
    call = isinstance(open_out, dict)
    net.pins[key] = [v[0] for v in pins.values()]
    for x in p.get('inputs', []):
        if x['pin'] in skip or (x.get('operand') or '').startswith('wire#') or x.get('signal'):
            continue
        if x['pin'] not in pins:
            raise XmlError('pin %s of %s is not written by the XML way' % (x['pin'], p.get('name')))
        if (x.get('operand') or '').strip():
            net.operand(key, pins[x['pin']][0], x['operand'], pins[x['pin']][1])
        elif not call:
            net.open.append((key, pins[x['pin']][0], False))
    for x in p.get('outputs', []):
        if x['pin'] not in pins:
            raise XmlError('pin %s of %s is not written by the XML way' % (x['pin'], p.get('name')))
        if x.get('neg'):
            raise XmlError('a negated output (%s) is not written by the XML way' % x['pin'])
        if (x.get('operand') or '').strip():
            net.operand(key, pins[x['pin']][0], x['operand'], pins[x['pin']][1], out=True)
        elif open_out is True:
            net.open.append((key, pins[x['pin']][0], True))
    if call:
        used = {x['pin'] for x in p.get('inputs', []) + p.get('outputs', []) if (x.get('operand') or '').strip() or x.get('signal')}
        net.open += [(key, n, s in ('Output', 'Return')) for n, s in open_out.items() if n not in used and n not in skip]


def _lad(model, call_params):
    net = _Net()
    P = {p['id']: p for p in model['elements']}
    power = {}
    for p in model['elements']:
        k, a, b = _element(net, p, call_params)
        power[p['id']] = (a, b)
    incoming = collections.OrderedDict()                      # node -> [sources]
    for r in model['rungs']:
        if r['from'] != 'powerrail' and r['from'].startswith('op:'):
            raise XmlError('a LAD rung that starts with an operand is not written by the XML way')
        src = ('P',) if r['from'] == 'powerrail' else ('J', r['from'])
        for it in r['path']:
            if it.startswith('wire#'):
                incoming.setdefault(it[5:], []).append(src)
                src = ('J', it[5:])
            elif it in P:
                net.connect(src, (it, power[it][0]))
                src = (it, power[it][1])
            else:
                raise XmlError('rung refers to a non-existent element %r' % it)
        if r.get('to'):
            incoming.setdefault(r['to'], []).append(src)
    for p in model['elements']:                               # box pins fed by a node (`pin := wire#wN`)
        for x in p.get('inputs', []) if p['type'] == 'box' else []:
            if (x.get('operand') or '').startswith('wire#'):
                pins = _box(p, call_params)[3]
                net.connect(('J', x['operand'][5:]), (p['id'], pins.get(x['pin'], (x['pin'],))[0]))
    joined = {}
    for j, srcs in incoming.items():                          # a join of several branches = an O part
        if len(srcs) == 1:
            joined[j] = srcs[0]
            continue
        key = 'O#' + j
        net.parts.append((key, None, _part_node('O', children=[_card(len(srcs))])))
        for i, s in enumerate(srcs, 1):
            net.connect(s, (key, 'in%d' % i))
        joined[j] = (key, 'out')

    def resolve(s, seen=()):
        while s[0] == 'J':
            if s[1] not in joined or s[1] in seen:
                raise XmlError('node %s has no source (an unpowered branch)' % s[1])
            seen += (s[1],)
            s = joined[s[1]]
        return s
    merged = collections.OrderedDict()
    for s, ds in net.sinks.items():
        merged.setdefault(resolve(s), []).extend(ds)
    net.sinks = merged
    return net.build()


def _fbd(model, call_params):
    import fbd                                                 # lazy: fbd imports layout / diff
    net = _Net()
    elements = fbd.fold_neg_output(model['elements'])
    main = {}
    for p in elements:
        key, t = p['id'], p['type']
        if t in LOGIC:
            names = ['in%d' % (i + 1) for i in range(len(p['inputs']))]
            negs = [('Negated', [('Name', n)], None) for n, v in zip(names, p['inputs']) if v.get('neg')]
            net.parts.append((key, None, _part_node(LOGIC[t], children=negs + [_card(len(names))])))
            pin_of = dict(zip([v['pin'] for v in p['inputs']], names))
            main[key] = 'out'
        elif t in FBD_COILS:
            if len(p['inputs']) != 1:
                raise XmlError('assignment %s needs exactly one input' % key)
            negs = [('Negated', [('Name', 'in')], None)] if p['inputs'][0].get('neg') else []
            net.parts.append((key, None, _part_node(FBD_COILS[t], children=negs)))
            net.operand(key, 'operand', _required(p, 'operand'))
            pin_of = {p['inputs'][0]['pin']: 'in'}
            main[key] = 'out'
        elif t in ('call', 'instruction'):
            node, pin_in, pin_out, pins, open_out = _box(p, call_params)
            chain = p['inputs'][0] if p['inputs'] and p['inputs'][0]['pin'] in ('EN', pin_in) else None
            pin_of = {k: v[0] for k, v in pins.items()}
            if chain is not None:
                pin_of[chain['pin']] = pin_in
            negs = [('Negated', [('Name', pin_of.get(v['pin'], v['pin']))], None) for v in p['inputs'] if v.get('neg')]
            if negs:
                if node[0] != 'Part':
                    raise XmlError('a negated input of a call is not written by the XML way')
                k = 1 if node[2] and node[2][0][0] == 'Instance' else 0      # Negated follows the Instance
                node[2][k:k] = negs
            net.parts.append((key, None, node))
            _box_pins(net, key, dict(p, inputs=[v for v in p['inputs'] if v is not chain]), pins, open_out)
            main[key] = pin_out
        else:
            raise XmlError('FBD %s is not written by the XML way (it writes %s)' % (fbd.SYMBOL.get(t, t), SUPPORTED))
        for v in p['inputs']:
            if t in ('call', 'instruction') and v['pin'] not in pin_of:
                continue
            pin = pin_of[v['pin']]
            if v.get('signal'):
                continue                                       # wired below, once every main output is known
            if t in LOGIC or t in FBD_COILS:
                if (v.get('operand') or '').strip():
                    net.operand(key, pin, v['operand'])
                else:
                    raise XmlError('input %s of %s is empty' % (v['pin'], key))
            elif t in ('call', 'instruction') and v is chain and (v.get('operand') or '').strip():
                net.operand(key, pin, v['operand'])
        p['_pins'] = pin_of
    for p in elements:
        for v in p['inputs']:
            if v.get('signal'):
                if v['signal'] not in main:
                    raise XmlError('signal from a non-existent box %s' % v['signal'])
                net.connect((v['signal'], main[v['signal']]), (p['id'], p['_pins'][v['pin']]))
        del p['_pins']
    return net.build()


def flgnet_node(model, call_params=None):
    """'After' model (LAD {elements, rungs} or FBD {language: 'FBD', elements}) -> FlgNet node; None for an empty network."""
    if not model.get('elements'):
        return None
    import copy
    m = copy.deepcopy(model)
    return _fbd(m, call_params) if m.get('language') == 'FBD' else _lad(m, call_params)


def flgnet(model, call_params=None, ind='', nl=NL):
    """-> FlgNet text ('' for an empty network)."""
    n = flgnet_node(model, call_params)
    return _xml(n, ind, nl) if n else ''


def unsupported(model, call_params=None):
    """Why the XML way cannot write the model -> text, else None (checked before any write)."""
    try:
        flgnet_node(model, call_params)
    except XmlError as e:
        return str(e)
    return None


# ---------- reading a FlgNet: the wiring as a multiset ----------

def _strip_ns(e):
    for x in e.iter():
        if isinstance(x.tag, str) and '}' in x.tag:
            x.tag = x.tag.split('}', 1)[1]
    return e


def _label_access(a):
    c = a.find('Constant')
    if c is not None:
        v = (c.findtext('ConstantValue') or c.get('Name') or '').strip()
        return 'C:' + v.lower()
    sym = a.find('Symbol')
    if sym is None:
        return a.get('Scope', '?')

    def comp(x):
        idx = ','.join(_label_access(i) for i in x.findall('Access'))
        return x.get('Name') + ('[%s]' % idx if idx else '')
    return ('#' if a.get('Scope') == 'LocalVariable' else '"') + '.'.join(comp(x) for x in sym.findall('Component'))


def graph(flg_text):
    """FlgNet text -> Counter of edges ('source.pin', 'target.pin'), parts labelled by name, negations, instance and their
    operands; open pins and UIds are left out. '' (empty network) -> empty Counter."""
    if not flg_text.strip():
        return collections.Counter()
    f = _strip_ns(ET.fromstring(flg_text))
    acc, lab = {}, {}
    parts = f.find('Parts')
    for x in list(parts) if parts is not None else []:
        uid = x.get('UId')
        if x.tag == 'Access':
            acc[uid] = _label_access(x)
        elif x.tag == 'Part':
            inst = x.find('Instance')
            lab[uid] = x.get('Name') + ''.join('!' + n.get('Name') for n in x.findall('Negated')) + \
                ('@' + '.'.join(c.get('Name') for c in inst.findall('Component')) if inst is not None else '')
        elif x.tag == 'Call':
            ci = x.find('CallInfo')
            inst = ci.find('Instance')
            lab[uid] = 'Call:%s%s' % (ci.get('Name'), '@' + '.'.join(c.get('Name') for c in inst.findall('Component'))
                                      if inst is not None else '')
    wires = [list(w) for w in (f.find('Wires') if f.find('Wires') is not None else [])]
    ops = collections.defaultdict(list)
    for w in wires:
        idents = [c for c in w if c.tag == 'IdentCon']
        names = [c for c in w if c.tag == 'NameCon']
        if idents and names:
            ops[names[0].get('UId')].append('%s=%s' % (names[0].get('Name'), acc.get(idents[0].get('UId'), '?')))
    full = {u: '%s[%s]' % (lab[u], '|'.join(sorted(ops[u]))) for u in lab}
    res = collections.Counter(('part', v) for v in full.values())
    for w in wires:
        if any(c.tag == 'IdentCon' for c in w) or w[0].tag == 'OpenCon':
            continue
        src = 'PR' if w[0].tag == 'Powerrail' else '%s.%s' % (full.get(w[0].get('UId'), '?'), w[0].get('Name'))
        for d in w[1:]:
            if d.tag == 'NameCon':
                res[(src, '%s.%s' % (full.get(d.get('UId'), '?'), d.get('Name')))] += 1
    return res


def check_flgnet(flg_text):
    """Structure of a FlgNet: unique UIds, every connection refers to a part or access, every wire has a target."""
    f = _strip_ns(ET.fromstring(flg_text))
    uids = [x.get('UId') for x in f.iter() if x.get('UId') and x.tag not in ('NameCon', 'IdentCon')]
    dup = [u for u, n in collections.Counter(uids).items() if n > 1]
    if dup:
        raise XmlError('duplicate UId %s' % ', '.join(dup))
    known = {x.get('UId') for x in f.find('Parts')}
    for w in f.find('Wires'):
        if len(w) < 2:
            raise XmlError('wire %s connects nothing' % w.get('UId'))
        for c in w:
            if c.tag in ('NameCon', 'IdentCon') and c.get('UId') not in known:
                raise XmlError('wire %s refers to a missing part %s' % (w.get('UId'), c.get('UId')))


# ---------- the block document ----------

CU = re.compile(r'([ \t]*)<SW\.Blocks\.CompileUnit ID="[0-9A-Fa-f]+" CompositionName="CompileUnits">.*?</SW\.Blocks\.CompileUnit>',
                re.S)
FLG = re.compile(r'<FlgNet xmlns="[^"]*">.*?</FlgNet>', re.S)


def _doc(text):
    return ET.fromstring(text.lstrip('﻿').encode('utf-8'))


def block_attributes(text):
    """-> (block element tag, {attribute: text}) of the block's AttributeList (Interface left out)."""
    d = _doc(text)
    b = next((x for x in d if x.tag.startswith('SW.Blocks.')), None)
    if b is None:
        raise XmlError('no block in the XML export')
    al = b.find('AttributeList')
    return b.tag, {x.tag: (x.text or '').strip() for x in (al if al is not None else []) if x.tag != 'Interface'}


def f_block(text):
    """An F-block (F_LAD / F_FBD language of the block or a network, or a safety attribute) -> reason, else None."""
    tag, a = block_attributes(text)
    if a.get('ProgrammingLanguage', '').upper().startswith('F_') or \
            re.search(r'<ProgrammingLanguage>F_', text) or \
            any(re.search(r'(?i)safety|failsafe', k) and v.lower() == 'true' for k, v in a.items()):
        return 'the block is an F-block — the editor never writes safety blocks, change it by hand in TIA'
    return None


OB_KEYS = ('Number', 'SecondaryType', 'EventClass', 'CyclicTime', 'PhaseOffset')


def ob_attributes(text):
    """OB -> {Number, SecondaryType, CyclicTime, …} as exported; other blocks -> None."""
    tag, a = block_attributes(text)
    return {k: a[k] for k in OB_KEYS if k in a} if tag == 'SW.Blocks.OB' else None


def _info_attrs(info):
    """GetBlockInfo answer (JSON text or dict) -> {attribute name: value text}; None when it is not JSON."""
    import json
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except ValueError:
            return None
    items = next((v for k, v in (info or {}).items() if k.lower() == 'attributes'), None) or []
    got = {}
    for x in items:
        x = {k.lower(): v for k, v in x.items()} if isinstance(x, dict) else {}
        if x.get('name'):
            got[x['name']] = '' if x.get('value') is None else str(x['value'])
    return got


def ob_mismatch(expected, info):
    """Exported OB attributes vs the attributes of GetBlockInfo (JSON text or dict) -> reason, else None. Number and event
    class must be reported; a cyclic interrupt's cycle too (only when the export carries it)."""
    got = _info_attrs(info)
    if got is None:
        return 'GetBlockInfo did not answer JSON — the OB number and event class cannot be checked'
    bad = []
    for k, v in expected.items():
        must = k in ('Number', 'SecondaryType') or (k == 'CyclicTime' and expected.get('SecondaryType') == 'CyclicInterrupt')
        if k not in got:
            if must:
                bad.append('%s not reported' % k)
            continue
        if got[k].strip().lower() != v.strip().lower():
            bad.append('%s %s, before the write %s' % (k, got[k], v))
    return '; '.join(bad) or None


OB_TIMING = re.compile(r'(?i)cycl|phase|event|prior|^number$|secondary')


def info_changed(before, after):
    """GetBlockInfo before vs after the import -> the OB attributes (number, event class, cycle time, phase, priority) that
    differ, else None. The SimaticML export of an OB does not carry its cycle time, so only this comparison sees it."""
    b, a = _info_attrs(before), _info_attrs(after)
    if b is None or a is None:
        return None
    bad = ['%s %s, before the write %s' % (k, a.get(k, '(missing)'), v) for k, v in b.items()
           if OB_TIMING.search(k) and a.get(k, '').strip().lower() != v.strip().lower()]
    return '; '.join(bad) or None


def culture(text):
    m = re.search(r'<Culture>([^<]+)</Culture>', text)
    return m.group(1) if m else 'en-US'


def _texts(cu):
    """-> {'Title': text, 'Comment': text} of a compile unit (first item)."""
    res = {}
    for comp in ('Title', 'Comment'):
        m = re.search(r'CompositionName="%s">.*?<Text(?: />|>(.*?)</Text>)' % comp, cu, re.S)
        res[comp] = _unesc(m.group(1) or '') if m else ''
    return res


def _unesc(s):
    return s.replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&apos;', "'").replace('&amp;', '&')


def networks(text):
    """Compile units of a block export -> [{nw, title, comment, language, flgnet}]."""
    res = []
    for i, m in enumerate(CU.finditer(text), 1):
        cu = m.group(0)
        f = FLG.search(cu)
        lang = re.search(r'</NetworkSource>\s*<ProgrammingLanguage>([^<]*)<|<NetworkSource />\s*<ProgrammingLanguage>([^<]*)<', cu)
        t = _texts(cu)
        res.append({'nw': i, 'title': t['Title'], 'comment': t['Comment'], 'language': (lang.group(1) or lang.group(2)) if lang else '',
                    'flgnet': f.group(0) if f else ''})
    return res


def _set_text(cu, comp, value, cult):
    """Title / Comment of a compile unit: the item of the culture (else the first one)."""
    m = re.search(r'<MultilingualText ID="[0-9A-Fa-f]+" CompositionName="%s">.*?</MultilingualText>' % comp, cu, re.S)
    if not m:
        raise XmlError('the network has no %s text in the export' % comp.lower())
    block = m.group(0)
    items = list(re.finditer(r'<MultilingualTextItem .*?</MultilingualTextItem>', block, re.S))
    it = next((x for x in items if '<Culture>%s</Culture>' % cult in x.group(0)), items[0] if items else None)
    if it is None:
        raise XmlError('the %s of the network has no text item' % comp.lower())
    new_item = re.sub(r'<Text />|<Text>.*?</Text>', lambda _: '<Text>%s</Text>' % esc(value).replace('&quot;', '"'),
                      it.group(0), count=1, flags=re.S)
    block = block[:it.start()] + new_item + block[it.end():]
    return cu[:m.start()] + block + cu[m.end():]


def _new_cu(ids, ind, nl, language, flg, title, comment, cult):
    def text(comp, v):
        return ('MultilingualText', [('ID', ids()), ('CompositionName', comp)], [
            ('ObjectList', [], [('MultilingualTextItem', [('ID', ids()), ('CompositionName', 'Items')], [
                ('AttributeList', [], [('Culture', [], cult), ('Text', [], v or None)])])])])
    cu_id = ids()
    node = ('SW.Blocks.CompileUnit', [('ID', cu_id), ('CompositionName', 'CompileUnits')], [
        ('AttributeList', [], [('NetworkSource', [], [flg] if flg else None), ('ProgrammingLanguage', [], language)]),
        ('ObjectList', [], [text('Comment', comment), text('Title', title)])])
    return _xml(node, ind, nl).lstrip(' \t')


def _id_counter(text):
    n = [max((int(x, 16) for x in re.findall(r' ID="([0-9A-Fa-f]+)"', text)), default=0)]

    def nxt():
        n[0] += 1
        return '%X' % n[0]
    return nxt


def _member(m, cult, ind, nl):
    t = m['type'].strip()
    attrs = [('Name', m['name']), ('Datatype', t)] + ([('Version', '1.0')] if t.upper() in VERSIONED else [])
    body = []
    if m.get('comment'):
        body.append(('Comment', [], [('MultiLanguageText', [('Lang', cult)], m['comment'])]))
    if m.get('initial'):
        body.append(('StartValue', [], str(m['initial'])))
    return _xml(('Member', attrs, body or None), ind, nl)


def add_members(text, section, members, cult=None):
    """Interface edit: members at the end of the block's top-level `<Section Name=…>` (v5). Names must be new."""
    nl = '\r\n' if '\r\n' in text else '\n'
    cult = cult or culture(text)
    top = re.search(r'<Sections xmlns="http://www\.siemens\.com/automation/Openness/SW/Interface/v\d+">', text)
    if not top:
        raise XmlError('the export has no block interface')
    first = re.compile(r'^([ \t]*)<Section Name=', re.M).search(text, top.end())
    if not first:
        raise XmlError('the block interface has no sections')
    ind = first.group(1)
    end = re.compile(r'^%s</Sections>' % ind[:-2] if len(ind) >= 2 else r'</Sections>', re.M).search(text, top.end())
    region = text[top.end():end.start() if end else len(text)]
    taken = {x.lower() for x in re.findall(r'^%s  <Member Name="([^"]+)"' % ind, region, re.M)}
    dup = [m['name'] for m in members if m['name'].lower() in taken]
    if dup:
        raise XmlError('the interface already has %s' % ', '.join(dup))
    rows = nl.join(_member(m, cult, ind + '  ', nl) for m in members)
    s = re.compile(r'^%s<Section Name="%s"( />|>)' % (ind, re.escape(section)), re.M).search(text, top.end())
    if not s or (end and s.start() > end.start()):
        raise XmlError('the block interface has no section %s' % section)
    if s.group(1) == ' />':
        return text[:s.start()] + '%s<Section Name="%s">%s%s%s%s</Section>' % (ind, section, nl, rows, nl, ind) + text[s.end():]
    close = re.compile(r'^%s</Section>' % ind, re.M).search(text, s.end())
    if not close:
        raise XmlError('section %s without its end' % section)
    return text[:close.start()] + rows + nl + text[close.start():]


def edit_block(text, edits, find_network, call_params=None):
    """Apply the edits of one block (interface -> new networks -> changes, the Upload order) to its XML export -> new text.
    find_network(networks, edit) -> the network or None (by title, else by number, as for documents). Raises XmlError."""
    why = f_block(text)
    if why:
        raise XmlError(why)
    nl = '\r\n' if '\r\n' in text else '\n'
    cult = culture(text)
    notes = []
    for u in edits:
        if u.get('kind') == 'interface':
            text = add_members(text, u['section'], u['members'], cult)
            notes.append('%s: %d member(s) added to %s' % (u['id'], len(u['members']), u['section']))
    found = list(CU.finditer(text))
    cus = [m.group(0)[len(m.group(1)):] for m in found]
    if found:
        ind = found[0].group(1)
        sep = text[found[0].end():found[1].start() + len(found[1].group(1))] if len(found) > 1 else nl + ind
        span = (found[0].start() + len(ind), found[-1].end())
    else:                                                     # a block without networks: before the block title
        t = list(re.finditer(r'^([ \t]*)<MultilingualText ID="[0-9A-Fa-f]+" CompositionName="Title">', text, re.M))
        if not t:
            raise XmlError('no place for a network in the export (block title not found)')
        ind, sep = t[-1].group(1), nl + t[-1].group(1)
        span = (t[-1].start() + len(ind), t[-1].start() + len(ind))
    ids = _id_counter(text)

    def nets():
        return [dict(n, nw=i) for i, n in enumerate(networks(''.join(x + sep for x in cus)), 1)]
    for u in [u for u in edits if u.get('kind') == 'new_network']:
        now = nets()
        if u['after_nw'] > len(now):
            raise XmlError('NW %d is not in the exported block (it has %d networks)' % (u['after_nw'], len(now)))
        if u['after_nw'] and u.get('after_title') and now[u['after_nw'] - 1]['title'] != u['after_title']:
            raise XmlError('NW %d in the project is "%s", the proposal follows "%s" — the networks moved, propose again'
                           % (u['after_nw'], now[u['after_nw'] - 1]['title'], u['after_title']))
        flg = flgnet_node(u['after'], call_params)
        cus.insert(u['after_nw'], _new_cu(ids, ind, nl, u['language'], flg, u['title'], u.get('comment') or '', cult))
        notes.append('%s: new %s network inserted as NW %d' % (u['id'], u['language'], u['after_nw'] + 1))
    inserted = [x['after_nw'] for x in edits if x.get('kind') == 'new_network']
    for u in [u for u in edits if u.get('kind', 'network') == 'network']:
        # D11: u['nw'] counts the export before the insertions; an untitled network is found by its number after them
        n = find_network(nets(), dict(u, nw=u['nw'] + sum(1 for a in inserted if a < u['nw'])))
        if n is None:
            raise XmlError('network %d ("%s") is not in the exported block' % (u['nw'], u.get('title') or ''))
        language = 'FBD' if u['after'].get('language') == 'FBD' else 'LAD'
        if n['language'] != language:
            raise XmlError('NW %d is %s in the project, the proposal is %s' % (n['nw'], n['language'] or '?', language))
        cu = cus[n['nw'] - 1]
        before = flgnet(u['before'], call_params)
        if graph(n['flgnet']) != graph(before):
            only_tia = graph(n['flgnet']) - graph(before)
            only_model = graph(before) - graph(n['flgnet'])
            raise XmlError('NW %d "%s" in the project is not the proposal\'s "before" network (it changed since the proposal, '
                           'or it has what the XML way does not write) — only in TIA: %s; only in the proposal: %s'
                           % (n['nw'], n['title'], _short(only_tia), _short(only_model)))
        after = flgnet_node(u['after'], call_params)
        if graph(_xml(after, '') if after else '') != graph(before):
            fi = FLG.search(cu)
            if fi:
                k = cu.rfind('\n', 0, fi.start()) + 1
                ind_f = cu[k:fi.start()]
                cu = cu[:fi.start()] + (_xml(after, ind_f, nl).lstrip(' \t') if after else '') + cu[fi.end():]
                if not after:
                    cu = re.sub(r'<NetworkSource>\s*</NetworkSource>', '<NetworkSource />', cu, count=1)
            elif after:
                k = re.search(r'([ \t]*)<NetworkSource />', cu)
                if not k:
                    raise XmlError('NW %d has no NetworkSource in the export' % n['nw'])
                ind_n = k.group(1)
                cu = cu[:k.start()] + '%s<NetworkSource>%s%s%s%s</NetworkSource>' % (
                    ind_n, nl, _xml(after, ind_n + '  ', nl), nl, ind_n) + cu[k.end():]
            notes.append('%s: NW %d "%s" replaced' % (u['id'], n['nw'], n['title']))
        if u.get('title_after'):
            cu = _set_text(cu, 'Title', u['title_after'], cult)
        if u.get('comment_after'):
            cu = _set_text(cu, 'Comment', u['comment_after'], cult)
        if u.get('title_after') or u.get('comment_after'):
            notes.append('%s: NW %d texts set' % (u['id'], n['nw']))
        cus[n['nw'] - 1] = cu
    if found:
        text = text[:span[0]] + sep.join(cus) + text[span[1]:]
    elif cus:                                                 # before the block title, which keeps its own line
        text = text[:span[0]] + sep.join(cus) + sep + text[span[0]:]
    for n in networks(text):
        if n['flgnet']:
            check_flgnet(n['flgnet'])
    _doc(text)                                                # well-formed
    return text, notes


def _short(c):
    return ', '.join('%s → %s' % k if k[0] != 'part' else k[1] for k in list(c)[:6]) or '—'


def call_info(text):
    """{(block name, FB|FC): [(parameter, section, type)]} of the calls in an export (TIA's own CallInfo)."""
    res = {}
    for m in re.finditer(r'<CallInfo Name="([^"]+)" BlockType="(\w+)">(.*?)</CallInfo>', text, re.S):
        res.setdefault((_unesc(m.group(1)), m.group(2)), [
            (_unesc(p.group(1)), p.group(2), _unesc(p.group(3)))
            for p in re.finditer(r'<Parameter Name="([^"]+)" Section="(\w+)" Type="([^"]*)"', m.group(3))])
    return res
