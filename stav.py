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
STAVY = ['Proposal', 'Approved', 'Signed', 'Checked', 'Uploaded', 'Tested']
ZAMEK = threading.RLock()


class ChybaStavu(Exception):
    def __init__(self, zprava, kod=409):
        super().__init__(zprava)
        self.kod = kod


def ted():
    return datetime.now().isoformat(timespec='seconds')


def _soubor(data, projekt):
    if not data:
        raise ChybaStavu('server běží bez --data', 503)
    if not re.fullmatch(r'[^\\/:*?"<>|]+', projekt or '') or projekt.strip('.') == '':
        raise ChybaStavu('neplatný projekt', 400)
    return os.path.join(data, projekt, 'stav.json')


def nacti(data, projekt):
    c = _soubor(data, projekt)
    if not os.path.exists(c):
        return {'upravy': []}
    with open(c, encoding='utf-8') as f:
        return json.load(f)


def uloz(data, projekt, obj):
    """Atomický zápis: celý soubor do .tmp ve stejné složce, pak os.replace (starý soubor zůstane při chybě)."""
    over_strukturu(obj)
    c = _soubor(data, projekt)
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


def over_strukturu(obj):
    if not isinstance(obj, dict) or not isinstance(obj.get('upravy'), list):
        raise ChybaStavu('stav musí být {"upravy": [...]}', 400)
    ids = set()
    for u in obj['upravy']:
        if not isinstance(u, dict) or not isinstance(u.get('id'), str) or u.get('stav') not in STAVY:
            raise ChybaStavu('úprava musí mít id a stav z %s' % STAVY, 400)
        if u['id'] in ids:
            raise ChybaStavu('duplicitní id ' + u['id'], 400)
        ids.add(u['id'])


def projekty(data):
    if not data or not os.path.isdir(data):
        return []
    return sorted(p for p in os.listdir(data) if os.path.isfile(os.path.join(data, p, 'stav.json')))


def nove_id(data):
    """Id úprav jsou jedinečná přes všechny projekty (?uprava=U001 pak nepotřebuje projekt)."""
    n = 0
    for p in projekty(data):
        for u in nacti(data, p)['upravy']:
            m = re.fullmatch(r'U(\d+)', u['id'])
            if m:
                n = max(n, int(m.group(1)))
    return 'U%03d' % (n + 1)


def najdi(data, uid):
    """-> (projekt, stav, uprava) ; KeyError když není."""
    for p in projekty(data):
        s = nacti(data, p)
        for u in s['upravy']:
            if u['id'] == uid:
                return p, s, u
    raise KeyError('úprava %s neexistuje' % uid)


def checked_povolen(u):
    """O odemknutí Checked rozhoduje poslední kontrola (pozdější neshoda zase zamkne); Signed je volný."""
    k = u.get('kontroly') or []
    return bool(k) and k[-1].get('shoda') is True


def prechody(u):
    """-> (další stav | None, předchozí stav | None)"""
    i = STAVY.index(u['stav'])
    return (STAVY[i + 1] if i + 1 < len(STAVY) else None, STAVY[i - 1] if i > 0 else None)


def zmen_stav(u, novy, kdo):
    if novy not in STAVY:
        raise ChybaStavu('neznámý stav %r' % novy, 400)
    i, j = STAVY.index(u['stav']), STAVY.index(novy)
    if abs(i - j) != 1:
        raise ChybaStavu('přechod %s → %s není povolen (jen o krok vpřed nebo zpět)' % (u['stav'], novy))
    if novy == 'Checked' and j > i and not checked_povolen(u):
        raise ChybaStavu('Checked až po kontrole exportem se shodou (Check / POST /api/kontrola)')
    u['stav'] = novy
    u.setdefault('historie', []).append({'stav': novy, 'cas': ted(), 'kdo': kdo or 'Honza'})
