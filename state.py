"""Change storage (`<data>/<project>/state.json`) and the change state machine.

The server is the only writer; writes are atomic (temporary file + os.replace) under a lock.
"""
import json
import os
import re
import threading
import time
from datetime import datetime


# Six states: Signed = written to the project (by hand or by the Writer), Checked = Reader export = proposal.
# An older state.json with five states loads unchanged (the state names stayed the same).
STATES = ['Proposal', 'Approved', 'Signed', 'Checked', 'Uploaded', 'Tested']
# side state off the chain: a person rejects a Proposal / Approved edit with a reason (for the AI to read);
# a new proposal (update) or "← Proposal" reopens it
DISAPPROVED = 'Disapproved'
ALL_STATES = STATES + [DISAPPROVED]
# what an edit changes: one network's model (and title/comment), block interface members, a new network, an HMI screen
KINDS = ['network', 'interface', 'new_network', 'hmi_screen']
LOCK = threading.RLock()


class StateError(Exception):
    def __init__(self, message, code=409):
        super().__init__(message)
        self.code = code


def now():
    return datetime.now().isoformat(timespec='seconds')


def check_name(project):
    """A project name is a folder name, never a path (it comes from the URL)."""
    if not re.fullmatch(r'[^\\/:*?"<>|]+', project or '') or project.strip('.') == '':
        raise StateError('invalid project', 400)
    return project


def _file(data, project):
    if not data:
        raise StateError('the server runs without --data', 503)
    return os.path.join(data, check_name(project), 'state.json')


def load(data, project):
    c = _file(data, project)
    with LOCK:                                        # an open file would make os.replace fail on Windows
        if not os.path.exists(c):
            return {'edits': []}
        with open(c, encoding='utf-8') as f:
            return migrate(json.load(f))


def migrate(obj):
    """Edits before kinds existed: `kind` held the block kind (FB, FC, OB) -> `block_kind`, `kind` = "network"."""
    for u in (obj.get('edits') if isinstance(obj, dict) else None) or []:
        if isinstance(u, dict) and u.get('kind') not in KINDS:
            if u.get('kind') is not None:
                u['block_kind'] = u['kind']
            u['kind'] = 'network'
    return obj


def save(data, project, obj):
    """Atomic write: the whole file to .tmp in the same folder, then os.replace (the old file survives an error)."""
    verify_structure(migrate(obj))
    _write_json(_file(data, project), obj)


def _write_json(c, obj):
    os.makedirs(os.path.dirname(c), exist_ok=True)
    tmp = '%s.tmp-%d-%d' % (c, os.getpid(), threading.get_ident())
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        for i in range(20):                           # Windows: a reader outside the lock (antivirus, an editor) blocks it
            try:
                os.replace(tmp, c)
                break
            except PermissionError:
                if i == 19:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def verify_structure(obj):
    if not isinstance(obj, dict) or not isinstance(obj.get('edits'), list):
        raise StateError('state must be {"edits": [...]}', 400)
    t = obj.get('tia')
    if t is not None and not (isinstance(t, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in t.items())):
        raise StateError('tia must be {softwarePath, plcName, groupPath?} (strings)', 400)
    ids = set()
    for u in obj['edits']:
        if not isinstance(u, dict) or not isinstance(u.get('id'), str) or u.get('state') not in ALL_STATES:
            raise StateError('a change needs an id and a state from %s' % ALL_STATES, 400)
        if u['id'] in ids:
            raise StateError('duplicate id %s' % u['id'], 400)
        ids.add(u['id'])


# ---------- project registry <data>/projects.json ----------
# [{name, source: exports|tia|ap21, export_root, project_path?, tia: {softwarePath, plcName}?, last_snapshot?, added}]

def registry(data):
    c = os.path.join(data, 'projects.json') if data else ''
    with LOCK:
        if not c or not os.path.exists(c):
            return []
        with open(c, encoding='utf-8') as f:
            return json.load(f)


def save_registry(data, items):
    if not data:
        raise StateError('the server runs without --data', 503)
    _write_json(os.path.join(data, 'projects.json'), items)


def register(data, name, **fields):
    """Add or update a registry entry (by name) -> entry."""
    _file(data, name)                                 # checks --data and the project name
    with LOCK:
        items = registry(data)
        p = next((x for x in items if x['name'] == name), None)
        if p is None:
            p = {'name': name, 'added': now()}
            items.append(p)
        p.update({k: v for k, v in fields.items() if v is not None})
        save_registry(data, items)
    return p


def projects(data):
    if not data or not os.path.isdir(data):
        return []
    return sorted(p for p in os.listdir(data) if os.path.isfile(os.path.join(data, p, 'state.json')))


def new_id(data):
    """Change ids are unique across all projects (so ?edit=U001 does not need the project); a deleted id is not reused."""
    n = 0
    for p in projects(data):
        s = load(data, p)
        for u in s['edits'] + [x for x in s.get('deleted') or [] if x.get('kind') == 'edit']:
            m = re.fullmatch(r'U(\d+)', u['id'])
            if m:
                n = max(n, int(m.group(1)))
    return 'U%03d' % (n + 1)


def find(data, uid):
    """-> (project, state, edit); KeyError when missing."""
    for p in projects(data):
        s = load(data, p)
        for u in s['edits']:
            if u['id'] == uid:
                return p, s, u
    raise KeyError('change %s does not exist' % uid)


def checked_allowed(u):
    """The last check decides whether Checked is unlocked (a later mismatch locks it again); Signed is free."""
    k = u.get('checks') or []
    return bool(k) and k[-1].get('match') is True


def rank(s):
    """Position in the chain; Disapproved counts as Proposal."""
    return 0 if s == DISAPPROVED else STATES.index(s)


def transitions(u):
    """-> (next state | None, previous state | None)"""
    if u['state'] == DISAPPROVED:
        return None, 'Proposal'
    i = STATES.index(u['state'])
    return (STATES[i + 1] if i + 1 < len(STATES) else None, STATES[i - 1] if i > 0 else None)


def change_state(u, new_value, by, reason=None):
    if new_value not in ALL_STATES:
        raise StateError('unknown state %r' % (new_value,), 400)
    if DISAPPROVED in (u['state'], new_value):
        if not (new_value == DISAPPROVED and u['state'] in ('Proposal', 'Approved')
                or u['state'] == DISAPPROVED and new_value == 'Proposal'):
            raise StateError('transition %s → %s is not allowed (Disapproved only from Proposal or Approved, '
                             'and back only to Proposal)' % (u['state'], new_value))
        reason = reason.strip()[:4000] if isinstance(reason, str) else ''
        if new_value == DISAPPROVED and not reason:
            raise StateError('Disapproved needs a reason: why the edit is rejected (the AI reads it to propose a fix)', 400)
    else:
        i, j = STATES.index(u['state']), STATES.index(new_value)
        if abs(i - j) != 1:
            raise StateError('transition %s → %s is not allowed (only one step forward or back)' % (u['state'], new_value))
        if new_value == 'Checked' and j > i and not checked_allowed(u):
            raise StateError('Checked only after an export check with a match (Check / POST /api/check)')
    h = {'state': new_value, 'time': now(), 'by': by or 'user'}
    if new_value == DISAPPROVED:
        h['reason'] = reason
        u['disapproved'] = dict(h)                    # the latest rejection stays readable after a reopen
    u['state'] = new_value
    u.setdefault('history', []).append(h)
