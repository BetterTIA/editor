"""BetterTIA Editor as an MCP server — stdio, JSON-RPC 2.0, one message per line, standard library only.

An AI client (Claude Code, Cursor, anything that speaks MCP) starts this script; it talks to an editor that is
already running (server.py) over its HTTP API:

    python mcp_server.py [--server http://localhost:8765] [--heartbeat 15]

On `initialize` it registers the client on the editor (POST /api/ai/clients, the status bar shows
"AI: connected: <client name>"), refreshes that every --heartbeat seconds and on every tool call, and unregisters
when stdin closes. No state of its own: everything goes through the editor's HTTP API.

State changes (Approved, Signed, …) and Upload / Check are deliberately not offered as tools: approving a change and
writing it into the PLC project are human steps, done in the editor's UI.
"""
import argparse
import base64
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

import screenshot as shot

PROTOCOL = '2025-06-18'
VERSION = '0.1'


class ToolError(Exception):
    pass


class Editor:
    """HTTP client of a running editor; every failure is a ToolError with a readable message."""

    def __init__(self, url):
        self.url = url.rstrip('/')

    def req(self, method, path, body=None, timeout=120):
        r = urllib.request.Request(self.url + path, method=method, headers={'Content-Type': 'application/json'},
                                   data=json.dumps(body).encode('utf-8') if body is not None else None)
        try:
            with urllib.request.urlopen(r, timeout=timeout) as f:
                return json.loads(f.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            try:
                message = json.loads(e.read().decode('utf-8')).get('error')
            except ValueError:
                message = None
            raise ToolError('HTTP %d on %s %s: %s' % (e.code, method, path.split('?')[0], message or e.reason))
        except (urllib.error.URLError, OSError) as e:
            raise ToolError('the editor is not reachable at %s (%s) — start it first: python server.py --export-root '
                            '<folder> --data <folder>' % (self.url, getattr(e, 'reason', e)))

    def get(self, path, **query):
        query = {k: v for k, v in query.items() if v not in (None, '')}
        return self.req('GET', path + ('?' + urllib.parse.urlencode(query) if query else ''))


def q(x):
    return urllib.parse.quote(str(x), safe='')


# ---------- tools ----------

def list_projects(e, a):
    p, st = e.get('/api/projects'), e.get('/api/tia/status')
    return {'projects': [{k: x.get(k) for k in ('name', 'source', 'latest_export', 'exports', 'edits', 'tia')}
                         for x in p['projects']],
            'tia': {k: st.get(k) for k in ('tia', 'project', 'project_path', 'writer', 'reader', 'ai')}}


def list_blocks(e, a):
    ex = next((x for x in e.get('/api/exports')['projects'] if x['project'] == a['project']), None)
    if not ex:
        raise ToolError('unknown project %s (see list_projects)' % a['project'])
    ts = a.get('export_ts')
    if ts and ts not in [x['ts'] for x in ex['exports']]:
        raise ToolError('project %s has no export %s (exports: %s)' % (a['project'], ts,
                                                                       ', '.join(x['ts'] for x in ex['exports'])))
    blocks = []
    for b in ex['blocks']:
        try:
            k = e.get('/api/block', project=a['project'], block=b['block'], ts=ts)
        except ToolError as err:
            if ts and 'HTTP 404' in str(err):
                continue                                  # the block is not in that (partial) export
            blocks.append({'block': b['block'], 'error': str(err)})
            continue
        blocks.append({'block': k['file'], 'name': k['block'], 'kind': k['kind'], 'number': k['number'],
                       'language': k['language'], 'title': k['title'], 'networks': len(k['networks']),
                       'export_ts': k['ts']})
    return {'project': a['project'], 'export_ts': ts or 'newest export of each block', 'blocks': blocks}


def get_network(e, a):
    n = e.get('/api/network', project=a['project'], block=a['block'], nw=a['nw'], ts=a.get('export_ts'))
    for k in ('drawing', 'layout'):                       # SVG geometry: large and useless to a model
        n.pop(k, None)
    if n.get('as_lad'):
        n['as_lad'].pop('drawing', None)
    return n


def _summary(u):
    k = u.get('checks') or []
    return {'id': u['id'], 'project': u['project'], 'round': u.get('round'), 'block': u['block'], 'nw': u['nw'],
            'title': u.get('title'), 'language': u.get('language'), 'f': u.get('f'), 'state': u['state'],
            'author': u.get('author'), 'time': u.get('time'), 'note': u.get('note'),
            'last_check': k[-1] if k else None}


def list_edits(e, a):
    if a.get('project'):
        edits = e.get('/api/state', project=a['project'])['edits']
    else:
        edits = [u for p in e.get('/api/state')['projects'] for u in p['edits']]
    return {'edits': [_summary(u) for u in edits if not a.get('state') or u['state'] == a['state']]}


def get_edit(e, a):
    d = e.get('/api/edits/' + q(a['id']))
    return {'edit': d['edit'], 'diff': d.get('diff'), 'errors': d.get('errors'), 'warnings': d.get('warnings'),
            'next': d.get('next'), 'previous': d.get('previous'), 'checked_allowed': d.get('checked_allowed'),
            'tia': d.get('tia'), 'procedure': e.get('/api/edits/%s/procedure' % q(a['id']))['markdown'],
            'url': '%s/?edit=%s' % (e.url, q(a['id']))}


def propose_edit(e, a, client=None):
    u = e.req('POST', '/api/edits', {'project': a['project'], 'round': a['round'], 'block': a['block'], 'nw': a['nw'],
                                     'after': a['after'], 'note': a.get('note') or '',
                                     'by': a.get('by') or (client or {}).get('name') or 'AI'})
    d = e.get('/api/edits/' + q(u['id']))
    return {'id': u['id'], 'state': u['state'], 'errors': d.get('errors'), 'warnings': d.get('warnings'),
            'url': '%s/?edit=%s' % (e.url, q(u['id']))}


def update_edit(e, a):
    r = e.req('POST', '/api/edits/%s/after' % q(a['id']), {'after': a['after'], 'note': a.get('note')})
    return {'id': r['edit']['id'], 'state': r['edit']['state'], 'errors': r['errors']}


def run_check(e, a):
    r = e.req('POST', '/api/edits/%s/check-export' % q(a['id']), {'export_ts': a['export_ts']} if a.get('export_ts') else {})
    return {k: r.get(k) for k in ('match', 'export_ts', 'nw_export', 'note', 'diffs')} | {'state': r['edit']['state']}


def get_procedure(e, a):
    return e.get('/api/edits/%s/procedure' % q(a['id']))['markdown']


def tia_status(e, a):
    return e.get('/api/tia/status', refresh='1' if a.get('refresh') else None)


def take_snapshot(e, a):
    return e.req('POST', '/api/projects/%s/snapshot' % q(a['project']), {})['job']


def get_job(e, a):
    return e.get('/api/jobs/' + q(a['id']))


def take_screenshot(e, a):
    if a.get('edit'):
        page = {'edit': a['edit']}
    elif a.get('project') and a.get('block') and a.get('nw'):
        page = {'project': a['project'], 'block': a['block'], 'nw': a['nw']}
    else:
        raise ToolError('give edit, or project + block + nw')
    if a.get('edit'):                                     # unknown edit / block -> a clear error instead of a blank PNG
        e.get('/api/edits/' + q(a['edit']))
    else:
        e.get('/api/block', project=a['project'], block=a['block'])
    light = a.get('theme') == 'light'
    url = '%s/?%s' % (e.url, urllib.parse.urlencode(dict(page, screenshot=1, theme='light' if light else 'dark')))
    fd, out = tempfile.mkstemp(suffix='.png', prefix='tiac_ai_')
    os.close(fd)
    try:
        try:
            shot.screenshot(url, out, int(a.get('width') or 1400), int(a.get('height') or 900), light)
        except (RuntimeError, OSError, ValueError) as err:
            raise ToolError('screenshot failed: %s — screenshot.py needs Google Chrome or Microsoft Edge installed '
                            '(headless); without it read the model with get_network / get_edit' % err)
        with open(out, 'rb') as f:
            png = base64.b64encode(f.read()).decode('ascii')
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    return [{'type': 'image', 'data': png, 'mimeType': 'image/png'}, {'type': 'text', 'text': url}]


def _schema(props, required=()):
    return {'type': 'object', 'properties': props, 'required': list(required), 'additionalProperties': False}


S, I, B = {'type': 'string'}, {'type': 'integer', 'minimum': 1}, {'type': 'boolean'}
MODEL = {'type': 'object', 'description': 'network model: LAD {elements, rungs} or FBD {language: "FBD", elements}; '
                                          'start from get_network, keep the ids of kept elements, new ids for new ones'}
TS = {'type': 'string', 'description': 'export timestamp YYYYMMDD-HHMMSS (default: the newest export of the block)'}
STATES = ['Proposal', 'Approved', 'Signed', 'Checked', 'Uploaded', 'Tested']

TOOLS = [
    ('list_projects', 'Projects in the editor with their exports, edits by state and the TIA Portal status.',
     _schema({}), list_projects),
    ('list_blocks', 'Blocks of a project: file name (use it as `block`), number, kind, language, title, network count.',
     _schema({'project': S, 'export_ts': TS}, ['project']), list_blocks),
    ('get_network', 'One network: elements, rungs (LAD) or signals (FBD), title, language, F-block flag, as_lad for '
                    'pure bit-logic FBD.',
     _schema({'project': S, 'block': S, 'nw': I, 'export_ts': TS}, ['project', 'block', 'nw']), get_network),
    ('list_edits', 'Edits (changes) with their state, optionally filtered by project and state.',
     _schema({'project': S, 'state': {'type': 'string', 'enum': STATES}}), list_edits),
    ('get_edit', 'One edit: before/after models, diff marks, rule errors, checks, writes, history and the TIA steps.',
     _schema({'id': S}, ['id']), get_edit),
    ('propose_edit', 'Create a new edit in state Proposal from an "after" model of one network. Returns the id and '
                     'LAD/FBD rule errors. A human reviews and approves it in the editor.',
     _schema({'project': S, 'round': {'type': 'string', 'description': 'round / topic the edit belongs to'},
              'block': S, 'nw': I, 'after': MODEL, 'note': S,
              'by': {'type': 'string', 'description': 'author shown in the history (default: the client name)'}},
             ['project', 'round', 'block', 'nw', 'after']), propose_edit),
    ('update_edit', 'Replace the "after" model (and optionally the note) of an edit — only while it is a Proposal.',
     _schema({'id': S, 'after': MODEL, 'note': S}, ['id', 'after']), update_edit),
    ('run_check', 'Compare an export (newest or given) with the edit\'s "after" model and record the check.',
     _schema({'id': S, 'export_ts': TS}, ['id']), run_check),
    ('get_procedure', 'Step-by-step instructions for making the edit by hand in TIA Portal (markdown).',
     _schema({'id': S}, ['id']), get_procedure),
    ('tia_status', 'TIA Portal, Writer, Reader and AI client status; refresh=true checks TIA Portal again.',
     _schema({'refresh': B}), tia_status),
    ('take_snapshot', 'Start a background job that exports the whole PLC of a project from TIA Portal; returns the job.',
     _schema({'project': S}, ['project']), take_snapshot),
    ('get_job', 'State and steps of a background job (snapshot).', _schema({'id': S}, ['id']), get_job),
    ('screenshot', 'PNG of an edit (diff) or of a network as the editor draws it (headless Chrome or Edge).',
     _schema({'edit': S, 'project': S, 'block': S, 'nw': I, 'width': I, 'height': I,
              'theme': {'type': 'string', 'enum': ['dark', 'light']}}), take_screenshot),
]
BY_NAME = {t[0]: t for t in TOOLS}


def call_tool(e, name, a, client=None):
    """-> MCP tool result {content, isError?}."""
    t = BY_NAME.get(name)
    try:
        if not t:
            raise ToolError('unknown tool %s' % name)
        a = a if isinstance(a, dict) else {}
        missing = [k for k in t[2]['required'] if a.get(k) in (None, '')]
        if missing:
            raise ToolError('missing argument(s): %s' % ', '.join(missing))
        v = t[3](e, a, client) if t[3] is propose_edit else t[3](e, a)
    except ToolError as err:
        return {'content': [{'type': 'text', 'text': str(err)}], 'isError': True}
    if isinstance(v, list):
        return {'content': v}
    return {'content': [{'type': 'text', 'text': v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1)}]}


# ---------- registration on the editor ----------

class Presence:
    """Registers the client on the editor and keeps the heartbeat; failures are silent (the editor may start later)."""

    def __init__(self, editor, interval):
        self.e, self.interval, self.id, self.client = editor, interval, uuid.uuid4().hex, None
        self.stop = threading.Event()

    def start(self, client):
        self.client = {'name': str(client.get('name') or 'unknown client'), 'version': str(client.get('version') or '')}
        self.beat()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stop.wait(self.interval):
            self.beat()

    def beat(self):
        if self.client:
            try:
                self.e.req('POST', '/api/ai/clients', dict(self.client, id=self.id), timeout=5)
            except ToolError:
                pass

    def close(self):
        self.stop.set()
        if self.client:
            try:
                self.e.req('DELETE', '/api/ai/clients/' + self.id, timeout=5)
            except ToolError:
                pass


def handle(e, presence, z):
    """One JSON-RPC message -> answer dict, or None for a notification."""
    if 'id' not in z:
        return None                                       # notifications/initialized, cancelled…
    m, p = z.get('method'), z.get('params') or {}
    if m == 'initialize':
        presence.start(p.get('clientInfo') or {})
        res = {'protocolVersion': p.get('protocolVersion') or PROTOCOL, 'capabilities': {'tools': {}},
               'serverInfo': {'name': 'bettertia-editor', 'version': VERSION},
               'instructions': 'BetterTIA Editor at %s: read projects, blocks and networks, propose edits of LAD/FBD '
                               'networks and check exports. Approving and writing to TIA Portal are human steps in the '
                               'editor UI.' % e.url}
    elif m == 'ping':
        res = {}
    elif m == 'tools/list':
        res = {'tools': [{'name': n, 'description': d, 'inputSchema': s} for n, d, s, _ in TOOLS]}
    elif m == 'tools/call':
        presence.beat()
        res = call_tool(e, p.get('name'), p.get('arguments') or {}, presence.client)
    else:
        return {'jsonrpc': '2.0', 'id': z['id'], 'error': {'code': -32601, 'message': 'unknown method %s' % m}}
    return {'jsonrpc': '2.0', 'id': z['id'], 'result': res}


def main(argv=None):
    ap = argparse.ArgumentParser(description='BetterTIA Editor MCP server (stdio)')
    ap.add_argument('--server', default=os.environ.get('TIAC_EDITOR_URL', 'http://localhost:8765'),
                    help='URL of the running editor (default http://localhost:8765)')
    ap.add_argument('--heartbeat', type=float, default=15, help='heartbeat interval to the editor (s)')
    a = ap.parse_args(argv)
    e = Editor(a.server)
    presence = Presence(e, a.heartbeat)
    out = sys.stdout.buffer
    try:
        for line in sys.stdin.buffer:
            if not line.strip():
                continue
            try:
                z = json.loads(line.decode('utf-8'))
            except ValueError:
                r = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
            else:
                r = handle(e, presence, z) if isinstance(z, dict) else None
            if r is not None:
                out.write((json.dumps(r, ensure_ascii=False) + '\n').encode('utf-8'))
                out.flush()
    finally:
        presence.close()


if __name__ == '__main__':
    sys.exit(main())
