"""TIAC_Editor — lokální server (Python stdlib). M1 prohlížeč networků z exportu Readeru, M2 strom úprav a stavy.

python server.py --port 8765 --export-root "C:\\TIA\\export" --data "<dir>"
"""
import argparse
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import fbd
import catalog
import layout
import mcp_client
import model
import procedure
import diff
import s7dcl
import s7dcl_write
import state

TS = re.compile(r'^\d{8}-\d{6}$')
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')


def export_index(root):
    """{projekt: {ts: {blok: cesta}}} — jen časové složky, které obsahují .s7dcl."""
    res = {}
    if not os.path.isdir(root):
        return res
    for project in sorted(os.listdir(root)):
        pd = os.path.join(root, project)
        if not os.path.isdir(pd):
            continue
        for ts in sorted(os.listdir(pd)):
            if not TS.match(ts):
                continue
            blocks = {}
            for dp, _, files in os.walk(os.path.join(pd, ts)):
                for f in files:
                    if f.endswith('.s7dcl'):
                        blocks.setdefault(f[:-6], os.path.join(dp, f))
            if blocks:
                res.setdefault(project, {})[ts] = blocks
    return res


def find_block(root, project, block, ts=None):
    """-> (ts, cesta, bloky) nejnovějšího exportu s blokem; bloky = nejnovější kopie všech bloků projektu do ts
    (dílčí export pak dohledá instanční DB i FC ze staršího). Jména jen z indexu (žádné skládání cest z URL)."""
    exp = export_index(root).get(project)
    if not exp:
        raise KeyError('unknown project')
    for t in sorted(exp, reverse=True):
        if (ts is None or t == ts) and block in exp[t]:
            blocks = {}
            for x in sorted(exp):
                if x <= t:
                    blocks.update(exp[x])
            return t, exp[t][block], blocks
    raise KeyError('block not in the export')


def _load(root, project, block, ts=None):
    t, fpath, blocks = find_block(root, project, block, ts)
    b = s7dcl.load_block(fpath)

    fc = set()

    def find_db(display_name):
        c = blocks.get(display_name)
        if c:
            try:
                x = s7dcl.parse_block(s7dcl.read(c))
            except s7dcl.SourceParseError:
                return None
            if x['kind'] == 'FC':
                fc.add(display_name)
            return x.get('instance_type')
    s7dcl.fill_fb_types(b, find_db)
    for nw in b['networks']:                          # "FC"(…) je volání FC, ne instance FB
        for p in nw.get('elements', []):
            if p.get('call') == 'fb' and (p['instance'] or '').strip('"') in fc:
                p.update(call='fc', name=p['instance'].strip('"'), instance=None)
    return t, b


def api_exports(root):
    res = []
    for project, exp in export_index(root).items():
        latest = {}
        for ts in sorted(exp):
            for block in exp[ts]:
                latest[block] = ts
        res.append({'project': project,
                    'exports': [{'ts': ts, 'block': len(b)} for ts, b in sorted(exp.items(), reverse=True)],
                    'blocks': [{'block': k, 'ts': v} for k, v in sorted(latest.items())]})
    return {'export_root': root, 'projects': res}


def api_block(root, project, block, ts=None):
    t, b = _load(root, project, block, ts)
    return {'project': project, 'ts': t, 'block': b['name'], 'file': block, 'kind': b['kind'],
            'number': b['number'], 'language': b['language'], 'title': b['title'],
            'networks': [{'nw': n['nw'], 'language': n['language'], 'title': n['title']} for n in b['networks']]}


def api_network(root, project, block, nw, ts=None, width=0):
    t, b = _load(root, project, block, ts)
    if not 1 <= nw <= len(b['networks']):
        raise KeyError('network does not exist')
    n = b['networks'][nw - 1]
    res = {'project': project, 'ts': t, 'block': b['name'], 'file': block, 'kind': b['kind'],
           'number': b['number'], 'nw': nw, 'language': n['language'], 'title': n['title'],
           'f': b['attributes'].get('S7_Safety') == 'TRUE',
           'comment': n['comment'], 'elements': n.get('elements', []), 'rungs': n.get('rungs', []),
           'wires': s7dcl.wires(n) if n['language'] == 'LAD' else [],
           'layout': None, 'drawing': None, 'warnings': []}
    if n['language'] == 'FBD' and 'elements' in n:
        res.update(rungs=[], signals=fbd.signals(n['elements']))
        try:
            res['layout'], res['drawing'] = fbd.layout(n['elements'], width)
        except fbd.LayoutError as e:
            res['warnings'].append('FBD layout failed (%s) — showing the text.' % e)
            res['text'] = textwrap.dedent(n['text']).strip(chr(10))
        lad = fbd.as_lad(n['elements'])                  # jen ke čtení: čistá bitová logika
        if lad:
            try:
                res['as_lad'] = {'elements': lad[1], 'rungs': lad[0], 'drawing': layout.layout(lad[0], lad[1], width)[1]}
            except layout.LayoutError:
                pass
        return res
    if n['language'] != 'LAD':
        res['warnings'].append('Network in %s — only LAD and FBD are drawn.' % (n['language'] or '?')
                               if not n.get('error') else 'FBD network cannot be read (%s) — showing the text.' % n['error'])
        res['text'] = textwrap.dedent(n['text']).strip(chr(10))
        return res
    try:
        res['layout'], res['drawing'] = layout.layout(n['rungs'], n['elements'], width)
    except layout.LayoutError as e:
        res['warnings'].append('Layout failed (%s) — shown as a list of rungs.' % e)
    return res


# ---------- M2: úpravy a stavy ----------

def _model(net):
    if net['language'] == 'FBD':
        return {'language': 'FBD', 'elements': net['elements'], 'signals': net['signals'], 'layout': net['layout']}
    return {k: net[k] for k in ('elements', 'rungs', 'wires', 'layout')}


def _fbd_after(after, width=0):
    """FBD model z API -> {jazyk, prvky, signaly, rozlozeni}; chybný tvar = 400."""
    try:
        fbd.verify_model(after)
    except ValueError as e:
        raise state.StateError(str(e), 400)
    m = fbd.model(after['elements'])
    try:
        m['layout'] = fbd.layout(m['elements'], width)[0]
    except fbd.LayoutError:
        m['layout'] = None
    return m


def verify_model(after):
    """Model „po" z API (hranice důvěry): prvky s id/typ, boxy s piny, rungy jen z existujících id a wire#."""
    error = state.StateError('after must be a network model {elements:[{id, type, …}], rungs:[{z, path, do}]}', 400)
    if not isinstance(after, dict) or not isinstance(after.get('elements'), list) or not isinstance(after.get('rungs'), list):
        raise error
    ids = set()
    for p in after['elements']:
        if not isinstance(p, dict) or not isinstance(p.get('id'), str) or not isinstance(p.get('type'), str) \
                or p['id'].startswith('x:') or p['id'] in ids:
            raise error
        if p['type'] == 'box':
            pwr = p.get('power')
            if not (isinstance(pwr, list) and len(pwr) == 2 and all(isinstance(x, str) for x in pwr)):
                raise error
            for k in ('inputs', 'outputs'):
                if not isinstance(p.get(k), list) or not all(
                        isinstance(x, dict) and isinstance(x.get('pin'), str) and isinstance(x.get('operand'), str)
                        for x in p[k]):
                    raise error
        ids.add(p['id'])
    for r in after['rungs']:
        if not isinstance(r, dict) or not isinstance(r.get('from'), str) or not isinstance(r.get('path'), list) \
                or not (r.get('to') is None or isinstance(r.get('to'), str)):
            raise error
        if not all(isinstance(it, str) and (it.startswith('wire#') or it in ids) for it in r['path']):
            raise error


def _layout(model, width=0):
    try:
        return layout.layout(model['rungs'], model['elements'], width)
    except layout.LayoutError:
        return None, None


def api_state(data, project=None):
    if project:
        return state.load(data, project)
    return {'projects': [{'project': p, 'edits': state.load(data, p)['edits']} for p in state.projects(data)]}


def api_state_put(root, data, project, obj):
    if project not in export_index(root):
        raise KeyError('unknown project')
    with state.LOCK:
        state.save(data, project, obj)
    return obj


def api_edit_create(root, data, body):
    if not isinstance(body, dict):
        raise state.StateError('the body must be a JSON object', 400)
    project, block = str(body.get('project', '')), str(body.get('block', ''))
    state._file(data, project)                      # kontrola --data a jména projektu
    net = api_network(root, project, block, int(body.get('nw', 0)))
    if net['language'] == 'FBD' and net.get('signals') is not None:
        if not isinstance(body.get('after'), dict):
            raise state.StateError('after must be an FBD model {language: "FBD", elements: [...]}', 400)
        after = _fbd_after(dict(body['after'], language='FBD'))
    elif net['language'] != 'LAD':
        raise state.StateError('changes only for LAD or FBD networks (this one is %s)' % (net['language'] or '?'), 400)
    else:
        verify_model(body.get('after'))
        after = {'elements': body['after']['elements'], 'rungs': body['after']['rungs']}
        after['wires'] = s7dcl.wires(after)
        after['layout'] = body['after'].get('layout') or _layout(after)[0]
    when = state.now()
    with state.LOCK:
        s = state.load(data, project)
        u = {'id': state.new_id(data), 'project': project, 'round': str(body.get('round') or '—'),
             'block': net['file'], 'kind': net['kind'], 'block_number': net['number'], 'nw': net['nw'],
             'title': net['title'], 'language': net['language'], 'f': bool(net.get('f') or body.get('f')), 'author': 'editor', 'time': when, 'state': 'Proposal',
             'note': str(body.get('note') or ''),
             'history': [{'state': 'Proposal', 'time': when, 'by': 'editor'}],
             'before': dict(_model(net), export_ts=net['ts']), 'after': after, 'checks': []}
        s['edits'].append(u)
        state.save(data, project, s)
    return u


def api_edit_state(data, uid, body):
    if not isinstance(body, dict):
        raise state.StateError('the body must be a JSON object', 400)
    with state.LOCK:
        project, s, u = state.find(data, uid)
        state.change_state(u, body.get('state'), body.get('by'))
        state.save(data, project, s)
    return u


def api_add_check(data, body):
    if not isinstance(body, dict) or not isinstance(body.get('match'), bool):
        raise state.StateError('check: {edit_id, match: true|false, export_ts, note}', 400)
    with state.LOCK:
        project, s, u = state.find(data, str(body.get('edit_id', '')))
        u.setdefault('checks', []).append({
            'time': state.now(), 'match': body['match'], 'export_ts': str(body.get('export_ts') or ''),
            'note': str(body.get('note') or '')})
        state.save(data, project, s)
    return u


def _diff_drawing(before, after, width=0):
    """Kresba modelu „po" se smazanými prvky jako přízraky a značkami rozdílu proti „pred"."""
    changes, deleted = diff.compare(before, after)
    if fbd.is_fbd(after):
        return _diff_drawing_fbd(before, after, changes, deleted, width)
    elements, rungs, unplaced = diff.merge(before, after, deleted)
    warnings = []
    lay, drawing = _layout({'elements': elements, 'rungs': rungs}, width)
    if drawing is None:                                # přízraky rozbily rozložení -> jen „po"
        elements, rungs, unplaced = after['elements'], after['rungs'], list(deleted)
        lay, drawing = _layout(after, width)
        if drawing is None:
            warnings.append('The "after" model cannot be laid out — shown as a list of rungs.')
    marks = dict(changes)
    marks.update({'x:' + i: 'deleted' for i in deleted if i not in unplaced})
    if unplaced:
        P = {p['id']: p for p in before['elements']}
        warnings.append('Deleted (no place in the drawing): %s' % ', '.join(
            '%s %s' % (P[i]['type'], P[i].get('operand') or P[i].get('name') or '') for i in unplaced))
    return {'elements': elements, 'rungs': rungs, 'layout': lay, 'drawing': drawing, 'diff': marks,
            'warnings': warnings}


def _diff_drawing_fbd(before, after, changes, deleted, width):
    """FBD: smazané boxy jako přízraky (samostatné stromy; signály ze zachovaných boxů zůstanou)."""
    warnings, elements = [], fbd.merge(before, after, deleted)
    try:
        lay, drawing = fbd.layout(elements, width)
        marks = dict(changes, **{'x:' + i: 'deleted' for i in deleted})
    except fbd.LayoutError:
        elements, marks = after['elements'], dict(changes)
        try:
            lay, drawing = fbd.layout(elements, width)
        except fbd.LayoutError as e:
            lay, drawing = None, None
            warnings.append('The "after" model cannot be laid out (%s).' % e)
        if deleted:
            P = {p['id']: p for p in before['elements']}
            warnings.append('Deleted: %s' % ', '.join(fbd.display_name(P[i]) for i in deleted))
    return {'language': 'FBD', 'elements': elements, 'rungs': [], 'signals': fbd.signals(elements), 'layout': lay,
            'drawing': drawing, 'diff': marks, 'warnings': warnings,
            'pin_diff': fbd.pin_changes(before, after, {i for i, z in changes.items() if z == 'changed'})}


def _non_f(root, project):
    """Jména bloků projektu, které nejsou F (pro pravidlo „v F-bloku jen F")."""
    try:
        return {b['name'] for b in api_catalog(root, project)['blocks'] if not b.get('f')}
    except KeyError:
        return set()


def _validate(after, f=False, root=None, project=None):
    if fbd.is_fbd(after):
        return fbd.validate(after, f, _non_f(root, project) if f and root else ())
    return model.validate(after)


def api_edit_detail(data, uid, width=0, root=None):
    """Úprava + kresba „po" s rozdílem, chyby pravidel LAD, přechody stavů."""
    _, s, u = state.find(data, uid)
    res = _diff_drawing(u['before'], u['after'], width)
    res['tia'] = s.get('tia')
    k = u.get('checks') or []
    if state.STATES.index(u['state']) >= state.STATES.index('Checked') and k and k[-1].get('match') is False:
        res['warnings'].insert(0, 'The last export check is a MISMATCH, but the change is already %s — the state does not go '
                                     'back by itself; check what was written to the project.' % u['state'])
    following, previous = state.transitions(u)
    res.update({'edit': u, 'next': following, 'previous': previous, 'checked_allowed': state.checked_allowed(u),
                'errors': _validate(u['after'], u.get('f'), root, u['project'])})
    return res


def api_procedure(data, uid, root=None):
    _, _, u = state.find(data, uid)
    if 'f' not in u and root:                         # úpravy z doby před F2 příznak F-bloku nemají -> z exportu
        try:
            u = dict(u, f=_load(root, u['project'], u['block'])[1]['attributes'].get('S7_Safety') == 'TRUE')
        except KeyError:
            pass
    return dict(procedure.procedure(u), edit=uid)


# ---------- M3: katalog, editor ----------

def api_catalog(root, project):
    if project not in export_index(root):
        raise KeyError('unknown project')
    return catalog.catalog(root, project)


def api_layout(root, body):
    """Editor / zkouška Clauda: {po, pred?, ops?: [op], projekt?, sirka?} -> „po" po operacích, kresba s rozdílem
    proti „pred", místa vložení, chyby pravidel LAD. Nic neukládá."""
    if not isinstance(body, dict):
        raise state.StateError('the body must be a JSON object', 400)
    if fbd.is_fbd(body.get('after')):
        return _layout_fbd(body, root)
    verify_model(body.get('after'))
    after = {'elements': body['after']['elements'], 'rungs': body['after']['rungs']}
    before = body.get('before')
    if before is not None:
        verify_model(before)
    cat = []

    def template(key):                                # klíč palety ('NO', 'fb:FB_Pas') -> prvek z katalogu
        if not cat:
            cat.append(api_catalog(root, str(body.get('project', ''))))
        try:
            return catalog.element(cat[0], key)
        except KeyError as e:
            raise model.OperationError(str(e).strip("'"))
    ops = body.get('ops') or []
    if not isinstance(ops, list):
        raise state.StateError('ops must be a list of operations', 400)
    last = None
    for op in ops:
        after, last = model.apply(after, op, template)
        verify_model(after)
    res = _diff_drawing(before or after, after, int(body.get('width') or 0))
    res.update({'after': after, 'places': model.places(after, res['drawing']), 'errors': model.validate(after), 'last': last})
    if res['drawing'] is None:
        res['errors'].append({'id': None, 'error': 'The circuit cannot be laid out (it is not series-parallel).'})
    return res


def _layout_fbd(body, root=None):
    """FBD: operace (fbd.proved; klíče palety FBD i katalogu), kresba s rozdílem proti pred, místa vložení, pravidla."""
    after = _fbd_after(body['after'])
    before = _fbd_after(dict(body['before'], language='FBD')) if isinstance(body.get('before'), dict) else None
    ops = body.get('ops') or []
    if not isinstance(ops, list):
        raise state.StateError('ops must be a list of operations', 400)
    last, cat, f = None, [], bool(body.get('f'))

    def from_catalog(key):
        if not cat:
            cat.append(api_catalog(root, str(body.get('project', ''))))
        return catalog.element(cat[0], key)
    for op in ops:
        try:
            after, last = fbd.apply(after, op, from_catalog, f)
        except fbd.OperationError as e:
            raise state.StateError(str(e), 400)
    res = _diff_drawing(before or after, after, int(body.get('width') or 0))
    if res['drawing'] is not None:
        after['layout'] = fbd.layout(after['elements'])[0]
    res.update({'after': after, 'places': fbd.places(after, res['drawing']), 'last': last,
                'errors': _validate(after, f, root, str(body.get('project', '')))})
    return res


def api_check_export(root, data, uid, body):
    """M4: porovná network úpravy v exportu (nejnovějším / zadaném) s modelem „po" podle obsahu
    a zapíše výsledek jako kontrolu (POST /api/kontrola). Network se hledá podle titulku, pak podle čísla."""
    if not isinstance(body, dict):
        raise state.StateError('the body must be a JSON object', 400)
    _, _, u = state.find(data, uid)
    t, b = _load(root, u['project'], u['block'], str(body['export_ts']) if body.get('export_ts') else None)
    notes = []
    cand = [n for n in b['networks'] if u.get('title') and n['title'] == u['title']]
    if cand:
        n = next((x for x in cand if x['nw'] == u['nw']), cand[0])
        if n['nw'] != u['nw']:
            notes.append('the network moved: NW %d → NW %d (by title "%s")' % (u['nw'], n['nw'], u['title']))
        if len(cand) > 1:
            notes.append('the title is in the block %d×, compared NW %d' % (len(cand), n['nw']))
    else:
        n = b['networks'][u['nw'] - 1] if u['nw'] <= len(b['networks']) else None
        if u.get('title'):
            notes.append('title "%s" is not in the export — compared by number NW %d' % (u['title'], u['nw']))
    if n is None:
        match, diffs = False, ['network %d is not in the export' % u['nw']]
    elif fbd.is_fbd(u['after']):
        if n['language'] != 'FBD' or 'elements' not in n:
            match, diffs = False, ['NW %d in the export is in %s' % (n['nw'], n['language']) if n['language'] != 'FBD'
                                  else 'NW %d in the export is unreadable (%s)' % (n['nw'], n.get('error'))]
        else:
            match, diffs = fbd.content_match(u['after'], n)
    elif n['language'] != 'LAD':
        match, diffs = False, ['NW %d in the export is in %s' % (n['nw'], n['language'] or '?')]
    else:
        match, diffs = diff.content_match(u['after'], n)
    if t == u['before'].get('export_ts'):
        notes.append('note: the export is the same as before the change')
    note = ('export = proposal' if match else 'MISMATCH: %s' % '; '.join(diffs)) + ''.join(' · ' + x for x in notes)
    u = api_add_check(data, {'edit_id': uid, 'match': match, 'export_ts': t, 'note': note})
    return {'match': match, 'export_ts': t, 'nw_export': n['nw'] if n else None, 'note': note,
            'diffs': diffs, 'edit': u}


def api_edit_after(data, uid, body):
    """Uloží model „po" k úpravě (editor „Uložit návrh"). Jen ve stavu Proposal; chyby pravidel uložení nebrání."""
    if not isinstance(body, dict):
        raise state.StateError('the body must be a JSON object', 400)
    if fbd.is_fbd(body.get('after')):
        after = _fbd_after(body['after'])
    else:
        verify_model(body.get('after'))
        after = {'elements': body['after']['elements'], 'rungs': body['after']['rungs']}
        after['wires'] = s7dcl.wires(after)
        after['layout'] = _layout(after)[0]
    with state.LOCK:
        project, s, u = state.find(data, uid)
        if fbd.is_fbd(after) != fbd.is_fbd(u['after']):
            raise state.StateError('the language of the proposal (LAD/FBD) must stay the same', 400)
        if u['state'] != 'Proposal':
            raise state.StateError('the proposal can be changed only in state Proposal (now %s)' % u['state'])
        u['after'] = after
        u['edited'] = state.now()
        state.save(data, project, s)
    return {'edit': u, 'errors': _validate(after, u.get('f'))}


# ---------- M5: Upload (TIAC_Writer) a Check (TIAC_Reader) ----------

TIA_LOCK = threading.Lock()          # jedna instance TIA = v jednu chvíli jeden Upload / Check
CONNECTION_ERROR = 'TIA is busy or the Writer/Reader cannot be started: '


def _tia(s, project):
    t = s.get('tia') or {}
    if not t.get('softwarePath'):
        raise state.StateError('project %s has no TIA settings (tia: {softwarePath, plcName, groupPath?}) — '
                                 'set them in the UI on a change ("TIA settings") or through PUT /api/state' % project)
    return t


def _client(mcp, role, timeout):
    cfg = (mcp or {}).get(role)
    if not cfg:
        raise mcp_client.MCPError('%s is not configured (--mcp-json / --%s-cmd)' % (role, role))
    return mcp_client.Client(cfg[0], cfg[1], timeout, 'TIAC_Writer' if role == 'writer' else 'TIAC_Reader')


def _without_call_names(m):
    """Pro kontrolu serializace: volání FB/FC porovnat podle instance (parser bez exportu název FB nezná)."""
    m = json.loads(json.dumps(m))
    for p in m['elements']:
        if p.get('call') == 'fc' and not p.get('instance'):
            p['instance'] = '"%s"' % p.get('name')
        if p.get('call') in ('fb', 'fc'):
            p['name'] = None
            p['call'] = 'fb'
    return m


def _replace_in_file(file, u):
    """Nahradí network úpravy (podle titulku, jinak čísla) v exportovaném .s7dcl modelem „po" -> popis.
    Pojistky: F-blok ani jiný jazyk networku se nepřepíše; text po náhradě se znovu přečte a musí odpovídat návrhu."""
    with open(file, 'rb') as f:
        raw = f.read()
    bom = raw.startswith(b'\xef\xbb\xbf')
    text = raw.decode('utf-8-sig')
    b = s7dcl.load_block(file)
    if b['attributes'].get('S7_Safety') == 'TRUE':
        raise s7dcl_write.WriteError('the block is an F-block (S7_Safety) — Upload is for non-safety blocks only, change it by hand')
    cand = [n for n in b['networks'] if u.get('title') and n['title'] == u['title']]
    n = next((x for x in cand if x['nw'] == u['nw']), cand[0]) if cand else \
        (b['networks'][u['nw'] - 1] if u['nw'] <= len(b['networks']) else None)
    if n is None:
        raise s7dcl_write.WriteError('network %d ("%s") is not in the exported block' % (u['nw'], u.get('title') or ''))
    language = 'FBD' if fbd.is_fbd(u['after']) else 'LAD'
    if n['language'] != language:
        raise s7dcl_write.WriteError('NW %d is %s in the project, the proposal is %s' % (n['nw'], n['language'] or '?', language))
    new_value = s7dcl_write.replace_network(text, n['nw'], s7dcl_write.network_text(u['after']))
    back = s7dcl.parse_block(new_value.replace('\r\n', '\n'))['networks'][n['nw'] - 1]
    if language == 'FBD':
        match, diffs = fbd.content_match(_without_call_names(u['after']), _without_call_names(back))
    else:
        match, diffs = diff.content_match(_without_call_names(u['after']), _without_call_names(back))
    if not match:
        raise s7dcl_write.WriteError('the serialized proposal does not match the proposal: %s' % '; '.join(diffs))
    with open(file, 'wb') as f:
        f.write((b'\xef\xbb\xbf' if bom else b'') + new_value.encode('utf-8'))
    return 'NW %d "%s" replaced with the "after" model%s' % (n['nw'], n['title'], '' if n['nw'] == u['nw'] else
                                                                  ' (NW %d in the proposal — the network moved)' % u['nw'])


def _path_from_text(t):
    """Odpověď ResolveObjectPath -> 'Skupina/Blok' (JSON s path/objectPath… nebo prostý text)."""
    try:
        j = json.loads(t)
        if isinstance(j, dict):
            t = next((str(j[k]) for k in ('path', 'objectPath', 'resolvedPath', 'rootRelativePath') if j.get(k)), t)
        elif isinstance(j, str):
            t = j
    except ValueError:
        pass
    return t.strip().strip('"').replace('\\', '/')


def _write(data, uid, write, new_state, by):
    """Uloží záznam Upload/Check do u['zapisy'] a při úspěchu změní stav (když ho mezitím nikdo nezměnil)."""
    with state.LOCK:
        project, s, u = state.find(data, uid)
        u.setdefault('writes', []).append(write)
        if new_state:
            try:
                state.change_state(u, new_state, by)
            except state.StateError as e:
                write['message'] += ' · state unchanged: %s' % e
        state.save(data, project, s)
    return u


def _is_f_edit(root, u):
    if 'f' in u:
        return bool(u['f'])
    try:                                              # úpravy před F2: příznak z exportu; neznámý = bereme jako F
        return _load(root, u['project'], u['block'])[1]['attributes'].get('S7_Safety') == 'TRUE'
    except KeyError:
        return True


def api_upload(root, data, uid, mcp, timeout=300):
    """Approved ne-safety úprava -> TIAC_Writer: Connect, ResolveObjectPath, export bloku jako dokumenty,
    náhrada networku, PreviewImport, ImportFromDocuments Override -> Signed (kdo TIAC_Writer)."""
    _, s, u = state.find(data, uid)
    if u['state'] != 'Approved':
        raise state.StateError('Upload only in state Approved (now %s)' % u['state'])
    if _is_f_edit(root, u):
        raise state.StateError('F-block: no Upload — make the change by hand following the TIA steps')
    tia = _tia(s, u['project'])
    if not TIA_LOCK.acquire(blocking=False):
        raise state.StateError('another Upload / Check is running — wait until it ends')
    steps, work = [], tempfile.mkdtemp(prefix='tiac_upload_')

    def step(name, fn):
        try:
            v = fn()
        except Exception as e:
            steps.append({'step': name, 'ok': False, 'message': str(e)[:1000]})
            raise
        steps.append({'step': name, 'ok': True, 'message': str(v)[:1000]})
        return v
    try:
        sp, block = tia['softwarePath'], u['block']
        try:
            with _client(mcp, 'writer', timeout) as k:
                step('Connect', lambda: k.call('Connect', {}))
                grp = tia.get('groupPath')
                if not grp:
                    fpath = _path_from_text(step('ResolveObjectPath', lambda: k.call(
                        'ResolveObjectPath', {'softwarePath': sp, 'name': block, 'kind': 'block'})))
                    grp = fpath.rsplit('/', 1)[0] if '/' in fpath else ''
                step('ExportBlocksAsDocuments', lambda: k.call('ExportBlocksAsDocuments', {
                    'softwarePath': sp, 'exportPath': work, 'regexName': '^%s$' % re.escape(block), 'preservePath': False}))
                files = glob.glob(os.path.join(work, '**', block + '.s7dcl'), recursive=True)
                if not files:
                    raise s7dcl_write.WriteError('the Writer did not export %s.s7dcl' % block)
                folder = os.path.dirname(files[0])
                step('Replace the network in .s7dcl', lambda: _replace_in_file(files[0], u))
                preview = step('PreviewImport', lambda: k.call('PreviewImport', {
                    'softwarePath': sp, 'importPath': folder, 'kind': 'block', 'groupPath': grp}))
                step('ImportFromDocuments', lambda: k.call('ImportFromDocuments', {
                    'softwarePath': sp, 'groupPath': grp, 'importPath': folder, 'fileNameWithoutExtension': block,
                    'importOption': 'Override'}))
            ok, message = True, ('written to the project (%s, group %s) — save the project in TIA (the Writer does not call '
                                   'SaveProject)') % (sp, grp or '/')
        except mcp_client.MCPError as e:
            ok, message, preview = False, CONNECTION_ERROR + str(e), None
        except (s7dcl_write.WriteError, s7dcl.SourceParseError, OSError, ValueError) as e:
            ok, message, preview = False, 'Upload stopped: %s' % e, None
        write = {'time': state.now(), 'tool': 'TIAC_Writer', 'steps': steps, 'ok': ok, 'message': message}
        if preview is not None:
            write['preview'] = preview[:4000]
        u = _write(data, uid, write, 'Signed' if ok else None, 'TIAC_Writer')
    finally:
        shutil.rmtree(work, ignore_errors=True)
        TIA_LOCK.release()
    return {'ok': ok, 'message': write['message'], 'write': write, 'edit': u}


def _json_or_empty(t):
    try:
        j = json.loads(t)
        return j if isinstance(j, dict) else {}
    except ValueError:
        return {}


def api_check(root, data, uid, mcp, timeout=300):
    """Signed úprava -> TIAC_Reader export_to_folder (blok do nové složky export rootu) -> kontrola exportem
    -> při shodě Checked (kdo TIAC_Reader); při neshodě zůstane Signed a kontrola se zapíše jako neshoda."""
    project, s, u = state.find(data, uid)
    if u['state'] != 'Signed':
        raise state.StateError('Check only in state Signed (now %s)' % u['state'])
    tia = _tia(s, project)
    try:                                              # PLC a skupina z dosavadního exportu bloku
        t0, fpath, _ = find_block(root, project, u['block'])
        parts = os.path.relpath(fpath, os.path.join(root, project, t0))[:-len('.s7dcl')].replace('\\', '/').split('/')
    except KeyError:
        parts = [tia.get('plcName') or tia['softwarePath'], 'Blocks', u['block']]
    if tia.get('groupPath'):
        parts = parts[:2] + [x for x in tia['groupPath'].split('/') if x] + [u['block']]
    if not TIA_LOCK.acquire(blocking=False):
        raise state.StateError('another Upload / Check is running — wait until it ends')
    steps, k_res = [], None
    try:
        before = set(export_index(root).get(project, {}))
        try:
            with _client(mcp, 'reader', timeout) as k:
                arg = {'pathPrefixes': ['/'.join(parts)], 'include': ['blocks'], 'formats': ['documents'],
                       'plcName': tia.get('plcName') or parts[0]}
                texts = []
                for _ in range(50):                   # complete:false -> pokračovat runId + nextOffset
                    t = k.call('export_to_folder', arg)
                    texts.append(t)
                    j = _json_or_empty(t)
                    steps.append({'step': 'export_to_folder', 'ok': True, 'message': t[:1000]})
                    if j.get('complete', True) is not False:
                        break
                    arg.update(runId=j.get('runId'), nextOffset=j.get('nextOffset'))
            new_items = set(export_index(root).get(project, {})) - before
            ts = [x for x in re.findall(r'\d{8}-\d{6}', ' '.join(texts)) if x in new_items] or sorted(new_items)
            if not ts:
                raise s7dcl_write.WriteError("no Reader export found in the editor's export root (%s\\%s) — response: %s"
                                              % (root, project, texts[-1][:300] if texts else ''))
            k_res = api_check_export(root, data, uid, {'export_ts': ts[-1]})
            steps.append({'step': 'Export check %s' % ts[-1], 'ok': k_res['match'], 'message': k_res['note']})
            ok, message = k_res['match'], ('export = proposal' if k_res['match'] else k_res['note'])
        except mcp_client.MCPError as e:
            steps.append({'step': 'export_to_folder', 'ok': False, 'message': str(e)[:1000]})
            ok, message = False, CONNECTION_ERROR + str(e)
        except (s7dcl_write.WriteError, s7dcl.SourceParseError, KeyError, OSError) as e:
            ok, message = False, 'Check stopped: %s' % str(e).strip("'")
        write = {'time': state.now(), 'tool': 'TIAC_Reader', 'steps': steps, 'ok': ok, 'message': message}
        u = _write(data, uid, write, 'Checked' if ok else None, 'TIAC_Reader')
    finally:
        TIA_LOCK.release()
    return {'ok': ok, 'match': bool(k_res and k_res['match']), 'export_ts': k_res and k_res['export_ts'],
            'message': write['message'], 'write': write, 'edit': u}


class Handler(BaseHTTPRequestHandler):
    root = ''
    data = ''
    mcp = None                       # {'writer': (příkaz, env) | None, 'reader': …}
    mcp_timeout = 300

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n > 5_000_000:
            raise ValueError('the body is too large')
        return json.loads(self.rfile.read(n).decode('utf-8') or 'null')

    def _handle(self, method):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        c = u.path
        try:
            if method == 'GET' and c in ('/', '/index.html'):
                with open(os.path.join(WEB, 'index.html'), 'rb') as f:
                    b = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(b)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(b)
            elif method == 'GET' and c == '/api/exports':
                self._json(api_exports(self.root))
            elif method == 'GET' and c == '/api/block':
                self._json(api_block(self.root, q.get('project', ''), q.get('block', ''), q.get('ts')))
            elif method == 'GET' and c == '/api/network':
                self._json(api_network(self.root, q.get('project', ''), q.get('block', ''),
                                       int(q.get('nw', '1')), q.get('ts'), int(q.get('width', '0'))))
            elif method == 'GET' and c == '/api/state':
                self._json(api_state(self.data, q.get('project')))
            elif method == 'PUT' and c == '/api/state':
                self._json(api_state_put(self.root, self.data, q.get('project', ''), self._body()))
            elif method == 'POST' and c == '/api/edits':
                self._json(api_edit_create(self.root, self.data, self._body()), 201)
            elif method == 'POST' and re.fullmatch(r'/api/edits/[\w-]+/state', c):
                self._json(api_edit_state(self.data, c.split('/')[3], self._body()))
            elif method == 'GET' and re.fullmatch(r'/api/edits/[\w-]+', c):
                self._json(api_edit_detail(self.data, c.split('/')[3], int(q.get('width', '0')), self.root))
            elif method == 'GET' and re.fullmatch(r'/api/edits/[\w-]+/procedure', c):
                self._json(api_procedure(self.data, c.split('/')[3], self.root))
            elif method == 'GET' and c == '/api/catalog':
                self._json(api_catalog(self.root, q.get('project', '')))
            elif method == 'POST' and c == '/api/layout':
                self._json(api_layout(self.root, self._body()))
            elif method in ('POST', 'PUT') and re.fullmatch(r'/api/edits/[\w-]+/after', c):
                self._json(api_edit_after(self.data, c.split('/')[3], self._body()))
            elif method == 'POST' and re.fullmatch(r'/api/edits/[\w-]+/check-export', c):
                self._json(api_check_export(self.root, self.data, c.split('/')[3], self._body()))
            elif method == 'POST' and re.fullmatch(r'/api/edits/[\w-]+/upload', c):
                self._json(api_upload(self.root, self.data, c.split('/')[3], self.mcp, self.mcp_timeout))
            elif method == 'POST' and re.fullmatch(r'/api/edits/[\w-]+/check', c):
                self._json(api_check(self.root, self.data, c.split('/')[3], self.mcp, self.mcp_timeout))
            elif method == 'POST' and c == '/api/check':
                self._json(api_add_check(self.data, self._body()))
            else:
                self._json({'error': 'not found'}, 404)
        except state.StateError as e:
            self._json({'error': str(e)}, e.code)
        except KeyError as e:
            self._json({'error': str(e).strip("'")}, 404)
        except (ValueError, TypeError, s7dcl.SourceParseError) as e:
            self._json({'error': str(e)}, 400)

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def do_PUT(self):
        self._handle('PUT')


def create_server(port, root, data, host='127.0.0.1', mcp=None, mcp_timeout=300):
    h = type('H', (Handler,), {'root': root, 'data': data, 'mcp': mcp, 'mcp_timeout': mcp_timeout})
    return ThreadingHTTPServer((host, port), h)


def mcp_config(mcp_json, writer_cmd=None, reader_cmd=None):
    """Příkazy Writeru / Readeru: z .mcp.json (klíče TIAC_Writer, tia-portal), přebité --writer-cmd / --reader-cmd."""
    return {'writer': (mcp_client.command_from_text(writer_cmd), None) if writer_cmd
            else mcp_client.from_mcp_json(mcp_json, 'TIAC_Writer'),
            'reader': (mcp_client.command_from_text(reader_cmd), None) if reader_cmd
            else mcp_client.from_mcp_json(mcp_json, 'tia-portal')}


def main():
    ap = argparse.ArgumentParser(description='TIAC_Editor server')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--export-root', required=True)
    ap.add_argument('--data', required=True, help='folder with the change data: <data>\\<project>\\state.json')
    ap.add_argument('--mcp-json', default=r'.mcp.json',
                    help='where to take the TIAC_Writer and tia-portal (Reader) commands from; the file is only read')
    ap.add_argument('--writer-cmd', help='Writer command instead of .mcp.json (e.g. "python tests\\fake_mcp.py writer")')
    ap.add_argument('--reader-cmd', help='Reader command instead of .mcp.json')
    ap.add_argument('--mcp-timeout', type=int, default=300, help='timeout of one tool call (s)')
    a = ap.parse_args()
    srv = create_server(a.port, a.export_root, a.data, mcp=mcp_config(a.mcp_json, a.writer_cmd, a.reader_cmd),
                        mcp_timeout=a.mcp_timeout)
    print('TIAC_Editor at http://localhost:%d  (export root %s, data %s)' % (a.port, a.export_root, a.data), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    sys.exit(main())
