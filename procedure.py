"""Postup skládání úpravy v editoru TIA V21 (F2): z rozdílu před / po očíslované kroky česky.

Hlavička vždy `<druh><číslo> "<blok>" · NW <n>: <titulek>` (pravidlo Honzy 12. 10.). Názvy povelů jen ty ověřené
v lokální nápovědě TIA V21 (nota TIAC_Editor §5b, tabulka POVELY se zdrojem); co ověřené není, je slovy s ⟦neověřeno⟧.
"""
import model
import diff
import fbd

N = '⟦neověřeno⟧'
# povel -> (text do kroku, zdroj v nápovědě V21) — §5b noty
POVELY = {
    'insert_input': ('pravým klikem na poslední vstup → **Insert input** (`Ctrl+Shift+3`)',
                     'ProgBlockFUP „Adding additional inputs and outputs to FBD elements"'),
    'delete_input': ('vyber vstup → pravý klik → **Delete** (jde jen u přidaných vstupů)',
                     'ProgBlockFUP „Removing instruction inputs and outputs"'),
    'negace': ('vyber pin → **Create a ones complement** (`Ctrl+Shift+4`)',
               'ProgPLCProgramEditor „Using the keyboard in the program editor"'),
    'delete': ('pravý klik → **Delete**', 'ProgBlockFUP „Deleting FBD elements"'),
    'replace': ('klikni na trojúhelník v rohu boxu (seznam otevře i `Alt+F2`) a vyber', 'ProgBlockFUP „Replacing FBD elements"'),
    'and': ('klávesa `F9` (AND)', 'ProgPLCProgramEditor „Using the keyboard…"'),
    'or': ('klávesa `F10` (OR)', 'ProgPLCProgramEditor „Using the keyboard…"'),
    'assign': ('klávesa `Shift+F7` (assignment)', 'ProgPLCProgramEditor „Using the keyboard…"'),
    'empty_box': ('prázdný box `F8` a typ vyber trojúhelníkem vpravo nahoře', 'ProgBlockFUP „…using an empty box"'),
    'instructions': ('task card **Instructions** → Basic instructions → Bit logic operations, přetáhni',
                     'ProgBlockFUP „Inserting FBD elements using the Instructions task card"'),
    'call': ('z Project tree → Program blocks přetáhni blok do networku; v dialogu **Call options** zvol',
             'ProgBlockFUP „Inserting block calls using a drag-and-drop operation"'),
    'change_instance': ('pravý klik na volání → **Change instance**', 'ProgBlockFUP „Changing the instance type"'),
    'logic_path': ('vlož instrukci bez spojení se stávající (nová logická cesta)', 'ProgBlockFUP „Inserting a logic path"'),
}
NAHRADITELNE = [{'and', 'or', 'xor'}, {'sr', 'rs'}]


def _p(k):
    return POVELY[k][0]


def hlavicka(u):
    return '%s%s "%s" · NW %s: %s' % (u.get('druh') or '', u.get('cislo_bloku') if u.get('cislo_bloku') is not None else '',
                                      u['blok'], u['nw'], u.get('titulek') or '(bez titulku)')


def _c(x):
    return '`%s`' % x if x else '`…`'


# ---------- FBD ----------

def _box(p, M):
    """Popis boxu, podle kterého ho Honza v networku najde."""
    if p['typ'] in ('volani', 'instrukce'):
        return 'volání %s%s' % (_c(p.get('nazev')), ' (instance %s)' % _c(p['instance']) if p.get('instance') else '')
    if p['typ'] in fbd.S_OPERANDEM:
        return 'box %s s operandem %s' % (_c(fbd.TITUL[p['typ']]), _c(p.get('operand')))
    vst = []
    for v in p['vstupy']:
        if v.get('signal'):
            z = M.get(v['signal'])
            vst.append(('NOT ' if v.get('neg') else '') + ('výstup %s' % _c(fbd.TITUL.get(z['typ']) or z.get('nazev')) if z else 'signál'))
        else:
            vst.append(('NOT ' if v.get('neg') else '') + _c(v['operand']))
    return 'box %s (vstupy %s)' % (_c(fbd.TITUL[p['typ']]), ', '.join(vst))


def _velke(t):
    return t[:1].upper() + t[1:]


def _zdroj(v, M):
    if v.get('signal'):
        z = M.get(v['signal'])
        return 'výstup %s' % (_box(z, M) if z else 'boxu')
    return _c(v['operand'])


def _vlozeni(p, kde):
    t = p['typ']
    if t == 'volani' and p.get('volani') in ('fb', 'fc'):
        inst = ('' if not p.get('instance') else ' **Multi-instance** (jméno v rozhraní %s)' % _c(p['instance'][1:])
                if p['instance'].startswith('#') else ' **Single instance** (DB %s)' % _c(p['instance']))
        return 'Vlož volání %s %s: %s%s.' % (_c(p.get('nazev')), kde, _p('call') if inst else _p('call').split(';')[0], inst)
    if t in ('volani', 'instrukce'):
        return 'Vlož instrukci %s %s: z task card Instructions (složku najdi hledáním názvu %s), přetáhni.' % (
            _c(p.get('nazev')), kde, N)
    jak = {'and': _p('and'), 'or': _p('or'), 'assign': _p('assign')}.get(t) or (_p('empty_box') + ' (nebo ' + _p('instructions') + ')')
    return 'Vlož box %s %s: %s.' % (_c(fbd.TITUL[t]), kde, jak)


def _neg_krok(kde, neg):
    if neg:
        return '%s: neguj — %s.' % (kde, _p('negace'))
    return '%s: zruš negaci — %s (týž povel podruhé %s).' % (kde, _p('negace'), N)


def kroky_fbd(u):
    pred, po = u['pred'], u['po']
    P = {p['id']: p for p in pred['prvky']}
    Q = {p['id']: p for p in po['prvky']}
    kroky = []
    for i, p in P.items():                                                  # smazané
        if i not in Q:
            kroky.append('Vyber %s a smaž ho: %s.' % (_box(p, P), _p('delete')))
    for i, q in Q.items():                                                  # změněné (i nahrazené)
        p = P.get(i)
        if p is None or fbd._j(p) == fbd._j(q):
            continue
        jm = _velke(_box(p, P))
        if p['typ'] != q['typ']:
            if not any({p['typ'], q['typ']} <= s for s in NAHRADITELNE):
                kroky.append('Vyber %s a smaž ho: %s.' % (_box(p, P), _p('delete')))
                continue
            kroky.append('Vyber %s, %s %s (**Replace**; vstupy zůstanou).' % (_box(p, P), _p('replace'), _c(fbd.TITUL[q['typ']])))
        if (p.get('operand') or '') != (q.get('operand') or '') and q['typ'] in fbd.S_OPERANDEM:
            kroky.append('%s: přepiš operand nad boxem na %s.' % (jm, _c(q.get('operand'))))
        if q['typ'] == 'volani' and (p.get('instance') or '') != (q.get('instance') or ''):
            kroky.append('%s: změň instanci na %s — %s.' % (jm, _c(q.get('instance')), _p('change_instance')))
        pv, qv = p['vstupy'], q['vstupy']
        for n in range(1, min(len(pv), len(qv)) + 1):
            a, b = pv[n - 1], qv[n - 1]
            kde = '%s, vstup %d%s' % (jm, n, ' (' + b['pin'] + ')' if q['typ'] not in fbd.KOMUT and b['pin'] else '')
            if (a['operand'], a.get('signal')) != (b['operand'], b.get('signal')):
                if b.get('signal') and b['signal'] not in P:
                    pass                                                    # nový box se vloží níže přímo na vstup
                elif b.get('signal'):
                    kroky.append('%s napoj na %s — spoj tažením od výstupu ke vstupu %s.' % (kde, _zdroj(b, Q), N))
                else:
                    kroky.append('%s: napiš %s (místo %s).' % (kde, _c(b['operand']), _zdroj(a, P)))
            if bool(a.get('neg')) != bool(b.get('neg')):
                kroky.append(_neg_krok(kde, b.get('neg')))
        for n in range(len(pv), len(qv)):
            b = qv[n]
            novy = b.get('signal') and b['signal'] not in P
            kroky.append('%s: %s. Do nového vstupu %d %s.' % (
                jm, _p('insert_input'), n + 1, 'zatím nic nepiš — vloží se do něj nový box (další krok)' if novy
                else 'napiš %s' % _zdroj(b, Q) if not b.get('signal') else 'napoj %s %s' % (_zdroj(b, Q), N)))
            if b.get('neg'):
                kroky.append(_neg_krok('%s, nový vstup %d' % (jm, n + 1), True))
        for a in pv[len(qv):]:
            kroky.append('%s: odeber vstup %s — %s.' % (jm, _zdroj(a, P), _p('delete_input')))
        pw = {v['pin']: v for v in p.get('vystupy', [])}
        for b in q.get('vystupy', []):
            a = pw.get(b['pin'], {})
            if a.get('operand', '') != b['operand']:
                kroky.append('%s: na výstup %s napiš %s.' % (jm, _c(b['pin']), _c(b['operand'])))
            if bool(a.get('neg')) != bool(b.get('neg')):
                kroky.append(_neg_krok('%s, výstup %s' % (jm, b['pin']), b.get('neg')))
        if bool(p.get('neg_vystup')) != bool(q.get('neg_vystup')):
            kroky.append(_neg_krok('%s, hlavní výstup' % jm, q.get('neg_vystup')))
    # přidané: od spotřebiče k zdrojům (box se pokládá na vstup, který bude napájet)
    spotr = {}
    for q in po['prvky']:
        for v in q['vstupy']:
            if v.get('signal'):
                spotr.setdefault(v['signal'], (q, v))
    for q in po['prvky']:
        if q['id'] in P:
            continue
        c = spotr.get(q['id'])
        prevzato = None
        if c is None:
            kde = 'jako novou logickou cestu (%s)' % _p('logic_path')
        else:
            ci, cv = c
            kde = 'na vstup %d%s %s' % (cv['n'], ' (' + cv['pin'] + ')' if ci['typ'] not in fbd.KOMUT and cv['pin'] else '',
                                        _box(P.get(ci['id'], ci), P if ci['id'] in P else Q).replace('box ', 'boxu ', 1))
            if q['vstupy'] and ci['id'] in P:
                puv = next((v for v in P[ci['id']]['vstupy'] if v['n'] == cv['n']), None)
                if puv and (puv['operand'] or puv.get('signal')):
                    kde += ' (box převezme dosavadní %s na svůj 1. vstup %s)' % (_zdroj(puv, P), N)
                    prevzato = (puv['operand'], puv.get('signal'), bool(puv.get('neg')))
        kroky.append(_vlozeni(q, kde))
        jm = 'Nový box %s' % _c(fbd.TITUL.get(q['typ']) or q.get('nazev'))
        if q['typ'] in fbd.S_OPERANDEM:
            kroky.append('%s: nad box napiš operand %s.' % (jm, _c(q.get('operand'))))
        for v in q['vstupy']:
            if v['pin'] == 'EN' and not v['operand'] and not v.get('signal'):
                continue
            if v['n'] == 1 and prevzato == (v['operand'], v.get('signal'), bool(v.get('neg'))):
                continue                                                    # převzato při vložení
            kde = '%s, vstup %d%s' % (jm, v['n'], ' (' + v['pin'] + ')' if q['typ'] not in fbd.KOMUT else '')
            if v.get('signal'):
                if v['signal'] in P:
                    kroky.append('%s napoj na %s %s.' % (kde, _zdroj(v, Q), N))
            elif v['operand']:
                kroky.append('%s: napiš %s.' % (kde, _c(v['operand'])))
            if v.get('neg'):
                kroky.append(_neg_krok(kde, True))
        for v in q.get('vystupy', []):
            if v['operand']:
                kroky.append('%s: na výstup %s napiš %s.' % (jm, _c(v['pin']), _c(v['operand'])))
    return kroky


# ---------- LAD ----------

def kroky_lad(u):
    pred, po = u['pred'], u['po']
    zmeny, smazane = diff.porovnej(pred, po)
    P = {p['id']: p for p in pred['prvky']}
    Q = {p['id']: p for p in po['prvky']}
    kroky = ['Vyber prvek %s a smaž ho (`Delete`) %s.' % (_c(model._jmeno(P[i])), N) for i in smazane]
    for i, z in zmeny.items():
        q = Q[i]
        if z == 'zmeneny':
            p = P[i]
            pole = [k for k in ('operand', 'bit', 'in1', 'in2', 'op', 'instance', 'pod', 'sablona') if p.get(k) != q.get(k)]
            zm = ['%s %s → %s' % (k, _c(p.get(k)), _c(q.get(k))) for k in pole]
            pw = {x['pin']: x['operand'] for x in p.get('vstupy', []) + p.get('vystupy', [])}
            zm += ['pin %s → %s' % (x['pin'], _c(x['operand'])) for x in q.get('vstupy', []) + q.get('vystupy', [])
                   if pw.get(x['pin']) != x['operand']]
            kroky.append('U prvku %s změň: %s.' % (_c(model._jmeno(p)), ', '.join(zm)))
            continue
        kde = 'na konec rungu'
        for ri, r in enumerate(po['rungy']):
            if i in r['cesta']:
                pred_it = [x for x in r['cesta'][:r['cesta'].index(i)] if not x.startswith('wire#')]
                kde = ('za prvek %s' % _c(model._jmeno(Q[pred_it[-1]]))) if pred_it else 'na začátek rungu %d' % (ri + 1)
                if r['do'] is not None:
                    kde += ' v paralelní větvi'
                break
        kroky.append('Vlož %s %s: z task card Instructions přetáhni na vodič %s.' % (_c(model._jmeno(q)), kde, N))
    return kroky


def procedure(u):
    """-> {hlavicka, kroky, markdown} pro úpravu (LAD i FBD)."""
    hl = hlavicka(u)
    vnitrek = kroky_fbd(u) if fbd.je_fbd(u['po']) else kroky_lad(u)
    kroky = ['Otevři blok %s a najdi network **NW %s: %s**.' % (
        _c('%s%s "%s"' % (u.get('druh') or '', u.get('cislo_bloku') or '', u['blok'])), u['nw'], u.get('titulek') or '')]
    kroky += vnitrek or ['Návrh se od exportu neliší — v TIA není co měnit.']
    if u.get('f') and vnitrek:
        kroky.append('Přelož safety program a zkontroluj F-signaturu; změny safety chrání heslo F-CPU / '
                     '**Safety Administration** (přesný název příkazu kompilace %s).' % N)
    kroky.append('Ulož projekt, vyexportuj blok Readerem a spusť `python kontrola.py --uprava %s` '
                 '(nebo tlačítko „Zkontrolovat exportem").' % u.get('id', ''))
    md = '### Postup pro TIA — %s\n' % hl + ''.join('%d. %s\n' % (i + 1, k) for i, k in enumerate(kroky))
    if any(N in k for k in kroky):
        md += '\n%s = v nápovědě TIA V21 neověřeno, ověř v editoru.\n' % N
    return {'hlavicka': hl, 'kroky': kroky, 'markdown': md, 'zdroje': {k: v[1] for k, v in POVELY.items()}}
