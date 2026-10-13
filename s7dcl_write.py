"""Serialization of the network model ("after", LAD from model.py / FBD from fbd.py) back into the `.s7dcl` grammar (TIA V21)
and replacement of one network's text in the block file (M5, Upload via TIAC_Writer).

The text shape copies the V21 exports in the samples (4-space indent, RUNG/END_RUNG, `wire#wN`, `Contact( x )`,
calls with `:=` / `=>`, `{ S7_Templates := … }`, FBD `A( in2 := … )`, `NOT` before an element). It is only verified that
our own parser (`s7dcl.py`, `fbd.py`) reads the text back to the same content — not that TIA imports it (see docs/reference.md, M5).
"""
import re

import s7dcl

IND = ' ' * 8                                   # RUNG in the network body
IND_P = ' ' * 12                                # element in a rung
IND_A = ' ' * 16                                # element argument

# displayed instruction name -> name in the export (inverse of s7dcl.NAMES)
EXPORT_NAME = {v: k for k, v in s7dcl.NAMES.items()}
COILS = {v: k for k, v in s7dcl.COILS.items()}
COMPARISONS = {v: k for k, v in s7dcl.COMPARISONS.items()}
# system instructions: pins in lower case in the export (the parser upper-cases them); F-instruction / FB pins are kept
SYSTEM = set(s7dcl.NAMES.values()) | set(s7dcl.POWER_PINS) | {'MOVE'}


class WriteError(ValueError):
    pass


# ---------- common ----------

def _template(name, tmpl):
    """'Int' / 'Int to DInt' -> attribute text `{ S7_Templates := "…" }`.
    shortcut: template keys are guessed (SrcType, time_type, DestType), only comparison and TON verified — complete from an export"""
    if not tmpl:
        return None
    parts = [x.strip() for x in str(tmpl).split(' to ')]
    if name in ('TON', 'TOF', 'TP', 'TONR'):
        keys = ['time_type']
    elif len(parts) == 2:
        keys = ['SrcType', 'DestType']
    else:
        keys = ['SrcType']
    return '{ S7_Templates := "%s" }' % '; '.join('%s := %s' % kv for kv in zip(keys, parts))


def _attributes(a):
    if not a:
        return []
    items = ['%s := "%s"' % (k, v) for k, v in a.items()]
    return [IND_P + '{'] + [IND_A + x + (';' if i < len(items) - 1 else '') for i, x in enumerate(items)] \
        + [IND_P + '}']


def _call(target, args):
    """target( a, b ) — one argument per line, a single argument on one line (like the export)."""
    if not args:
        return [IND_P + target + '(  )']
    if len(args) == 1 and '\n' not in args[0]:
        return [IND_P + '%s( %s )' % (target, args[0])]
    return [IND_P + target + '('] + [IND_A + a + (',' if i < len(args) - 1 else '') for i, a in enumerate(args)] \
        + [IND_P + ')']


def _pin(pin, direction, value, neg=False, lower=False):
    h = (('NOT ' if neg else '') + (value or '').strip()).strip()
    return '%s %s %s' % (pin.lower() if lower else pin, direction, h) if h else '%s %s  ' % (pin.lower() if lower else pin, direction)   # empty pin like the export: `pin :=  ,`


def _target_box(p):
    """-> (call target text, lower-case pins?)"""
    name, inst = p.get('name'), p.get('instance')
    if p.get('call') == 'fc' or (p.get('call') == 'fb' and not inst):
        return '"%s"' % (name or '').strip('"'), False
    if p.get('call') == 'fb':
        return inst, False
    if inst:                                           # #T.TON(…), "DB".TP(…), #NouzStop.ESTOP1(…)
        return '%s.%s' % (inst, name), name in SYSTEM
    return EXPORT_NAME.get(name, name), True


# ---------- LAD ----------

def _lad_element(p):
    t = p['type']
    if t in ('contact', 'contact_nc'):
        return [IND_P + '%s( %s )' % ('Contact' if t == 'contact' else 'I_Contact', p.get('operand', ''))]
    if t in ('contact_p', 'contact_n'):
        # shortcut: the P_Contact shape in a V21 export is unverified (not in the samples) — the parser reads named and positional
        return _call('P_Contact' if t == 'contact_p' else 'N_Contact',
                       ['operand := %s' % p.get('operand', ''), 'bit := %s' % p.get('bit', '')])
    if t == 'comparison':
        s = _template(None, p.get('template'))
        return ([IND_P + s] if s else []) + _call('%s_Contact' % COMPARISONS[p['op']],
                                                     ['in1 := %s' % p.get('in1', ''), 'in2 := %s' % p.get('in2', '')])
    if t == 'coil_neg':
        return [IND_P + 'NOT Coil( %s )' % p.get('operand', '')]
    if t in COILS:
        return [IND_P + '%s( %s )' % (COILS[t], p.get('operand', ''))]
    if t == 'ret':
        return [IND_P + {'TRUE': 'ReturnTrue', 'FALSE': 'ReturnFalse'}.get(p.get('operand'), 'Return') + '(  )']
    if t == 'not':
        return [IND_P + 'NOT']
    if t != 'box':
        raise WriteError('unknown element type %r (%s)' % (t, p.get('id')))
    target, lower = _target_box(p)
    if p.get('name') in ('P_TRIG', 'N_TRIG') and not p.get('instance'):
        rows = [IND_P + '%s( %s )' % (target, p.get('below', ''))]
    else:
        args = [_pin(x['pin'], ':=', x['operand'], lower=lower) for x in p.get('inputs', [])]
        if p.get('name') in ('SR', 'RS') and p.get('call') == 'instruction':
            args.append('operand := %s' % (p.get('instance') or ''))
            target = EXPORT_NAME[p['name']]
        args += [_pin(x['pin'], '=>', x['operand'], lower=lower) for x in p.get('outputs', [])]
        rows = _call(target, args)
    if p.get('neg'):
        rows[0] = IND_P + 'NOT ' + rows[0].lstrip()
    s = _template(p.get('name'), p.get('template'))
    return ([IND_P + s] if s else []) + rows


def _node(u):
    return u[3:] if u.startswith('op:') else 'wire#' + u


def lad_text(m):
    """LAD model {elements, rungs} -> network body text (lines between NETWORK and END_NETWORK)."""
    P = {p['id']: p for p in m['elements']}
    rows = []
    for r in m.get('rungs') or [{'from': 'powerrail', 'path': [], 'to': None}]:
        rows.append(IND + 'RUNG ' + _node(r['from']))
        for it in r['path']:
            if it.startswith('wire#'):
                rows.append(IND_P + it)
            elif it in P:
                rows += _lad_element(P[it])
            else:
                raise WriteError('rung refers to a non-existent element %r' % it)
        rows.append(IND + 'END_RUNG' + (' wire#' + r['to'] if r.get('to') else ''))
    return '\n'.join(rows) + '\n'


# ---------- FBD ----------

def _has_chain(p):
    """Does the box have a chain input (input 1 = output of the previous one / RUNG operand)? A call in an F-block has no EN."""
    return bool(p['inputs']) and (p['type'] not in ('call', 'instruction') or p['inputs'][0]['pin'] == 'EN')


def _fbd_box(p, wire_of):
    t, before = p['type'], []                             # before = attributes / template above the element
    inp = p['inputs'][1:] if _has_chain(p) else p['inputs']

    def value(v):
        return 'wire#' + wire_of[v['signal']] if v.get('signal') else v.get('operand', '')
    if t in ('and', 'or', 'xor'):
        rows = _call({'and': 'A', 'or': 'O', 'xor': 'X'}[t], [_pin(v['pin'], ':=', value(v), v.get('neg')) for v in inp])
    elif t in ('assign', 's', 'r'):
        rows = [IND_P + '%s( %s )' % ({'assign': 'Coil', 's': 'S_Coil', 'r': 'R_Coil'}[t], p.get('operand', ''))]
    elif t in ('ptrig', 'ntrig'):
        rows = [IND_P + '%s( %s )' % ('P_Trig' if t == 'ptrig' else 'N_Trig', p.get('operand', ''))]
    elif t in ('sr', 'rs'):
        rows = _call('S_SR' if t == 'sr' else 'S_RS', [_pin(v['pin'], ':=', value(v), v.get('neg'), lower=True)
                                                          for v in inp] + ['operand := %s' % p.get('operand', '')])
    else:
        target, lower = _target_box(p)
        args = [_pin(v['pin'], ':=', value(v), v.get('neg'), lower) for v in inp]
        args += [_pin(v['pin'], '=>', v.get('operand', ''), v.get('neg'), lower) for v in p.get('outputs', [])]
        s = _template(p.get('name'), p.get('template'))
        before = ([IND_P + s] if s else []) + _attributes(p.get('attributes'))
        rows = _call(target, args)
    if _has_chain(p) and p['inputs'][0].get('neg'):
        rows[0] = IND_P + 'NOT ' + rows[0].lstrip()
    return before + rows


def fbd_text(m):
    """FBD model {elements} -> network body text. Chain = a box whose input 1 is the main output of the previous one;
    other consumers of the output get `wire#wN` (tap in the rung / END_RUNG wire#wN). Negation of the main output
    (`neg_output`, editor only) is written as negation of the consumers' inputs."""
    elements = [dict(p, inputs=[dict(v) for v in p['inputs']]) for p in m['elements']]
    P = {p['id']: p for p in elements}
    consumers = {p['id']: [] for p in elements}
    for c in elements:
        for v in c['inputs']:
            if v.get('signal'):
                if v['signal'] not in P:
                    raise WriteError('signal from a non-existent box %s' % v['signal'])
                consumers[v['signal']].append((c, v))
    for p in elements:
        if p.get('neg_output'):
            for _, v in consumers[p['id']]:
                v['neg'] = not v.get('neg')
    nxt = {}
    for p in elements:
        k = next((c for c, v in consumers[p['id']] if _has_chain(c) and v is c['inputs'][0]), None)
        if k is not None:
            nxt[p['id']] = k
    continuation = {c['id'] for c in nxt.values()}
    wire_of = {}
    for p in elements:
        if any(c is not nxt.get(p['id']) or v is not c['inputs'][0] for c, v in consumers[p['id']]):
            wire_of[p['id']] = 'w%d' % (len(wire_of) + 1)
    rows, done = [], set()
    for start in elements:
        if start['id'] in continuation:
            continue
        v0 = start['inputs'][0] if _has_chain(start) else None
        if v0 is None:
            z = ''
        elif v0.get('signal'):
            z = ' wire#' + wire_of[v0['signal']]
        else:
            z = (' ' + v0['operand'].strip()) if v0.get('operand', '').strip() else ''
        rows.append(IND + 'RUNG' + z)
        p = start
        while True:
            done.add(p['id'])
            rows += _fbd_box(p, wire_of)
            following = nxt.get(p['id'])
            if following is None or following['id'] in done:
                break
            if p['id'] in wire_of:
                rows.append(IND_P + 'wire#' + wire_of[p['id']])
            p = following
        rows.append(IND + 'END_RUNG' + (' wire#' + wire_of[p['id']] if p['id'] in wire_of else ''))
    if len(done) != len(elements):
        raise WriteError('FBD wiring has a cycle (%s)' % ', '.join(sorted(set(P) - done)))
    if not elements:
        rows = [IND + 'RUNG', IND + 'END_RUNG']
    return '\n'.join(rows) + '\n'


def network_text(m):
    """"After" model (LAD or FBD) -> network body text for `.s7dcl`."""
    if m.get('language') == 'FBD':
        return fbd_text(m)
    return lad_text(m)


# ---------- replacing a network in the block file ----------

def replace_network(text_s7dcl, nw_number, new_text):
    """Replaces the body of the N-th NETWORK … END_NETWORK (1 = first) with `new_text`; the block header, network attributes
    (title, comment, language) and other networks stay unchanged. CRLF line endings are preserved."""
    crlf = '\r\n' in text_s7dcl
    t = text_s7dcl.replace('\r\n', '\n')
    starts = list(re.finditer(r'^[ \t]*NETWORK[ \t]*\n', t, re.M))
    if not 1 <= nw_number <= len(starts):
        raise WriteError('network %d is not in the block (networks in block: %d)' % (nw_number, len(starts)))
    z = starts[nw_number - 1].end()
    k = re.compile(r'^[ \t]*END_NETWORK[ \t]*$', re.M).search(t, z)
    if not k:
        raise WriteError('NETWORK without END_NETWORK')
    new_value = new_text.replace('\r\n', '\n')
    if not new_value.endswith('\n'):
        new_value += '\n'
    t = t[:z] + new_value + t[k.start():]
    return t.replace('\n', '\r\n') if crlf else t


# ---------- block interface (E2) ----------

# section of an interface edit -> the keyword line that opens it in `.s7dcl`
SECTION_KEYWORD = {'Input': 'VAR_INPUT', 'Output': 'VAR_OUTPUT', 'InOut': 'VAR_IN_OUT', 'Static': 'VAR', 'Temp': 'VAR_TEMP',
                   'Constant': 'VAR CONSTANT'}


def member_line(m):
    """{name, type, initial?} -> `name : Type := initial;` (comments are not written: their document form is unverified)."""
    return '%s : %s%s;' % (m['name'], m['type'], ' := %s' % m['initial'] if m.get('initial') else '')


def add_members(text_s7dcl, section, members):
    """Appends member lines at the end of the interface section (before its END_VAR); a missing section is created after
    the last END_VAR of the interface (or right after the block header line). Networks are not touched. CRLF is kept."""
    if section not in SECTION_KEYWORD:
        raise WriteError('unknown interface section %r' % (section,))
    crlf = '\r\n' in text_s7dcl
    t = text_s7dcl.replace('\r\n', '\n')
    m = re.search(r'^[ \t]*(?:NETWORK|BEGIN)[ \t]*$', t, re.M)
    head_end = m.start() if m else len(t)
    kw = SECTION_KEYWORD[section].replace(' ', r'[ \t]+')
    opening = re.compile(r'^([ \t]*)%s[ \t]*$' % kw, re.M).search(t, 0, head_end)
    if opening:
        end = re.compile(r'^([ \t]*)END_VAR[ \t]*$', re.M).search(t, opening.end(), head_end)
        if not end:
            raise WriteError('%s without END_VAR' % SECTION_KEYWORD[section])
        ind = opening.group(1) + '    '
        t = t[:end.start()] + ''.join(ind + member_line(x) + '\n' for x in members) + t[end.start():]
    else:
        ends = list(re.compile(r'^([ \t]*)END_VAR[ \t]*\n', re.M).finditer(t, 0, head_end))
        if ends:
            at, ind = ends[-1].end(), ends[-1].group(1)
        else:
            hl = re.compile(r'^(FUNCTION_BLOCK|FUNCTION|ORGANIZATION_BLOCK)\b.*\n', re.M).search(t, 0, head_end)
            if not hl:
                raise WriteError('block header not found')
            at, ind = hl.end(), '    '
        t = t[:at] + ind + SECTION_KEYWORD[section] + '\n' + ''.join(ind + '    ' + member_line(x) + '\n' for x in members) \
            + ind + 'END_VAR\n' + t[at:]
    return t.replace('\n', '\r\n') if crlf else t
