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
import hmi
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


_INDEX = {}                          # snapshot folder -> (mtime, {block: path})


def project_index(root, project):
    """{ts: {block: path}} of one project folder <root>/<project> (do not change the dicts: they are cached).
    shortcut: a snapshot folder other than the newest is walked again only when its own mtime changes (not a deeper one)
    — exports always go into a new folder; drop the cache if snapshots are ever edited in place."""
    res, pd = {}, os.path.join(root, project)
    if not os.path.isdir(pd):
        return res
    folders = sorted(ts for ts in os.listdir(pd) if TS.match(ts))
    for ts in folders:
        folder = os.path.join(pd, ts)
        mt, c = os.path.getmtime(folder), _INDEX.get(folder)
        if not c or c[0] != mt or ts == folders[-1]:  # the newest one may still be being written
            blocks = {}
            for dp, _, files in os.walk(folder):
                for f in files:
                    if f.endswith('.s7dcl'):
                        blocks.setdefault(f[:-6], os.path.join(dp, f))
            c = _INDEX[folder] = (mt, blocks)
        if c[1]:
            res[ts] = c[1]
    return res


def project_root(root, data, project):
    """Export root of a project: from the registry, otherwise the server's default --export-root. The name is checked:
    it is joined into paths."""
    if project:
        state.check_name(project)
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
        x = blocks.get(display_name) and catalog._read_cache(blocks[display_name], lambda c: catalog.interface(s7dcl.read(c)))
        if x:
            if x['kind'] == 'FC':
                fc.add(display_name)
            return x['instance_type']
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


def api_blocks(root, project, ts=None):
    """Headers of all blocks (the newest copy of each, or the blocks of export ts) in one request (MCP list_blocks);
    only the block header is parsed."""
    exp = project_index(root, project)
    if not exp:
        raise KeyError('unknown project')
    if ts and ts not in exp:
        raise KeyError('project %s has no export %s (exports: %s)' % (project, ts, ', '.join(sorted(exp))))
    files = {}
    for t in sorted(exp):
        if not ts or t == ts:
            files.update({b: (t, f) for b, f in exp[t].items()})
    res = []
    for block, (t, fpath) in sorted(files.items()):
        try:
            text = s7dcl.read(fpath)
            m = re.search(r'^[ \t]*(?:NETWORK|BEGIN)[ \t]*$', text, re.M)
            b = s7dcl.parse_block(text[:m.start()] if m else text)
        except (s7dcl.SourceParseError, OSError, UnicodeDecodeError) as e:
            res.append({'block': block, 'error': str(e)})
            continue
        res.append({'block': block, 'name': b['name'], 'kind': b['kind'], 'number': b['number'], 'language': b['language'],
                    'title': s7dcl.text_mlc(s7dcl.load_s7res(fpath[:-6] + '.s7res'), b['attributes'].get('S7_BlockTitle')),
                    'networks': len(re.findall(r'^[ \t]*NETWORK[ \t]*$', text, re.M)), 'export_ts': t})
    return {'project': project, 'export_ts': ts or 'newest export of each block', 'blocks': res}


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


TEXT_FIELDS = ('operand', 'op', 'in1', 'in2', 'bit', 'below', 'call', 'name', 'instance', 'template')


def verify_model(after):
    """The "after" model from the API (trust boundary): elements with id/type, boxes with pins, rungs only of existing ids and wire#."""
    error = state.StateError('after must be a network model {elements:[{id, type, …}], rungs:[{from, path, to}]}', 400)
    if not isinstance(after, dict) or not isinstance(after.get('elements'), list) or not isinstance(after.get('rungs'), list):
        raise error
    ids = set()
    for p in after['elements']:
        if not isinstance(p, dict) or not isinstance(p.get('id'), str) or not isinstance(p.get('type'), str) \
                or p['id'].startswith('x:') or p['id'] in ids \
                or any(p.get(k) is not None and not isinstance(p[k], str) for k in TEXT_FIELDS) \
                or p['type'] == 'comparison' and p.get('op') not in s7dcl.COMPARISONS.values():
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


def api_state_tia(root, data, project, body):
    """PUT /api/state/tia?project= {softwarePath, plcName?, groupPath?}: only the TIA settings change, under the lock
    (empty values are dropped, {} clears them)."""
    if not project_index(root, project):
        raise KeyError('unknown project')
    if not isinstance(body, dict) or set(body) - {'softwarePath', 'plcName', 'groupPath'} \
            or not all(isinstance(v, str) for v in body.values()):
        raise state.StateError('tia must be {softwarePath, plcName?, groupPath?} (strings)', 400)
    t = {k: v.strip() for k, v in body.items() if v.strip()}
    with state.LOCK:
        s = state.load(data, project)
        s['tia'] = t
        if not t:
            del s['tia']
        state.save(data, project, s)
    return {'project': project, 'tia': s.get('tia')}


def _after_model(after, language):
    """"after" from the API (trust boundary) -> stored model with wires/layout; wrong shape = 400."""
    if language == 'FBD':
        if not isinstance(after, dict):
            raise state.StateError('after must be an FBD model {language: "FBD", elements: [...]}', 400)
        return _fbd_after(dict(after, language='FBD'))
    verify_model(after)
    m = {'elements': after['elements'], 'rungs': after['rungs']}
    m['wires'] = s7dcl.wires(m)
    m['layout'] = _layout(m)[0]                     # the client's layout is not trusted
    return m


def _before(u):
    """The "before" model of an edit with a model: a new network starts empty."""
    return dict(_empty_model(u['after']), export_ts=u['before'].get('export_ts')) if u.get('kind') == 'new_network' else u['before']


def _empty_model(after):
    return {'language': 'FBD', 'elements': [], 'signals': [], 'layout': {}} if fbd.is_fbd(after) else \
        {'elements': [], 'rungs': [], 'wires': [], 'layout': {}}


# ---------- block interface (E2) ----------

SECTIONS = {'Input': 'inputs', 'Output': 'outputs', 'InOut': 'inout', 'Static': 'static', 'Temp': 'temp',
            'Constant': 'constants'}


def block_interface(root, project, block, ts=None):
    """-> (export ts, {Section: [{name, type}]}) from the block's `.s7dcl` header (top-level members)."""
    t, fpath, _ = find_block(root, project, block, ts)
    r = catalog.interface(s7dcl.read(fpath)) or {'section': {}}
    inv = {v: k for k, v in SECTIONS.items()}
    return t, {inv[k]: v for k, v in r['section'].items() if k in inv}


def _text(v, what, n=300, required=False, multiline=False, bad=''):
    """Text from the API: None / '' = None (or an error when required); one line unless multiline, at most n characters."""
    if v is None or v == '':
        if required:
            raise state.StateError('%s is required' % what, 400)
        return None
    if not isinstance(v, str) or len(v) > n or '\r' in v or '\n' in v and not multiline or any(c in v for c in bad):
        raise state.StateError('%s must be text%s (at most %d characters%s)' % (
            what, '' if multiline else ' on one line', n, ', without %s' % bad if bad else ''), 400)
    return v.strip('\r\n') if multiline else v.strip()


def _members(v, taken):
    """members from the API -> [{name, type, comment?, initial?}]; added members only, a name may not exist yet."""
    if not isinstance(v, list) or not v:
        raise state.StateError('members: [{name, type, comment?, initial?}] — at least one', 400)
    res, seen = [], {x.lower() for x in taken}
    for m in v:
        if not isinstance(m, dict) or not re.fullmatch(r'[A-Za-z_]\w{0,127}', str(m.get('name') or '')) \
                or not re.fullmatch(r'[^;:\r\n{}]{1,200}', str(m.get('type') or '').strip()):
            raise state.StateError('a member needs a name (identifier) and a type, e.g. {"name": "Aux", "type": "Bool"}', 400)
        if m['name'].lower() in seen:
            raise state.StateError('member %s is already in the interface (or twice in the edit)' % m['name'], 400)
        seen.add(m['name'].lower())
        x = {'name': m['name'], 'type': m['type'].strip()}
        for k, bad in (('comment', ''), ('initial', ';')):
            t = _text(m.get(k), 'member %s: %s' % (m['name'], k), 500, bad=bad)
            if t:
                x[k] = t
        res.append(x)
    return res


def _section(v):
    if v not in SECTIONS:
        raise state.StateError('section must be one of %s' % ', '.join(SECTIONS), 400)
    return v


def _interface_rows(u):
    """before = the export's interface, after = the same with the edit's members appended (added: true)."""
    before = u['before']['interface']
    after = {k: [dict(m) for m in v] for k, v in before.items()}
    after.setdefault(u['section'], []).extend(dict(m, added=True) for m in u['members'])
    return {'section': u['section'], 'before': before, 'after': after}


def _local_operands(x, res=None):
    """Names of local operands (#name, first component) used anywhere in a model; T#5s, 16#FF are literals."""
    res = set() if res is None else res
    if isinstance(x, dict):
        for k, v in x.items():
            if k not in ('id', 'type', 'call', 'name', 'template', 'power', 'signal', 'pin', 'op', 'layout', 'wires'):
                _local_operands(v, res)
    elif isinstance(x, list):
        for v in x:
            _local_operands(v, res)
    elif isinstance(x, str):
        res.update(m.strip('"') for m in re.findall(r'(?<![\w#])#("[^"]+"|[A-Za-z_]\w*)', x))
    return res


def _operand_warnings(root, data, u):
    """Local operands of "after" that the block interface lacks: defined by a pending interface edit, or not at all."""
    names = _local_operands(u.get('after'))
    if not names:
        return []
    try:
        known = {m['name'].lower() for v in block_interface(root, u['project'], u['block'])[1].values() for m in v}
        edits = [x for x in state.load(data, u['project'])['edits']
                 if x.get('kind') == 'interface' and x['block'] == u['block'] and x['state'] != 'Tested']
    except (KeyError, OSError, ValueError, s7dcl.SourceParseError, state.StateError):
        return []
    res = []
    for n in sorted(names, key=str.lower):
        if n.lower() in known:
            continue
        by = next((x for x in edits if any(m['name'].lower() == n.lower() for m in x.get('members') or [])), None)
        res.append('operand #%s is not in the block interface — %s' % (
            n, 'defined by edit %s (state %s)' % (by['id'], by['state']) if by else 'propose it first (interface edit)'))
    return res


def _new_interface(root, project, block, body):
    t, iface = block_interface(root, project, block)
    _, b = _load(root, project, block, t)
    if b['kind'] not in ('FB', 'FC', 'OB'):
        raise state.StateError('interface edits are for FB, FC and OB blocks (this one is %s)' % b['kind'], 400)
    return {'block': block, 'nw': None, 'title': '', 'section': _section(body.get('section')),
            'members': _members(body.get('members'), [m['name'] for v in iface.values() for m in v]),
            'before': {'interface': iface, 'export_ts': t}, '_block': b}


def _new_net(root, project, block, body):
    """New network after NW after_nw (0 = at the start): title, comment?, language (LAD|FBD, default the block's), after."""
    t, b = _load(root, project, block)
    try:
        after_nw = int(body.get('after_nw'))
    except (TypeError, ValueError):
        raise state.StateError('after_nw: the number of the network the new one follows (0 = at the start)', 400)
    if not 0 <= after_nw <= len(b['networks']):
        raise state.StateError('after_nw must be 0…%d (the block has %d networks)' % (len(b['networks']), len(b['networks'])), 400)
    language = body.get('language') or b['language']
    if language not in ('LAD', 'FBD'):
        raise state.StateError('language must be LAD or FBD (the block language is %s)' % (b['language'] or '?'), 400)
    u = {'block': block, 'nw': after_nw + 1, 'after_nw': after_nw,
         'after_title': b['networks'][after_nw - 1]['title'] if after_nw else '',
         'title': _text(body.get('title'), 'title', required=True), 'language': language,
         'after': _after_model(body.get('after'), language), 'before': {'export_ts': t}, '_block': b}
    comment = _text(body.get('comment'), 'comment', 2000, multiline=True)
    if comment:
        u['comment'] = comment
    if any(n['title'] == u['title'] for n in b['networks']):
        raise state.StateError('a network titled "%s" is already in the block — the export check finds a new network by '
                               'its title, give it a unique one' % u['title'], 400)
    return u


def _new_change(root, project, block, body):
    net = api_network(root, project, block, int(body.get('nw', 0)))
    if net['language'] == 'FBD' and net.get('signals') is not None:
        language = 'FBD'
    elif net['language'] != 'LAD':
        raise state.StateError('changes only for LAD or FBD networks (this one is %s)' % (net['language'] or '?'), 400)
    else:
        language = 'LAD'
    u = {'block': net['file'], 'nw': net['nw'], 'title': net['title'], 'comment_before': net['comment'] or '',
         'language': net['language'], 'f_net': net.get('f'), 'before': dict(_model(net), export_ts=net['ts'])}
    _texts_after(u, body)
    if body.get('after') is None and ('title_after' in u or 'comment_after' in u):    # only the title / comment
        u['after'] = _after_model(dict(_model(net), language=language), language)
    else:
        u['after'] = _after_model(body.get('after'), language)
    return u


def _texts_after(u, body):
    """title_after / comment_after of a network edit (E4): a new title / comment; '' or equal to the export = unchanged."""
    for k, old, what, n in (('title_after', 'title', 'title', 300), ('comment_after', 'comment_before', 'comment', 2000)):
        if k not in body or body[k] is None:
            continue
        v = _text(body[k], what, n, multiline=what == 'comment')
        if v and v != (u.get(old) or ''):
            u[k] = v
        else:
            u.pop(k, None)


def api_edit_create(root, data, body):
    if not isinstance(body, dict):
        raise state.StateError('the body must be a JSON object', 400)
    project, block = str(body.get('project', '')), str(body.get('block', ''))
    kind = body.get('kind') or 'network'
    if kind not in state.KINDS:
        raise state.StateError('kind must be one of %s' % ', '.join(state.KINDS), 400)
    state._file(data, project)                      # checks --data and the project name
    u = {'interface': _new_interface, 'network': _new_change, 'new_network': _new_net, 'hmi_screen': _new_hmi}[kind](
        root, project, block, body)
    b = u.pop('_block', None) or _load(root, project, u['block'])[1]
    f = bool(u.pop('f_net', None) or b['attributes'].get('S7_Safety') == 'TRUE' or body.get('f'))
    when, by = state.now(), str(body.get('by') or 'editor')[:80]
    with state.LOCK:
        s = state.load(data, project)
        u.update({'id': state.new_id(data), 'project': project, 'round': str(body.get('round') or '—'), 'kind': kind,
                  'block_kind': b['kind'], 'block_number': b['number'], 'f': f, 'author': by, 'time': when,
                  'state': 'Proposal', 'note': str(body.get('note') or ''),
                  'history': [{'state': 'Proposal', 'time': when, 'by': by}], 'checks': []})
        s['edits'].append(u)
        state.save(data, project, s)
    return u


# ---------- HMI screens (H2): an edit of one WinCC Unified screen ----------

def _known_tags(root, project, hmi_name=None):
    """{tag name: data type} of the PLC tag tables and the HMI tag tables of the newest exports (binding suggestions)."""
    res = {t['name']: t['type'] for t in catalog.catalog(root, project)['tags']} if project_index(root, project) else {}
    try:
        res.update({t['name']: t['type'] for t in hmi.tags(hmi.find(root, project, None, hmi_name)[2])})
    except KeyError:
        pass
    return res


def _hmi_after(m, name):
    """The screen model from the API (trust boundary), named after the edit's screen; wrong shape = 400."""
    try:
        return hmi.verify(dict(m, name=name) if isinstance(m, dict) else m)
    except ValueError as e:
        raise state.StateError(str(e), 400)


def _preset_model(root, project, key, name, params):
    """A preset's screen model; params {machine?, tags?: [names], width?, height?, screens?: [names]} from the API."""
    p = params if isinstance(params, dict) else {}
    tg, scr = p.get('tags') or [], p.get('screens')
    if not isinstance(tg, list) or len(tg) > 200 or not all(isinstance(t, str) and 0 < len(t) <= 300 for t in tg):
        raise state.StateError('params.tags must be a list of tag names', 400)
    if scr is not None and not (isinstance(scr, list) and all(isinstance(x, str) and x for x in scr)):
        raise state.StateError('params.screens must be a list of screen names', 400)
    known = _known_tags(root, project)
    try:
        return hmi.preset(key, name, _text(p.get('machine'), 'machine', 80) or 'Machine',
                          [{'name': t, 'type': known.get(t.strip('"'), '')} for t in tg],
                          int(p.get('width') or 1280), int(p.get('height') or 800), scr)
    except (ValueError, TypeError) as e:
        raise state.StateError(str(e), 400)


def api_hmi_preset(root, body):
    """POST /api/hmi/preset {project, preset, screen?, params?} -> the screen model of a preset (saves nothing)."""
    b = body if isinstance(body, dict) else {}
    return _preset_model(root, str(b.get('project') or ''), b.get('preset'), _text(b.get('screen'), 'screen', 128) or 'New_screen',
                         b.get('params'))


def _new_hmi(root, project, block, body):
    """An HMI screen edit: a change of an exported screen (new: false, "after" defaults to the export) or a new screen
    (new: true) from "after" or a preset."""
    new = bool(body.get('new'))
    name = _text(body.get('screen'), 'screen', 128, required=True, bad='\\/:*?"<>|')
    try:
        t, h, folder = hmi.find(root, project, None, body.get('hmi') or None)
    except KeyError:
        if not new or not body.get('hmi'):
            raise
        t, h, folder = None, _text(body['hmi'], 'hmi', 128, bad='\\/:*?"<>|'), None
    exported = next((s for s in hmi._screens(folder)[0] if s['name'] == name), None) if folder else None
    if new and exported:
        raise state.StateError('screen %s is already in the HMI export %s — propose a change of it (new: false)' % (name, t), 400)
    if not new and not exported:
        raise state.StateError('screen %s is not in the HMI export %s of %s (a new screen: new: true)' % (name, t, h), 400)
    if body.get('preset'):
        after = _preset_model(root, project, body['preset'], name, body.get('params'))
    elif body.get('after') is not None or new:
        if body.get('after') is None:
            raise state.StateError('after: the screen model is required for a new screen (or a preset)', 400)
        after = _hmi_after(body['after'], name)
    else:
        after = exported
    return {'block': h, 'nw': None, 'title': name, 'hmi': h, 'screen': name, 'new': new, 'after': after,
            'before': {'export_ts': t, 'screen': exported}, '_block': {'kind': '', 'number': None, 'attributes': {}}}


HMI_ATTR = {'left': 'Left', 'top': 'Top', 'width': 'Width', 'height': 'Height', 'text': 'Text', 'background': 'BackColor'}


def hmi_writes(u):
    """Writer calls for an HMI edit: SetHmiItemAttribute / SetHmiDynamizationTag for every changed attribute of the screen
    and of its existing items. StateError 409 when the edit needs what the Writer cannot do (create a screen or an item,
    delete an item, change a type or a font, add or remove a binding or a color)."""
    c = hmi.compare(u['before'].get('screen'), u['after'])
    why = (['a new screen'] if u.get('new') else []) + ['new item %s' % n for n in c['added']] + \
        ['deleted item %s' % n for n in c['deleted']] + \
        ['%s: %s' % (n, x['attr']) for n, ch in [('screen', c['screen'])] + list(c['changed'].items()) for x in ch
         if x['attr'] == 'type' or x['attr'].startswith('font.') or x['attr'] != 'text' and None in (x['before'], x['after'])]
    if why:
        raise state.StateError('Upload cannot make this edit: the Writer has no tool to create screens and items yet '
                               '(ImportHmiScreen) and none for %s — make the change by hand following the TIA steps'
                               % ', '.join(why))
    calls = []
    for n, ch in [('', c['screen'])] + list(c['changed'].items()):
        for x in ch:
            a, base = x['attr'], {'softwarePath': u['hmi'], 'screenName': u['screen'], 'itemName': n, 'dryRun': False}
            if a.startswith('binding '):
                calls.append(('SetHmiDynamizationTag', dict(base, property=a[8:], tag=x['after'])))
            else:
                attr = a[7:8].upper() + a[8:] if a.startswith('colors.') else HMI_ATTR[a]
                value = hmi.tia_color(x['after']) if a.startswith('colors.') or a == 'background' else str(x['after'] or '')
                calls.append(('SetHmiItemAttribute', dict(base, attribute=attr, value=value, **({'culture': 'en-US'} if a == 'text' else {}))))
    return calls


def _upload_hmi(data, uid, u, mcp, timeout, portal):
    """Approved HMI edit -> TIAC_Writer: GetPortals, Connect, one call per changed attribute -> Signed (by TIAC_Writer)."""
    calls = hmi_writes(u)
    if not TIA_LOCK.acquire(blocking=False):
        raise state.StateError('another Upload / Check is running — wait until it ends')
    steps = []
    try:
        try:
            with _client(mcp, 'writer', timeout) as k:
                _connect(k, steps, portal)
                for nm, arg in calls:
                    _step(steps, '%s %s %s' % (nm, arg['itemName'] or u['screen'], arg.get('attribute') or arg.get('property')),
                          lambda: k.call(nm, arg))
            ok, message = True, ('%d attribute(s) written to screen %s of %s — compile the HMI and save the project in TIA '
                                 '(the Writer does not call SaveProject)' % (len(calls), u['screen'], u['hmi']))
        except mcp_client.MCPError as e:
            ok, message = False, CONNECTION_ERROR + str(e)
        except Exception as e:                        # whatever stops it must be recorded
            ok, message = False, 'Upload stopped: %s' % e
        write = {'time': state.now(), 'tool': 'TIAC_Writer', 'steps': steps, 'ok': ok, 'message': message}
        u = _write(data, uid, write, 'Signed' if ok else None, 'TIAC_Writer')
    finally:
        TIA_LOCK.release()
    return {'ok': ok, 'message': write['message'], 'write': write, 'edit': u}


def _check_hmi(root, data, uid, u, body):
    """HMI edit: the screen in the HMI export (newest / given) has the items of the proposal (hmi.check)."""
    t, _, folder = hmi.find(root, u['project'], str(body['export_ts']) if body.get('export_ts') else None, u['hmi'])
    match, diffs = hmi.check(u['after'], next((s for s in hmi._screens(folder)[0] if s['name'] == u['screen']), None))
    note = 'export = proposal (%d items)' % len(u['after']['items']) if match else 'MISMATCH: %s' % '; '.join(diffs[:20])
    if t == u['before'].get('export_ts'):
        note += ' · note: the export is the same as before the change'
    u = api_add_check(data, {'edit_id': uid, 'match': match, 'export_ts': t, 'note': note})
    return {'match': match, 'export_ts': t, 'nw_export': None, 'note': note, 'diffs': diffs, 'edit': u}


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


def _validate(after, f=False, root=None, project=None, before=None):
    if 'items' in after:                              # an HMI screen
        return hmi.validate(after, set(_known_tags(root, project)) if root else None)
    if fbd.is_fbd(after):
        return fbd.validate(after, f, _non_f(root, project) if f and root else (), before)
    return model.validate(after)


def api_edit_detail(data, uid, width=0, root=None):
    """Change + "after" drawing with the diff, LAD rule errors, state transitions."""
    _, s, u = state.find(data, uid)
    if u.get('kind') == 'interface':
        res = {'interface': _interface_rows(u), 'diff': {}, 'drawing': None, 'elements': [], 'rungs': [], 'warnings': []}
    elif u.get('kind') == 'hmi_screen':
        b = u['before'].get('screen')
        c = hmi.compare(b, u['after'])
        res = {'screen_diff': c, 'ghosts': [i for i in (b or {}).get('items', []) if i['name'] in c['deleted']],
               'diff': {}, 'drawing': None, 'elements': [], 'rungs': [], 'warnings': []}
        try:
            hmi_writes(u)
        except state.StateError as e:
            res['upload_note'] = str(e)
    else:
        res = _diff_drawing(_before(u), u['after'], width)
        if root:
            res['warnings'] += _operand_warnings(root, data, u)
        if 'title_after' in u or 'comment_after' in u:
            res['texts'] = {k: [u.get(old) or '', u.get(k, u.get(old) or '')]
                            for k, old in (('title_after', 'title'), ('comment_after', 'comment_before')) if k in u}
    res['tia'] = s.get('tia')
    k = u.get('checks') or []
    if state.STATES.index(u['state']) >= state.STATES.index('Checked') and k and k[-1].get('match') is False:
        res['warnings'].insert(0, 'The last export check is a MISMATCH, but the change is already %s — the state does not go '
                                     'back by itself; check what was written to the project.' % u['state'])
    following, previous = state.transitions(u)
    res.update({'edit': u, 'next': following, 'previous': previous, 'checked_allowed': state.checked_allowed(u),
                'errors': _validate(u['after'], u.get('f'), root, u['project'], _before(u)) if 'after' in u else []})
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
                'errors': _validate(after, f, root, str(body.get('project', '')), before)})
    return res


def _norm_type(t):
    return re.sub(r'\s+', '', str(t or '')).strip('"').lower()


def _check_interface(root, data, uid, u, body):
    """Interface edit: every member is in its section of the export with the same type (order does not matter)."""
    t, iface = block_interface(root, u['project'], u['block'], str(body['export_ts']) if body.get('export_ts') else None)
    diffs = []
    for m in u['members']:
        where = {sec: x for sec, v in iface.items() for x in v if x['name'].lower() == m['name'].lower()}
        if not where:
            diffs.append('missing in the export: %s : %s (%s)' % (m['name'], m['type'], u['section']))
        elif u['section'] not in where:
            diffs.append('%s is in section %s, the proposal says %s' % (m['name'], ', '.join(where), u['section']))
        elif _norm_type(where[u['section']]['type']) != _norm_type(m['type']):
            diffs.append('different type: %s : %s in the export, %s in the proposal' % (m['name'], where[u['section']]['type'], m['type']))
    match = not diffs
    note = ('export = proposal (%d member%s in %s)' % (len(u['members']), '' if len(u['members']) == 1 else 's', u['section'])
            if match else 'MISMATCH: %s' % '; '.join(diffs))
    if t == u['before'].get('export_ts'):
        note += ' · note: the export is the same as before the change'
    u = api_add_check(data, {'edit_id': uid, 'match': match, 'export_ts': t, 'note': note})
    return {'match': match, 'export_ts': t, 'nw_export': None, 'note': note, 'diffs': diffs, 'edit': u}


def api_check_export(root, data, uid, body):
    """Compare the change's network in the export (newest / given) with the "after" model by content
    and record the result as a check (POST /api/check). The network is found by title, then by number."""
    if not isinstance(body, dict):
        raise state.StateError('the body must be a JSON object', 400)
    _, _, u = state.find(data, uid)
    if u.get('kind') == 'interface':
        return _check_interface(root, data, uid, u, body)
    if u.get('kind') == 'hmi_screen':
        return _check_hmi(root, data, uid, u, body)
    t, b = _load(root, u['project'], u['block'], str(body['export_ts']) if body.get('export_ts') else None)
    n, cand = _find_network(b['networks'], u)
    notes, new, language = [], u.get('kind') == 'new_network', 'FBD' if fbd.is_fbd(u['after']) else 'LAD'
    if new and n:                                     # a new network is found only by its title; its number is a note
        notes.append('the new network is NW %d in the export%s' % (
            n['nw'], '' if n['nw'] == u['nw'] else ' (expected NW %d, after NW %d)' % (u['nw'], u['after_nw'])))
    elif cand and n['nw'] != u['nw']:
        notes.append('the network moved: NW %d → NW %d (by title "%s")' % (u['nw'], n['nw'], n['title']))
    elif not cand and not new and u.get('title'):
        notes.append('title "%s" is not in the export — compared by number NW %d' % (u['title'], u['nw']))
    if len(cand) > 1:
        notes.append('the title occurs %d× in the block, compared NW %d' % (len(cand), n['nw']))
    if n is None:
        match, diffs = False, ['no network titled "%s" in the export' % u['title'] if new
                               else 'network %d is not in the export' % u['nw']]
    elif n['language'] != language or 'elements' not in n:
        match, diffs = False, ['NW %d in the export is %s' % (n['nw'], 'in %s' % (n['language'] or '?') if n['language'] != language
                                                              else 'unreadable (%s)' % n.get('error'))]
    else:
        match, diffs = _matcher(u['after'])(u['after'], n)
    if n is not None:
        for k, have, what in (('title_after', n['title'], 'title'), ('comment_after', n.get('comment') or '', 'comment')):
            if k in u and _lines(have) != _lines(u[k]):
                match = False
                diffs = diffs + ['%s in the export "%s", the proposal "%s"' % (what, have, u[k])]
    if t == u['before'].get('export_ts'):
        notes.append('note: the export is the same as before the change')
    note = ('export = proposal' if match else 'MISMATCH: %s' % '; '.join(diffs)) + ''.join(' · ' + x for x in notes)
    u = api_add_check(data, {'edit_id': uid, 'match': match, 'export_ts': t, 'note': note})
    return {'match': match, 'export_ts': t, 'nw_export': n['nw'] if n else None, 'note': note,
            'diffs': diffs, 'edit': u}


def _find_network(networks, u):
    """The edit's network in a block -> (network | None, [networks with its title]): by title (after writing a title edit:
    the new title; a new network also by language), the one at the edit's number first; otherwise by number."""
    cand = [n for n in networks if u.get('title') and n['title'] == u['title']]
    if u.get('title_after'):
        cand = [n for n in networks if n['title'] == u['title_after']] or cand
    if u.get('kind') == 'new_network':
        cand = [n for n in cand if n['language'] == u['language']] or cand
    if cand:
        return next((x for x in cand if x['nw'] == u['nw']), cand[0]), cand
    return (networks[u['nw'] - 1] if u.get('kind') != 'new_network' and u['nw'] <= len(networks) else None), cand


def _matcher(m):
    """content_match for the language of a model."""
    return fbd.content_match if fbd.is_fbd(m) else diff.content_match


def _write_text(file, bom, text):
    with open(file, 'wb') as f:
        f.write((b'\xef\xbb\xbf' if bom else b'') + text.encode('utf-8'))


def _lines(t):
    """Text as `.s7res` keeps it: lines without their indentation, no empty lines at the ends."""
    return '\n'.join(x.strip() for x in t.splitlines()).strip()


def api_edit_after(data, uid, body, root=None):
    """Save a proposal (editor "Save proposal", update_edit): the "after" model of a network edit, the members/section of an
    interface edit, the note. Only in state Proposal; rule errors do not block saving."""
    if not isinstance(body, dict):
        raise state.StateError('the body must be a JSON object', 400)
    with state.LOCK:
        project, s, u = state.find(data, uid)
        if u['state'] != 'Proposal':
            raise state.StateError('the proposal can be changed only in state Proposal (now %s)' % u['state'])
        if u.get('kind') == 'hmi_screen':
            if body.get('after') is not None:
                u['after'] = _hmi_after(body['after'], u['screen'])
        elif u.get('kind') == 'interface':
            if body.get('section') is not None:
                u['section'] = _section(body['section'])
            if body.get('members') is not None:
                u['members'] = _members(body['members'], [m['name'] for v in u['before']['interface'].values() for m in v])
        elif body.get('after') is not None or u.get('kind') == 'network' and not (
                body.get('title_after') is not None or body.get('comment_after') is not None):
            if body.get('after') is None:
                raise state.StateError('after: the network model is required (or title_after / comment_after)', 400)
            after = _after_model(body['after'], 'FBD' if fbd.is_fbd(body['after']) else 'LAD')
            if fbd.is_fbd(after) != fbd.is_fbd(u['after']):
                raise state.StateError('the language of the proposal (LAD/FBD) must stay the same', 400)
            u['after'] = after
        if u.get('kind') == 'network':
            _texts_after(u, body)
        if u.get('kind') == 'new_network':
            if body.get('title') is not None:
                u['title'] = _text(body['title'], 'title', required=True)
            if body.get('comment') is not None:
                u['comment'] = _text(body['comment'], 'comment', 2000, multiline=True) or ''
        u['edited'] = state.now()
        if body.get('note') is not None:
            u['note'] = str(body['note'])
        state.save(data, project, s)
    return {'edit': u, 'errors': _validate(u['after'], u.get('f'), root, u['project'], _before(u)) if 'after' in u else []}


# ---------- Upload (TIAC_Writer) and Check (TIAC_Reader) ----------

TIA_LOCK = threading.Lock()          # one TIA instance = one Upload / Check at a time
CONNECTION_ERROR = 'TIA is busy or the Writer/Reader cannot be started: '


def _tia(s, project, data=''):
    t = s.get('tia') or next((p.get('tia') for p in state.registry(data) if p['name'] == project), None) or {}
    if not t.get('softwarePath'):
        raise state.StateError('project %s has no TIA settings (tia: {softwarePath, plcName, groupPath?}) — '
                                 'set them in the UI on a change ("TIA settings") or through PUT /api/state/tia' % project)
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


def _add_members_in_file(file, bom, text, u):
    """Interface edit: member lines into the VAR section of the exported .s7dcl; read back, every member must be there."""
    new_value = s7dcl_write.add_members(text, u['section'], u['members'])
    got = (catalog.interface(new_value.replace('\r\n', '\n')) or {'section': {}})['section'].get(SECTIONS[u['section']], [])
    have = {(x['name'].lower(), _norm_type(x['type'])) for x in got}
    missing = [m['name'] for m in u['members'] if (m['name'].lower(), _norm_type(m['type'])) not in have]
    if missing:
        raise s7dcl_write.WriteError('the written interface does not read back with %s' % ', '.join(missing))
    _write_text(file, bom, new_value)
    comments = [m['name'] for m in u['members'] if m.get('comment')]
    return '%d member(s) added to section %s%s' % (len(u['members']), u['section'], (
        ' — comments are not written to the document (%s): add them in TIA' % ', '.join(comments)) if comments else '')


def _insert_in_file(file, bom, text, b, u):
    """New network: inserted after NW after_nw (no title attribute — the title is set after the import through the Writer's
    SetNetworkText); the text is parsed again and the new network must match the proposal."""
    if u['after_nw'] > len(b['networks']):
        raise s7dcl_write.WriteError('NW %d is not in the exported block (it has %d networks)' % (u['after_nw'], len(b['networks'])))
    if u['after_nw'] and u.get('after_title') and b['networks'][u['after_nw'] - 1]['title'] != u['after_title']:
        raise s7dcl_write.WriteError('NW %d in the project is "%s", the proposal follows "%s" — the networks moved, propose again'
                                     % (u['after_nw'], b['networks'][u['after_nw'] - 1]['title'], u['after_title']))
    new_value = s7dcl_write.insert_network(text, u['after_nw'], s7dcl_write.network_text(u['after'], s7dcl_write.templates(text)), u['language'])
    nets = s7dcl.parse_block(new_value.replace('\r\n', '\n'))['networks']
    if len(nets) != len(b['networks']) + 1:
        raise s7dcl_write.WriteError('the block does not have one network more after the insertion')
    back = nets[u['after_nw']]
    m = _matcher(u['after'])(_without_call_names(u['after']), _without_call_names(back))
    if not m[0]:
        raise s7dcl_write.WriteError('the serialized proposal does not match the proposal: %s' % '; '.join(m[1]))
    _write_text(file, bom, new_value)
    return 'new %s network inserted as NW %d (after NW %d)' % (u['language'], u['after_nw'] + 1, u['after_nw'])


def _network_texts(u, file):
    """(network number, title, comment) to set through SetNetworkText after the import, or None."""
    if u.get('kind') == 'new_network':
        return u['after_nw'] + 1, u['title'], u.get('comment') or None
    if 'title_after' in u or 'comment_after' in u:   # the network as _replace_in_file found it
        n = _find_network(s7dcl.load_block(file)['networks'], u)[0]
        if n is None:
            raise s7dcl_write.WriteError('network %d is not in the written block' % u['nw'])
        return n['nw'], u.get('title_after'), u.get('comment_after')
    return None


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
    if u.get('kind') == 'interface':
        return _add_members_in_file(file, bom, text, u)
    if u.get('kind') == 'new_network':
        return _insert_in_file(file, bom, text, b, u)
    n = _find_network(b['networks'], u)[0]
    if n is None:
        raise s7dcl_write.WriteError('network %d ("%s") is not in the exported block' % (u['nw'], u.get('title') or ''))
    language = 'FBD' if fbd.is_fbd(u['after']) else 'LAD'
    if n['language'] != language:
        raise s7dcl_write.WriteError('NW %d is %s in the project, the proposal is %s' % (n['nw'], n['language'] or '?', language))
    match = _matcher(u['after'])
    if match(_without_call_names(u['after']), _without_call_names(u['before']))[0]:   # only the title / comment changes
        return 'NW %d "%s": the logic is unchanged, only the network text is set' % (n['nw'], n['title'])
    if 'elements' not in n or not match(_without_call_names(u['before']), _without_call_names(n))[0]:
        raise s7dcl_write.WriteError('NW %d "%s" changed in the project since the proposal (its base export %s) — '
                                     'propose the change again from a fresh snapshot' % (n['nw'], n['title'],
                                                                                         u['before'].get('export_ts')))
    new_value = s7dcl_write.replace_network(text, n['nw'], s7dcl_write.network_text(u['after'], s7dcl_write.templates(text)))
    back = s7dcl.parse_block(new_value.replace('\r\n', '\n'))['networks'][n['nw'] - 1]
    ok, diffs = match(_without_call_names(u['after']), _without_call_names(back))
    if not ok:
        raise s7dcl_write.WriteError('the serialized proposal does not match the proposal: %s' % '; '.join(diffs))
    _write_text(file, bom, new_value)
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


def _step(steps, name, fn):
    """Run one Writer / Reader call and record it in steps, also when it fails."""
    try:
        v = fn()
    except Exception as e:
        steps.append({'step': name, 'ok': False, 'message': str(e)[:1000]})
        raise
    steps.append({'step': name, 'ok': True, 'message': str(v)[:1000]})
    return v


def _pick_portal(inst, wanted=None):
    """The wanted TIA Portal instance, else the only one running; None = the user has to choose."""
    return next((x for x in inst if wanted and str(x['portalId']) == str(wanted)), None) or (inst[0] if len(inst) == 1 else None)


def _connect(k, steps, portal):
    """GetPortals first — Connect without portalId would start TIA Portal when none is running — then Connect {portalId}."""
    inst = portal_instances(_step(steps, 'GetPortals', lambda: k.call('GetPortals', {}))) or []
    if not inst:
        raise SnapshotError('TIA Portal is not running — open the project in TIA Portal first (Upload does not start TIA)')
    chosen = _pick_portal(inst, portal)
    if chosen is None:
        raise SnapshotError('More than one TIA Portal instance is running — choose one in the status bar')
    _step(steps, 'Connect', lambda: k.call('Connect', {'portalId': chosen['portalId']}))


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
    if u.get('kind') == 'hmi_screen':
        return _upload_hmi(data, uid, u, mcp, timeout, portal)
    tia = _tia(s, u['project'], data)
    if not TIA_LOCK.acquire(blocking=False):
        raise state.StateError('another Upload / Check is running — wait until it ends')
    steps, work = [], tempfile.mkdtemp(prefix='tiac_upload_')

    def step(name, fn):
        return _step(steps, name, fn)
    try:
        sp, block = tia['softwarePath'], u['block']
        try:
            with _client(mcp, 'writer', timeout) as k:
                _connect(k, steps, portal)
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
                step({'interface': 'Add interface members in .s7dcl', 'new_network': 'Insert the network in .s7dcl'}.get(
                    u.get('kind'), 'Replace the network in .s7dcl'),
                     lambda: _replace_in_file(files[0], u))
                preview = step('PreviewImport', lambda: k.call('PreviewImport', {
                    'softwarePath': sp, 'importPath': folder, 'kind': 'block', 'groupPath': grp}))
                step('ImportFromDocuments', lambda: k.call('ImportFromDocuments', {
                    'softwarePath': sp, 'groupPath': grp, 'importPath': folder, 'fileNameWithoutExtension': block,
                    'importOption': 'Override'}))
                texts = _network_texts(u, files[0])
                if texts:                             # title / comment: the Writer edits the texts of the imported block
                    arg = {'softwarePath': sp, 'blockPath': (grp + '/' if grp else '') + block, 'network': texts[0],
                           'dryRun': False}
                    arg.update({k_: v for k_, v in (('title', texts[1]), ('comment', texts[2])) if v is not None})
                    step('SetNetworkText', lambda: k.call('SetNetworkText', arg))
            ok, message = True, ('written to the project (%s, group %s) — save the project in TIA (the Writer does not call '
                                   'SaveProject)') % (sp, grp or '/')
        except mcp_client.MCPError as e:
            ok, message, preview = False, CONNECTION_ERROR + str(e), None
        except SnapshotError as e:
            ok, message, preview = False, str(e), None
        except Exception as e:                        # whatever stops it (also after the import) must be recorded
            ok, message, preview = False, 'Upload stopped: %s' % e, None
        write = {'time': state.now(), 'tool': 'TIAC_Writer', 'steps': steps, 'ok': ok, 'message': message}
        if preview is not None:
            write['preview'] = preview[:4000]
        u = _write(data, uid, write, 'Signed' if ok else None, 'TIAC_Writer')
    finally:
        shutil.rmtree(work, ignore_errors=True)
        TIA_LOCK.release()
    return {'ok': ok, 'message': write['message'], 'write': write, 'edit': u}


def api_check(root, data, uid, mcp, timeout=300):
    """Signed change -> TIAC_Reader export_to_folder (block into a new export root folder) -> export check
    -> on a match Checked (by TIAC_Reader); on a mismatch it stays Signed and the check is recorded as a mismatch."""
    project, s, u = state.find(data, uid)
    if u['state'] != 'Signed':
        raise state.StateError('Check only in state Signed (now %s)' % u['state'])
    if u.get('kind') == 'hmi_screen':                 # the screens of the HMI into a new folder of the export root
        tool, arg, index = 'export_hmi_to_folder', {'hmiName': u['hmi'], 'include': ['screens'], 'pathPrefixes': ['Screens']}, \
            lambda: set(hmi.exports(root, project))
    else:
        tia = _tia(s, project, data)
        try:                                          # PLC and group from the existing export of the block
            t0, fpath, _ = find_block(root, project, u['block'])
            parts = os.path.relpath(fpath, os.path.join(root, project, t0))[:-len('.s7dcl')].replace('\\', '/').split('/')
        except KeyError:
            parts = [tia.get('plcName') or tia['softwarePath'], 'Blocks', u['block']]
        if tia.get('groupPath'):
            parts = parts[:2] + [x for x in tia['groupPath'].split('/') if x] + [u['block']]
        tool, arg, index = 'export_to_folder', {'pathPrefixes': ['/'.join(parts)], 'include': ['blocks'], 'formats': ['documents'],
                                                'plcName': tia.get('plcName') or parts[0]}, lambda: set(project_index(root, project))
    if not TIA_LOCK.acquire(blocking=False):
        raise state.StateError('another Upload / Check is running — wait until it ends')
    steps, k_res = [], None
    try:
        before = index()
        try:
            with _client(mcp, 'reader', timeout) as k:
                texts = _reader_export(k, arg, steps, tool=tool)
            new_items = index() - before
            ts = [x for x in re.findall(r'\d{8}-\d{6}', ' '.join(texts)) if x in new_items] or sorted(new_items)
            if not ts:
                raise s7dcl_write.WriteError("no Reader export found in the editor's export root (%s\\%s) — response: %s"
                                              % (root, project, texts[-1][:300] if texts else ''))
            k_res = api_check_export(root, data, uid, {'export_ts': ts[-1]})
            steps.append({'step': 'Export check %s' % ts[-1], 'ok': k_res['match'], 'message': k_res['note']})
            ok, message = k_res['match'], ('export = proposal' if k_res['match'] else k_res['note'])
        except mcp_client.MCPError as e:
            ok, message = False, CONNECTION_ERROR + str(e)
        except (SnapshotError, s7dcl_write.WriteError, s7dcl.SourceParseError, KeyError, OSError) as e:
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
            chosen = _pick_portal(inst, portal_id)
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
    """Recompute the TIA status (holding TIA_LOCK: no job and no other refresh starts a Writer meanwhile) and keep it
    as the cached one; while TIA is busy the cached one is returned."""
    if not TIA_LOCK.acquire(blocking=False):
        return dict(h.tia_status, busy=True)
    try:
        chosen = portal_id or h.tia_status.get('chosen')
        st = dict(api_tia_status(h.mcp, chosen), chosen=chosen, autoconnect=h.tia_status.get('autoconnect'))
        h.tia_status.update(st)                       # one call, always the same keys: readers never see it half done
    finally:
        TIA_LOCK.release()
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
    running = [j for j in list(jobs.values()) if j['state'] == 'running']
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


JOBS_KEPT = 50                       # finished jobs kept in memory (GET /api/jobs, changelog)


def start_job(jobs, kind, project, fn):
    """Background job under TIA_LOCK (one Upload / Check / snapshot at a time) -> job record (GET /api/jobs/<id>)."""
    if not TIA_LOCK.acquire(blocking=False):
        raise state.StateError('another Upload / Check / snapshot is running — wait until it ends')
    for k in [k for k, j in list(jobs.items()) if j['state'] != 'running'][:-JOBS_KEPT]:
        jobs.pop(k, None)
    job = {'id': 'J%03d' % (max((int(k[1:]) for k in list(jobs)), default=0) + 1), 'kind': kind, 'project': project, 'state': 'running', 'steps': [],
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


def _reader_export(r, arg, steps, chunk=None, tool='export_to_folder'):
    """Reader export_to_folder (or export_hmi_to_folder) in chunks (complete: false -> continue with runId +
    offset=nextOffset) -> the answers; chunk(answer as a dict) is called after each one."""
    texts = []
    while True:
        t = _step(steps, '%s #%d' % (tool, len(texts) + 1), lambda: r.call(tool, arg))
        texts.append(t)
        j = _json_any(t)
        j = j if isinstance(j, dict) else {}
        if chunk:
            chunk(j)
        if j.get('complete', True) is not False:
            return texts
        if j.get('nextOffset') is None or len(texts) > 10000:
            raise SnapshotError('the Reader export is not complete and gives no nextOffset: %s' % t[:300])
        arg.update(runId=j.get('runId'), offset=j['nextOffset'])


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


def _inside(path, root):
    """Is path the root or below it? (different drives = no)"""
    a, b = (os.path.normcase(os.path.abspath(x)) for x in (path, root))
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:
        return False


def is_fake(mcp):
    """Writer or Reader command runs tests/fake_mcp.py -> the editor is a demo instance."""
    return any(os.path.basename(str(x)) == 'fake_mcp.py' for cfg in (mcp or {}).values() if cfg for x in cfg[0])


DEMO_REFUSED = 'demo instance: start the editor without fake Writer/Reader for real projects'


def snapshot(job, root, data, mcp, timeout, name=None, source='tia', path=None, portal=None, software_path=None,
             close=False):
    """TIA project -> Reader export of the whole PLC (chunked) -> registry. name None = a new project named by the Reader folder."""
    reg = next((p for p in state.registry(data) if p['name'] == name), None) if name else None
    if reg:
        source, path = reg.get('source') or source, path or reg.get('project_path')
        software_path = software_path or (reg.get('tia') or {}).get('softwarePath')
    steps = job['steps']

    def step(label, fn):
        return _step(steps, label, fn)
    if isinstance(portal, str):
        portal = int(portal) if portal.isdigit() else (portal or None)   # the Writer's portalId is a process id (integer)
    with _client(mcp, 'writer', timeout) as w:
        plcs, tree = [], ''
        if source == 'ap21':
            if not path:
                raise SnapshotError('project %s has no .ap21 path' % name)
            t = step('OpenTiaProject', lambda: w.call(
                'OpenTiaProject', dict({'path': path}, **({'portalId': portal} if portal else {})), max(timeout, OPEN_TIMEOUT)))
            portal = next((d['portalId'] for d in _dicts(_json_any(t)) if d.get('portalId') not in (None, '')), portal)
            plcs, tree = plc_list(t), t
        else:
            if not portal:
                inst = portal_instances(step('GetPortals', lambda: w.call('GetPortals', {})))
                if not inst:
                    raise SnapshotError('TIA Portal is not running — open the project in TIA Portal first '
                                        '(the editor does not start TIA for an open project)')
                if _pick_portal(inst) is None:
                    job['portal_choices'] = inst
                    raise SnapshotError('%d TIA Portal instances are running — choose one in the status bar' % len(inst))
                portal = inst[0]['portalId']
            step('Connect', lambda: w.call('Connect', {'portalId': portal}))
        pid = {'portalId': portal} if portal else {}
        pr = project_info(step('GetProject', lambda: w.call('GetProject', pid)))
        job['tia_project'] = pr['name']
        if not plcs:
            tree += step('GetProjectTree', lambda: w.call('GetProjectTree', pid))
            plcs = plc_list(tree)
        if not plcs:
            tree += step('GetDevices', lambda: w.call('GetDevices', pid))
            plcs = plc_list(tree)
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
            folders = []

            def chunk(j):                             # progress of the job while it runs
                job['chunks'] += 1
                job['exported'] += next((j[k] for k in ('exported', 'exportedCount', 'count') if isinstance(j.get(k), int)), 0)
                folders.append(_export_folder(j))
            t = _reader_export(r, arg, steps, chunk)[-1]
            folder = next((f for f in folders if f), None)
            if folder is None:                        # no folder in the answer -> a new timestamp in the project's root
                new = sorted(set(project_index(target, name)) - before) if target else []
                if not new:
                    raise SnapshotError('the Reader answer names no export folder and none appeared in the project — '
                                        'the Reader writes to its own --export-root (%s): %s'
                                        % (reader_export_root(mcp) or 'unknown', t[:300]))
                folder = (target, name, new[-1])
            # shortcut: "is there an HMI" = the project tree mentions HMI/Unified — use a typed device list when the
            # Writer gives one; the HMI dump goes into the same snapshot folder (runId = its timestamp)
            if re.search(r'hmi|unified', tree, re.I):
                try:
                    _reader_export(r, dict({'runId': folder[2], 'offset': 0, 'timeBudgetSeconds': 120},
                                           **({'projectPath': path} if source == 'ap21' and path else {})), steps,
                                   lambda j: job.update(hmi_chunks=job.get('hmi_chunks', 0) + 1), 'export_hmi_to_folder')
                except (mcp_client.MCPError, SnapshotError) as e:   # the PLC snapshot stands; the HMI is a warning
                    job.setdefault('warnings', []).append('HMI export failed (the PLC snapshot is complete): %s' % str(e)[:500])
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
        where = x.get('where') or ''
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
            x.update(edit=u['id'], block=u['block'], nw=u['nw'], title=u.get('title') or '', block_kind=u.get('block_kind'),
                     edit_kind=u.get('kind') or 'network', where=procedure.header(u),
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
    snap_jobs = [j for j in list((jobs or {}).values()) if j.get('kind') == 'snapshot' and j.get('project') == project]
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
    if la in ('LAD', 'FBD') and all('elements' in x and (la == 'FBD' or 'rungs' in x) for x in (a, b)):
        same, d = _matcher(a)(a, b)
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
    return [u['id'] for u in edits if u.get('block') == block and 'after' in u
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


# ---------- groups of edits (E5): one change made of several edits, written and checked together ----------

UPLOAD_ORDER = {'interface': 0, 'new_network': 1, 'network': 2}      # members first declare, then insert, then change


def _group_members(data, gid):
    """-> (project, state, [edits of the group in order]); KeyError when missing."""
    for p in state.projects(data):
        s = state.load(data, p)
        m = sorted((u for u in s['edits'] if (u.get('group') or {}).get('id') == gid), key=lambda u: u['group']['order'])
        if m:
            return p, s, m
    raise KeyError('group %s does not exist' % gid)


def _group_view(project, gid, members):
    low = min(state.STATES.index(u['state']) for u in members)
    g = {'id': gid, 'project': project, 'name': members[0]['group'].get('name') or '', 'state': state.STATES[low],
         'f': any(u.get('f') for u in members), 'blocks': sorted({u['block'] for u in members}),
         'members': [{'id': u['id'], 'order': u['group']['order'], 'kind': u.get('kind', 'network'), 'block': u['block'],
                      'where': procedure.header(u), 'state': u['state'], 'f': bool(u.get('f')), 'note': u.get('note') or ''}
                     for u in members]}
    g['next'] = state.STATES[low + 1] if low + 1 < len(state.STATES) else None
    g['previous'] = state.STATES[low - 1] if low > 0 else None
    if g['f']:
        g['f_note'] = 'one F-signature change — write all members, then compile the safety program'
    return g


def api_group_create(data, body):
    """POST /api/groups {edit_ids: [...] in order, name?} -> the group (G001…, unique across projects)."""
    ids = body.get('edit_ids') if isinstance(body, dict) else None
    if not isinstance(ids, list) or len(ids) < 2 or not all(isinstance(x, str) for x in ids) or len(set(ids)) != len(ids):
        raise state.StateError('edit_ids: at least two different edit ids, in the order they are made', 400)
    with state.LOCK:
        found = [state.find(data, x) for x in ids]
        project = found[0][0]
        if any(f[0] != project for f in found):
            raise state.StateError('the edits of a group must be in one project', 400)
        taken = [f[2]['id'] + ' (' + f[2]['group']['id'] + ')' for f in found if f[2].get('group')]
        if taken:
            raise state.StateError('already in a group: %s' % ', '.join(taken))
        n = 0
        for p in state.projects(data):
            for u in state.load(data, p)['edits']:
                m = re.fullmatch(r'G(\d+)', (u.get('group') or {}).get('id') or '')
                if m:
                    n = max(n, int(m.group(1)))
        gid = 'G%03d' % (n + 1)
        s = found[0][1]
        by_id = {u['id']: u for u in s['edits']}
        name = str(body.get('name') or '')[:120]
        for i, x in enumerate(ids, 1):
            by_id[x]['group'] = dict({'id': gid, 'order': i}, **({'name': name} if name else {}))
        state.save(data, project, s)
    return api_group(data, gid)


def api_group(data, gid):
    project, _, members = _group_members(data, gid)
    return _group_view(project, gid, members)


def api_group_state(data, gid, body):
    """Move every member to the state (members already there stay); only when every one of them can move, else 409."""
    if not isinstance(body, dict):
        raise state.StateError('the body must be a JSON object', 400)
    target = body.get('state')
    with state.LOCK:
        project, s, members = _group_members(data, gid)
        why = []
        for u in members:
            if u['state'] == target:
                continue
            try:
                state.change_state(json.loads(json.dumps(u)), target, body.get('by'))    # a trial on a copy
            except state.StateError as e:
                why.append('%s: %s' % (u['id'], e))
        if why:
            raise state.StateError('the group cannot move to %s — %s' % (target, '; '.join(why)),
                                   400 if target not in state.STATES else 409)
        for u in members:
            if u['state'] != target:
                state.change_state(u, target, body.get('by'))
        state.save(data, project, s)
    return api_group(data, gid)


def api_group_procedure(data, gid, root=None):
    """TIA steps of the members in order, each under its own heading."""
    project, _, members = _group_members(data, gid)
    g = _group_view(project, gid, members)
    parts, md = [], '## TIA steps — group %s%s\n' % (gid, ' · ' + g['name'] if g['name'] else '')
    if g['f']:
        md += '\n⚠ F-block: %s.\n' % g['f_note']
    compile_step = None
    for u in members:
        p = api_procedure(data, u['id'], root)
        steps = p['steps']
        if g['f']:                                    # one F-signature change: compile once, after the last member
            compile_step = next((k for k in steps if k.startswith('Compile the safety program')), compile_step)
            steps = [k for k in steps if not k.startswith('Compile the safety program')]
        parts.append({'edit': u['id'], 'header': p['header'], 'steps': steps})
    if compile_step:
        parts.append({'edit': None, 'header': 'After the last member', 'steps': [compile_step]})
    for i, x in enumerate(parts, 1):
        md += '\n### %d. %s%s\n' % (i, x['edit'] + ' — ' if x['edit'] else '', x['header']) + \
            ''.join('%d. %s\n' % (k, t) for k, t in enumerate(x['steps'], 1))
    if procedure.N in md:
        md += '\n%s = not verified in the TIA V21 help, check it in the editor.\n' % procedure.N
    return {'group': gid, 'f': g['f'], 'f_note': g.get('f_note'), 'parts': parts, 'markdown': md}


def api_group_check(root_of, data, gid):
    """Export check of every member (newest export of each block) -> {match: all matched, results}."""
    _, _, members = _group_members(data, gid)
    res = []
    for u in members:
        k = api_check_export(root_of(u['id']), data, u['id'], {})
        res.append({'edit': u['id'], 'match': k['match'], 'export_ts': k['export_ts'], 'note': k['note']})
    return {'group': gid, 'match': all(x['match'] for x in res), 'results': res, 'view': api_group(data, gid)}


def api_group_upload(root_of, data, gid, upload):
    """Upload of the members (Approved, non-F) one by one: interface edits, then new networks, then network changes;
    stops at the first failure. upload(uid) = api_upload for one edit."""
    _, _, members = _group_members(data, gid)
    if any(u.get('f') for u in members):
        raise state.StateError('F-block in the group: no Upload — make the changes by hand following the TIA steps, '
                               'then compile the safety program once')
    not_ready = ['%s (%s)' % (u['id'], u['state']) for u in members if u['state'] != 'Approved']
    if not_ready:
        raise state.StateError('Upload of a group only when every member is Approved: %s' % ', '.join(not_ready))
    res = []
    for u in sorted(members, key=lambda u: (UPLOAD_ORDER.get(u.get('kind', 'network'), 2), u['group']['order'])):
        r = upload(u['id'])
        res.append({'edit': u['id'], 'ok': r['ok'], 'message': r['message']})
        if not r['ok']:
            break
    ok = len(res) == len(members) and all(x['ok'] for x in res)
    return {'group': gid, 'ok': ok, 'results': res, 'view': api_group(data, gid)}


# ---------- HTTP routes ----------

def _portal(h):
    """The attached TIA Portal instance of the status bar, if any."""
    return h.tia_status.get('portalId') if h.tia_status.get('tia') == 'attached' else None


def _demo_guard(h, uid):
    """Upload / Check of a real project through fake servers is refused."""
    if h.demo and not _inside(h.root_of(uid), h.root):
        raise state.StateError(DEMO_REFUSED)


def _index_html(h):
    with open(os.path.join(WEB, 'index.html'), 'rb') as f:
        return f.read(), 200, 'text/html'


def _add_project(h):
    b = h._body() if isinstance(h._body(), dict) else {}
    if b.get('source') == 'exports':
        path = str(b.get('path') or '').strip().strip('"')
        if h.demo and path and not b.get('force') and not _inside(path, h.root):
            raise state.StateError(DEMO_REFUSED)
        return api_project_add_exports(h.data, b), 201
    if b.get('source') in ('tia', 'ap21'):
        return {'job': api_snapshot(h, None, b, b['source'])}, 202
    raise state.StateError('source must be exports | tia | ap21', 400)


def _group_upload(h, gid):
    _demo_guard(h, _group_members(h.data, gid)[2][0]['id'])
    return api_group_upload(h.root_of, h.data, gid, lambda uid: api_upload(
        h.root_of(uid), h.data, uid, h.mcp, h.mcp_timeout, _portal(h)))


def _changelog(h, name):
    r = api_changelog(h.root_for(name), h.data, name, h.jobs, *(h.q.get(k) for k in ('since', 'until', 'state', 'block')))
    return (changelog_markdown(name, r['items']), 200, 'text/markdown') if h.q.get('format') == 'md' else r


def _tia_status(h):
    """GET = the cached status; POST {portalId?} checks TIA again (it starts the Writer)."""
    b = h._body() if h.command == 'POST' else None
    st = refresh_status(h, b.get('portalId') if isinstance(b, dict) else None) if h.command == 'POST' else dict(h.tia_status)
    return dict(st, demo=h.demo, **ai_status(h))


def _project(h):
    """(export root, project) of the routes with ?project="""
    return h.root_for(h.q.get('project')), h.q.get('project', '')


def _ai_client_gone(h, cid):
    h.ai_clients.pop(cid, None)
    return ai_status(h)


E, G, P = r'/api/edits/([\w-]+)', r'/api/groups/([\w-]+)', r'/api/projects/([^/]+)'
# (methods, path, handler(h, *path groups) -> body | (body, status[, content type]))
ROUTES = [
    ('GET', r'/(?:index\.html)?', _index_html),
    ('GET', r'/api/exports', lambda h: api_exports(h.root, h.data)),
    ('GET', r'/api/blocks', lambda h: api_blocks(*_project(h), h.q.get('ts'))),
    ('GET', r'/api/block', lambda h: api_block(*_project(h), h.q.get('block', ''), h.q.get('ts'))),
    ('GET', r'/api/network', lambda h: api_network(*_project(h), h.q.get('block', ''), int(h.q.get('nw', '1')), h.q.get('ts'),
                                                   int(h.q.get('width', '0')))),
    ('GET', r'/api/catalog', lambda h: api_catalog(*_project(h))),
    ('GET', r'/api/hmi/screens', lambda h: hmi.api_screens(*_project(h), h.q.get('export_ts'), h.q.get('hmi'))),
    ('GET', r'/api/hmi/presets', lambda h: hmi.list_presets()),
    ('POST', r'/api/hmi/preset', lambda h: api_hmi_preset(h.root_for(h.body_project()), h._body())),
    ('GET', r'/api/hmi/screen', lambda h: hmi.api_screen(*_project(h), h.q.get('screen', ''), h.q.get('export_ts'),
                                                         h.q.get('hmi'))),
    ('POST', r'/api/layout', lambda h: api_layout(h.root_for(h.body_project()), h._body())),
    ('GET', r'/api/state', lambda h: api_state(h.data, h.q.get('project'))),
    ('PUT', r'/api/state/tia', lambda h: api_state_tia(_project(h)[0], h.data, _project(h)[1], h._body())),
    ('POST', r'/api/edits', lambda h: (api_edit_create(h.root_for(h.body_project()), h.data, h._body()), 201)),
    ('GET', E, lambda h, uid: api_edit_detail(h.data, uid, int(h.q.get('width', '0')), h.root_of(uid))),
    ('POST', E + '/state', lambda h, uid: api_edit_state(h.data, uid, h._body())),
    ('GET', E + '/procedure', lambda h, uid: api_procedure(h.data, uid, h.root_of(uid))),
    ('POST PUT', E + '/after', lambda h, uid: api_edit_after(h.data, uid, h._body(), h.root_of(uid))),
    ('POST', E + '/check-export', lambda h, uid: api_check_export(h.root_of(uid), h.data, uid, h._body())),
    ('POST', E + '/upload', lambda h, uid: _demo_guard(h, uid) or api_upload(
        h.root_of(uid), h.data, uid, h.mcp, h.mcp_timeout, _portal(h))),
    ('POST', E + '/check', lambda h, uid: _demo_guard(h, uid) or api_check(h.root_of(uid), h.data, uid, h.mcp, h.mcp_timeout)),
    ('POST', r'/api/check', lambda h: api_add_check(h.data, h._body())),
    ('POST', r'/api/groups', lambda h: (api_group_create(h.data, h._body()), 201)),
    ('GET', G, lambda h, gid: api_group(h.data, gid)),
    ('POST', G + '/state', lambda h, gid: api_group_state(h.data, gid, h._body())),
    ('GET', G + '/procedure', lambda h, gid: api_group_procedure(h.data, gid, h.root_for(_group_members(h.data, gid)[0]))),
    ('POST', G + '/check-export', lambda h, gid: api_group_check(h.root_of, h.data, gid)),
    ('POST', G + '/upload', _group_upload),
    ('GET', r'/api/projects', lambda h: api_projects(h.root, h.data, h.jobs)),
    ('POST', r'/api/projects', _add_project),
    ('DELETE', P, lambda h, name: api_project_delete(h.data, name)),
    ('POST', P + '/snapshot', lambda h, name: ({'job': api_snapshot(h, name, h._body())}, 202)),
    ('GET', P + '/changelog', _changelog),
    ('GET', P + '/snapshots', lambda h, name: api_snapshots(h.root_for(name), name)),
    ('GET', P + '/compare', lambda h, name: api_compare(h.root_for(name), h.data, name, h.q.get('from', ''), h.q.get('to', ''),
                                                        h.q.get('block'))),
    ('GET', r'/api/jobs', lambda h: {'jobs': list(h.jobs.values())}),
    ('GET', r'/api/jobs/([\w-]+)', lambda h, jid: h.jobs[jid]),
    ('GET POST', r'/api/tia/status', _tia_status),
    ('GET', r'/api/ai/clients', ai_status),
    ('POST', r'/api/ai/clients', lambda h: api_ai_heartbeat(h, h._body())),
    ('DELETE', r'/api/ai/clients/([\w-]+)', _ai_client_gone),
]


class Handler(BaseHTTPRequestHandler):
    root = ''
    data = ''
    mcp = None                       # {'writer': (command, env) | None, 'reader': …}
    mcp_timeout = 300
    jobs = {}
    tia_status = {}
    ai_clients = {}
    ai_timeout = AI_TIMEOUT
    demo = False                     # fake Writer/Reader: only projects under the default export root

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200, content_type='application/json'):
        b = obj if isinstance(obj, bytes) else (obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        if self._b is None:                           # read once (the route may look at the project first)
            n = int(self.headers.get('Content-Length') or 0)
            if not 0 <= n <= 5_000_000:
                raise ValueError('Content-Length must be 0…5000000')
            self._b = [json.loads(self.rfile.read(n).decode('utf-8') or 'null')]
        return self._b[0]

    def body_project(self):
        b = self._body()
        return b.get('project') if isinstance(b, dict) else ''

    def root_for(self, project):
        """Export root of the project (registry or --export-root)."""
        return project_root(self.root, self.data, str(project or ''))

    def root_of(self, uid):
        return self.root_for(state.find(self.data, uid)[0])

    def _handle(self, method):
        u = urlparse(self.path)
        self.q, self._b = {k: v[0] for k, v in parse_qs(u.query).items()}, None
        origin = self.headers.get('Origin')
        if method != 'GET' and origin and urlparse(origin).hostname not in ('localhost', '127.0.0.1'):
            self._json({'error': 'cross-origin request refused'}, 403)   # another web page must not drive TIA
            return
        if self.headers.get('Host') not in ('localhost:%d' % self.server.server_port, '127.0.0.1:%d' % self.server.server_port):
            self._json({'error': 'host refused — open the editor at http://localhost:%d/' % self.server.server_port}, 403)
            return                                    # DNS rebinding: a foreign name pointing at 127.0.0.1
        try:
            r = {'error': 'not found'}, 404
            for methods, path, fn in ROUTES:
                m = re.fullmatch(path, u.path)
                if m and method in methods.split():
                    r = fn(self, *map(unquote, m.groups()))
                    break
            self._json(*(r if isinstance(r, tuple) else (r,)))
        except state.StateError as e:
            self._json({'error': str(e)}, e.code)
        except KeyError as e:
            self._json({'error': str(e).strip("'")}, 404)
        except (ValueError, TypeError, s7dcl.SourceParseError) as e:
            self._json({'error': str(e)}, 400)
        except OSError as e:
            self._json({'error': str(e)}, 500)

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def do_PUT(self):
        self._handle('PUT')

    def do_DELETE(self):
        self._handle('DELETE')


def create_server(port, root, data, host='127.0.0.1', mcp=None, mcp_timeout=300, autoconnect=False, ai_timeout=AI_TIMEOUT,
                  demo=False):
    """autoconnect: check the TIA status once in the background at start (and the home page checks it when opened).
    demo: fake Writer/Reader — projects outside the default export root are refused (add, Upload, Check)."""
    h = type('H', (Handler,), {'root': root, 'data': data, 'mcp': mcp, 'mcp_timeout': mcp_timeout, 'jobs': {},
                               'ai_clients': {}, 'ai_timeout': ai_timeout, 'demo': demo,
                               'tia_status': {'tia': 'unknown', 'checked': False, 'autoconnect': autoconnect}})
    sync_registry(root, data)
    srv = ThreadingHTTPServer((host, port), h)
    if autoconnect:
        threading.Thread(target=refresh_status, args=(h,), daemon=True).start()
    return srv


def mcp_config(mcp_json, writer_cmd=None, reader_cmd=None):
    """Writer / Reader commands: from .mcp.json (first key found of WRITER_KEYS / READER_KEYS), overridden by --writer-cmd / --reader-cmd."""
    def first(keys):
        return next((c for k in keys if (c := mcp_client.from_mcp_json(mcp_json, k))), None)
    return {'writer': (mcp_client.command_from_text(writer_cmd), None) if writer_cmd else first(WRITER_KEYS),
            'reader': (mcp_client.command_from_text(reader_cmd), None) if reader_cmd else first(READER_KEYS)}


# .mcp.json server keys, newest name first (the tools are being renamed TIAC_* -> BTIA_*; both keep working)
WRITER_KEYS = ('BTIA_Writer', 'TIAC_Writer')
READER_KEYS = ('BTIA_Reader', 'tia-portal')


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
    ap.add_argument('--demo', action='store_true',
                    help='demo instance (also implied by a fake Writer/Reader): a banner on every page, real projects refused')
    a = ap.parse_args()
    mcp = mcp_config(a.mcp_json, a.writer_cmd, a.reader_cmd)
    demo = a.demo or is_fake(mcp)
    srv = create_server(a.port, a.export_root, a.data, mcp=mcp, mcp_timeout=a.mcp_timeout,
                        autoconnect=not a.no_autoconnect, ai_timeout=a.ai_timeout, demo=demo)
    print('TIAC_Editor at http://localhost:%d  (export root %s, data %s%s)' % (a.port, a.export_root, a.data,
                                                                              ', DEMO — fake TIA' if demo else ''), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    sys.exit(main())
