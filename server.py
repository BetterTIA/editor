"""TIAC_Editor — local server (Python stdlib): network viewer for Reader exports, change tree and states.

python server.py --port 8765 --export-root <folder> --data <folder>
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
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

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
    """{project: {ts: {block: path}}} — only timestamp folders that contain .s7dcl files."""
    res = {}
    if not os.path.isdir(root):
        return res
    for project in sorted(os.listdir(root)):
        x = project_index(root, project)
        if x:
            res[project] = x
    return res


def project_index(root, project):
    """{ts: {block: path}} of one project folder <root>/<project>."""
    res, pd = {}, os.path.join(root, project)
    if not os.path.isdir(pd):
        return res
    for ts in sorted(os.listdir(pd)):
        if not TS.match(ts):
            continue
        blocks = {}
        for dp, _, files in os.walk(os.path.join(pd, ts)):
            for f in files:
                if f.endswith('.s7dcl'):
                    blocks.setdefault(f[:-6], os.path.join(dp, f))
        if blocks:
            res[ts] = blocks
    return res


def project_root(root, data, project):
    """Export root of a project: from the registry, otherwise the server's default --export-root."""
    try:
        return next((p['export_root'] for p in state.registry(data) if p['name'] == project), root)
    except (OSError, ValueError, KeyError, TypeError):
        return root


def all_exports(root, data):
    """{project: {ts: {block: path}}} of the default export root plus the registered projects with their own root."""
    res = export_index(root)
    try:
        reg = state.registry(data)
    except (OSError, ValueError):
        reg = []
    for p in reg:
        if os.path.normcase(os.path.abspath(p.get('export_root') or root)) != os.path.normcase(os.path.abspath(root)):
            x = project_index(p['export_root'], p['name'])
            if x:
                res[p['name']] = x
            else:
                res.pop(p['name'], None)
    return res


def find_block(root, project, block, ts=None):
    """-> (ts, path, blocks) of the newest export with the block; blocks = newest copies of all project blocks up to ts
    (so a partial export finds instance DBs and FCs in an older one). Names come only from the index (no paths built from the URL)."""
    exp = project_index(root, project)
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
    for nw in b['networks']:                          # "FC"(…) is an FC call, not an FB instance
        for p in nw.get('elements', []):
            if p.get('call') == 'fb' and (p['instance'] or '').strip('"') in fc:
                p.update(call='fc', name=p['instance'].strip('"'), instance=None)
    return t, b


def api_exports(root, data=''):
    res = []
    for project, exp in all_exports(root, data).items():
        latest = {}
        for ts in sorted(exp):
            for block in exp[ts]:
                latest[block] = ts
        res.append({'project': project,
                    'exports': [{'ts': ts, 'block_count': len(b)} for ts, b in sorted(exp.items(), reverse=True)],
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
        lad = fbd.as_lad(n['elements'])                  # read-only: pure bit logic
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


# ---------- changes and states ----------

def _model(net):
    if net['language'] == 'FBD':
        return {'language': 'FBD', 'elements': net['elements'], 'signals': net['signals'], 'layout': net['layout']}
    return {k: net[k] for k in ('elements', 'rungs', 'wires', 'layout')}


def _fbd_after(after, width=0):
    """FBD model from the API -> {language, elements, signals, layout}; wrong shape = 400."""
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
    """The "after" model from the API (trust boundary): elements with id/type, boxes with pins, rungs only of existing ids and wire#."""
    error = state.StateError('after must be a network model {elements:[{id, type, …}], rungs:[{from, path, to}]}', 400)
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
    if not project_index(root, project):
        raise KeyError('unknown project')
    with state.LOCK:
        state.save(data, project, obj)
    return obj


def api_edit_create(root, data, body):
    if not isinstance(body, dict):
        raise state.StateError('the body must be a JSON object', 400)
    project, block = str(body.get('project', '')), str(body.get('block', ''))
    state._file(data, project)                      # checks --data and the project name
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
    when, by = state.now(), str(body.get('by') or 'editor')[:80]
    with state.LOCK:
        s = state.load(data, project)
        u = {'id': state.new_id(data), 'project': project, 'round': str(body.get('round') or '—'),
             'block': net['file'], 'kind': net['kind'], 'block_number': net['number'], 'nw': net['nw'],
             'title': net['title'], 'language': net['language'], 'f': bool(net.get('f') or body.get('f')), 'author': by, 'time': when, 'state': 'Proposal',
             'note': str(body.get('note') or ''),
             'history': [{'state': 'Proposal', 'time': when, 'by': by}],
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
    """Drawing of the "after" model with deleted elements as ghosts and diff marks against "before"."""
    changes, deleted = diff.compare(before, after)
    if fbd.is_fbd(after):
        return _diff_drawing_fbd(before, after, changes, deleted, width)
    elements, rungs, unplaced = diff.merge(before, after, deleted)
    warnings = []
    lay, drawing = _layout({'elements': elements, 'rungs': rungs}, width)
    if drawing is None:                                # ghosts broke the layout -> "after" only
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
    """FBD: deleted boxes as ghosts (separate trees; signals from kept boxes stay)."""
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
    """Names of project blocks that are not F-blocks (for the rule "only F in an F-block")."""
    try:
        return {b['name'] for b in api_catalog(root, project)['blocks'] if not b.get('f')}
    except KeyError:
        return set()


def _validate(after, f=False, root=None, project=None):
    if fbd.is_fbd(after):
        return fbd.validate(after, f, _non_f(root, project) if f and root else ())
    return model.validate(after)


def api_edit_detail(data, uid, width=0, root=None):
    """Change + "after" drawing with the diff, LAD rule errors, state transitions."""
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
    if 'f' not in u and root:                         # older changes have no F-block flag -> take it from the export
        try:
            u = dict(u, f=_load(root, u['project'], u['block'])[1]['attributes'].get('S7_Safety') == 'TRUE')
        except KeyError:
            pass
    return dict(procedure.procedure(u), edit=uid)


# ---------- catalog, editor ----------

def api_catalog(root, project):
    if not project_index(root, project):
        raise KeyError('unknown project')
    return catalog.catalog(root, project)


def api_layout(root, body):
    """Editor / dry run: {after, before?, ops?: [op], project?, width?} -> "after" after the operations, drawing with
    the diff against "before", insertion places, LAD rule errors. Saves nothing."""
    if not isinstance(body, dict):
        raise state.StateError('the body must be a JSON object', 400)
    if fbd.is_fbd(body.get('after')):
        return _layout_fbd(body, root)
    verify_model(body.get('after'))
    after = {'elements': body['after']['elements'], 'rungs': body['after']['rungs']}
    before = body.get('before')
    if before is not None:
        verify_model(before)
        if body.get('align'):                         # two exports of one network: ids follow content, not order
            after = diff.align(before, after)
    cat = []

    def template(key):                                # palette key ('NO', 'fb:FB_Belt') -> element from the catalog
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
    """FBD: operations (fbd.apply; FBD palette and catalog keys), drawing with the diff against before, insertion places, rules."""
    after = _fbd_after(body['after'])
    before = _fbd_after(dict(body['before'], language='FBD')) if isinstance(body.get('before'), dict) else None
    if before is not None and body.get('align'):
        after = _fbd_after(diff.align(before, after))
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
    """Compare the change's network in the export (newest / given) with the "after" model by content
    and record the result as a check (POST /api/check). The network is found by title, then by number."""
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
            notes.append('the title occurs %d× in the block, compared NW %d' % (len(cand), n['nw']))
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
    """Save the "after" model of a change (editor "Save proposal"). Only in state Proposal; rule errors do not block saving."""
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
        if body.get('note') is not None:
            u['note'] = str(body['note'])
        state.save(data, project, s)
    return {'edit': u, 'errors': _validate(after, u.get('f'))}


# ---------- Upload (TIAC_Writer) and Check (TIAC_Reader) ----------

TIA_LOCK = threading.Lock()          # one TIA instance = one Upload / Check at a time
CONNECTION_ERROR = 'TIA is busy or the Writer/Reader cannot be started: '


def _tia(s, project, data=''):
    t = s.get('tia') or next((p.get('tia') for p in state.registry(data) if p['name'] == project), None) or {}
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
    """For the serialization check: compare FB/FC calls by instance (without an export the parser does not know the FB name)."""
    m = json.loads(json.dumps(m))
    for p in m['elements']:
        if p.get('call') == 'fc' and not p.get('instance'):
            p['instance'] = '"%s"' % p.get('name')
        if p.get('call') in ('fb', 'fc'):
            p['name'] = None
            p['call'] = 'fb'
    return m


def _replace_in_file(file, u):
    """Replace the change's network (by title, otherwise by number) in the exported .s7dcl with the "after" model -> description.
    Safeguards: no F-block and no network in another language is overwritten; the replaced text is parsed again and must match the proposal."""
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
    """ResolveObjectPath answer -> 'Group/Block' (JSON with path/objectPath… or plain text)."""
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
    """Store the Upload/Check record in u['writes'] and on success change the state (if nobody changed it meanwhile)."""
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
    try:                                              # older changes: flag from the export; unknown = treated as F
        return _load(root, u['project'], u['block'])[1]['attributes'].get('S7_Safety') == 'TRUE'
    except KeyError:
        return True


def api_upload(root, data, uid, mcp, timeout=300, portal=None):
    """Approved non-safety change -> TIAC_Writer: GetPortals, Connect {portalId}, ResolveObjectPath, export the block as documents,
    replace the network, PreviewImport, ImportFromDocuments Override -> Signed (by TIAC_Writer)."""
    _, s, u = state.find(data, uid)
    if u['state'] != 'Approved':
        raise state.StateError('Upload only in state Approved (now %s)' % u['state'])
    if _is_f_edit(root, u):
        raise state.StateError('F-block: no Upload — make the change by hand following the TIA steps')
    tia = _tia(s, u['project'], data)
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
                # GetPortals first: Connect without portalId would start TIA Portal when none is running
                inst = portal_instances(step('GetPortals', lambda: k.call('GetPortals', {}))) or []
                if not inst:
                    raise SnapshotError('TIA Portal is not running — open the project in TIA Portal first '
                                        '(Upload does not start TIA)')
                chosen = next((x for x in inst if str(x['portalId']) == str(portal)), None) if portal else None
                if chosen is None and len(inst) > 1:
                    raise SnapshotError('More than one TIA Portal instance is running — choose one in the status bar')
                pid = (chosen or inst[0])['portalId']
                step('Connect', lambda: k.call('Connect', {'portalId': pid}))
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
        except SnapshotError as e:
            ok, message, preview = False, str(e), None
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
    """Signed change -> TIAC_Reader export_to_folder (block into a new export root folder) -> export check
    -> on a match Checked (by TIAC_Reader); on a mismatch it stays Signed and the check is recorded as a mismatch."""
    project, s, u = state.find(data, uid)
    if u['state'] != 'Signed':
        raise state.StateError('Check only in state Signed (now %s)' % u['state'])
    tia = _tia(s, project, data)
    try:                                              # PLC and group from the existing export of the block
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
        before = set(project_index(root, project))
        try:
            with _client(mcp, 'reader', timeout) as k:
                arg = {'pathPrefixes': ['/'.join(parts)], 'include': ['blocks'], 'formats': ['documents'],
                       'plcName': tia.get('plcName') or parts[0]}
                texts = []
                for _ in range(50):                   # complete:false -> continue with runId + nextOffset
                    t = k.call('export_to_folder', arg)
                    texts.append(t)
                    j = _json_or_empty(t)
                    steps.append({'step': 'export_to_folder', 'ok': True, 'message': t[:1000]})
                    if j.get('complete', True) is not False:
                        break
                    arg.update(runId=j.get('runId'), offset=j.get('nextOffset'))
            new_items = set(project_index(root, project)) - before
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


# ---------- projects, TIA status, snapshots (jobs) ----------

STATUS_TIMEOUT = 30                  # one tool call of the status check (s)
OPEN_TIMEOUT = 1800                  # OpenTiaProject may start TIA Portal and open a large project


def _json_any(t):
    try:
        return json.loads(t)
    except (ValueError, TypeError):
        return None


def _dicts(x):
    """All dicts in a JSON value, depth first, the value itself first."""
    if isinstance(x, dict):
        yield x
        for v in x.values():
            yield from _dicts(v)
    elif isinstance(x, list):
        for v in x:
            yield from _dicts(v)


def _project_of(d):
    """{name, path} of a project from a dict with projectPath/path/projectName/name… (tolerant: the shape is not fixed)."""
    path = next((d[k] for k in ('projectPath', 'path', 'fullPath', 'filePath') if isinstance(d.get(k), str) and d[k]), '')
    name = next((d[k] for k in ('projectName', 'name', 'project') if isinstance(d.get(k), str) and d[k]), '')
    if not name and path:
        name = os.path.splitext(os.path.basename(path.replace('\\', '/')))[0]
    return {'name': name, 'path': path}


def portal_instances(t):
    """GetPortals answer -> [{portalId, project, path, attached}]; None when the answer is not JSON.
    The top-level portalId (the attached instance) is not an instance of its own."""
    j = _json_any(t)
    if j is None:
        return None
    res = []
    for d in _dicts(list(j.values()) if isinstance(j, dict) else j):
        if d.get('portalId') not in (None, '') and not any(r['portalId'] == d['portalId'] for r in res):
            pr = _project_of(next((x for x in _dicts(d) if _project_of(x)['path']), d))
            res.append({'portalId': d['portalId'], 'project': pr['name'], 'path': pr['path'],
                        'attached': bool(d.get('isConnected') or d.get('connected') or d.get('attached'))})
    return res


def project_info(t):
    """GetProject / OpenTiaProject answer -> {name, path} of the first open project (empty when not found)."""
    for d in _dicts(_json_any(t)):
        pr = _project_of(d)
        if pr['path']:
            return pr
    return {'name': '', 'path': ''}


def plc_list(t):
    """GetProjectTree / GetDevices / OpenTiaProject answer -> [{softwarePath, plcName}].
    shortcut: any dict with a softwarePath (or a softwarePaths list) counts, filtered by a kind/type field saying 'plc'
    when there is one — tighten when the real answer shape is known."""
    res, j = [], _json_any(t)

    def add(sp, name, kind=''):
        if isinstance(sp, str) and sp and not any(r['softwarePath'] == sp for r in res):
            res.append({'softwarePath': sp, 'plcName': str(name or sp.split('/')[-1]), 'kind': str(kind or '')})
    for d in _dicts(j):
        add(d.get('softwarePath'), d.get('plcName') or d.get('name'), d.get('kind') or d.get('type'))
    for d in _dicts(j):                               # plain lists last: entries with a kind win
        for sp in d.get('softwarePaths') or []:
            add(sp, None)
    plc = [r for r in res if 'plc' in r['kind'].lower()]
    return [{'softwarePath': r['softwarePath'], 'plcName': r['plcName']} for r in (plc or res)]


def reader_export_root(mcp):
    """The Reader's own export root from its command (--export-root X) or env TIA_MCP_EXPORT_ROOT; None when unknown."""
    cfg = (mcp or {}).get('reader')
    if not cfg:
        return None
    cmd, env = cfg[0], cfg[1] or {}
    for i, x in enumerate(cmd[:-1]):
        if x == '--export-root':
            return cmd[i + 1]
    return env.get('TIA_MCP_EXPORT_ROOT')


def api_tia_status(mcp, portal_id=None, timeout=STATUS_TIMEOUT):
    """Writer GetPortals: no instance -> not running (no Connect: without portalId it would start TIA); one (or the
    chosen one) -> Connect {portalId} + GetProject -> attached; more -> choose. Reader get_project_status when attached."""
    res = {'tia': 'unknown', 'project': None, 'project_path': None, 'portalId': None, 'instances': [],
           'writer': 'configured' if (mcp or {}).get('writer') else 'missing',
           'reader': 'configured' if (mcp or {}).get('reader') else 'missing',
           'reader_export_root': reader_export_root(mcp), 'ai': 'no client', 'checked': True, 'time': state.now()}
    if res['writer'] == 'missing':
        return res
    try:
        with _client(mcp, 'writer', timeout) as w:
            inst = portal_instances(w.call('GetPortals', {}))
            if inst is None:
                raise mcp_client.MCPError('GetPortals answer is not JSON')
            res['instances'] = inst
            if not inst:
                res['tia'] = 'not running'
                return res
            chosen = next((x for x in inst if str(x['portalId']) == str(portal_id)), None) if portal_id \
                else (inst[0] if len(inst) == 1 else None)
            if chosen is None:
                res['tia'] = 'choose'
                return res
            res.update(tia='running', project=chosen['project'], project_path=chosen['path'], portalId=chosen['portalId'])
            w.call('Connect', {'portalId': chosen['portalId']})
            pr = project_info(w.call('GetProject', {'portalId': chosen['portalId']}))
            res.update(tia='attached', project=pr['name'] or chosen['project'], project_path=pr['path'] or chosen['path'])
    except mcp_client.MCPError as e:
        res['writer'] = 'error: %s' % str(e)[:300]
        return res
    if res['reader'] == 'configured' and res['tia'] == 'attached':
        try:
            with _client(mcp, 'reader', timeout) as r:
                r.call('get_project_status', {})
        except mcp_client.MCPError as e:
            res['reader'] = 'error: %s' % str(e)[:300]
    return res


def refresh_status(h, portal_id=None):
    """Recompute the TIA status and keep it as the cached one; while a job holds TIA the cached one is returned."""
    if TIA_LOCK.locked():
        return dict(h.tia_status, busy=True)
    chosen = portal_id or h.tia_status.get('chosen')
    st = dict(api_tia_status(h.mcp, chosen), chosen=chosen, autoconnect=h.tia_status.get('autoconnect'))
    h.tia_status.clear()
    h.tia_status.update(st)
    return st


def sync_registry(root, data):
    """Projects found in the default export root are added to the registry (source exports)."""
    if not data:
        return
    known = {p['name'] for p in state.registry(data)}
    for name, exp in export_index(root).items():
        if name not in known:
            state.register(data, name, source='exports', export_root=root, last_snapshot=max(exp))


def api_projects(root, data, jobs):
    if data:
        items = state.registry(data)
    else:                                             # without --data: the default export root only
        items = [{'name': n, 'source': 'exports', 'export_root': root} for n in export_index(root)]
    for p in items:
        idx = project_index(p.get('export_root') or root, p['name'])
        counts = {}
        if data:
            try:
                for u in state.load(data, p['name'])['edits']:
                    counts[u['state']] = counts.get(u['state'], 0) + 1
            except (state.StateError, OSError, ValueError):
                pass
        job = next((j for j in reversed(list(jobs.values())) if j.get('project') == p['name']), None)
        p.update(exports=len(idx), latest_export=max(idx) if idx else None, edits=counts, job=job)
    running = [j for j in jobs.values() if j['state'] == 'running']
    return {'projects': items, 'default_export_root': root, 'running_job': running[-1] if running else None}


def api_project_add_exports(data, body):
    """Export folder on disk: a project folder (<project>\\<YYYYMMDD-HHMMSS>\\…) or an export root with project folders."""
    raw = str(body.get('path') or '').strip().strip('"')
    path = os.path.abspath(raw) if raw else ''
    if not path or not os.path.isdir(path):
        raise state.StateError('the export folder does not exist: %s' % (raw or '(empty)'), 400)
    idx = project_index(os.path.dirname(path), os.path.basename(path))
    found = [(os.path.dirname(path), os.path.basename(path), max(idx))] if idx else \
        [(path, n, max(e)) for n, e in export_index(path).items()]
    if not found:
        raise state.StateError('no Reader export in %s (expected <project>\\<YYYYMMDD-HHMMSS>\\<PLC>\\…\\*.s7dcl)' % path, 400)
    return {'projects': [state.register(data, n, source='exports', export_root=r, last_snapshot=ts) for r, n, ts in found]}


def api_project_delete(data, name):
    """Remove from the registry only — export folders and changes (state.json) stay on disk."""
    with state.LOCK:
        items = state.registry(data)
        rest = [p for p in items if p['name'] != name]
        if len(rest) == len(items):
            raise KeyError('project %s is not registered' % name)
        state.save_registry(data, rest)
    return {'removed': name}


# ---------- AI clients (mcp_server.py registers here) ----------

AI_TIMEOUT = 60                      # a client without a heartbeat for this long is forgotten (s)


def ai_status(h):
    """-> {ai: "no client" | "connected: <name>[, <name>…]", ai_clients: [...]}; drops clients without a heartbeat."""
    now = time.time()
    for k in [k for k, c in list(h.ai_clients.items()) if now - c['seen'] > h.ai_timeout]:
        h.ai_clients.pop(k, None)
    cl = sorted(h.ai_clients.values(), key=lambda c: c['since'])
    return {'ai': 'connected: ' + ', '.join(c['name'] for c in cl) if cl else 'no client',
            'ai_clients': [{k: c[k] for k in ('id', 'name', 'version', 'since', 'last_seen')} for c in cl]}


def api_ai_heartbeat(h, body):
    """POST /api/ai/clients {id, name, version?} — register a client or refresh its heartbeat."""
    if not isinstance(body, dict) or not re.fullmatch(r'[\w-]{1,64}', str(body.get('id') or '')):
        raise state.StateError('ai client: {id: [A-Za-z0-9_-]{1,64}, name, version?}', 400)
    c = h.ai_clients.get(body['id']) or {'id': body['id'], 'since': state.now()}
    c.update(name=str(body.get('name') or 'unknown client')[:80], version=str(body.get('version') or '')[:40],
             last_seen=state.now(), seen=time.time())
    h.ai_clients[c['id']] = c
    return ai_status(h)


class SnapshotError(Exception):
    pass


def start_job(jobs, kind, project, fn):
    """Background job under TIA_LOCK (one Upload / Check / snapshot at a time) -> job record (GET /api/jobs/<id>)."""
    if not TIA_LOCK.acquire(blocking=False):
        raise state.StateError('another Upload / Check / snapshot is running — wait until it ends')
    job = {'id': 'J%03d' % (len(jobs) + 1), 'kind': kind, 'project': project, 'state': 'running', 'steps': [],
           'exported': 0, 'chunks': 0, 'error': None, 'started': state.now(), 'finished': None}
    jobs[job['id']] = job

    def run():
        try:
            job['result'] = fn(job)
            job['state'] = 'done'
        except Exception as e:                        # the thread must always end the job and free the lock
            job['state'] = 'error'
            job['error'] = (CONNECTION_ERROR if isinstance(e, mcp_client.MCPError) else '') + str(e)[:2000]
        finally:
            job['finished'] = state.now()
            TIA_LOCK.release()
    threading.Thread(target=run, daemon=True).start()
    return job


def _export_folder(j):
    """Reader answer -> (export root, project folder, ts) from the first string with a …\\<project>\\<YYYYMMDD-HHMMSS> part."""
    for d in _dicts(j):
        for v in d.values():
            if isinstance(v, str):
                parts = re.split(r'[\\/]', v)
                for i in range(len(parts) - 1, 1, -1):
                    if TS.match(parts[i]):
                        return os.sep.join(parts[:i - 1]), parts[i - 1], parts[i]
    return None


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def snapshot(job, root, data, mcp, timeout, name=None, source='tia', path=None, portal=None, software_path=None,
             close=False):
    """TIA project -> Reader export of the whole PLC (chunked) -> registry. name None = a new project named by the Reader folder."""
    reg = next((p for p in state.registry(data) if p['name'] == name), None) if name else None
    if reg:
        source, path = reg.get('source') or source, path or reg.get('project_path')
        software_path = software_path or (reg.get('tia') or {}).get('softwarePath')
    steps = job['steps']

    def step(label, fn):
        try:
            v = fn()
        except Exception as e:
            steps.append({'step': label, 'ok': False, 'message': str(e)[:1000]})
            raise
        steps.append({'step': label, 'ok': True, 'message': str(v)[:1000]})
        return v
    if isinstance(portal, str):
        portal = int(portal) if portal.isdigit() else (portal or None)   # the Writer's portalId is a process id (integer)
    with _client(mcp, 'writer', timeout) as w:
        plcs = []
        if source == 'ap21':
            if not path:
                raise SnapshotError('project %s has no .ap21 path' % name)
            t = step('OpenTiaProject', lambda: w.call(
                'OpenTiaProject', dict({'path': path}, **({'portalId': portal} if portal else {})), max(timeout, OPEN_TIMEOUT)))
            portal = next((d['portalId'] for d in _dicts(_json_any(t)) if d.get('portalId') not in (None, '')), portal)
            plcs = plc_list(t)
        else:
            if not portal:
                inst = portal_instances(step('GetPortals', lambda: w.call('GetPortals', {})))
                if not inst:
                    raise SnapshotError('TIA Portal is not running — open the project in TIA Portal first '
                                        '(the editor does not start TIA for an open project)')
                if len(inst) > 1:
                    job['portal_choices'] = inst
                    raise SnapshotError('%d TIA Portal instances are running — choose one in the status bar' % len(inst))
                portal = inst[0]['portalId']
            step('Connect', lambda: w.call('Connect', {'portalId': portal}))
        pid = {'portalId': portal} if portal else {}
        pr = project_info(step('GetProject', lambda: w.call('GetProject', pid)))
        job['tia_project'] = pr['name']
        if not plcs:
            plcs = plc_list(step('GetProjectTree', lambda: w.call('GetProjectTree', pid)))
        if not plcs:
            plcs = plc_list(step('GetDevices', lambda: w.call('GetDevices', pid)))
        if software_path:
            plc = next((x for x in plcs if x['softwarePath'] == software_path),
                       {'softwarePath': software_path, 'plcName': software_path.split('/')[-1]})
        elif len(plcs) == 1:
            plc = plcs[0]
        else:
            job['plc_choices'] = plcs
            raise SnapshotError('the project has %d PLCs — choose one' % len(plcs) if plcs else
                                'no PLC software found (GetProjectTree / GetDevices) — give the softwarePath')
        job['plc'] = plc
        target = (reg or {}).get('export_root')
        before = set(project_index(target, name)) if target else set()
        with _client(mcp, 'reader', timeout) as r:
            arg = {'include': ['blocks', 'types', 'tagTables'], 'formats': ['documents'], 'plcName': plc['plcName'],
                   'timeBudgetSeconds': 120}
            if source == 'ap21' and path:
                arg['projectPath'] = path
            folder, t = None, ''
            while True:                               # complete:false -> continue with runId + offset=nextOffset
                t = step('export_to_folder #%d' % (job['chunks'] + 1), lambda: r.call('export_to_folder', arg))
                j = _json_any(t)
                j = j if isinstance(j, dict) else {}
                job['chunks'] += 1
                job['exported'] += next((j[k] for k in ('exported', 'exportedCount', 'count') if isinstance(j.get(k), int)), 0)
                folder = folder or _export_folder(j)
                if j.get('complete', True) is not False:
                    break
                if j.get('nextOffset') is None or job['chunks'] > 10000:
                    raise SnapshotError('the Reader export is not complete and gives no nextOffset: %s' % t[:300])
                arg.update(runId=j.get('runId'), offset=j['nextOffset'])
        if folder is None:                            # no folder in the answer -> a new timestamp in the project's root
            new = sorted(set(project_index(target, name)) - before) if target else []
            if not new:
                raise SnapshotError('the Reader answer names no export folder and none appeared in the project — '
                                    'the Reader writes to its own --export-root (%s): %s'
                                    % (reader_export_root(mcp) or 'unknown', t[:300]))
            folder = (target, name, new[-1])
        rroot, pname, ts = folder
        if reg and not (_same(rroot, reg['export_root']) and pname == name):
            raise SnapshotError(('the Reader exported to %s, but project %s reads %s — the Reader writes to its own '
                                 '--export-root and names the folder after the TIA project; make them match, or add the '
                                 'project from the Reader folder') % (os.path.join(rroot, pname, ts), name,
                                                                       os.path.join(reg['export_root'], name)))
        if not reg and any(p['name'] == pname and not _same(p['export_root'], rroot) for p in state.registry(data)):
            raise SnapshotError('project %s is already registered with another export root — remove it first or take '
                                'the snapshot from its card' % pname)
        p = state.register(data, pname, source=source, export_root=rroot, last_snapshot=ts,
                           project_path=path or pr['path'] or None,
                           tia={'softwarePath': plc['softwarePath'], 'plcName': plc['plcName']})
        job['project'] = pname
        if close:
            step('CloseProject', lambda: w.call('CloseProject', pid))
    return {'project': p, 'export_ts': ts, 'export_folder': os.path.join(rroot, pname, ts)}


def api_snapshot(h, name, body, source=None):
    """POST /api/projects/<name>/snapshot, or a new project from TIA (name None, source tia | ap21) -> job."""
    body = body if isinstance(body, dict) else {}
    if not h.data:
        raise state.StateError('the server runs without --data', 503)
    if name and not any(p['name'] == name for p in state.registry(h.data)):
        raise KeyError('project %s is not registered' % name)
    path = str(body.get('path') or '').strip().strip('"') or None
    if source == 'ap21' and not (path and re.search(r'\.(ap|als)\d+$', path, re.I)):
        raise state.StateError('give the full path of a .apXX project or .alsXX session file', 400)
    portal = body.get('portalId') or (h.tia_status.get('portalId') if h.tia_status.get('tia') == 'attached' else None)
    return start_job(h.jobs, 'snapshot', name, lambda job: snapshot(
        job, h.root, h.data, h.mcp, h.mcp_timeout, name, source or 'tia', path, portal,
        str(body.get('softwarePath') or '') or None, bool(body.get('close'))))


# ---------- changelog (D3): one timeline from state.json, export folders and jobs — no storage of its own ----------

def _ts_time(ts):
    return '%s-%s-%sT%s:%s:%s' % (ts[:4], ts[4:6], ts[6:8], ts[9:11], ts[11:13], ts[13:15])


def _full_export(root, project, ts):
    """A snapshot exports blocks + tag tables, a Check only one block: a Tags folder marks a full export."""
    return bool(glob.glob(os.path.join(glob.escape(os.path.join(root, project, ts)), '*', 'Tags')))


def _cell(s):
    return str(s if s is not None else '').replace('\r', ' ').replace('\n', ' ').replace('|', '\\|')


def changelog_markdown(project, items):
    """Markdown: one table per day; every edit row carries block + number + network title, the way TIA shows it."""
    out, day = ['# Changelog — %s' % project], None
    if not items:
        out += ['', 'Nothing recorded yet.']
    for x in items:
        if x['time'][:10] != day:
            day = x['time'][:10]
            out += ['', '## %s' % day, '', '| Time | Event | Block · network | By | Details |', '|---|---|---|---|---|']
        what = '%s %s' % (x['edit'], x['kind']) if x.get('edit') else x['kind']
        where = procedure.header(dict(x, kind=x.get('block_kind'))) if x.get('edit') else ''
        out.append('| %s | %s | %s | %s | %s |' % tuple(_cell(v) for v in (x['time'][11:19], what, where, x['by'], x['summary'])))
    return '\n'.join(out) + '\n'


def api_changelog(root, data, project, jobs=None, since=None, until=None, edit_state=None, block=None):
    """-> {project, items: [{time, kind, edit?, block, nw, title, by, summary, …}]} oldest first.
    kind: proposed | state | upload | write | check | snapshot. state/block filter the edit items (snapshots are dropped then)."""
    exp = project_index(root, project)
    edits = state.load(data, project)['edits'] if data else []
    if not exp and not edits:
        raise KeyError('unknown project')
    items, checked = [], {}                          # checked: export ts -> edit whose Check (Reader) made it

    def add(time_, kind, summary, u=None, by=''):
        x = {'time': time_ or '', 'kind': kind, 'block': '', 'nw': None, 'title': '', 'by': by or '', 'summary': summary}
        if u:
            x.update(edit=u['id'], block=u['block'], nw=u['nw'], title=u.get('title') or '', block_kind=u.get('kind'),
                     block_number=u.get('block_number'), state=u['state'])
        items.append(x)
    for u in edits:
        hist = u.get('history') or []
        add(u.get('time'), 'proposed', u.get('note') or 'proposal', u, u.get('author'))
        for i, h in enumerate(hist):
            if i:                                     # the first entry is the proposal itself
                add(h.get('time'), 'state', '%s → %s' % (hist[i - 1]['state'], h['state']), u, h.get('by'))
        reader_ts = set()
        for w in u.get('writes') or []:
            steps = w.get('steps') or []
            ts = [k['step'][len('Export check '):] for k in steps if k.get('step', '').startswith('Export check ')]
            if ts:                                    # a Check that ran: the check item below says the same
                reader_ts.update(ts)
                checked.update({t: u['id'] for t in ts})
                continue
            add(w.get('time'), 'upload' if w.get('tool') == 'TIAC_Writer' else 'write',
                '%s %s' % ('✓' if w.get('ok') else '✗', w.get('message') or ''), u, w.get('tool'))
        for k in u.get('checks') or []:
            add(k.get('time'), 'check', '%s · export %s%s' % ('match' if k.get('match') else 'MISMATCH', k.get('export_ts') or '?',
                                                             ' · ' + k['note'] if k.get('note') else ''),
                u, 'TIAC_Reader' if k.get('export_ts') in reader_ts else 'export check')
    snap_jobs = [j for j in (jobs or {}).values() if j.get('kind') == 'snapshot' and j.get('project') == project]
    by_job = {j['result']['export_ts']: j for j in snap_jobs if j.get('result')}
    for ts in sorted(exp):
        if _full_export(root, project, ts):
            summary = 'snapshot: full export, %d blocks' % len(exp[ts])
        else:
            summary = 'partial export: %s' % ', '.join(sorted(exp[ts])[:5]) + (' …' if len(exp[ts]) > 5 else '')
        j = by_job.get(ts)
        if j:
            summary += ' (job %s)' % j['id']
        elif ts in checked:
            summary += ' (Check %s)' % checked[ts]
        add(_ts_time(ts), 'snapshot', summary, by='TIAC_Reader' if j or ts in checked else '')
    for j in snap_jobs:
        if j.get('state') == 'error':
            add(j.get('finished') or j.get('started'), 'snapshot', 'snapshot failed (job %s): %s' % (j['id'], j.get('error')),
                by='TIAC_Reader')

    def when(x):
        x = str(x or '')
        return _ts_time(x) if TS.match(x) else x
    since, until = when(since), when(until)
    items = [x for x in items if (not since or x['time'] >= since) and (not until or x['time'][:len(until)] <= until)
             and (not edit_state or x.get('state') == edit_state) and (not block or x.get('block') == block)]
    # shortcut: times have one-second resolution, so ties follow the usual causal order (a write, then the state it set;
    # an export, then its check); a user's click within the same second as a tool may sort wrong — add a sequence if it matters
    rank = {'proposed': 0, 'upload': 2, 'write': 2, 'snapshot': 4, 'check': 5}
    items.sort(key=lambda x: (x['time'], rank.get(x['kind']) if x['kind'] != 'state' else
                              1 if x['by'] not in ('TIAC_Writer', 'TIAC_Reader') else 3 if x['by'] == 'TIAC_Writer' else 6))
    return {'project': project, 'items': items}


# ---------- versions (D3): snapshots of the project and their comparison ----------

def _project_at(exp, full, ts):
    """{block: ts of its copy} — the project as it was at export ts: the newest full export up to ts plus the partial
    exports after it (a block missing from a full export was removed)."""
    res = {}
    for t in sorted(exp):
        if t > ts:
            break
        if full[t]:
            res = {}
        res.update({b: t for b in exp[t]})
    return res


def api_snapshots(root, project):
    exp = project_index(root, project)
    if not exp:
        raise KeyError('unknown project')
    res = []
    for ts in sorted(exp, reverse=True):
        x = {'ts': ts, 'time': _ts_time(ts), 'block_count': len(exp[ts]), 'full': _full_export(root, project, ts)}
        if not x['full']:
            x['blocks'] = sorted(exp[ts])
        res.append(x)
    return {'project': project, 'snapshots': res}


def _wording(diffs):
    """content_match speaks of proposal / export; here it is the older / newer snapshot."""
    return [d.replace('missing in export', 'removed').replace('extra in export', 'added')
            .replace('proposal ', 'before ').replace('export ', 'after ') for d in diffs]


def _same_network(a, b):
    """-> (same, [differences]) by content (diff / fbd content_match), other languages by text."""
    la, lb = a.get('language') or 'LAD', b.get('language') or 'LAD'
    if la != lb:
        return False, ['language %s → %s' % (la, lb)]
    if la == 'FBD' and 'elements' in a and 'elements' in b:
        same, d = fbd.content_match(a, b)
    elif la == 'LAD' and all(k in x for x in (a, b) for k in ('elements', 'rungs')):
        same, d = diff.content_match(a, b)
    else:
        t = [textwrap.dedent(x.get('text') or '').strip() for x in (a, b)]
        same, d = t[0] == t[1], ['the network text differs']
    return same, ([] if same else _wording(d))


def _pair(xa, xb):
    """Networks of two versions of a block: by a title unique in both, then by equal content, then by number."""
    left, right, pairs = list(xa), list(xb), []
    ta, tb = [n['title'] for n in xa], [n['title'] for n in xb]

    def take(cond):
        for m in list(right):
            n = next((n for n in left if cond(n, m)), None)
            if n is not None:
                pairs.append((n, m))
                left.remove(n)
                right.remove(m)
    take(lambda n, m: m['title'] and n['title'] == m['title'] and ta.count(m['title']) == 1 and tb.count(m['title']) == 1)
    take(lambda n, m: _same_network(n, m)[0])
    take(lambda n, m: n['nw'] == m['nw'])
    pairs += [(n, None) for n in left] + [(None, m) for m in right]
    return sorted(pairs, key=lambda p: (p[1] or p[0])['nw'] + (0.5 if p[1] is None else 0))


def _matches(edits, block, n):
    """Edits of this block whose "after" model is this network (same title or number, same content)."""
    return [u['id'] for u in edits if u.get('block') == block
            and (u.get('title') and u['title'] == n['title'] or u.get('nw') == n['nw'])
            and _same_network(dict(u['after'], language='FBD' if fbd.is_fbd(u['after']) else 'LAD'), n)[0]]


def _compare_block(root, project, name, ta, tb, edits):
    _, x = _load(root, project, name, ta)
    _, y = _load(root, project, name, tb)
    res, notes = [], []
    for na, nb in _pair(x['networks'], y['networks']):
        z = {'nw_from': na and na['nw'], 'nw_to': nb and nb['nw'], 'title': (nb or na)['title'],
             'language': (nb or na)['language'], 'notes': [], 'diffs': [], 'matches': []}
        if na is None:
            z['status'] = 'added'
        elif nb is None:
            z['status'] = 'removed'
        else:
            same, z['diffs'] = _same_network(na, nb)
            if na['title'] != nb['title']:
                z['notes'].append('title "%s" → "%s"' % (na['title'], nb['title']))
            if na.get('comment') != nb.get('comment'):
                z['notes'].append('comment changed')
            if na['nw'] != nb['nw']:
                z['notes'].append('the network moved: NW %d → NW %d' % (na['nw'], nb['nw']))
            z['status'] = 'changed' if not same or na['title'] != nb['title'] or na.get('comment') != nb.get('comment') \
                else 'moved' if na['nw'] != nb['nw'] else None
        if z['status'] is None:
            continue
        if nb is not None and z['status'] != 'moved':
            z['matches'] = _matches(edits, name, nb)
        res.append(z)
    va, vb = x.get('variables') or {}, y.get('variables') or {}
    iface = ['+ %s : %s' % (k, vb[k]) for k in vb if k not in va] + ['− %s : %s' % (k, va[k]) for k in va if k not in vb] + \
            ['%s : %s → %s' % (k, va[k], vb[k]) for k in va if k in vb and va[k] != vb[k]]
    if iface:
        notes.append('interface: ' + ', '.join(iface))
    if x['attributes'] != y['attributes']:
        notes.append('block attributes changed')
    if not res and not notes:
        notes.append('the export text differs outside the networks')
    return {'kind': y['kind'], 'number': y['number'], 'networks': res, 'notes': notes}


def _same_files(x, y):
    """Byte-equal files (both missing = equal)."""
    if not (os.path.exists(x) and os.path.exists(y)):
        return os.path.exists(x) == os.path.exists(y)
    with open(x, 'rb') as f, open(y, 'rb') as g:
        return f.read() == g.read()


def api_compare(root, data, project, a, b, block=None):
    """Two snapshots (export timestamps) -> changed blocks; per block the networks added / removed / changed / moved."""
    exp = project_index(root, project)
    if not exp:
        raise KeyError('unknown project')
    for t in (a, b):
        if t not in exp:
            raise KeyError('project %s has no export %s' % (project, t))
    full = {t: _full_export(root, project, t) for t in exp}
    A, B = _project_at(exp, full, a), _project_at(exp, full, b)
    names = sorted(set(A) | set(B))
    if block:
        if block not in names:
            raise KeyError('block %s is in neither snapshot' % block)
        names = [block]
    try:
        edits = state.load(data, project)['edits'] if data else []
    except (state.StateError, OSError, ValueError):
        edits = []
    blocks = []
    for name in names:
        ta, tb = A.get(name), B.get(name)
        if ta == tb or ta and tb and all(_same_files(exp[ta][name][:-6] + e, exp[tb][name][:-6] + e) for e in ('.s7dcl', '.s7res')):
            continue
        z = {'block': name, 'from_ts': ta, 'to_ts': tb, 'status': 'added' if not ta else 'removed' if not tb else 'changed'}
        try:
            if ta and tb:
                z.update(_compare_block(root, project, name, ta, tb, edits))
            else:
                k = _load(root, project, name, ta or tb)[1]
                z.update(kind=k['kind'], number=k['number'], networks=[], notes=[])
        except (s7dcl.SourceParseError, KeyError, OSError) as e:
            z.update(networks=[], notes=['cannot be read: %s' % e])
        blocks.append(z)
    return {'project': project, 'from': a, 'to': b, 'blocks': blocks}


class Handler(BaseHTTPRequestHandler):
    root = ''
    data = ''
    mcp = None                       # {'writer': (command, env) | None, 'reader': …}
    mcp_timeout = 300
    jobs = {}
    tia_status = {}
    ai_clients = {}
    ai_timeout = AI_TIMEOUT

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200, content_type='application/json'):
        b = (obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        if self._b is None:                           # read once (the route may look at the project first)
            n = int(self.headers.get('Content-Length') or 0)
            if n > 5_000_000:
                raise ValueError('the body is too large')
            self._b = [json.loads(self.rfile.read(n).decode('utf-8') or 'null')]
        return self._b[0]

    def _handle(self, method):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        c = u.path
        self._b = None

        def root(project):                                # export root of the project (registry or --export-root)
            return project_root(self.root, self.data, str(project or ''))

        def root_of(uid):
            return root(state.find(self.data, uid)[0])

        def body_project():
            b = self._body()
            return b.get('project') if isinstance(b, dict) else ''
        origin = self.headers.get('Origin')
        if method != 'GET' and origin and urlparse(origin).hostname not in ('localhost', '127.0.0.1'):
            self._json({'error': 'cross-origin request refused'}, 403)   # another web page must not drive TIA
            return
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
                self._json(api_exports(self.root, self.data))
            elif method == 'GET' and c == '/api/block':
                self._json(api_block(root(q.get('project')), q.get('project', ''), q.get('block', ''), q.get('ts')))
            elif method == 'GET' and c == '/api/network':
                self._json(api_network(root(q.get('project')), q.get('project', ''), q.get('block', ''),
                                       int(q.get('nw', '1')), q.get('ts'), int(q.get('width', '0'))))
            elif method == 'GET' and c == '/api/state':
                self._json(api_state(self.data, q.get('project')))
            elif method == 'PUT' and c == '/api/state':
                self._json(api_state_put(root(q.get('project')), self.data, q.get('project', ''), self._body()))
            elif method == 'POST' and c == '/api/edits':
                self._json(api_edit_create(root(body_project()), self.data, self._body()), 201)
            elif method == 'POST' and re.fullmatch(r'/api/edits/[\w-]+/state', c):
                self._json(api_edit_state(self.data, c.split('/')[3], self._body()))
            elif method == 'GET' and re.fullmatch(r'/api/edits/[\w-]+', c):
                self._json(api_edit_detail(self.data, c.split('/')[3], int(q.get('width', '0')), root_of(c.split('/')[3])))
            elif method == 'GET' and re.fullmatch(r'/api/edits/[\w-]+/procedure', c):
                self._json(api_procedure(self.data, c.split('/')[3], root_of(c.split('/')[3])))
            elif method == 'GET' and c == '/api/catalog':
                self._json(api_catalog(root(q.get('project')), q.get('project', '')))
            elif method == 'POST' and c == '/api/layout':
                self._json(api_layout(root(body_project()), self._body()))
            elif method in ('POST', 'PUT') and re.fullmatch(r'/api/edits/[\w-]+/after', c):
                self._json(api_edit_after(self.data, c.split('/')[3], self._body()))
            elif method == 'POST' and re.fullmatch(r'/api/edits/[\w-]+/check-export', c):
                self._json(api_check_export(root_of(c.split('/')[3]), self.data, c.split('/')[3], self._body()))
            elif method == 'POST' and re.fullmatch(r'/api/edits/[\w-]+/upload', c):
                self._json(api_upload(root_of(c.split('/')[3]), self.data, c.split('/')[3], self.mcp, self.mcp_timeout,
                                      self.tia_status.get('portalId') if self.tia_status.get('tia') == 'attached' else None))
            elif method == 'POST' and re.fullmatch(r'/api/edits/[\w-]+/check', c):
                self._json(api_check(root_of(c.split('/')[3]), self.data, c.split('/')[3], self.mcp, self.mcp_timeout))
            elif method == 'GET' and c == '/api/projects':
                self._json(api_projects(self.root, self.data, self.jobs))
            elif method == 'POST' and c == '/api/projects':
                b = self._body() if isinstance(self._body(), dict) else {}
                if b.get('source') == 'exports':
                    self._json(api_project_add_exports(self.data, b), 201)
                elif b.get('source') in ('tia', 'ap21'):
                    self._json({'job': api_snapshot(self, None, b, b['source'])}, 202)
                else:
                    raise state.StateError('source must be exports | tia | ap21', 400)
            elif method == 'DELETE' and re.fullmatch(r'/api/projects/[^/]+', c):
                self._json(api_project_delete(self.data, unquote(c.split('/')[3])))
            elif method == 'POST' and re.fullmatch(r'/api/projects/[^/]+/snapshot', c):
                self._json({'job': api_snapshot(self, unquote(c.split('/')[3]), self._body())}, 202)
            elif method == 'GET' and re.fullmatch(r'/api/projects/[^/]+/changelog', c):
                name = unquote(c.split('/')[3])
                r = api_changelog(root(name), self.data, name, self.jobs, q.get('since'), q.get('until'), q.get('state'),
                                  q.get('block'))
                if q.get('format') == 'md':
                    self._json(changelog_markdown(name, r['items']), content_type='text/markdown')
                else:
                    self._json(r)
            elif method == 'GET' and re.fullmatch(r'/api/projects/[^/]+/snapshots', c):
                name = unquote(c.split('/')[3])
                self._json(api_snapshots(root(name), name))
            elif method == 'GET' and re.fullmatch(r'/api/projects/[^/]+/compare', c):
                name = unquote(c.split('/')[3])
                self._json(api_compare(root(name), self.data, name, q.get('from', ''), q.get('to', ''), q.get('block')))
            elif method == 'GET' and c == '/api/jobs':
                self._json({'jobs': list(self.jobs.values())})
            elif method == 'GET' and re.fullmatch(r'/api/jobs/[\w-]+', c):
                self._json(self.jobs[c.split('/')[3]])
            elif method == 'GET' and c == '/api/tia/status':
                st = refresh_status(self, q.get('portalId')) if q.get('refresh') == '1' or q.get('portalId')                     else dict(self.tia_status)
                self._json(dict(st, **ai_status(self)))
            elif method == 'GET' and c == '/api/ai/clients':
                self._json(ai_status(self))
            elif method == 'POST' and c == '/api/ai/clients':
                self._json(api_ai_heartbeat(self, self._body()))
            elif method == 'DELETE' and re.fullmatch(r'/api/ai/clients/[\w-]+', c):
                self.ai_clients.pop(c.split('/')[4], None)
                self._json(ai_status(self))
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

    def do_DELETE(self):
        self._handle('DELETE')


def create_server(port, root, data, host='127.0.0.1', mcp=None, mcp_timeout=300, autoconnect=False, ai_timeout=AI_TIMEOUT):
    """autoconnect: check the TIA status once in the background at start (and the home page checks it when opened)."""
    h = type('H', (Handler,), {'root': root, 'data': data, 'mcp': mcp, 'mcp_timeout': mcp_timeout, 'jobs': {},
                               'ai_clients': {}, 'ai_timeout': ai_timeout,
                               'tia_status': {'tia': 'unknown', 'checked': False, 'autoconnect': autoconnect}})
    sync_registry(root, data)
    srv = ThreadingHTTPServer((host, port), h)
    if autoconnect:
        threading.Thread(target=refresh_status, args=(h,), daemon=True).start()
    return srv


def mcp_config(mcp_json, writer_cmd=None, reader_cmd=None):
    """Writer / Reader commands: from .mcp.json (keys TIAC_Writer, tia-portal), overridden by --writer-cmd / --reader-cmd."""
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
    ap.add_argument('--no-autoconnect', action='store_true',
                    help='do not check TIA Portal at start and when the home page opens (only the Refresh button does)')
    ap.add_argument('--ai-timeout', type=float, default=AI_TIMEOUT,
                    help='forget an AI client (mcp_server.py) after this many seconds without a heartbeat')
    a = ap.parse_args()
    srv = create_server(a.port, a.export_root, a.data, mcp=mcp_config(a.mcp_json, a.writer_cmd, a.reader_cmd),
                        mcp_timeout=a.mcp_timeout, autoconnect=not a.no_autoconnect, ai_timeout=a.ai_timeout)
    print('TIAC_Editor at http://localhost:%d  (export root %s, data %s)' % (a.port, a.export_root, a.data), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    sys.exit(main())
