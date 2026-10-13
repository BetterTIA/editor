"""Export check "export after writing = proposal" — command-line client for the editor server.

python check.py --server http://localhost:8765 --edit U003 [--export-ts 20261012-140000]
python check.py --procedure U003        # prints the TIA steps for the change (markdown)
python check.py --check U003            # Check = TIAC_Reader exports the block + export check; on a match Signed -> Checked

The server finds the newest (or the given) export of the change's block in the export root, the network by title
(then by number), compares it with the "after" model by content (type + operand/pins + series/branches, ignoring ids
and layout) and records the check.
Exit: 0 = match, 1 = mismatch, 2 = error.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request


def check_export(server, edit, export_ts=None):
    body = json.dumps({'export_ts': export_ts} if export_ts else {}).encode('utf-8')
    r = urllib.request.Request('%s/api/edits/%s/check-export' % (server.rstrip('/'), edit), data=body,
                               method='POST', headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(r, timeout=60) as f:
        return json.loads(f.read().decode('utf-8'))


def run_check(server, edit):
    r = urllib.request.Request('%s/api/edits/%s/check' % (server.rstrip('/'), edit), data=b'{}', method='POST',
                               headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(r, timeout=900) as f:
        return json.loads(f.read().decode('utf-8'))


def procedure(server, edit):
    with urllib.request.urlopen('%s/api/edits/%s/procedure' % (server.rstrip('/'), edit), timeout=60) as f:
        return json.loads(f.read().decode('utf-8'))


def main(argv=None):
    ap = argparse.ArgumentParser(description='TIAC_Editor: check the export after writing against the proposal')
    ap.add_argument('--server', default='http://localhost:8765')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--edit', help='check the export after writing against the proposal')
    g.add_argument('--procedure', metavar='EDIT', help='print the TIA steps for the change (markdown)')
    g.add_argument('--check', metavar='EDIT', help='Check: the Reader exports the block, export check, Checked on a match')
    ap.add_argument('--export-ts', help='export timestamp (default: the newest one with the block)')
    a = ap.parse_args(argv)
    try:
        if a.procedure:
            if hasattr(sys.stdout, 'reconfigure'):
                sys.stdout.reconfigure(encoding='utf-8')     # a cp1250 Windows console would fail on characters like ⟦
            print(procedure(a.server, a.procedure)['markdown'], end='')
            return 0
        if a.check:
            if hasattr(sys.stdout, 'reconfigure'):
                sys.stdout.reconfigure(encoding='utf-8')
            c = run_check(a.server, a.check)
            print('%s %s · state %s · export %s · %s' % ('CHECKED' if c['match'] else 'MISMATCH' if c['export_ts'] else 'ERROR',
                                                      a.check, c['edit']['state'], c['export_ts'] or '—', c['message']))
            return 0 if c['match'] else 1 if c['export_ts'] else 2
        k = check_export(a.server, a.edit, a.export_ts)
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read().decode('utf-8')).get('error')
        except ValueError:
            message = str(e)
        print('ERROR %s: %s' % (e.code, message), file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError, ValueError) as e:
        print('ERROR: %s' % e, file=sys.stderr)
        return 2
    print('%s %s · export %s · NW %s · %s' % ('MATCH' if k['match'] else 'MISMATCH', a.edit, k['export_ts'],
                                              k['nw_export'], k['note']))
    return 0 if k['match'] else 1


if __name__ == '__main__':
    sys.exit(main())
