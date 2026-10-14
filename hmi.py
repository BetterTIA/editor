"""WinCC Unified HMI screens (H): the Reader's `export_hmi_to_folder` dump -> screen models, the screen diff, the export
check and the presets for new screens.

The dump (Reader HmiUnifiedExporter) is `<project>\\<ts>\\<HMI>\\Screens\\…\\<screen>.json` (screen attributes + `screenItems`,
each item with `$type` = Openness class, lower-camel attributes, `dynamizations`, `eventHandlers`), `Tags\\…\\<table>.json`,
`alarms.json`, `connections.json`, and `<ts>\\manifest-hmi.jsonl`. The folder is walked; the manifest is not needed.
"""
import json
import os
import re

TS = re.compile(r'^\d{8}-\d{6}$')
ALIASES = {'ToggleSwitch': 'Switch'}                 # Openness class (without Hmi) -> palette name
PALETTE = ['Text', 'Rectangle', 'Line', 'Button', 'IOField', 'Switch', 'Bar', 'Gauge', 'AlarmControl', 'ScreenWindow']
COLOR = re.compile(r'#[0-9a-f]{6}(?:[0-9a-f]{2})?')


# ---------- reading the dump ----------

def exports(root, project):
    """{ts: {hmi: folder}} — snapshot folders with an HMI dump (a child folder with Screens\\)."""
    pd, res = os.path.join(root, project), {}
    if not os.path.isdir(pd):
        return res
    for ts in sorted(os.listdir(pd)):
        d = os.path.join(pd, ts)
        if TS.match(ts) and os.path.isdir(d):
            hmis = {h: os.path.join(d, h) for h in sorted(os.listdir(d)) if os.path.isdir(os.path.join(d, h, 'Screens'))}
            if hmis:
                res[ts] = hmis
    return res


def find(root, project, ts=None, hmi=None):
    """-> (ts, hmi, folder) of the newest (or the given) export with an HMI dump.
    shortcut: with several HMIs in one project the first by name is taken unless hmi is given — add a chooser when needed."""
    exp = exports(root, project)
    if ts and ts not in exp:
        raise KeyError('project %s has no HMI export %s (HMI exports: %s)' % (project, ts, ', '.join(exp) or 'none'))
    for t in sorted(exp, reverse=True):
        if not ts or t == ts:
            h = hmi or next(iter(exp[t]))
            if h in exp[t]:
                return t, h, exp[t][h]
    raise KeyError('no HMI export in project %s — take a snapshot of a project with a WinCC Unified HMI' % project)


def _read(p):
    with open(p, encoding='utf-8-sig') as f:
        return json.load(f)


def _files(folder, sub):
    return sorted(os.path.join(dp, f) for dp, _, fs in os.walk(os.path.join(folder, sub)) for f in fs if f.endswith('.json'))


def _num(v, default=0):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def text(v):
    """Multilingual text -> the en-US translation (else the first one) without rich-text markup.
    shortcut: one language only — keep all translations when the editor edits more than en-US."""
    if isinstance(v, dict):
        items = [x for x in v.get('items') or [] if isinstance(x, dict)]
        v = next((x for x in items if x.get('language') == 'en-US'), items[0] if items else {}).get('text')
    return (re.sub(r'<[^>]+>', '', v).strip() or None) if isinstance(v, str) else None


def color(v):
    """Openness "#AARRGGBB" -> CSS "#rrggbb" (opaque) or "#rrggbbaa"; anything else -> None."""
    m = re.fullmatch(r'#([0-9A-Fa-f]{2})([0-9A-Fa-f]{6})', v) if isinstance(v, str) else None
    return ('#' + m[2] + ('' if m[1].upper() == 'FF' else m[1])).lower() if m else None


def tia_color(c):
    """CSS "#rrggbb[aa]" -> Openness "#AARRGGBB"."""
    return ('#' + (c[7:9] or 'ff') + c[1:7]).upper()


def item(x):
    """One screen item of the dump -> {name, type, left, top, width, height, text?, colors?, font?, bindings, events?}."""
    t = str(x.get('$type') or 'Item')
    t = t[3:] if t.startswith('Hmi') and len(t) > 3 else t
    it = {'name': str(x.get('name') or ''), 'type': ALIASES.get(t, t)}
    it.update({k: _num(x.get(k)) for k in ('left', 'top', 'width', 'height')})
    tx = text(x.get('text'))
    if tx is not None:
        it['text'] = tx
    colors = {k: color(v) for k, v in x.items() if k.endswith('Color') and color(v)}
    if colors:
        it['colors'] = colors
    if isinstance(x.get('font'), dict):
        it['font'] = {k: x['font'][k] for k in ('name', 'size', 'weight') if x['font'].get(k) is not None}
    it['bindings'] = [dict({'property': str(d.get('propertyName') or ''), 'tag': str(d['tag'])},
                           **({'indirect': True} if d.get('useIndirectAddressing') else {}))
                      for d in x.get('dynamizations') or [] if isinstance(d, dict) and d.get('tag')]
    ev = [{'event': str(e.get('eventType') or '?'),
           'script': e['script'].get('scriptCode') if isinstance(e.get('script'), dict) else None}
          for e in x.get('eventHandlers') or [] if isinstance(e, dict)]
    if ev:
        it['events'] = ev
    if x.get('visible') is False:
        it['visible'] = False
    return it


def screen(x):
    """Screen file of the dump -> {name, width, height, background, items}; missing attributes get defaults."""
    return {'name': str(x.get('name') or ''), 'width': _num(x.get('width'), 1280), 'height': _num(x.get('height'), 800),
            'background': color(x.get('backColor')),
            'items': [item(i) for i in x.get('screenItems') or [] if isinstance(i, dict)]}


def tags(folder):
    """HMI tag tables of the dump -> [{name, type, plc_tag, connection, table}]."""
    res = []
    for p in _files(folder, 'Tags'):
        try:
            t = _read(p)
        except (OSError, ValueError):
            continue
        for x in t.get('tags') or []:
            if isinstance(x, dict) and x.get('name'):
                res.append({'name': x['name'], 'type': x.get('dataType') or '', 'plc_tag': x.get('plcTag') or '',
                            'connection': x.get('connection') or '', 'table': t.get('name') or ''})
    return res


def _screens(folder):
    """-> ([screen models], [errors]) — one unreadable file does not hide the others."""
    res, errors = [], []
    for p in _files(folder, 'Screens'):
        try:
            res.append(screen(_read(p)))
        except (OSError, ValueError, AttributeError) as e:
            errors.append('%s: %s' % (os.path.basename(p), e))
    return res, errors


def api_screens(root, project, ts=None, hmi=None):
    t, h, folder = find(root, project, ts, hmi)
    scr, errors = _screens(folder)
    return {'project': project, 'export_ts': t, 'hmi': h, 'hmis': list(exports(root, project)[t]), 'errors': errors,
            'screens': [{'name': s['name'], 'width': s['width'], 'height': s['height'], 'background': s['background'],
                         'items': len(s['items'])} for s in scr],
            'tags': tags(folder)}


def api_screen(root, project, name, ts=None, hmi=None):
    t, h, folder = find(root, project, ts, hmi)
    s = next((s for s in _screens(folder)[0] if s['name'] == name), None)
    if s is None:
        raise KeyError('screen %s is not in the HMI export %s of %s' % (name, t, project))
    return dict(s, project=project, export_ts=t, hmi=h)


# ---------- the "after" model from the API (trust boundary) ----------

def _str(v, what, n=300, required=False):
    if v is None or v == '':
        if required:
            raise ValueError('%s is required' % what)
        return None
    if not isinstance(v, str) or len(v) > n or '\r' in v:
        raise ValueError('%s must be text (at most %d characters)' % (what, n))
    return v


def _int(v, what, lo=-100000, hi=100000):
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
        raise ValueError('%s must be a number %d…%d' % (what, lo, hi))
    return round(v)


def verify(m):
    """The screen model from the API -> a normalized copy (only known keys); wrong shape or a duplicate item name =
    ValueError. Geometry outside the screen and unknown tags are rule errors (validate), not shape errors."""
    if not isinstance(m, dict) or not isinstance(m.get('items'), list) or len(m['items']) > 2000:
        raise ValueError('after must be a screen model {name, width, height, background?, items: [{name, type, left, top, '
                         'width, height, text?, colors?, font?, bindings?}]}')
    res = {'name': _str(m.get('name'), 'screen name', 128, True), 'width': _int(m.get('width', 1280), 'width', 1, 10000),
           'height': _int(m.get('height', 800), 'height', 1, 10000), 'background': _color(m.get('background'), 'background'),
           'items': []}
    seen = set()
    for x in m['items']:
        if not isinstance(x, dict):
            raise ValueError('an item must be an object {name, type, left, top, width, height, …}')
        nm = _str(x.get('name'), 'item name', 128, True)
        if nm.lower() in seen:
            raise ValueError('item name %s is used twice — names must be unique (the export check finds items by name)' % nm)
        seen.add(nm.lower())
        it = {'name': nm, 'type': _str(x.get('type'), '%s: type' % nm, 64, True)}
        it.update({k: _int(x.get(k, 0), '%s: %s' % (nm, k), *((0, 10000) if k in ('width', 'height') else ())) for k in
                   ('left', 'top', 'width', 'height')})
        t = _str(x.get('text'), '%s: text' % nm, 2000)
        if t is not None:
            it['text'] = t
        if x.get('colors'):
            if not isinstance(x['colors'], dict):
                raise ValueError('%s: colors must be {backColor: "#rrggbb", …}' % nm)
            it['colors'] = {k: _color(v, '%s: %s' % (nm, k)) for k, v in x['colors'].items() if v}
        if x.get('font'):
            f = x['font']
            if not isinstance(f, dict):
                raise ValueError('%s: font must be {size, name?, weight?}' % nm)
            it['font'] = {k: v for k, v in (('name', _str(f.get('name'), '%s: font name' % nm, 64)),
                                             ('size', _int(f['size'], '%s: font size' % nm, 1, 500) if 'size' in f else None),
                                             ('weight', _str(f.get('weight'), '%s: font weight' % nm, 32))) if v is not None}
        b = x.get('bindings') or []
        if not isinstance(b, list) or not all(isinstance(z, dict) for z in b):
            raise ValueError('%s: bindings must be [{property, tag}]' % nm)
        it['bindings'] = [dict({'property': _str(z.get('property'), '%s: binding property' % nm, 64, True),
                                'tag': _str(z.get('tag'), '%s: binding tag' % nm, 300, True)}, **({'indirect': True} if z.get('indirect') else {}))
                          for z in b]
        if x.get('events'):
            if not isinstance(x['events'], list) or not all(isinstance(z, dict) for z in x['events']):
                raise ValueError('%s: events must be [{event, script?}]' % nm)
            it['events'] = [{'event': _str(z.get('event'), '%s: event' % nm, 64, True),
                             'script': _str(z.get('script'), '%s: script' % nm, 20000)} for z in x['events']]
        if x.get('visible') is False:
            it['visible'] = False
        res['items'].append(it)
    return res


def _color(v, what):
    if v is None or v == '':
        return None
    if not isinstance(v, str) or not COLOR.fullmatch(v.lower()):
        raise ValueError('%s must be a color "#rrggbb" or "#rrggbbaa"' % what)
    return v.lower()


def validate(m, known_tags=None):
    """Rule errors of a screen model: items outside the screen (error), bindings to unknown tags (warning)."""
    res = []
    for x in m['items']:
        if x['left'] < 0 or x['top'] < 0 or x['left'] + x['width'] > m['width'] or x['top'] + x['height'] > m['height']:
            res.append({'id': x['name'], 'error': '%s is outside the screen (%d, %d, %d × %d on %d × %d)' % (
                x['name'], x['left'], x['top'], x['width'], x['height'], m['width'], m['height'])})
        for b in x.get('bindings') or []:
            if known_tags and b['tag'].strip('"') not in known_tags:
                res.append({'id': x['name'], 'warning': True,
                            'error': '%s: %s → tag %s is neither an HMI tag nor a PLC tag of the export' % (x['name'], b['property'], b['tag'])})
    return res


# ---------- difference and export check ----------

def _flat(it):
    d = {k: it.get(k) for k in ('type', 'left', 'top', 'width', 'height')}
    d['text'] = it.get('text') or None
    d.update({'colors.' + k: v for k, v in (it.get('colors') or {}).items()})
    d.update({'font.' + k: v for k, v in (it.get('font') or {}).items()})
    d.update({'binding ' + b['property']: b['tag'] for b in it.get('bindings') or []})
    return d


def _changes(b, a, keys=None, tol=0):
    out = []
    for k in list(b) + [k for k in a if k not in b]:
        x, y = b.get(k), a.get(k)
        if keys and not any(k == z or k.startswith(z) for z in keys) or x == y:
            continue
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) and abs(x - y) <= tol:
            continue
        out.append({'attr': k, 'before': x, 'after': y})
    return out


def compare(before, after, keys=None, tol=0):
    """-> {added: [names], deleted: [names], changed: {name: [{attr, before, after}]}, screen: [{attr, before, after}]}.
    Items are paired by name; before None = a new screen (everything added)."""
    B = {i['name']: i for i in (before or {}).get('items') or []}
    A = {i['name']: i for i in after['items']}
    res = {'added': [n for n in A if n not in B], 'deleted': [n for n in B if n not in A], 'changed': {}, 'screen': []}
    for n in A:
        ch = _changes(_flat(B[n]), _flat(A[n]), keys, tol) if n in B else []
        if ch:
            res['changed'][n] = ch
    if before:
        res['screen'] = _changes({k: before.get(k) for k in ('width', 'height', 'background')},
                                 {k: after.get(k) for k in ('width', 'height', 'background')}, keys, tol)
    return res


CHECKED = ('type', 'left', 'top', 'width', 'height', 'text', 'binding ')


def check(after, exported):
    """Export = proposal: every item by name with its type, geometry (±1 px), text and tag bindings; no item more or less."""
    if exported is None:
        return False, ['screen %s is not in the export' % after['name']]
    c = compare(exported, after, CHECKED, 1)
    diffs = ['missing in export: %s' % n for n in c['added']] + ['extra in export: %s' % n for n in c['deleted']] + \
        ['%s %s: export %s, proposal %s' % (n, x['attr'], json.dumps(x['before']), json.dumps(x['after']))
         for n, ch in c['changed'].items() for x in ch]
    return not diffs, diffs


# ---------- presets: new screens parametrized by the machine name and a list of tags ----------

PRESETS = {
    'machine_overview': ('Machine overview', 'header with the machine name and state, IO fields for the values, state lamps '
                                             'for Bool tags, a navigation bar'),
    'alarm_page': ('Alarm page', 'header, an alarm control over the whole screen, a navigation bar'),
    'manual_control': ('Manual control', 'one row per tag: label, switch (Bool) or IO field, Start and Stop buttons'),
    'settings': ('Settings', 'parameters as IO fields with labels, Save and Cancel buttons'),
}
NAV = ['Overview', 'Alarms', 'Manual', 'Settings']
INK, PANEL, BLUE, GREEN = '#1b2430', '#ffffff', '#2f6fa3', '#3aa655'


def _it(type_, name, left, top, width, height, **kw):
    return dict({'name': name, 'type': type_, 'left': left, 'top': top, 'width': width, 'height': height, 'bindings': []}, **kw)


def _font(size, weight=None):
    return dict({'size': size}, **({'weight': weight} if weight else {}))


def _button(name, left, top, text, width=200, script=None):
    return _it('Button', name, left, top, width, 56, text=text, colors={'backColor': BLUE, 'foreColor': '#ffffff'},
               font=_font(18, 'Bold'), **({'events': [{'event': 'Tapped', 'script': script}]} if script else {}))


def _label(name, left, top, text, width=200):
    return _it('Text', name, left, top, width, 40, text=text, colors={'foreColor': INK}, font=_font(18))


def _io(name, left, top, tag):
    return _it('IOField', name, left, top, 160, 40, colors={'backColor': PANEL, 'foreColor': INK, 'borderColor': '#8a9199'},
               font=_font(18), bindings=[{'property': 'ProcessValue', 'tag': tag}])


def _key(tag):
    return re.sub(r'\W+', '_', tag.strip('"')).strip('_') or 'Tag'


def preset(key, name, machine='Machine', tags=(), width=1280, height=800, screens=None):
    """Screen model of a preset. tags: [{name, type?}] (or names); Bool tags become lamps / switches."""
    if key not in PRESETS:
        raise ValueError('preset must be one of %s' % ', '.join(PRESETS))
    tags = [t if isinstance(t, dict) else {'name': str(t)} for t in tags]
    is_bool = lambda t: str(t.get('type') or '').lower() == 'bool'                  # noqa: E731
    items = [_it('Rectangle', 'Header', 0, 0, width, 64, colors={'backColor': '#1f3a5f', 'borderColor': '#1f3a5f'}),
             _it('Text', 'Title', 24, 12, 600, 40, text='%s — %s' % (machine, PRESETS[key][0].lower()) if key != 'machine_overview'
                 else machine, colors={'foreColor': '#ffffff'}, font=_font(24, 'Bold'))]
    rows = []
    if key == 'machine_overview':
        items += [_it('Text', 'State_Text', width - 300, 16, 220, 32, text='State', colors={'foreColor': '#ffffff'}, font=_font(18))]
        values, lamps = [t for t in tags if not is_bool(t)], [t for t in tags if is_bool(t)]
        col = width // 2
        for i, t in enumerate(values):
            x, y = 40 + i % 2 * col, 100 + i // 2 * 56
            rows += [_label('Label_' + _key(t['name']), x, y, t['name']), _io('IO_' + _key(t['name']), x + 220, y, t['name'])]
        y0 = 100 + (len(values) + 1) // 2 * 56 + 24
        for i, t in enumerate(lamps):
            x, y = 40 + i % 4 * (width // 4), y0 + i // 4 * 48
            rows += [_it('Rectangle', 'Lamp_' + _key(t['name']), x, y + 8, 24, 24, colors={'backColor': GREEN, 'borderColor': INK},
                         bindings=[{'property': 'BackColor', 'tag': t['name']}]),
                     _label('Label_' + _key(t['name']), x + 36, y, t['name'], width // 4 - 48)]
    elif key == 'alarm_page':
        rows.append(_it('AlarmControl', 'Alarm_View', 24, 80, width - 48, height - 180,
                        colors={'backColor': PANEL, 'borderColor': '#8a9199'}, font=_font(14)))
    elif key == 'manual_control':
        for i, t in enumerate(tags):
            k, y = _key(t['name']), 100 + i * 64
            rows += [_label('Label_' + k, 40, y, t['name']),
                     _it('Switch', 'Switch_' + k, 260, y, 120, 40, colors={'backColor': '#dde3ea', 'foreColor': BLUE},
                         bindings=[{'property': 'ProcessValue', 'tag': t['name']}]) if is_bool(t) else _io('IO_' + k, 260, y, t['name']),
                     _button('Btn_Start_' + k, 460, y - 8, 'Start', 120), _button('Btn_Stop_' + k, 596, y - 8, 'Stop', 120)]
    else:                                             # settings
        for i, t in enumerate(tags):
            rows += [_label('Label_' + _key(t['name']), 40, 100 + i * 56, t['name'], 300), _io('IO_' + _key(t['name']), 360, 100 + i * 56, t['name'])]
        rows += [_button('Btn_Save', width - 448, height - 160, 'Save'), _button('Btn_Cancel', width - 232, height - 160, 'Cancel')]
    nav = [s for s in (screens or NAV) if s != name]
    rows += [_button('Nav_' + _key(s), 24 + i * 216, height - 80, s, script='HMIRuntime.UI.SysFct.ChangeScreen("%s", "../");' % s)
             for i, s in enumerate(nav[:max(1, (width - 24) // 216)])]
    return verify({'name': name, 'width': width, 'height': height, 'background': '#eef1f4', 'items': items + rows})


def list_presets():
    return {'presets': [{'key': k, 'name': n, 'description': d} for k, (n, d) in PRESETS.items()],
            'params': {'machine': 'machine name in the header (default "Machine")',
                       'tags': 'tag names (HMI tags or PLC tags) the screen shows',
                       'width': 'screen width (default 1280)', 'height': 'screen height (default 800)',
                       'screens': 'screens of the navigation bar (default %s)' % ', '.join(NAV)}}
