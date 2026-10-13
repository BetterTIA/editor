"""FBD network (`S7_Language := "FBD"` in .s7dcl) -> own model of boxes and signals, TIA-like layout, operations.

Grammar (V21 exports): `RUNG [<operand>|wire#wN|true]` = chain input; elements follow one another and the output of
the previous one is the 1st input of the next: `A( in2 := …, in3 := … )` (&), `O(…)` (>=1), `X(…)` (x), `Coil(x)` (=),
`S_Coil` / `R_Coil`, `S_SR` / `S_RS`, `P_Trig(mem)` / `N_Trig`, instruction `Move(…)` (EN), calls `#inst.TYPE(…)`,
`"Inst"(…)`, `#inst(…)` with attributes `{ f_user_card := "1"; … }`. `NOT` before an element = negated 1st input,
`pin := NOT x` = negated input. `wire#wN` inside a rung = tap after a box, `END_RUNG wire#wN` = rung output,
`in2 := wire#wN` / `RUNG wire#wN` = input fed by a tap / by the output of another rung.

Model: {language: 'FBD', elements: [box], signals: [{from, to, input}]} ; box = {id (b1…, new n1…), type, inputs, outputs, …}
  type: and | or | xor | assign | s | r | sr | rs | ptrig | ntrig | call | instruction
  inputs: [{n, pin, operand, signal, neg}] — signal = id of the box whose main output feeds the input (operand is then '')
  outputs: [{pin, operand, neg}] (pins of calls / instructions)
  operand: assignment / S / R / SR / RS / edge memory bit ; call: call fb|fc|instruction, name, instance, attributes
The first input is the chain input (in1, EN, CLK, S …); a call without power (F-block) has no EN.
`signals` are derived from the inputs (the server computes them) — the client sends only `elements`.
"""
import copy
import json
import re

import s7dcl

LOGIC = {'A': 'and', 'O': 'or', 'X': 'xor'}
ASSIGNMENT = {'Coil': 'assign', 'S_Coil': 's', 'R_Coil': 'r'}
EDGES = {'P_Trig': 'ptrig', 'N_Trig': 'ntrig'}
TYPES = {'and', 'or', 'xor', 'assign', 's', 'r', 'sr', 'rs', 'ptrig', 'ntrig', 'call', 'instruction'}
SYMBOL = {'and': '&', 'or': '>=1', 'xor': 'x', 'assign': '=', 's': 'S', 'r': 'R', 'sr': 'SR', 'rs': 'RS',
         'ptrig': 'P_TRIG', 'ntrig': 'N_TRIG'}
WITH_OPERAND = {'assign', 's', 'r', 'sr', 'rs', 'ptrig', 'ntrig'}      # operand above the box
PINS_VISIBLE = {'sr', 'rs', 'ptrig', 'ntrig', 'call', 'instruction'}   # TIA does not show pin names on & / >=1 / =


class LayoutError(Exception):
    pass


# ---------- parser ----------

def _attrs(t):
    return {m.group(1): m.group(2) for m in re.finditer(r'(\w+)\s*:=\s*"([^"]*)"', t)}


def _value(s):
    """'NOT "x"' -> ('x', neg); 'wire#w1' stays in the operand as a marker."""
    s = (s or '').strip()
    neg = False
    if s.startswith('NOT '):
        neg, s = True, s[4:].strip()
    return s, neg


def _rungs(content):
    """-> [{'from': text|None, 'items': [('box', target, args, attrs) | ('wire', 'wN')], 'to': 'wN'|None}]"""
    rungs, active, attrs, i, t = [], None, {}, 0, content
    while i < len(t):
        if t[i].isspace():
            i += 1
            continue
        row_k = t.find('\n', i)
        row_k = len(t) if row_k < 0 else row_k
        line = t[i:row_k].strip()
        if re.match(r'RUNG\b', line):
            if active is not None:
                raise s7dcl.SourceParseError('RUNG inside RUNG')
            active = {'from': line[4:].strip() or None, 'items': [], 'to': None}
            i = row_k
        elif re.match(r'END_RUNG\b', line):
            if active is None:
                raise s7dcl.SourceParseError('END_RUNG without RUNG')
            d = line[8:].strip()
            if d and not d.startswith('wire#'):
                raise s7dcl.SourceParseError('END_RUNG with an operand')
            active['to'] = d[5:] if d else None
            rungs.append(active)
            active = None
            i = row_k
        elif t[i] == '{':
            k = s7dcl._paired(t, i, '{', '}')
            attrs = _attrs(t[i:k])
            i = k
        elif re.match(r'wire#\w+$', line):
            if active is None:
                raise s7dcl.SourceParseError('wire outside RUNG')
            active['items'].append(('wire', line[5:]))
            i = row_k
        else:
            if active is None:
                raise s7dcl.SourceParseError('element outside RUNG: ' + line[:40])
            j = i
            while j < len(t) and t[j] not in '(\n':
                j = s7dcl._string_end(t, j) if t[j] in '"\'' else j + 1
            if j >= len(t) or t[j] != '(':
                raise s7dcl.SourceParseError('FBD element without parentheses: ' + line[:40])
            k = s7dcl._paired(t, j, '(', ')')
            active['items'].append(('box', t[i:j].strip(), t[j + 1:k - 1], attrs))
            attrs = {}
            i = k
    if active is not None:
        raise s7dcl.SourceParseError('RUNG without END_RUNG')
    return rungs


def _input(pin, value):
    op, neg = _value(value)
    return {'n': 0, 'pin': pin, 'operand': op, 'signal': None, 'neg': neg}


def _box(target, args, attrs):
    """-> (box without id and without chain input, chain input pin | None, chain input negation)"""
    neg = target.startswith('NOT ')
    if neg:
        target = target[4:].strip()
    pos, pins = s7dcl._arguments(args)
    tmpl = s7dcl._template({'S7_Templates': attrs['S7_Templates']}) if 'S7_Templates' in attrs else None
    if target in LOGIC:
        b = {'type': LOGIC[target], 'inputs': [_input(p['pin'].lower(), p['operand']) for p in pins], 'outputs': []}
        return b, 'in1', neg
    if target in ASSIGNMENT or target in EDGES:
        b = {'type': ASSIGNMENT.get(target) or EDGES[target], 'operand': (pos or [''])[0], 'inputs': [], 'outputs': []}
        return b, 'CLK' if target in EDGES else 'in1', neg
    if target in ('S_SR', 'S_RS'):
        op = next((p['operand'] for p in pins if p['pin'] == 'operand'), '')
        b = {'type': 'sr' if target == 'S_SR' else 'rs', 'operand': op, 'outputs': [],
             'inputs': [_input(p['pin'].upper(), p['operand']) for p in pins if p['pin'] != 'operand' and p['direction'] == 'in']}
        return b, 'S' if target == 'S_SR' else 'R', neg
    # instruction / block call: shape from the LAD parser (name, instance, pins), only the type differs
    p = s7dcl.element(target, args, attrs)
    b = {'type': 'instruction' if p['call'] == 'instruction' and not p.get('instance') else 'call',
         'call': p['call'], 'name': p['name'], 'instance': p.get('instance'), 'template': tmpl,
         'inputs': [_input(x['pin'], x['operand']) for x in p['inputs']],
         'outputs': [dict(zip(('operand', 'neg'), _value(x['operand'])), pin=x['pin']) for x in p['outputs']]}
    a = {k: v for k, v in attrs.items() if not k.startswith('S7_')}
    if a:
        b['attributes'] = a
    return b, 'EN', neg


def parse(content):
    """FBD network -> elements (signals in the inputs). Raises SourceParseError."""
    elements, wire, waiting = [], {}, []     # wire: wN -> ('box', id) | ('op', operand, neg) | ('wire', wM)

    def source(z):
        if z is None:
            return None
        if z.startswith('wire#'):
            return ('wire', z[5:])
        op, neg = _value(z)
        return ('op', op, neg)

    for r in _rungs(content):
        before = source(r['from'])
        for item in r['items']:
            if item[0] == 'wire':
                if before is None:
                    raise s7dcl.SourceParseError('tap wire#%s without a source' % item[1])
                wire[item[1]] = before
                continue
            b, pin, neg = _box(*item[1:])
            b['id'] = 'b%d' % (len(elements) + 1)
            for v in b['inputs']:
                if v['operand'].startswith('wire#'):
                    waiting.append((v, ('wire', v['operand'][5:])))
                    v['operand'] = ''
            if before is not None or b['type'] not in ('call', 'instruction'):   # chain input (an F-block without EN has none)
                v = {'n': 0, 'pin': pin, 'operand': '', 'signal': None, 'neg': neg}
                b['inputs'].insert(0, v)
                if before is not None:
                    waiting.append((v, before))
            for k, v in enumerate(b['inputs']):
                v['n'] = k + 1
            elements.append(b)
            before = ('box', b['id'])
        if r['to']:
            if before is None:
                raise s7dcl.SourceParseError('END_RUNG wire#%s without a source' % r['to'])
            wire[r['to']] = before
    for v, z in waiting:
        seen = set()
        while z[0] == 'wire':
            if z[1] in seen or z[1] not in wire:
                raise s7dcl.SourceParseError('unconnected wire wire#%s' % z[1])
            seen.add(z[1])
            z = wire[z[1]]
        if z[0] == 'box':
            v['signal'] = z[1]
        else:
            v['operand'], v['neg'] = z[1], v['neg'] != z[2]
    return elements


def signals(elements):
    """Edges box output -> box input: [{from, to, input}]"""
    return [{'from': v['signal'], 'to': p['id'], 'input': v['n']} for p in elements for v in p.get('inputs', [])
            if v.get('signal')]


def model(elements):
    return {'language': 'FBD', 'elements': elements, 'signals': signals(elements)}


def is_fbd(m):
    return isinstance(m, dict) and m.get('language') == 'FBD'


# ---------- checking the model from the API (trust boundary) ----------

def verify_model(after):
    """Raises ValueError when the model is not FBD-shaped; renumbers the input numbers `n`."""
    if not isinstance(after, dict) or not isinstance(after.get('elements'), list):
        raise ValueError('FBD after must be {language: "FBD", elements: [{id, type, inputs, outputs, …}]}')
    ids = set()
    for p in after['elements']:
        if not isinstance(p, dict) or not isinstance(p.get('id'), str) or p['id'] in ids or p['id'].startswith('x:'):
            raise ValueError('FBD element without a unique id: %r' % (p if not isinstance(p, dict) else p.get('id'),))
        if p.get('type') not in TYPES:
            raise ValueError('FBD element %s: type must be one of %s' % (p['id'], sorted(TYPES)))
        for k, field in (('inputs', ('pin', 'operand')), ('outputs', ('pin', 'operand'))):
            if not isinstance(p.get(k), list) or not all(isinstance(x, dict) and all(isinstance(x.get(f), str) for f in field)
                                                         for x in p[k]):
                raise ValueError('FBD element %s: %s must be [{pin, operand, …}]' % (p['id'], k))
        for f in ('operand', 'name', 'instance', 'template'):
            if p.get(f) is not None and not isinstance(p[f], str):
                raise ValueError('FBD element %s: %s must be text' % (p['id'], f))
        ids.add(p['id'])
    for p in after['elements']:
        for i, v in enumerate(p['inputs']):
            v['n'] = i + 1
            v['neg'] = bool(v.get('neg'))
            if v.get('signal') is not None and v['signal'] not in ids:
                raise ValueError('FBD element %s: input %d is fed by non-existent box %r' % (p['id'], i + 1, v['signal']))
            v.setdefault('signal', None)
        for v in p['outputs']:
            v['neg'] = bool(v.get('neg'))
    if _cycle(after['elements']):
        raise ValueError('FBD: signals form a cycle')


def _cycle(elements):
    P = {p['id']: p for p in elements}
    state = {}

    def dfs(i):
        state[i] = 1
        for v in P[i]['inputs']:
            s = v.get('signal')
            if s in P and (state.get(s) == 1 or (s not in state and dfs(s))):
                return True
        state[i] = 2
        return False
    return any(i not in state and dfs(i) for i in P)


# ---------- operations (API and editor) ----------

class OperationError(ValueError):
    pass


def _find(m, pid):
    for p in m['elements']:
        if p['id'] == pid:
            return p
    raise OperationError('element %s does not exist' % pid)


def _pin(p, op):
    """op.input = n (1…) / op.output = pin name -> pin dict, or None (the box itself)."""
    if op.get('input') is not None:
        try:
            return p['inputs'][int(op['input']) - 1]
        except (ValueError, TypeError, IndexError):
            raise OperationError('box %s has no input %r' % (p['id'], op['input']))
    if op.get('output') is not None:
        for v in p['outputs']:
            if v['pin'] == op['output']:
                return v
        raise OperationError('box %s has no output %r' % (p['id'], op['output']))
    return None


def _inputs(*pins):
    return [{'n': i + 1, 'pin': p, 'operand': '', 'signal': None, 'neg': False} for i, p in enumerate(pins)]


def _palette():
    P = []

    def add(key, name, group, element, description=''):
        P.append({'key': key, 'name': name, 'group': group, 'description': description,
                  'element': dict(element, outputs=[])})
    add('AND', '&', 'Bit logic', {'type': 'and', 'inputs': _inputs('in1', 'in2')}, 'AND (F9)')
    add('OR', '>=1', 'Bit logic', {'type': 'or', 'inputs': _inputs('in1', 'in2')}, 'OR (F10)')
    add('XOR', 'x', 'Bit logic', {'type': 'xor', 'inputs': _inputs('in1', 'in2')}, 'XOR')
    add('ASSIGN', '=', 'Assignment', {'type': 'assign', 'operand': '', 'inputs': _inputs('in1')}, 'assignment (Shift+F7)')
    add('S', 'S', 'Assignment', {'type': 's', 'operand': '', 'inputs': _inputs('in1')}, 'set')
    add('R', 'R', 'Assignment', {'type': 'r', 'operand': '', 'inputs': _inputs('in1')}, 'reset')
    add('SR', 'SR', 'Memory', {'type': 'sr', 'operand': '', 'inputs': _inputs('S', 'R1')}, 'reset priority')
    add('RS', 'RS', 'Memory', {'type': 'rs', 'operand': '', 'inputs': _inputs('R', 'S1')}, 'set priority')
    add('P_TRIG', 'P_TRIG', 'Edges', {'type': 'ptrig', 'operand': '', 'inputs': _inputs('CLK')}, 'rising edge, memory bit above the box')
    add('N_TRIG', 'N_TRIG', 'Edges', {'type': 'ntrig', 'operand': '', 'inputs': _inputs('CLK')}, 'falling edge')
    return P


PALETTE = _palette()
LAD_INSTRUCTIONS = {'MOVE', 'SWAP', 'CONV', 'ADD', 'SUB', 'MUL', 'DIV', 'TON', 'TOF', 'TP', 'CTU', 'CTD'}  # from the LAD catalog


def from_lad(box, f=False):
    """Box template from the LAD catalog (instruction / FB / FC) -> FBD box; in an F-block without EN (not connectable).
    shortcut: timers and counters get EN/ENO instead of IN/Q (TIA FBD draws them with IN/Q) — fix once they are needed in F-blocks"""
    call = box.get('call') in ('fb', 'fc') or bool(box.get('instance'))
    res = {'type': 'call' if call else 'instruction', 'call': box.get('call') or 'instruction', 'name': box.get('name'),
           'instance': box.get('instance'), 'template': box.get('template'),
           'inputs': ([] if f else _inputs('EN')) + [{'pin': x['pin'], 'operand': x.get('operand', ''), 'signal': None,
                                                      'neg': False} for x in box.get('inputs', [])],
           'outputs': [{'pin': x['pin'], 'operand': x.get('operand', ''), 'neg': False} for x in box.get('outputs', [])]}
    for i, v in enumerate(res['inputs']):
        v['n'] = i + 1
    return res


def template(key, catalog_element=None, f=False):
    """FBD palette key ('AND', 'P_TRIG') / catalog key ('MOVE', 'fb:FB_Belt') -> FBD box without id."""
    for x in PALETTE:
        if x['key'] == key:
            return copy.deepcopy(x['element'])
    if catalog_element is None:
        raise OperationError('palette element %s does not exist' % key)
    return from_lad(catalog_element(key), f)


def _new_id(m):
    n = [int(x['id'][1:]) for x in m['elements'] if re.fullmatch(r'n\d+', x['id'])]
    return 'n%d' % (max(n, default=0) + 1)


def _take_over(target, source, neg=False):
    """Input `target` takes the source of input `source` (signal or operand; negations combine)."""
    target.update(operand=source['operand'], signal=source.get('signal'), neg=bool(source.get('neg')) != neg)


def _renumber(p):
    for i, v in enumerate(p['inputs']):
        v['n'] = i + 1
        if p['type'] in ('and', 'or', 'xor'):
            v['pin'] = 'in%d' % (i + 1)


def insert_box(m, element, dst=None):
    """New box (id n…). dst = {id, input: n}: the box is inserted into that input (takes over its signal / operand on
    its 1st input and feeds it with its output); input len+1 on &/>=1/x = new input. Without `dst` = new logic path."""
    if not isinstance(element, dict) or element.get('type') not in TYPES:
        raise OperationError('element must be an FBD box {type: %s, …}' % '|'.join(sorted(TYPES)))
    p = copy.deepcopy(element)
    p['id'] = _new_id(m)
    p.setdefault('outputs', [])
    p['inputs'] = [dict({'signal': None, 'neg': False, 'operand': ''}, **v) for v in p.get('inputs', [])]
    _renumber(p)
    if dst:
        c = _find(m, dst.get('id'))
        try:
            n = int(dst.get('input'))
        except (TypeError, ValueError):
            raise OperationError('insert: to = {id, input: n}')
        if n == len(c['inputs']) + 1 and c['type'] in ('and', 'or', 'xor'):
            c['inputs'].append({'n': n, 'pin': 'in%d' % n, 'operand': '', 'signal': None, 'neg': False})
        if not 1 <= n <= len(c['inputs']):
            raise OperationError('box %s has no input %r' % (c['id'], n))
        v = c['inputs'][n - 1]
        if p['inputs']:
            _take_over(p['inputs'][0], v)
        v.update(operand='', signal=p['id'], neg=False)
    m['elements'].append(p)
    return p['id']


def delete(m, pid):
    """Deletes a box; inputs it fed take over its 1st input (chain); a call / instruction without EN leaves them open."""
    p = _find(m, pid)
    m['elements'].remove(p)
    first = p['inputs'][0] if p['inputs'] and (p['type'] not in ('call', 'instruction') or p['inputs'][0]['pin'] == 'EN') \
        else None
    for q in m['elements']:
        for v in q['inputs']:
            if v.get('signal') == pid:
                if first is not None:
                    _take_over(v, first, bool(v.get('neg')))
                else:
                    v.update(operand='', signal=None)


def apply(m, op, catalog_element=None, f=False):
    """FBD operation from the API -> (model, id of the affected / new box | None):
    add_input {id, operand?, neg?} · remove_input {id, input} · negate {id, input | output: pin | "main"} ·
    change_operand {id, input? | output?, operand} · change_pin {id, pin, operand?, neg?} · delete {id} ·
    insert_box {element: key | box, to?: {id, input}} · insert_call {element: 'fb:X' | 'MOVE' …, instance?, to?}"""
    if not isinstance(op, dict):
        raise OperationError('operation must be an object')
    m = copy.deepcopy(m)
    t = op.get('type')
    if t in ('insert_box', 'insert_call'):
        element = op.get('element')
        if isinstance(element, str):
            try:
                element = template(element, catalog_element, f)
            except KeyError as e:
                raise OperationError(str(e).strip("'"))
        if t == 'insert_call':
            if not isinstance(element, dict) or element.get('type') not in ('call', 'instruction'):
                raise OperationError('insert_call: element must be a project block (fb:X / fc:X) or an instruction')
            if op.get('instance') is not None:
                element['instance'] = str(op['instance']).strip()
        pid = insert_box(m, element, op.get('to'))
        m['signals'] = signals(m['elements'])
        return m, pid
    p = _find(m, op.get('id'))
    if t == 'add_input':
        if p['type'] not in ('and', 'or', 'xor'):
            raise OperationError('an input can only be added to a &, >=1 or x box')
        n = len(p['inputs']) + 1
        p['inputs'].append({'n': n, 'pin': 'in%d' % n, 'operand': str(op.get('operand') or '').strip(),
                            'signal': None, 'neg': bool(op.get('neg'))})
    elif t == 'remove_input':
        if p['type'] not in ('and', 'or', 'xor') or len(p['inputs']) <= 2:
            raise OperationError('only an input of a &, >=1 or x box with more than 2 inputs can be removed')
        p['inputs'].remove(_pin(p, {'input': op.get('input')}))
        _renumber(p)
    elif t == 'change_operand':
        if not isinstance(op.get('operand'), str):
            raise OperationError('operand must be text')
        pin = _pin(p, op)
        if pin is None:
            if p['type'] in WITH_OPERAND:
                p['operand'] = op['operand'].strip()
            elif p['type'] == 'call':
                p['instance'] = op['operand'].strip()
            else:
                raise OperationError('box %s has no operand — specify an input' % p['id'])
        else:
            pin['operand'] = op['operand'].strip()
            if 'signal' in pin:
                pin['signal'] = None
    elif t == 'change_pin':
        pin = next((v for v in p['inputs'] + p['outputs'] if v['pin'] == op.get('pin')), None)
        if pin is None:
            raise OperationError('box %s has no pin %r' % (p['id'], op.get('pin')))
        if op.get('operand') is not None:
            if not isinstance(op['operand'], str):
                raise OperationError('operand must be text')
            pin['operand'] = op['operand'].strip()
            if 'signal' in pin:
                pin['signal'] = None
        if op.get('neg') is not None:
            pin['neg'] = bool(op['neg'])
    elif t == 'negate':
        if op.get('output') == 'main':
            p['neg_output'] = not p.get('neg_output')
        else:
            pin = _pin(p, op)
            if pin is None:
                raise OperationError('negate: specify input (n) or output (pin | "main")')
            pin['neg'] = not pin.get('neg')
    elif t == 'delete':
        delete(m, p['id'])
        m['signals'] = signals(m['elements'])
        return m, None
    else:
        raise OperationError('unknown FBD operation %r (add_input, remove_input, negate, change_operand, change_pin, delete, '
                           'insert_box, insert_call)' % t)
    m['signals'] = signals(m['elements'])
    return m, p['id']


# ---------- rules ----------

def display_name(p):
    if p['type'] in ('call', 'instruction'):
        return '%s%s' % (p.get('name') or 'box', ' ' + p['instance'] if p.get('instance') else '')
    return ('%s %s' % (SYMBOL[p['type']], p.get('operand') or '')).strip()


def validate(m, f_block=False, non_f=()):
    """-> [{id, error, warnings?}]. f_block: the edited block is an F-block; non_f: names of project blocks that are not F."""
    ch = []
    consumers = {v['signal'] for p in m['elements'] for v in p['inputs'] if v.get('signal')}
    for p in m['elements']:
        if p['type'] in WITH_OPERAND and not (p.get('operand') or '').strip():
            ch.append({'id': p['id'], 'error': 'Missing operand: %s' % display_name(p)})
        if p['type'] == 'call' and p.get('instance') is not None and not p['instance'].strip():
            ch.append({'id': p['id'], 'error': 'Missing instance: %s' % display_name(p)})
        if p['type'] not in ('call', 'instruction'):
            for v in p['inputs']:
                # shortcut: V21 export has `RUNG` + `Coil(x)` (= without input, probably "always 1") — for boxes from the
                # export the 1st input of =/S/R is not reported; refine once it is verified in TIA how the editor draws it
                if p['type'] in ('assign', 's', 'r') and v['n'] == 1 and not p['id'].startswith('n'):
                    continue
                if not v.get('signal') and not v['operand'].strip():
                    ch.append({'id': p['id'], 'error': 'Unconnected input %d of box %s — add an operand or a signal'
                                                       % (v['n'], display_name(p))})
        if p['type'] in ('and', 'or', 'xor', 'ptrig', 'ntrig') and p['id'] not in consumers:
            ch.append({'id': p['id'], 'warning': True,
                       'error': 'Box %s leads nowhere — output without assignment (warning)' % display_name(p)})
        if f_block and p['type'] == 'call' and p.get('name') in set(non_f):
            ch.append({'id': p['id'], 'error': 'An F-block may contain only F-instructions and F-blocks — %s is not an F-block' % display_name(p)})
        if f_block and p['type'] in ('call', 'instruction') and p['inputs'] and p['inputs'][0]['pin'] == 'EN' \
                and (p['inputs'][0]['operand'] or p['inputs'][0].get('signal')):
            ch.append({'id': p['id'], 'error': 'EN cannot be connected in an F-block: %s' % display_name(p)})
    return ch


# ---------- insertion points (editor) ----------

def places(m, drawing):
    """On every box input (to = {id, input}), below the last input of &/>=1/x (new input), and a new logic path."""
    if not drawing:
        return []
    P = {p['id']: p for p in m['elements']}
    res = []
    for d in drawing['boxes']:
        if d['id'] not in P:
            continue
        for v in d['inputs']:
            res.append({'x': round(d['x'] - (17 if v['neg'] else 9), 1), 'y': v['y'], 'fbd': True,
                        'to': {'id': d['id'], 'input': v['n']}})
        if P[d['id']]['type'] in ('and', 'or', 'xor') and d['inputs']:
            res.append({'x': round(d['x'] - 9, 1), 'y': d['inputs'][-1]['y'] + L, 'fbd': True, 'new_input': True,
                        'to': {'id': d['id'], 'input': len(d['inputs']) + 1}})
    res.append({'x': 30, 'y': round(drawing['height'] - L / 2, 1), 'fbd': True, 'to': None})
    return res


# ---------- export check (match by content) ----------

COMMUTATIVE = {'and', 'or', 'xor'}


def _j(x):
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


def _content(p, source):
    """Box content without id; input = [pin, neg, operand | source(signal)]; for &/>=1/x without pin and sorted;
    empty call pins are ignored."""
    d = {k: p[k].strip() for k in ('operand', 'name', 'instance', 'template') if isinstance(p.get(k), str) and p[k].strip()}
    d['type'] = p['type']
    if p.get('neg_output'):
        d['neg_output'] = True
    inp = []
    for v in p['inputs']:
        x = source(v['signal']) if v.get('signal') else v['operand'].strip()
        if p['type'] in ('call', 'instruction') and x == '':
            continue
        inp.append([bool(v.get('neg')), x] if p['type'] in COMMUTATIVE else [v['pin'], bool(v.get('neg')), x])
    d['inputs'] = sorted(inp, key=_j) if p['type'] in COMMUTATIVE else inp
    d['outputs'] = [[v['pin'], bool(v.get('neg')), v['operand'].strip()] for v in p.get('outputs', []) if v['operand'].strip()]
    return d


def canon(elements):
    """Wiring as a sorted list of trees from the outputs (boxes without a consumer); a shared source is repeated."""
    P = {p['id']: p for p in elements}
    consumers = {v['signal'] for p in elements for v in p['inputs'] if v.get('signal')}

    def k(i):
        return _content(P[i], k) if i in P else '?'
    return sorted((_j(k(p['id'])) for p in elements if p['id'] not in consumers))


def description(p):
    inp = ', '.join(('NOT ' if v.get('neg') else '') + ('(signal)' if v.get('signal') else v['operand'] or '…')
                    for v in p['inputs'] if not (p['type'] in ('call', 'instruction') and not v['operand'] and not v.get('signal')))
    return '%s (%s)' % (display_name(p), inp)


def content_match(proposal, export):
    """-> (match, [readable differences]) — boxes by content (for &, >=1, x regardless of input order), then wiring."""
    def multiset(elements):
        d = {}
        for p in elements:
            d.setdefault(_j(_content(p, lambda s: 'signal')), []).append(p)
        return d
    a, b = multiset(proposal['elements']), multiset(export['elements'])
    missing = [p for k, v in a.items() for p in v[len(b.get(k, [])):]]
    extra = [p for k, v in b.items() for p in v[len(a.get(k, [])):]]
    res = []
    for p in list(missing):
        q = next((x for x in extra if x['type'] == p['type'] and x.get('name') == p.get('name')), None)
        if q is not None:
            missing.remove(p)
            extra.remove(q)
            res.append('different box %s: proposal %s, export %s' % (SYMBOL.get(p['type']) or p.get('name'), description(p), description(q)))
    res += ['missing in export: %s' % description(p) for p in missing]
    res += ['extra in export: %s' % description(p) for p in extra]
    if not res and canon(proposal['elements']) != canon(export['elements']):
        res.append('boxes match, but signal wiring differs')
    return not res, res


# ---------- "show as LAD" (read-only) ----------

def as_lad(elements):
    """Network of only &, >=1, negations and = -> (rungs, elements) of a LAD model; otherwise None. Each assignment = own
    rung from the power rail (shared parts repeat), & = series, >=1 = parallel branches, negation = NC contact / NOT."""
    if not elements or any(p['type'] not in ('and', 'or', 'assign') or p.get('neg_output') for p in elements):
        return None
    P = {p['id']: p for p in elements}
    lad, rungs, w = [], [], [0]

    def new_value(p):
        p['id'] = 'p%d' % (len(lad) + 1)
        lad.append(p)
        return p['id']

    def wire():
        w[0] += 1
        return 'w%d' % w[0]

    def expr(v):
        e = ('box', v['signal']) if v.get('signal') else ('op', v['operand'])
        return ('not', e) if v.get('neg') else e

    def unwrap(e):
        while e[0] == 'box':
            p = P[e[1]]
            if p['type'] == 'assign':
                e = expr(p['inputs'][0]) if p['inputs'] else ('op', '')
            else:
                return ('S' if p['type'] == 'and' else 'P', [expr(v) for v in p['inputs']])
        return e

    def series(e, fpath, node):
        """Appends an expression to the rung path; node = named node at the current position (or None) -> node after it."""
        e = unwrap(e)
        if e[0] == 'op':
            fpath.append(new_value({'type': 'contact', 'operand': e[1]}))
            return None
        if e[0] == 'not':
            x = unwrap(e[1])
            if x[0] == 'op':
                fpath.append(new_value({'type': 'contact_nc', 'operand': x[1]}))
            else:
                series(x, fpath, node)
                fpath.append(new_value({'type': 'not'}))
            return None
        if e[0] == 'S':
            for x in e[1]:
                node = series(x, fpath, node)
            return node
        if node is None:
            node = wire()
            fpath.append('wire#' + node)
        end = wire()
        series(e[1][0], fpath, node)
        for x in e[1][1:]:
            r = {'from': node, 'path': [], 'to': end}
            rungs.append(r)
            series(x, r['path'], node)
        fpath.append('wire#' + end)
        return end

    for p in elements:
        if p['type'] == 'assign':
            r = {'from': 'powerrail', 'path': [], 'to': None}
            rungs.append(r)
            series(expr(p['inputs'][0]) if p['inputs'] else ('op', ''), r['path'], 'powerrail')
            r['path'].append(new_value({'type': 'coil', 'operand': p.get('operand') or ''}))
    return (rungs, lad) if lad else None


# ---------- diff: deleted boxes as ghosts ----------

def merge(before, after, deleted):
    """Model after + deleted boxes (id 'x:<id>') as separate trees; signals from deleted boxes renamed,
    from vanished sources -> empty operand."""
    elements = copy.deepcopy(after['elements'])
    live = {p['id'] for p in elements}
    sm = set(deleted)
    for p in before['elements']:
        if p['id'] not in sm:
            continue
        g = copy.deepcopy(p)
        g['id'] = 'x:' + p['id']
        for v in g['inputs']:
            s = v.get('signal')
            if s in sm:
                v['signal'] = 'x:' + s
            elif s and s not in live:
                v['signal'] = None
        elements.append(g)
    return elements


def pin_changes(before, after, changed):
    """For changed boxes: which inputs (n), outputs (pin) and whether operand / instance -> {id: {inputs, outputs, above}}"""
    P = {p['id']: p for p in before['elements']}
    res = {}
    for q in after['elements']:
        p = P.get(q['id'])
        if q['id'] not in changed or p is None:
            continue
        pv = {v['n']: (v['operand'], v.get('signal'), bool(v.get('neg'))) for v in p['inputs']}
        pw = {v['pin']: (v['operand'], bool(v.get('neg'))) for v in p['outputs']}
        res[q['id']] = {
            'inputs': [v['n'] for v in q['inputs'] if pv.get(v['n']) != (v['operand'], v.get('signal'), bool(v.get('neg')))],
            'outputs': [v['pin'] for v in q['outputs'] if pw.get(v['pin']) != (v['operand'], bool(v.get('neg')))],
            'above': p.get('operand') != q.get('operand') or p.get('instance') != q.get('instance')}
    return res


# ---------- layout ----------

L = 18
CHAR_W = 7.2
MAX_CHARS = 30
CHANNEL = 14       # free strip on the left of a column for vertical signal wires
UP = 8


def _tw(s):
    return min(len(s or '...'), MAX_CHARS) * CHAR_W


def _right(p):
    """Right-side pins: [{pin, operand|None, neg, main}] — main output (chain) first."""
    if p['type'] in ('call', 'instruction'):
        hl = [{'pin': 'ENO', 'operand': None, 'neg': bool(p.get('neg_output')), 'main': True}] \
            if p['inputs'] and p['inputs'][0]['pin'] == 'EN' else []
        return hl + [dict(v, main=False) for v in p['outputs']]
    return [{'pin': 'Q' if p['type'] in PINS_VISIBLE else '', 'operand': None, 'neg': bool(p.get('neg_output')), 'main': True}]


def _header(p):
    """(rows above the first pin: title + template + label above the box, text above the box)"""
    above = p.get('operand') if p['type'] in WITH_OPERAND else p.get('instance')
    return 1 + (1 if p.get('template') else 0) + (1 if above is not None else 0), above


def layout(elements, min_width=0):
    """-> (layout {id: {r, c}}, drawing) ; raises LayoutError."""
    P = {p['id']: p for p in elements}
    if not elements:
        return {}, {'fbd': True, 'width': 260, 'height': 3 * L, 'boxes': [], 'lines': [], 'nodes': [], 'empty': True}
    consumers = {}                                          # source -> [(consumer, input index)]
    for p in elements:
        for i, v in enumerate(p['inputs']):
            s = v.get('signal')
            if s:
                if s not in P:
                    raise LayoutError('signal from non-existent box %s' % s)
                consumers.setdefault(s, []).append((p['id'], i))
    if _cycle(elements):
        raise LayoutError('signals form a cycle')
    parent = {s: sp[0] for s, sp in consumers.items()}       # main consumer = first in export order
    tree_roots = [p['id'] for p in elements if p['id'] not in consumers]
    if not tree_roots:
        raise LayoutError('network has no output')

    # 1) subtree measure: left pin rows (relative to the 1st pin), height and anchor (output row from the top)
    ratio = {}

    def mer(b):
        p = P[b]
        hdr, _ = _header(p)
        right = _right(p)
        top, bottom = -hdr, max(len(p['inputs']), len(right), 1) - 1
        rows, down_y = [], None
        for i, v in enumerate(p['inputs']):
            r = 0 if i == 0 else rows[-1] + 1
            s = v.get('signal')
            if s and parent.get(s) == (b, i):
                hc, ac = mer(s)
                if down_y is not None:
                    r = max(r, down_y + 2 + ac)                   # free row between neighbouring subtrees
                down_y = r - ac + hc - 1
                top = min(top, r - ac)
            elif s:                                     # signal from elsewhere: the row on the left must be free
                if down_y is not None:
                    r = max(r, down_y + 1)
                down_y = r
            rows.append(r)
            bottom = max(bottom, r, down_y if down_y is not None else r)
        ratio[b] = {'rows': rows, 'right': right, 'hdr': hdr}
        return bottom - top + 1, -top

    # 2) trees, order (sources of signals from elsewhere first), columns and rows
    box_tree = {}

    def mark(b, k, relc, rel):
        box_tree[b] = k
        rel[b] = relc
        for i, v in enumerate(P[b]['inputs']):
            s = v.get('signal')
            if s and parent.get(s) == (b, i):
                mark(s, k, relc - 1, rel)

    rel = {}
    for k in tree_roots:
        mark(k, k, 0, rel)
    deps = {k: [] for k in tree_roots}
    for p in elements:
        for i, v in enumerate(p['inputs']):
            s = v.get('signal')
            if s and parent.get(s) != (p['id'], i) and box_tree[s] != box_tree[p['id']]:
                deps[box_tree[p['id']]].append(box_tree[s])
    order, state = [], {}

    def visit(k):
        if k in state:
            return
        state[k] = 1
        for d in deps[k]:
            visit(d)
        order.append(k)
    for k in tree_roots:
        visit(k)

    column, line = {}, {}
    r0 = 0
    for k in order:
        h, a = mer(k)
        members = [b for b in rel if box_tree[b] == k]
        shift = -min(rel[b] for b in members)
        for b in members:
            for i, v in enumerate(P[b]['inputs']):
                s = v.get('signal')
                if s and s in column and parent.get(s) != (b, i):
                    shift = max(shift, column[s] + 1 - rel[b])
        for b in members:
            column[b] = rel[b] + shift

        def place(b, r):
            line[b] = r
            for i, v in enumerate(P[b]['inputs']):
                s = v.get('signal')
                if s and parent.get(s) == (b, i):
                    place(s, r + ratio[b]['rows'][i])
        place(k, r0 + a)
        r0 += h + 1

    # 3) pixels: column = [channel | input labels | box | output labels]
    ncol = max(column.values()) + 1
    lw, bw, rw, above_w = [0.0] * ncol, [0.0] * ncol, [0.0] * ncol, [0.0] * ncol
    geo = {}
    for b, c in column.items():
        p, m = P[b], ratio[b]
        pins = p['type'] in PINS_VISIBLE
        l = max([_tw(v['operand']) + 18 + (8 if v.get('neg') else 0) for v in p['inputs'] if not v.get('signal')] + [12])
        r = max([_tw(v['operand']) + 18 + (8 if v.get('neg') else 0) for v in m['right'] if v['operand'] is not None]
                + [14 + (8 if any(v.get('neg') for v in m['right']) else 0)])
        nl = max([len(v['pin']) for v in p['inputs']] + [0]) if pins else 0
        nr = max([len(v['pin']) for v in m['right']] + [0]) if pins else 0
        symbol = SYMBOL.get(p['type']) or p.get('name') or 'FB ?'
        w = max(40 if p['type'] in ('assign', 's', 'r') else 52, len(symbol) * CHAR_W + 18, (nl + nr) * CHAR_W + 22 if pins else 0,
                len(p.get('template') or '') * CHAR_W + 18)
        _, above = _header(p)
        geo[b] = (symbol, w, above)
        lw[c], bw[c], rw[c] = max(lw[c], l), max(bw[c], w), max(rw[c], r)
        above_w[c] = max(above_w[c], _tw(above) + 10 if above is not None else 0)
    xs = [12.0]
    for c in range(ncol):
        wid = max(CHANNEL + lw[c] + bw[c] + rw[c], CHANNEL + lw[c] + above_w[c])
        xs.append(xs[-1] + wid)
    rmin = min(line[b] - ratio[b]['hdr'] for b in line)

    def Y(r):
        return round(UP + (r - rmin) * L + L / 2, 1)

    boxes, position = [], {}
    for b in sorted(line, key=lambda x: (column[x], line[x])):
        p, m = P[b], ratio[b]
        symbol, w, above = geo[b]
        c, r = column[b], line[b]
        bx = xs[c] + CHANNEL + lw[c]
        tmpl = 1 if p.get('template') else 0
        last_col = max(m['rows'][-1] if m['rows'] else 0, len(m['right']) - 1)
        pins = p['type'] in PINS_VISIBLE
        d = {'id': b, 'type': p['type'], 'x': round(bx, 1), 'w': round(w, 1), 'symbol': symbol,
             'y': round(Y(r - 1 - tmpl) - L / 2 + 1, 1), 'y_symbol': round(Y(r - 1 - tmpl) + 5, 1),
             'template': p.get('template'), 'y_template': round(Y(r - 1) + 4, 1),
             'above': above, 'y_above': round(Y(r - 1 - tmpl) - L / 2 - 3, 1),
             'inputs': [{'y': Y(r + rr), 'pin': v['pin'] if pins else '', 'n': v['n'],
                         'operand': None if v.get('signal') else v['operand'], 'neg': bool(v.get('neg'))}
                        for v, rr in zip(p['inputs'], m['rows'])],
             'outputs': [{'y': Y(r + j), 'pin': v['pin'] if pins else '', 'operand': v['operand'], 'neg': bool(v.get('neg')),
                          'stub': not (v['main'] and (b in consumers or p['type'] in ('assign', 's', 'r')))}
                         for j, v in enumerate(m['right'])]}
        d['h'] = round(Y(r + last_col) + L / 2 - 1 - d['y'], 1)
        boxes.append(d)
        position[b] = d

    lines, nodes = [], set()
    for s, sp in consumers.items():
        S = position[s]
        hl = next((v for v in S['outputs'] if v['operand'] is None), S['outputs'][0] if S['outputs'] else None)
        sy = hl['y'] if hl else S['y'] + L
        sx = S['x'] + S['w'] + (8 if hl and hl['neg'] else 0)
        for cid, i in sp:
            C = position[cid]
            v = C['inputs'][i]
            tx = C['x'] - (8 if v['neg'] else 0)
            if v['y'] == sy and parent[s] == (cid, i):
                lines.append([round(sx, 1), sy, round(tx, 1), sy])
                continue
            chx = xs[column[s] + 1] + CHANNEL / 2 if column[cid] > column[s] else sx + CHANNEL / 2
            lines += [[round(sx, 1), sy, round(chx, 1), sy], [round(chx, 1), sy, round(chx, 1), v['y']],
                     [round(chx, 1), v['y'], round(tx, 1), v['y']]]
            if len(sp) > 1:
                nodes.add((round(chx, 1), sy))
    width = round(max(xs[-1] + 12, min_width), 1)
    height = round(max(d['y'] + d['h'] for d in boxes) + L, 1)
    lay = {b: {'r': line[b] - rmin, 'c': column[b]} for b in line}
    return lay, {'fbd': True, 'width': width, 'height': height, 'boxes': boxes, 'lines': lines,
                  'nodes': [list(u) for u in sorted(nodes)]}
