"""TIA steps for an edit in the TIA V21 editor (F2): numbered steps derived from the before / after difference.

The header is always `<kind><number> "<block>" · NW <n>: <title>`. Command names are only those verified in the local
TIA V21 help (table COMMANDS with the source); anything unverified is described in words and marked ⟦unverified⟧.
"""
import model
import diff
import fbd

N = '⟦unverified⟧'
# command -> (text for the step, source in the V21 help)
COMMANDS = {
    'insert_input': ('right-click the last input → **Insert input** (`Ctrl+Shift+3`)',
                     'ProgBlockFUP "Adding additional inputs and outputs to FBD elements"'),
    'delete_input': ('select the input → right-click → **Delete** (only possible for added inputs)',
                     'ProgBlockFUP "Removing instruction inputs and outputs"'),
    'negate': ('select the pin → **Create a ones complement** (`Ctrl+Shift+4`)',
               'ProgPLCProgramEditor "Using the keyboard in the program editor"'),
    'delete': ('right-click → **Delete**', 'ProgBlockFUP "Deleting FBD elements"'),
    'replace': ('click the triangle in the corner of the box (`Alt+F2` also opens the list) and select',
                'ProgBlockFUP "Replacing FBD elements"'),
    'and': ('key `F9` (AND)', 'ProgPLCProgramEditor "Using the keyboard…"'),
    'or': ('key `F10` (OR)', 'ProgPLCProgramEditor "Using the keyboard…"'),
    'assign': ('key `Shift+F7` (assignment)', 'ProgPLCProgramEditor "Using the keyboard…"'),
    'empty_box': ('empty box `F8`, then select the type with the triangle at the top right', 'ProgBlockFUP "…using an empty box"'),
    'instructions': ('**Instructions** task card → Basic instructions → Bit logic operations, drag it in',
                     'ProgBlockFUP "Inserting FBD elements using the Instructions task card"'),
    'call': ('drag the block from Project tree → Program blocks into the network; in the **Call options** dialog choose',
             'ProgBlockFUP "Inserting block calls using a drag-and-drop operation"'),
    'change_instance': ('right-click the call → **Change instance**', 'ProgBlockFUP "Changing the instance type"'),
    'logic_path': ('insert the instruction without connecting it to the existing logic (new logic path)',
                   'ProgBlockFUP "Inserting a logic path"'),
}
REPLACEABLE = [{'and', 'or', 'xor'}, {'sr', 'rs'}]


def _p(k):
    return COMMANDS[k][0]


def block_name(u):
    return '%s%s "%s"' % (u.get('block_kind') or '', u.get('block_number') if u.get('block_number') is not None else '', u['block'])


def where(u):
    """What the edit changes, as the tree and the changelog show it."""
    if u.get('kind') == 'interface':
        return 'Interface: +%d %s' % (len(u.get('members') or []), u.get('section'))
    if u.get('kind') == 'new_network':
        return 'NW %s+ (new): %s' % (u.get('after_nw'), u.get('title') or '(no title)')
    return 'NW %s: %s' % (u['nw'], u.get('title') or '(no title)')


def header(u):
    return '%s · %s' % (block_name(u), where(u))


def _c(x):
    return '`%s`' % x if x else '`…`'


# ---------- FBD ----------

def _box(p, M):
    """Description of the box by which the user finds it in the network."""
    if p['type'] in ('call', 'instruction'):
        return 'call %s%s' % (_c(p.get('name')), ' (instance %s)' % _c(p['instance']) if p.get('instance') else '')
    if p['type'] in fbd.WITH_OPERAND:
        return 'box %s with operand %s' % (_c(fbd.SYMBOL[p['type']]), _c(p.get('operand')))
    inp = []
    for v in p['inputs']:
        if v.get('signal'):
            z = M.get(v['signal'])
            inp.append(('NOT ' if v.get('neg') else '') + ('output of %s' % _c(fbd.SYMBOL.get(z['type']) or z.get('name')) if z else 'signal'))
        else:
            inp.append(('NOT ' if v.get('neg') else '') + _c(v['operand']))
    return 'box %s (inputs %s)' % (_c(fbd.SYMBOL[p['type']]), ', '.join(inp))


def _capitalize(t):
    return t[:1].upper() + t[1:]


def _source(v, M):
    if v.get('signal'):
        z = M.get(v['signal'])
        return 'the output of %s' % (_box(z, M) if z else 'box')
    return _c(v['operand'])


def _insertion(p, where):
    t = p['type']
    if t == 'call' and p.get('call') in ('fb', 'fc'):
        inst = ('' if not p.get('instance') else ' **Multi-instance** (name in the interface %s)' % _c(p['instance'][1:])
                if p['instance'].startswith('#') else ' **Single instance** (DB %s)' % _c(p['instance']))
        return 'Insert the call %s %s: %s%s.' % (_c(p.get('name')), where, _p('call') if inst else _p('call').split(';')[0], inst)
    if t in ('call', 'instruction'):
        return 'Insert the instruction %s %s: from the Instructions task card (find the folder by searching for %s), drag it in.' % (
            _c(p.get('name')), where, N)
    how = {'and': _p('and'), 'or': _p('or'), 'assign': _p('assign')}.get(t) or (_p('empty_box') + ' (or ' + _p('instructions') + ')')
    return 'Insert box %s %s: %s.' % (_c(fbd.SYMBOL[t]), where, how)


def _neg_step(where, neg):
    if neg:
        return '%s: negate — %s.' % (where, _p('negate'))
    return '%s: remove the negation — %s (the same command again %s).' % (where, _p('negate'), N)


def steps_fbd(u):
    before, after = u['before'], u['after']
    P = {p['id']: p for p in before['elements']}
    Q = {p['id']: p for p in after['elements']}
    steps = []
    for i, p in P.items():                                                  # deleted
        if i not in Q:
            steps.append('Select %s and delete it: %s.' % (_box(p, P), _p('delete')))
    for i, q in Q.items():                                                  # changed (including replaced)
        p = P.get(i)
        if p is None or fbd._j(p) == fbd._j(q):
            continue
        nm = _capitalize(_box(p, P))
        if p['type'] != q['type']:
            if not any({p['type'], q['type']} <= s for s in REPLACEABLE):
                steps.append('Select %s and delete it: %s.' % (_box(p, P), _p('delete')))
                continue
            steps.append('Select %s, %s %s (**Replace**; the inputs are kept).' % (_box(p, P), _p('replace'), _c(fbd.SYMBOL[q['type']])))
        if (p.get('operand') or '') != (q.get('operand') or '') and q['type'] in fbd.WITH_OPERAND:
            steps.append('%s: change the operand above the box to %s.' % (nm, _c(q.get('operand'))))
        if q['type'] == 'call' and (p.get('instance') or '') != (q.get('instance') or ''):
            steps.append('%s: change the instance to %s — %s.' % (nm, _c(q.get('instance')), _p('change_instance')))
        pv, qv = p['inputs'], q['inputs']
        for n in range(1, min(len(pv), len(qv)) + 1):
            a, b = pv[n - 1], qv[n - 1]
            where = '%s, input %d%s' % (nm, n, ' (' + b['pin'] + ')' if q['type'] not in fbd.COMMUTATIVE and b['pin'] else '')
            if (a['operand'], a.get('signal')) != (b['operand'], b.get('signal')):
                if b.get('signal') and b['signal'] not in P:
                    pass                                                    # the new box is inserted directly on the input below
                elif b.get('signal'):
                    steps.append('%s: connect to %s — drag from the output to the input %s.' % (where, _source(b, Q), N))
                else:
                    steps.append('%s: type %s (instead of %s).' % (where, _c(b['operand']), _source(a, P)))
            if bool(a.get('neg')) != bool(b.get('neg')):
                steps.append(_neg_step(where, b.get('neg')))
        for n in range(len(pv), len(qv)):
            b = qv[n]
            new_value = b.get('signal') and b['signal'] not in P
            steps.append('%s: %s. In the new input %d %s.' % (
                nm, _p('insert_input'), n + 1, 'type nothing yet — a new box is inserted on it (next step)' if new_value
                else 'type %s' % _source(b, Q) if not b.get('signal') else 'connect %s %s' % (_source(b, Q), N)))
            if b.get('neg'):
                steps.append(_neg_step('%s, new input %d' % (nm, n + 1), True))
        for a in pv[len(qv):]:
            steps.append('%s: remove the input %s — %s.' % (nm, _source(a, P), _p('delete_input')))
        pw = {v['pin']: v for v in p.get('outputs', [])}
        for b in q.get('outputs', []):
            a = pw.get(b['pin'], {})
            if a.get('operand', '') != b['operand']:
                steps.append('%s: at output %s type %s.' % (nm, _c(b['pin']), _c(b['operand'])))
            if bool(a.get('neg')) != bool(b.get('neg')):
                steps.append(_neg_step('%s, output %s' % (nm, b['pin']), b.get('neg')))
        if bool(p.get('neg_output')) != bool(q.get('neg_output')):
            steps.append(_neg_step('%s, main output' % nm, q.get('neg_output')))
    # added: from the consumer to the sources (a box is placed on the input it will feed)
    consumers = {}
    for q in after['elements']:
        for v in q['inputs']:
            if v.get('signal'):
                consumers.setdefault(v['signal'], (q, v))
    for q in after['elements']:
        if q['id'] in P:
            continue
        c = consumers.get(q['id'])
        taken = None
        if c is None:
            where = 'as a new logic path (%s)' % _p('logic_path')
        else:
            ci, cv = c
            where = 'on input %d%s of %s' % (cv['n'], ' (' + cv['pin'] + ')' if ci['type'] not in fbd.COMMUTATIVE and cv['pin'] else '',
                                             _box(P.get(ci['id'], ci), P if ci['id'] in P else Q).replace('box ', 'box ', 1))
            if q['inputs'] and ci['id'] in P:
                orig = next((v for v in P[ci['id']]['inputs'] if v['n'] == cv['n']), None)
                if orig and (orig['operand'] or orig.get('signal')):
                    where += ' (the box takes over %s on its input 1 %s)' % (_source(orig, P), N)
                    taken = (orig['operand'], orig.get('signal'), bool(orig.get('neg')))
        steps.append(_insertion(q, where))
        nm = 'New box %s' % _c(fbd.SYMBOL.get(q['type']) or q.get('name'))
        if q['type'] in fbd.WITH_OPERAND:
            steps.append('%s: type the operand %s above the box.' % (nm, _c(q.get('operand'))))
        for v in q['inputs']:
            if v['pin'] == 'EN' and not v['operand'] and not v.get('signal'):
                continue
            if v['n'] == 1 and taken == (v['operand'], v.get('signal'), bool(v.get('neg'))):
                continue                                                    # taken over on insertion
            where = '%s, input %d%s' % (nm, v['n'], ' (' + v['pin'] + ')' if q['type'] not in fbd.COMMUTATIVE else '')
            if v.get('signal'):
                if v['signal'] in P:
                    steps.append('%s: connect to %s %s.' % (where, _source(v, Q), N))
            elif v['operand']:
                steps.append('%s: type %s.' % (where, _c(v['operand'])))
            if v.get('neg'):
                steps.append(_neg_step(where, True))
        for v in q.get('outputs', []):
            if v['operand']:
                steps.append('%s: at output %s type %s.' % (nm, _c(v['pin']), _c(v['operand'])))
    return steps


# ---------- LAD ----------

def steps_lad(u):
    before, after = u['before'], u['after']
    changes, deleted = diff.compare(before, after)
    P = {p['id']: p for p in before['elements']}
    Q = {p['id']: p for p in after['elements']}
    steps = ['Select the element %s and delete it (`Delete`) %s.' % (_c(model._display_name(P[i])), N) for i in deleted]
    for i, z in changes.items():
        q = Q[i]
        if z == 'changed':
            p = P[i]
            field = [k for k in ('operand', 'bit', 'in1', 'in2', 'op', 'instance', 'below', 'template') if p.get(k) != q.get(k)]
            chg = ['%s %s → %s' % (k, _c(p.get(k)), _c(q.get(k))) for k in field]
            pw = {x['pin']: x['operand'] for x in p.get('inputs', []) + p.get('outputs', [])}
            chg += ['pin %s → %s' % (x['pin'], _c(x['operand'])) for x in q.get('inputs', []) + q.get('outputs', [])
                   if pw.get(x['pin']) != x['operand']]
            steps.append('On the element %s change: %s.' % (_c(model._display_name(p)), ', '.join(chg)))
            continue
        where = 'at the end of the rung'
        for ri, r in enumerate(after['rungs']):
            if i in r['path']:
                before_it = [x for x in r['path'][:r['path'].index(i)] if not x.startswith('wire#')]
                where = ('after the element %s' % _c(model._display_name(Q[before_it[-1]]))) if before_it else 'at the start of rung %d' % (ri + 1)
                if r['to'] is not None:
                    where += ' in a parallel branch'
                break
        steps.append('Insert %s %s: drag it from the Instructions task card onto the wire %s.' % (_c(model._display_name(q)), where, N))
    return steps


SECTION_TEXT = {'Input': 'Input', 'Output': 'Output', 'InOut': 'InOut', 'Static': 'Static', 'Temp': 'Temp',
                'Constant': 'Constant'}


def member_text(m):
    return '`%s : %s%s`%s' % (m['name'], m['type'], ' := %s' % m['initial'] if m.get('initial') else '',
                              ' (%s)' % m['comment'] if m.get('comment') else '')


def steps_interface(u):
    sec = SECTION_TEXT.get(u.get('section'), u.get('section'))
    return ['Open the block interface → section **%s** → add %s.' % (sec, member_text(m)) for m in u.get('members') or []]


def procedure(u):
    """-> {header, steps, markdown} for an edit (network change, interface members)."""
    hl = header(u)
    if u.get('kind') == 'interface':
        inner = steps_interface(u)
        steps = ['Open the block %s (the interface is the upper part of the program editor).' % _c(block_name(u))]
    else:
        inner = steps_fbd(u) if fbd.is_fbd(u['after']) else steps_lad(u)
        steps = ['Open the block %s and find network **NW %s: %s**.' % (_c(block_name(u)), u['nw'], u.get('title') or '')]
    steps += inner or ['The proposal does not differ from the export — nothing to change in TIA.']
    if u.get('f') and inner:
        steps.append('Compile the safety program and check the F-signature; safety changes are protected by the F-CPU password / '
                     '**Safety Administration** (exact name of the compile command %s).' % N)
    steps.append('Save the project, export the block with the Reader and run `python check.py --edit %s` '
                 '(or the "Check export" button).' % u.get('id', ''))
    md = '### TIA steps — %s\n' % hl + ''.join('%d. %s\n' % (i + 1, k) for i, k in enumerate(steps))
    if any(N in k for k in steps):
        md += '\n%s = not verified in the TIA V21 help, check it in the editor.\n' % N
    return {'header': hl, 'steps': steps, 'markdown': md, 'sources': {k: v[1] for k, v in COMMANDS.items()}}
