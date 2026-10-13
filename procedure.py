"""Postup skládání úpravy v editoru TIA V21 (F2): z rozdílu před / po očíslované kroky česky.

Hlavička vždy `<druh><číslo> "<blok>" · NW <n>: <titulek>` (pravidlo Honzy 12. 10.). Názvy povelů jen ty ověřené
v lokální nápovědě TIA V21 (nota TIAC_Editor §5b, tabulka POVELY se zdrojem); co ověřené není, je slovy s ⟦neověřeno⟧.
"""
import model
import diff
import fbd

N = '⟦neověřeno⟧'
# povel -> (text do kroku, zdroj v nápovědě V21) — §5b noty
COMMANDS = {
    'insert_input': ('pravým klikem on poslední input → **Insert input** (`Ctrl+Shift+3`)',
                     'ProgBlockFUP „Adding additional inputs and outputs to FBD elements"'),
    'delete_input': ('select input → pravý click → **Delete** (jde only u přidaných vstupů)',
                     'ProgBlockFUP „Removing instruction inputs and outputs"'),
    'negate': ('select pin → **Create a ones complement** (`Ctrl+Shift+4`)',
               'ProgPLCProgramEditor „Using the keyboard in the program editor"'),
    'delete': ('pravý click → **Delete**', 'ProgBlockFUP „Deleting FBD elements"'),
    'replace': ('klikni on trojúhelník v rohu box (list otevře i `Alt+F2`) a select', 'ProgBlockFUP „Replacing FBD elements"'),
    'and': ('klávesa `F9` (AND)', 'ProgPLCProgramEditor „Using the keyboard…"'),
    'or': ('klávesa `F10` (OR)', 'ProgPLCProgramEditor „Using the keyboard…"'),
    'assign': ('klávesa `Shift+F7` (assignment)', 'ProgPLCProgramEditor „Using the keyboard…"'),
    'empty_box': ('prázdný box `F8` a type select trojúhelníkem right nahoře', 'ProgBlockFUP „…using an empty box"'),
    'instructions': ('task card **Instructions** → Basic instructions → Bit logic operations, přetáhni',
                     'ProgBlockFUP „Inserting FBD elements using the Instructions task card"'),
    'call': ('z Project tree → Program blocks přetáhni block do network; v dialogu **Call options** zvol',
             'ProgBlockFUP „Inserting block calls using a drag-and-drop operation"'),
    'change_instance': ('pravý click on volání → **Change instance**', 'ProgBlockFUP „Changing the instance type"'),
    'logic_path': ('vlož instruction without spojení se stávající (nová logická path)', 'ProgBlockFUP „Inserting a logic path"'),
}
REPLACEABLE = [{'and', 'or', 'xor'}, {'sr', 'rs'}]


def _p(k):
    return COMMANDS[k][0]


def header(u):
    return '%s%s "%s" · NW %s: %s' % (u.get('kind') or '', u.get('block_number') if u.get('block_number') is not None else '',
                                      u['block'], u['nw'], u.get('title') or '(without title)')


def _c(x):
    return '`%s`' % x if x else '`…`'


# ---------- FBD ----------

def _box(p, M):
    """Popis boxu, podle kterého ho Honza v networku najde."""
    if p['type'] in ('call', 'instruction'):
        return 'volání %s%s' % (_c(p.get('name')), ' (instance %s)' % _c(p['instance']) if p.get('instance') else '')
    if p['type'] in fbd.WITH_OPERAND:
        return 'box %s s operand %s' % (_c(fbd.SYMBOL[p['type']]), _c(p.get('operand')))
    inp = []
    for v in p['inputs']:
        if v.get('signal'):
            z = M.get(v['signal'])
            inp.append(('NOT ' if v.get('neg') else '') + ('výstup %s' % _c(fbd.SYMBOL.get(z['type']) or z.get('name')) if z else 'signál'))
        else:
            inp.append(('NOT ' if v.get('neg') else '') + _c(v['operand']))
    return 'box %s (inputs %s)' % (_c(fbd.SYMBOL[p['type']]), ', '.join(inp))


def _capitalize(t):
    return t[:1].upper() + t[1:]


def _source(v, M):
    if v.get('signal'):
        z = M.get(v['signal'])
        return 'výstup %s' % (_box(z, M) if z else 'box')
    return _c(v['operand'])


def _insertion(p, where):
    t = p['type']
    if t == 'call' and p.get('call') in ('fb', 'fc'):
        inst = ('' if not p.get('instance') else ' **Multi-instance** (jméno v rozhraní %s)' % _c(p['instance'][1:])
                if p['instance'].startswith('#') else ' **Single instance** (DB %s)' % _c(p['instance']))
        return 'Vlož volání %s %s: %s%s.' % (_c(p.get('name')), where, _p('call') if inst else _p('call').split(';')[0], inst)
    if t in ('call', 'instruction'):
        return 'Vlož instruction %s %s: z task card Instructions (složku find hledáním názvu %s), přetáhni.' % (
            _c(p.get('name')), where, N)
    how = {'and': _p('and'), 'or': _p('or'), 'assign': _p('assign')}.get(t) or (_p('empty_box') + ' (nebo ' + _p('instructions') + ')')
    return 'Vlož box %s %s: %s.' % (_c(fbd.SYMBOL[t]), where, how)


def _neg_step(where, neg):
    if neg:
        return '%s: neguj — %s.' % (where, _p('negate'))
    return '%s: zruš negation — %s (týž command podruhé %s).' % (where, _p('negate'), N)


def steps_fbd(u):
    before, after = u['before'], u['after']
    P = {p['id']: p for p in before['elements']}
    Q = {p['id']: p for p in after['elements']}
    steps = []
    for i, p in P.items():                                                  # smazané
        if i not in Q:
            steps.append('Select %s a smaž ho: %s.' % (_box(p, P), _p('delete')))
    for i, q in Q.items():                                                  # změněné (i nahrazené)
        p = P.get(i)
        if p is None or fbd._j(p) == fbd._j(q):
            continue
        nm = _capitalize(_box(p, P))
        if p['type'] != q['type']:
            if not any({p['type'], q['type']} <= s for s in REPLACEABLE):
                steps.append('Select %s a smaž ho: %s.' % (_box(p, P), _p('delete')))
                continue
            steps.append('Select %s, %s %s (**Replace**; inputs zůstanou).' % (_box(p, P), _p('replace'), _c(fbd.SYMBOL[q['type']])))
        if (p.get('operand') or '') != (q.get('operand') or '') and q['type'] in fbd.WITH_OPERAND:
            steps.append('%s: přepiš operand above box on %s.' % (nm, _c(q.get('operand'))))
        if q['type'] == 'call' and (p.get('instance') or '') != (q.get('instance') or ''):
            steps.append('%s: změň instances on %s — %s.' % (nm, _c(q.get('instance')), _p('change_instance')))
        pv, qv = p['inputs'], q['inputs']
        for n in range(1, min(len(pv), len(qv)) + 1):
            a, b = pv[n - 1], qv[n - 1]
            where = '%s, input %d%s' % (nm, n, ' (' + b['pin'] + ')' if q['type'] not in fbd.COMMUTATIVE and b['pin'] else '')
            if (a['operand'], a.get('signal')) != (b['operand'], b.get('signal')):
                if b.get('signal') and b['signal'] not in P:
                    pass                                                    # nový box se vloží níže přímo na vstup
                elif b.get('signal'):
                    steps.append('%s connect on %s — connect tažením od výstupu ke input %s.' % (where, _source(b, Q), N))
                else:
                    steps.append('%s: napiš %s (místo %s).' % (where, _c(b['operand']), _source(a, P)))
            if bool(a.get('neg')) != bool(b.get('neg')):
                steps.append(_neg_step(where, b.get('neg')))
        for n in range(len(pv), len(qv)):
            b = qv[n]
            new_value = b.get('signal') and b['signal'] not in P
            steps.append('%s: %s. Do nového input %d %s.' % (
                nm, _p('insert_input'), n + 1, 'zatím nothing nepiš — vloží se do něj nový box (další step)' if new_value
                else 'napiš %s' % _source(b, Q) if not b.get('signal') else 'connect %s %s' % (_source(b, Q), N)))
            if b.get('neg'):
                steps.append(_neg_step('%s, nový input %d' % (nm, n + 1), True))
        for a in pv[len(qv):]:
            steps.append('%s: remove input %s — %s.' % (nm, _source(a, P), _p('delete_input')))
        pw = {v['pin']: v for v in p.get('outputs', [])}
        for b in q.get('outputs', []):
            a = pw.get(b['pin'], {})
            if a.get('operand', '') != b['operand']:
                steps.append('%s: on výstup %s napiš %s.' % (nm, _c(b['pin']), _c(b['operand'])))
            if bool(a.get('neg')) != bool(b.get('neg')):
                steps.append(_neg_step('%s, výstup %s' % (nm, b['pin']), b.get('neg')))
        if bool(p.get('neg_output')) != bool(q.get('neg_output')):
            steps.append(_neg_step('%s, hlavní výstup' % nm, q.get('neg_output')))
    # přidané: od spotřebiče k zdrojům (box se pokládá na vstup, který bude napájet)
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
            where = 'as novou logic path (%s)' % _p('logic_path')
        else:
            ci, cv = c
            where = 'on input %d%s %s' % (cv['n'], ' (' + cv['pin'] + ')' if ci['type'] not in fbd.COMMUTATIVE and cv['pin'] else '',
                                        _box(P.get(ci['id'], ci), P if ci['id'] in P else Q).replace('box ', 'box ', 1))
            if q['inputs'] and ci['id'] in P:
                orig = next((v for v in P[ci['id']]['inputs'] if v['n'] == cv['n']), None)
                if orig and (orig['operand'] or orig.get('signal')):
                    where += ' (box převezme dosavadní %s on svůj 1. input %s)' % (_source(orig, P), N)
                    taken = (orig['operand'], orig.get('signal'), bool(orig.get('neg')))
        steps.append(_insertion(q, where))
        nm = 'Nový box %s' % _c(fbd.SYMBOL.get(q['type']) or q.get('name'))
        if q['type'] in fbd.WITH_OPERAND:
            steps.append('%s: above box napiš operand %s.' % (nm, _c(q.get('operand'))))
        for v in q['inputs']:
            if v['pin'] == 'EN' and not v['operand'] and not v.get('signal'):
                continue
            if v['n'] == 1 and taken == (v['operand'], v.get('signal'), bool(v.get('neg'))):
                continue                                                    # převzato při vložení
            where = '%s, input %d%s' % (nm, v['n'], ' (' + v['pin'] + ')' if q['type'] not in fbd.COMMUTATIVE else '')
            if v.get('signal'):
                if v['signal'] in P:
                    steps.append('%s connect on %s %s.' % (where, _source(v, Q), N))
            elif v['operand']:
                steps.append('%s: napiš %s.' % (where, _c(v['operand'])))
            if v.get('neg'):
                steps.append(_neg_step(where, True))
        for v in q.get('outputs', []):
            if v['operand']:
                steps.append('%s: on výstup %s napiš %s.' % (nm, _c(v['pin']), _c(v['operand'])))
    return steps


# ---------- LAD ----------

def steps_lad(u):
    before, after = u['before'], u['after']
    changes, deleted = diff.compare(before, after)
    P = {p['id']: p for p in before['elements']}
    Q = {p['id']: p for p in after['elements']}
    steps = ['Select element %s a smaž ho (`Delete`) %s.' % (_c(model._display_name(P[i])), N) for i in deleted]
    for i, z in changes.items():
        q = Q[i]
        if z == 'changed':
            p = P[i]
            field = [k for k in ('operand', 'bit', 'in1', 'in2', 'op', 'instance', 'below', 'template') if p.get(k) != q.get(k)]
            chg = ['%s %s → %s' % (k, _c(p.get(k)), _c(q.get(k))) for k in field]
            pw = {x['pin']: x['operand'] for x in p.get('inputs', []) + p.get('outputs', [])}
            chg += ['pin %s → %s' % (x['pin'], _c(x['operand'])) for x in q.get('inputs', []) + q.get('outputs', [])
                   if pw.get(x['pin']) != x['operand']]
            steps.append('U element %s změň: %s.' % (_c(model._display_name(p)), ', '.join(chg)))
            continue
        where = 'on end rung'
        for ri, r in enumerate(after['rungs']):
            if i in r['path']:
                before_it = [x for x in r['path'][:r['path'].index(i)] if not x.startswith('wire#')]
                where = ('after element %s' % _c(model._display_name(Q[before_it[-1]]))) if before_it else 'on začátek rung %d' % (ri + 1)
                if r['to'] is not None:
                    where += ' v paralelní větvi'
                break
        steps.append('Vlož %s %s: z task card Instructions přetáhni on vodič %s.' % (_c(model._display_name(q)), where, N))
    return steps


def procedure(u):
    """-> {hlavicka, kroky, markdown} pro úpravu (LAD i FBD)."""
    hl = header(u)
    inner = steps_fbd(u) if fbd.is_fbd(u['after']) else steps_lad(u)
    steps = ['Otevři block %s a find network **NW %s: %s**.' % (
        _c('%s%s "%s"' % (u.get('kind') or '', u.get('block_number') or '', u['block'])), u['nw'], u.get('title') or '')]
    steps += inner or ['Návrh se od export neliší — v TIA není co měnit.']
    if u.get('f') and inner:
        steps.append('Přelož safety program a zkontroluj F-signaturu; změny safety chrání heslo F-CPU / '
                     '**Safety Administration** (přesný název příkazu kompilace %s).' % N)
    steps.append('Ulož project, vyexportuj block Readerem a spusť `python check.py --edit %s` '
                 '(nebo tlačítko „Zkontrolovat export").' % u.get('id', ''))
    md = '### Procedure for TIA — %s\n' % hl + ''.join('%d. %s\n' % (i + 1, k) for i, k in enumerate(steps))
    if any(N in k for k in steps):
        md += '\n%s = v nápovědě TIA V21 neověřeno, ověř v editor.\n' % N
    return {'header': hl, 'steps': steps, 'markdown': md, 'sources': {k: v[1] for k, v in COMMANDS.items()}}
