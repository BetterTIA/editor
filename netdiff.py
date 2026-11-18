"""Two exports of one block compared network by network (file level, no TIA): code with masked MLC ids, titles and
comments (resolved from the .s7res) separately."""
import difflib
import re

import s7dcl

MAX_NETWORKS = 200       # listed networks (changed ones first); the summary always counts all
MAX_DIFF_LINES = 200


def _code(nw):
    """Network code with MLC ids masked and trailing blanks dropped, so a renumbered text id is no difference."""
    return '\n'.join(x.rstrip() for x in re.sub(r'MLC_\w+', 'MLC', nw['text']).strip().splitlines())


def _pairs(a, b):
    """Network pairs (i | None, j | None) in order; equal runs pair 1:1, a replaced run pairs by position, the rest is added / removed."""
    res = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        n = min(i2 - i1, j2 - j1) if op == 'replace' else i2 - i1 if op == 'equal' else 0
        res += [(i1 + k, j1 + k) for k in range(n)]
        res += [(i1 + k, None) for k in range(n, i2 - i1)] + [(None, j1 + k) for k in range(n, j2 - j1)]
    return res


def compare(path_a, path_b, detail=None):
    """-> {summary, networks: [...], notes}; detail = [(nw_a, nw_b)] numbers (1-based) of pairs that get a unified diff."""
    A, B = s7dcl.load_block(path_a), s7dcl.load_block(path_b)
    na, nb = A['networks'], B['networks']
    ca, cb = [_code(n) for n in na], [_code(n) for n in nb]
    want = set(detail or [])
    res, counts = [], {'same': 0, 'changed': 0, 'added': 0, 'removed': 0}
    for i, j in _pairs(ca, cb):
        x, y = na[i] if i is not None else None, nb[j] if j is not None else None
        z = {'nw_a': x and x['nw'], 'nw_b': y and y['nw'], 'title': (y or x)['title'], 'language': (y or x)['language']}
        if x is None or y is None:
            z['status'] = 'added' if x is None else 'removed'
            z['lines'] = len((cb[j] if x is None else ca[i]).splitlines())
        else:
            z['code_changed'] = ca[i] != cb[j]
            if x['title'] != y['title']:
                z['title_change'] = [x['title'], y['title']]
            if x['comment'] != y['comment']:
                z['comment_change'] = [x['comment'], y['comment']]
            z['status'] = 'changed' if z['code_changed'] or 'title_change' in z or 'comment_change' in z else 'same'
            if z['code_changed'] and (z['nw_a'], z['nw_b']) in want:
                d = list(difflib.unified_diff(ca[i].splitlines(), cb[j].splitlines(), 'A NW%d' % z['nw_a'],
                                              'B NW%d' % z['nw_b'], lineterm='', n=1))
                z['diff'] = '\n'.join(d[:MAX_DIFF_LINES]) + ('\n… %d more lines' % (len(d) - MAX_DIFF_LINES)
                                                               if len(d) > MAX_DIFF_LINES else '')
        counts[z['status']] += 1
        res.append(z)
    notes = []
    if A['title'] != B['title']:
        notes.append('block title "%s" → "%s"' % (A['title'], B['title']))
    if A['attributes'] != B['attributes']:
        notes.append('block attributes differ')
    if A['variables'] != B['variables']:
        notes.append('interface variables differ')
    shown = [z for z in res if z['status'] != 'same'][:MAX_NETWORKS]
    return {'summary': dict(counts, networks_a=len(na), networks_b=len(nb)), 'networks': shown,
            'truncated': counts['same'] + len(shown) < len(res) or counts['changed'] + counts['added'] + counts['removed'] > len(shown),
            'notes': notes}
