"""Element catalog for the editor palette (M3).

- fixed LAD instructions with pins (element templates shaped like the network model, without `id`),
- project blocks from the export: FB/FC with pins from the `.s7dcl` interface, instance DBs (`DATA_BLOCK "X" : "FB"`),
- operands for autocomplete: tags from tag tables (`<PLC>\\Tags\\**\\*.xml`, SimaticML) and members of global DBs.
Only the newest export of each block / table is read; the interface is read only up to the first NETWORK/BEGIN.
"""
import copy
import os
import re
import xml.etree.ElementTree as ET

import s7dcl


def _box(name, power=('EN', 'ENO'), inputs=(), outputs=(), template=None, instance=None, **extra):
    p = {'type': 'box', 'call': 'instruction', 'name': name, 'instance': instance, 'template': template,
         'power': list(power), 'inputs': [{'pin': x, 'operand': ''} for x in inputs],
         'outputs': [{'pin': x, 'operand': ''} for x in outputs]}
    p.update(extra)
    return p


def _instruction():
    I = []

    def add(key, name, group, element, description=''):
        I.append({'key': key, 'name': name, 'group': group, 'element': element, 'description': description})
    add('NO', '-| |-', 'Contacts', {'type': 'contact', 'operand': ''}, 'NO contact')
    add('NC', '-|/|-', 'Contacts', {'type': 'contact_nc', 'operand': ''}, 'NC contact')
    add('P', '-|P|-', 'Contacts', {'type': 'contact_p', 'operand': '', 'bit': ''}, 'rising edge')
    add('N', '-|N|-', 'Contacts', {'type': 'contact_n', 'operand': '', 'bit': ''}, 'falling edge')
    add('C', '-( )-', 'Coils', {'type': 'coil', 'operand': ''}, 'coil')
    add('S', '-(S)-', 'Coils', {'type': 'coil_s', 'operand': ''}, 'set')
    add('R', '-(R)-', 'Coils', {'type': 'coil_r', 'operand': ''}, 'reset')
    add('NEG', '-(/)-', 'Coils', {'type': 'coil_neg', 'operand': ''}, 'negated coil')
    add('RET', 'RET', 'Coils', {'type': 'ret', 'operand': ''}, 'return from block')
    for k, op in s7dcl.COMPARISONS.items():
        add(k, 'CMP ' + op, 'Comparison', {'type': 'comparison', 'op': op, 'in1': '', 'in2': '', 'template': 'Int'},
            k + '_Contact, S7_Templates SrcType')
    for k in ('TON', 'TOF', 'TP'):
        add(k, k, 'Timers', _box(k, ('IN', 'Q'), ['PT'], ['ET'], 'Time', ''), 'instance: #multi or "DB"')
    add('CTU', 'CTU', 'Counters', _box('CTU', ('CU', 'Q'), ['R', 'PV'], ['CV'], 'Int', ''))
    add('CTD', 'CTD', 'Counters', _box('CTD', ('CD', 'QD'), ['LD', 'PV'], ['CV'], 'Int', ''))
    add('MOVE', 'MOVE', 'Move', _box('MOVE', inputs=['IN'], outputs=['OUT1']))
    add('SWAP', 'SWAP', 'Move', _box('SWAP', inputs=['IN'], outputs=['OUT'], template='Word'))
    add('CONV', 'CONV', 'Move', _box('CONV', inputs=['IN'], outputs=['OUT'], template='Int to DInt'))
    for k in ('ADD', 'SUB', 'MUL', 'DIV'):
        add(k, k, 'Math', _box(k, inputs=['IN1', 'IN2'], outputs=['OUT'], template='Int'))
    add('IN_RANGE', 'IN_RANGE', 'Comparison', _box('IN_RANGE', ('', ''), ['MIN', 'VAL', 'MAX'], [], 'Int'))
    add('P_TRIG', 'P_TRIG', 'Edges', _box('P_TRIG', ('CLK', 'Q'), below=''), 'edge memory bit below the box')
    add('N_TRIG', 'N_TRIG', 'Edges', _box('N_TRIG', ('CLK', 'Q'), below=''))
    return I


INSTRUCTIONS = _instruction()

_SECTIONS = {'VAR_INPUT': 'inputs', 'VAR_OUTPUT': 'outputs', 'VAR_IN_OUT': 'inout', 'VAR_TEMP': 'temp'}
_cache = {}   # path -> (mtime, result)


def interface(text):
    """Block header + interface (.s7dcl) without networks -> {kind, name, number, instance_type, section: {..: [{name, type}]}}.
    Top-level members only (STRUCT = 'Struct', its content is skipped)."""
    m = re.search(r'^\s*(?:NETWORK|BEGIN)\s*$', text, re.M)
    head = text[:m.start()] if m else text
    # shortcut: attributes `{…}` are removed by a regex that ignores quotes — a `}` inside an attribute value would break it
    head_rest = re.sub(r'\{[^{}]*\}', '', head)
    if not re.search(r'^(FUNCTION_BLOCK|FUNCTION|DATA_BLOCK)\b', head_rest, re.M):
        return None
    b = s7dcl.parse_block(head_rest + '\n')
    hm = re.search(r'S7_BlockNumber\s*:=\s*"(\d+)"', head)
    res = {'kind': b['kind'], 'name': b['name'], 'number': int(hm.group(1)) if hm else None,
           'instance_type': b['instance_type'], 'section': {}, 'f': bool(re.search(r'S7_Safety\s*:=\s*"TRUE"', head))}
    section, depth = None, 0
    for r in head_rest.splitlines():
        s = r.strip()
        if not s:
            continue
        sm = re.match(r'^(VAR_INPUT|VAR_OUTPUT|VAR_IN_OUT|VAR_TEMP|VAR)\b(?!\s*:)', s)
        if sm and depth == 0:
            section = 'constants' if 'CONSTANT' in s else _SECTIONS.get(sm.group(1), 'static')
            continue
        if s.startswith('END_VAR'):
            section = None
            continue
        if section is None:
            continue
        if s.startswith('END_STRUCT'):
            depth -= 1
            continue
        dm = re.match(r'^("[^"]+"|[A-Za-z_]\w*)\s*:\s*([^;:=]+?)\s*(:=.*)?;?\s*$', s)
        if not dm:
            continue
        etype = dm.group(2).strip()
        struct = bool(re.search(r'\bSTRUCT$', etype))
        if struct:
            etype = etype[:-6] + 'Struct'
        if depth == 0:
            etype = etype[2:] if etype.startswith('_.') else etype
            res['section'].setdefault(section, []).append({'name': dm.group(1).strip('"'), 'type': etype.strip('"')})
        if struct:
            depth += 1
    return res


def _read_cache(fpath, fn):
    try:
        mt = os.path.getmtime(fpath)
    except OSError:
        return None
    c = _cache.get(fpath)
    if c and c[0] == mt:
        return c[1]
    try:
        v = fn(fpath)
    except (s7dcl.SourceParseError, ET.ParseError, OSError, UnicodeDecodeError, ValueError):
        v = None
    _cache[fpath] = (mt, v)
    return v


def tag_table(fpath):
    """SimaticML tag table -> [{name, type, address, table}]."""
    tree_root = ET.parse(fpath).getroot()
    res = []
    for tab in tree_root.iter('SW.Tags.PlcTagTable'):
        tab_name = tab.findtext('AttributeList/Name') or ''
        for t in tab.iter('SW.Tags.PlcTag'):
            nm = t.findtext('AttributeList/Name')
            if nm:
                res.append({'name': nm, 'type': t.findtext('AttributeList/DataTypeName') or '',
                            'address': t.findtext('AttributeList/LogicalAddress') or '', 'table': tab_name})
    return res


def _latest(root, project, suffix, subfolder=None):
    """{key: full path} from the newest export that contains the file (key = file name,
    with a subfolder the relative path below it)."""
    pd = os.path.join(root, project)
    res = {}
    if not os.path.isdir(pd):
        return res
    for ts in sorted(os.listdir(pd)):
        if not re.match(r'^\d{8}-\d{6}$', ts):
            continue
        for dp, _, files in os.walk(os.path.join(pd, ts)):
            parts = os.path.relpath(dp, os.path.join(pd, ts)).split(os.sep)
            if subfolder and subfolder not in parts:
                continue
            for f in files:
                if f.endswith(suffix):
                    k = os.path.join(*parts[parts.index(subfolder):], f) if subfolder else f
                    res[k] = os.path.join(dp, f)            # a newer export overrides an older one
    return res


def block_template(b, instance=''):
    """Palette element template for an FB/FC call from the interface (IN_OUT as inputs on the left)."""
    s = b['section']
    return {'type': 'box', 'call': 'fb' if b['kind'] == 'FB' else 'fc', 'name': b['name'],
            'instance': instance if b['kind'] == 'FB' else None, 'template': None,
            'power': ['EN', 'ENO'],
            'inputs': [{'pin': x['name'], 'operand': ''} for x in s.get('inputs', []) + s.get('inout', [])],
            'outputs': [{'pin': x['name'], 'operand': ''} for x in s.get('outputs', [])]}


def catalog(root, project):
    blocks, dbs = [], []
    for _, fpath in sorted(_latest(root, project, '.s7dcl').items()):
        r = _read_cache(fpath, lambda c: interface(s7dcl.read(c)))
        if r:
            (dbs if r['kind'] == 'DB' else blocks).append(r)
    inst = {}
    for d in dbs:
        if d['instance_type']:
            inst.setdefault(d['instance_type'], []).append('"%s"' % d['name'])
    res_blocks = []
    for b in blocks:
        if b['kind'] in ('FB', 'FC'):
            instance = inst.get(b['name'], [])
            res_blocks.append({'key': '%s:%s' % (b['kind'].lower(), b['name']), 'name': b['name'],
                              'kind': b['kind'], 'number': b['number'], 'interface': b['section'], 'instance': instance,
                              'f': b.get('f', False),
                              'element': block_template(b, instance[0] if instance else '')})
    tags = []
    for fpath in _latest(root, project, '.xml', 'Tags').values():
        tags += _read_cache(fpath, tag_table) or []
    import fbd                                       # lazy: fbd imports s7dcl
    return {'project': project, 'instruction': INSTRUCTIONS, 'blocks': res_blocks, 'fbd': fbd.PALETTE,
            'db': [{'name': d['name'], 'number': d['number'], 'instance_type': d['instance_type'],
                    'members': [] if d['instance_type'] else [c for v in d['section'].values() for c in v]} for d in dbs],
            'tags': sorted(tags, key=lambda t: t['name'].casefold())}


def element(cat, key):
    """Element template by palette key ('NO', 'TON', 'fb:FB_Belt', …) -> a copy; KeyError if it does not exist."""
    for x in cat['instruction'] + cat['blocks']:
        if x['key'] == key:
            return copy.deepcopy(x['element'])
    raise KeyError('palette element %s does not exist' % key)
