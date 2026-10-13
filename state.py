"""Úložiště úprav (`<data>/<projekt>/stav.json`) a stavový automat úprav.

Server je jediný zapisovatel; zápis atomicky (dočasný soubor + os.replace) pod zámkem.
"""
import json
import os
import re
import threading
from datetime import datetime


# od 13. 10. 2026 šest stavů: Signed = zapsáno v projektu (Honza / Writer), Checked = Reader export = návrh.
# Starší stav.json s pěti stavy se načte beze změny (jména stavů zůstala).
STATES = ['Proposal', 'Approved', 'Signed', 'Checked', 'Uploaded', 'Tested']
LOCK = threading.RLock()


class StateError(Exception):
    def __init__(self, message, code=409):
        super().__init__(message)
        self.code = code


def now():
    return datetime.now().isoformat(timespec='seconds')


def _file(data, project):
    if not data:
        raise StateError('the server runs without --data', 503)
    if not re.fullmatch(r'[^\\/:*?"<>|]+', project or '') or project.strip('.') == '':
        raise StateError('invalid project', 400)
    return os.path.join(data, project, 'state.json')


def load(data, project):
    c = _file(data, project)
    if not os.path.exists(c):
        return {'edits': []}
    with open(c, encoding='utf-8') as f:
        return json.load(f)


def save(data, project, obj):
    """Atomický zápis: celý soubor do .tmp ve stejné složce, pak os.replace (starý soubor zůstane při chybě)."""
    verify_structure(obj)
    c = _file(data, project)
    os.makedirs(os.path.dirname(c), exist_ok=True)
    tmp = '%s.tmp-%d-%d' % (c, os.getpid(), threading.get_ident())
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, c)
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
        if not isinstance(u, dict) or not isinstance(u.get('id'), str) or u.get('state') not in STATES:
            raise StateError('a change needs an id and a state from %s' % STATES, 400)
        if u['id'] in ids:
            raise StateError('duplicate id %s' % u['id'], 400)
        ids.add(u['id'])


def projects(data):
    if not data or not os.path.isdir(data):
        return []
    return sorted(p for p in os.listdir(data) if os.path.isfile(os.path.join(data, p, 'state.json')))


def new_id(data):
    """Id úprav jsou jedinečná přes všechny projekty (?uprava=U001 pak nepotřebuje projekt)."""
    n = 0
    for p in projects(data):
        for u in load(data, p)['edits']:
            m = re.fullmatch(r'U(\d+)', u['id'])
            if m:
                n = max(n, int(m.group(1)))
    return 'U%03d' % (n + 1)


def find(data, uid):
    """-> (projekt, stav, uprava) ; KeyError když není."""
    for p in projects(data):
        s = load(data, p)
        for u in s['edits']:
            if u['id'] == uid:
                return p, s, u
    raise KeyError('change %s does not exist' % uid)


def checked_allowed(u):
    """O odemknutí Checked rozhoduje poslední kontrola (pozdější neshoda zase zamkne); Signed je volný."""
    k = u.get('checks') or []
    return bool(k) and k[-1].get('match') is True


def transitions(u):
    """-> (další stav | None, předchozí stav | None)"""
    i = STATES.index(u['state'])
    return (STATES[i + 1] if i + 1 < len(STATES) else None, STATES[i - 1] if i > 0 else None)


def change_state(u, new_value, by):
    if new_value not in STATES:
        raise StateError('unknown state %r' % (new_value,), 400)
    i, j = STATES.index(u['state']), STATES.index(new_value)
    if abs(i - j) != 1:
        raise StateError('transition %s → %s is not allowed (only one step forward or back)' % (u['state'], new_value))
    if new_value == 'Checked' and j > i and not checked_allowed(u):
        raise StateError('Checked only after an export check with a match (Check / POST /api/check)')
    u['state'] = new_value
    u.setdefault('history', []).append({'state': new_value, 'time': now(), 'by': by or 'user'})
