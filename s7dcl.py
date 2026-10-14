"""Parser for .s7dcl (TIA V20+/Reader export) -> network model; titles from .s7res.

The grammar is derived from V21 exports (undocumented). Supports LAD:
RUNG wire#<from> ... END_RUNG [wire#<to>], inline `wire#wN`, attributes `{ ... }` before an element,
elements `Name( args )`, `NOT Coil( .. )`, calls `"Inst"( .. )`, `#inst( .. )`, `#inst.TON( .. )`.
"""
import os
import re

COMPARISONS = {'EQ': '==', 'NE': '<>', 'GT': '>', 'LT': '<', 'GE': '>=', 'LE': '<='}
# displayed instruction names (the export uses CamelCase, TIA uses upper case)
NAMES = {
    'Move': 'MOVE', 'Add': 'ADD', 'Sub': 'SUB', 'Mul': 'MUL', 'Div': 'DIV', 'Mod': 'MOD',
    'Convert': 'CONV', 'InRange': 'IN_RANGE', 'OutRange': 'OUT_RANGE', 'Swap': 'SWAP',
    'Abs': 'ABS', 'Inc': 'INC', 'Dec': 'DEC', 'Neg': 'NEG', 'Min': 'MIN', 'Max': 'MAX',
    'Limit': 'LIMIT', 'Sel': 'SEL', 'Mux': 'MUX', 'Calculate': 'CALCULATE', 'Round': 'ROUND',
    'Trunc': 'TRUNC', 'Ceil': 'CEIL', 'Floor': 'FLOOR', 'P_Trig': 'P_TRIG', 'N_Trig': 'N_TRIG',
    'S_SR': 'SR', 'S_RS': 'RS', 'Scale_X': 'SCALE_X', 'Norm_X': 'NORM_X', 'Fill': 'FILL',
}
# power pins (input, output) per instruction; other boxes EN/ENO
POWER_PINS = {
    'TON': ('IN', 'Q'), 'TOF': ('IN', 'Q'), 'TP': ('IN', 'Q'), 'TONR': ('IN', 'Q'),
    'CTU': ('CU', 'Q'), 'CTD': ('CD', 'QD'), 'CTUD': ('CU', 'QU'),
    'R_TRIG': ('CLK', 'Q'), 'F_TRIG': ('CLK', 'Q'), 'P_TRIG': ('CLK', 'Q'), 'N_TRIG': ('CLK', 'Q'),
    'SR': ('S', 'Q'), 'RS': ('R', 'Q'), 'IN_RANGE': ('', ''), 'OUT_RANGE': ('', ''),
}
COILS = {'Coil': 'coil', 'S_Coil': 'coil_s', 'R_Coil': 'coil_r', 'P_Coil': 'coil_p', 'N_Coil': 'coil_n'}


def read(fpath):
    with open(fpath, encoding='utf-8-sig') as f:
        return f.read()


class SourceParseError(Exception):
    pass


# ---------- low level: reading text with respect to quotes ----------

def _string_end(t, i):
    """t[i] is " or ' -> index after the closing quote."""
    q = t[i]
    j = t.find(q, i + 1)
    if j < 0:
        raise SourceParseError('unterminated string')
    return j + 1


def _paired(t, i, opening, close):
    """t[i] == opening -> index after the matching bracket (quotes are skipped)."""
    d = 0
    while i < len(t):
        c = t[i]
        if c in '"\'':
            i = _string_end(t, i)
            continue
        if c == opening:
            d += 1
        elif c == close:
            d -= 1
            if d == 0:
                return i + 1
        i += 1
    raise SourceParseError('unclosed bracket ' + opening)


def split_commas(t):
    """Splits argument text at commas outside quotes and brackets."""
    parts, d, z, i = [], 0, 0, 0
    while i < len(t):
        c = t[i]
        if c in '"\'':
            i = _string_end(t, i)
            continue
        if c in '([':
            d += 1
        elif c in ')]':
            d -= 1
        elif c == ',' and d == 0:
            parts.append(t[z:i])
            z = i + 1
        i += 1
    parts.append(t[z:])
    return [c.strip() for c in parts]


def attributes(t):
    """'{ S7_A := "x"; S7_B := "y" }' -> {'S7_A': 'x', ...}"""
    return {m.group(1): m.group(2) for m in re.finditer(r'(S7_\w+)\s*:=\s*"([^"]*)"', t)}


# ---------- .s7res ----------

def load_s7res(fpath):
    """MultiLingualTexts -> {id: {language: text}}. Mini-YAML: plain, '...' / "..." values and blocks |, |-, |+."""
    if not os.path.exists(fpath):
        return {}
    rows = read(fpath).splitlines()
    texts, active, i = {}, None, 0
    while i < len(rows):
        r = rows[i]
        m = re.match(r'^\s*-\s+id:\s*(\S+)\s*$', r)
        if m:
            active = texts.setdefault(m.group(1), {})
            i += 1
            continue
        m = re.match(r'^(\s+)([a-z]{2}-[A-Z]{2}):\s?(.*)$', r)
        if m and active is not None:
            indent, language, h = len(m.group(1)), m.group(2), m.group(3)
            if h[:1] in '|>':
                block = []
                i += 1
                while i < len(rows) and (not rows[i].strip() or len(rows[i]) - len(rows[i].lstrip()) > indent):
                    block.append(rows[i].strip())
                    i += 1
                active[language] = '\n'.join(block).strip()
                continue
            if h.startswith("'") and h.endswith("'") and len(h) > 1:
                h = h[1:-1].replace("''", "'")
            elif h.startswith('"') and h.endswith('"') and len(h) > 1:
                h = h[1:-1].replace('\\"', '"')
            active[language] = h
        i += 1
    return texts


def text_mlc(texts, ident, languages=('cs-CZ', 'en-US')):
    if not ident:
        return ''
    t = texts.get(ident)
    if t is None:
        return '' if ident.startswith('MLC_') else ident
    for j in languages:
        if t.get(j):
            return t[j]
    return next(iter(t.values()), '')


# ---------- block ----------

def load_block(path_s7dcl):
    text = read(path_s7dcl)
    block = parse_block(text)
    texts = load_s7res(path_s7dcl[:-len('.s7dcl')] + '.s7res')
    for nw in block['networks']:
        nw['title'] = text_mlc(texts, nw['attributes'].get('S7_NetworkTitle'))
        nw['comment'] = text_mlc(texts, nw['attributes'].get('S7_NetworkComment'))
    block['title'] = text_mlc(texts, block['attributes'].get('S7_BlockTitle'))
    return block


def parse_block(text):
    m = re.search(r'^(FUNCTION_BLOCK|FUNCTION|ORGANIZATION_BLOCK|DATA_BLOCK|TYPE)\s+("([^"]+)"|(\S+))(\s*:\s*("([^"]+)"|\S+))?',
                  text, re.M)
    if not m:
        raise SourceParseError('block header not found')
    kind = {'FUNCTION_BLOCK': 'FB', 'FUNCTION': 'FC', 'ORGANIZATION_BLOCK': 'OB',
            'DATA_BLOCK': 'DB', 'TYPE': 'UDT'}[m.group(1)]
    head = text[:m.start()]
    attrs = attributes(head[head.find('{'):] if '{' in head else '')
    block = {
        'kind': kind, 'name': m.group(3) or m.group(4), 'attributes': attrs,
        'number': int(attrs['S7_BlockNumber']) if attrs.get('S7_BlockNumber', '').isdigit() else None,
        'language': attrs.get('S7_PreferredLanguage') or attrs.get('S7_Language') or '',
        'instance_type': (m.group(7) or m.group(6)) if kind == 'DB' and m.group(5) else None,
        'variables': variable_types(re.split(r'^\s*(?:NETWORK|BEGIN)\s*$', text[m.end():], maxsplit=1, flags=re.M)[0]),
        'networks': [],
    }
    body = text[m.end():]
    for nw_attrs, content in _networks(body):
        nw = {'nw': len(block['networks']) + 1, 'attributes': nw_attrs,
              'language': nw_attrs.get('S7_Language', ''), 'text': content}
        if nw['language'] == 'LAD':
            nw['rungs'], nw['elements'] = parse_lad(content)
        elif nw['language'] == 'FBD':
            import fbd                                   # lazy: fbd imports s7dcl
            try:
                nw['elements'] = fbd.parse(content)
            except SourceParseError as e:                    # an unknown FBD construct must not break the whole block
                nw['error'] = str(e)
        block['networks'].append(nw)
    return block


def variable_types(text):
    """Interface declarations `name : Type;` -> {name: Type} (only for looking up the multi-instance type)."""
    res = {}
    for m in re.finditer(r'^\s*("[^"]+"|\w+)\s*(\{[^}]*\})?\s*:\s*([^;:=\n]+?)\s*(:=[^;\n]*)?;', text, re.M):
        nm = m.group(1).strip('"')
        etype = m.group(3).strip()
        if etype.startswith('_.'):
            etype = etype[2:]
        res.setdefault(nm, etype.strip('"'))
    return res


NETWORK_OR_ATTRIBUTES = re.compile(r'(?:^|(?<=\}))[ \t]*(\{|NETWORK[ \t]*$)', re.M)
END_NETWORK = re.compile(r'^[ \t]*END_NETWORK[ \t]*$', re.M)


def _networks(body):
    """Returns [(attributes, content)] for each NETWORK ... END_NETWORK."""
    res, i, last_attr = [], 0, ''
    while True:
        m = NETWORK_OR_ATTRIBUTES.search(body, i)
        if not m:
            break
        start = m.start(1)
        if body[start] == '{':
            k = _paired(body, start, '{', '}')
            last_attr = body[start:k]
            i = k
            continue
        k = END_NETWORK.search(body, start)
        if not k:
            raise SourceParseError('NETWORK without END_NETWORK')
        res.append((attributes(last_attr), body[start + len('NETWORK'):k.start()]))
        last_attr = ''
        i = k.end()
    return res


# ---------- LAD network ----------

def split_rungs(content):
    """RUNG … END_RUNG of a LAD or FBD network -> [{'from': text | None, 'items': [('element', target, args | None,
    attribute text) | ('wire', 'wN')], 'to': text | None}]; args None = an element without parentheses."""
    rungs, active, attrs, i, t = [], None, '', 0, content
    while i < len(t):
        if t[i].isspace():
            i += 1
            continue
        row_k = t.find('\n', i)
        row_k = len(t) if row_k < 0 else row_k
        line = t[i:row_k].strip()
        if re.match(r'RUNG\b', line):
            if active is not None:
                raise SourceParseError('RUNG inside RUNG')
            active = {'from': line[4:].strip() or None, 'items': [], 'to': None}
            i = row_k
        elif re.match(r'END_RUNG\b', line):
            if active is None:
                raise SourceParseError('END_RUNG without RUNG')
            active['to'] = line[8:].strip() or None
            rungs.append(active)
            active = None
            i = row_k
        elif t[i] == '{':
            k = _paired(t, i, '{', '}')
            attrs = t[i:k]
            i = k
        elif re.match(r'wire#\w+$', line):
            if active is None:
                raise SourceParseError('wire outside RUNG')
            active['items'].append(('wire', line[5:]))
            i = row_k
        else:
            if active is None:
                raise SourceParseError('element outside RUNG: ' + line[:40])
            j = i                                       # call target = text before the first '(' outside quotes
            while j < len(t) and t[j] not in '(\n':
                j = _string_end(t, j) if t[j] in '"\'' else j + 1
            k = _paired(t, j, '(', ')') if j < len(t) and t[j] == '(' else j
            active['items'].append(('element', t[i:j].strip(), t[j + 1:k - 1] if k > j else None, attrs))
            attrs = ''
            i = k
    if active is not None:
        raise SourceParseError('RUNG without END_RUNG')
    return rungs


def parse_lad(content):
    """-> (rungs, elements). rung = {'from': node, 'path': [element id | 'wire#wN'], 'to': node|None}.
    Node = 'powerrail' | 'wN'; an FBD-like start with an operand is 'op:<text>'."""
    rungs, elements = [], []
    for r in split_rungs(content):
        path = []
        for item in r['items']:
            if item[0] == 'wire':
                path.append('wire#' + item[1])
                continue
            p = element(item[1], item[2], attributes(item[3]))
            p['id'] = 'p%d' % (len(elements) + 1)
            elements.append(p)
            path.append(p['id'])
        rungs.append({'from': _node(r['from']) if r['from'] else 'powerrail', 'path': path,
                      'to': _node(r['to']) if r['to'] else None})
    return rungs, elements


def _node(s):
    if s.startswith('wire#'):
        return s[5:]
    return 'op:' + s


def _arguments(args):
    if args is None or not args.strip():
        return [], []
    pos, pins = [], []
    for c in split_commas(args):
        m = re.match(r'^("[^"]+"|[A-Za-z_]\w*)\s*(:=|=>)\s*(.*)$', c, re.S)
        if m:
            pins.append({'pin': m.group(1), 'direction': 'in' if m.group(2) == ':=' else 'out',
                         'operand': m.group(3).strip()})
        elif c:
            pos.append(c)
    return pos, pins


def _template(attrs):
    s = attrs.get('S7_Templates')
    if not s:
        return None
    values = re.findall(r':=\s*([\w"]+)', s)
    return ' to '.join(h.strip('"') for h in values) or s


def element(target, args, attrs):
    """Classifies an element by its call target."""
    neg = False
    if target.startswith('NOT '):
        neg, target = True, target[4:].strip()
    pos, pins = _arguments(args)
    pin = {p['pin']: p['operand'] for p in pins}
    tmpl = _template(attrs)
    if re.fullmatch(r'[A-Za-z_]\w*', target):
        if target in ('Contact', 'I_Contact'):
            return {'type': 'contact_nc' if target == 'I_Contact' else 'contact', 'operand': (pos or [''])[0]}
        if target in ('P_Contact', 'N_Contact'):
            return {'type': 'contact_p' if target[0] == 'P' else 'contact_n',
                    'operand': pin.get('operand', (pos or [''])[0]), 'bit': pin.get('bit', '')}
        m = re.fullmatch(r'(EQ|NE|GT|LT|GE|LE)_Contact', target)
        if m:
            return {'type': 'comparison', 'op': COMPARISONS[m.group(1)], 'in1': pin.get('in1', ''),
                    'in2': pin.get('in2', ''), 'template': tmpl}
        if target in COILS:
            return {'type': 'coil_neg' if neg and target == 'Coil' else COILS[target], 'operand': (pos or [''])[0]}
        if target in ('ReturnTrue', 'ReturnFalse', 'Return'):
            return {'type': 'ret', 'operand': {'ReturnTrue': 'TRUE', 'ReturnFalse': 'FALSE'}.get(target, '')}
        if target == 'NOT' and not pins:
            return {'type': 'not'}
        name = NAMES.get(target, target.upper() if target.isupper() or '_' in target else target)
        box = {'type': 'box', 'call': 'instruction', 'name': name, 'instance': None, 'template': tmpl}
        if target in ('P_Trig', 'N_Trig'):
            box['below'] = (pos or [''])[0]
        elif target in ('S_SR', 'S_RS'):
            box['instance'] = pin.pop('operand', None)
            pins = [p for p in pins if p['pin'] != 'operand']
        elif pos:
            pins = [{'pin': 'IN', 'direction': 'in', 'operand': pos[0]}] + pins
    else:
        # calls: "Inst"(..), #inst(..), #inst.TON(..), "DB".PID_Temp(..)
        m = re.fullmatch(r'(.+)\.([A-Za-z_]\w*)', target)
        if m:
            box = {'type': 'box', 'call': 'instruction', 'name': m.group(2), 'instance': m.group(1), 'template': tmpl}
        else:
            box = {'type': 'box', 'call': 'fb', 'name': None, 'instance': target, 'template': tmpl}
    if box['call'] == 'instruction':
        for p in pins:
            p['pin'] = p['pin'].upper()
    box['power'] = list(POWER_PINS.get(box['name'], ('EN', 'ENO')))
    box['inputs'] = [p for p in pins if p['direction'] == 'in']
    box['outputs'] = [p for p in pins if p['direction'] == 'out']
    for p in pins:
        del p['direction']
    if neg:
        box['neg'] = True
    return box


def fill_fb_types(block, find_db):
    """Fills in the FB name of calls: #inst -> type from the block interface, "Inst" -> DATA_BLOCK "Inst" : "FB" (find_db)."""
    for nw in block['networks']:
        for p in nw.get('elements', []):
            if p.get('call') == 'fb' and not p['name']:          # LAD box and FBD call
                inst = p['instance']
                if inst.startswith('#'):
                    p['name'] = block['variables'].get(inst[1:].strip('"'))
                elif inst.startswith('"'):
                    p['name'] = find_db(inst.strip('"'))
                p['name'] = p['name'] or 'FB ?'


def wires(nw):
    """Model connections: [{from, to}] between nodes (powerrail, wN, element id, '<id>.<pin>'); to=None = open end."""
    res = []
    for r in nw.get('rungs', []):
        before = r['from']
        for it in r['path']:
            already = it[5:] if it.startswith('wire#') else it
            res.append({'from': before, 'to': already})
            before = already
        res.append({'from': before, 'to': r['to']})
    for p in nw.get('elements', []):
        if p['type'] == 'box':
            for pin in p['inputs']:
                if pin['operand'].startswith('wire#'):
                    res.append({'from': pin['operand'][5:], 'to': '%s.%s' % (p['id'], pin['pin'])})
    return res
