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
import sys

from mcp_server import Editor, ToolError, q


def main(argv=None):
    ap = argparse.ArgumentParser(description='TIAC_Editor: check the export after writing against the proposal')
    ap.add_argument('--server', default='http://localhost:8765')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--edit', help='check the export after writing against the proposal')
    g.add_argument('--procedure', metavar='EDIT', help='print the TIA steps for the change (markdown)')
    g.add_argument('--check', metavar='EDIT', help='Check: the Reader exports the block, export check, Checked on a match')
    ap.add_argument('--export-ts', help='export timestamp (default: the newest one with the block)')
    a = ap.parse_args(argv)
    e = Editor(a.server)
    if (a.procedure or a.check) and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')             # a cp1250 Windows console would fail on characters like ⟦
    try:
        if a.procedure:
            print(e.get('/api/edits/%s/procedure' % q(a.procedure))['markdown'], end='')
            return 0
        if a.check:
            c = e.req('POST', '/api/edits/%s/check' % q(a.check), {}, timeout=900)
            print('%s %s · state %s · export %s · %s' % ('CHECKED' if c['match'] else 'MISMATCH' if c['export_ts'] else 'ERROR',
                                                      a.check, c['edit']['state'], c['export_ts'] or '—', c['message']))
            return 0 if c['match'] else 1 if c['export_ts'] else 2
        k = e.req('POST', '/api/edits/%s/check-export' % q(a.edit), {'export_ts': a.export_ts} if a.export_ts else {})
    except (ToolError, ValueError) as err:
        print('ERROR: %s' % err, file=sys.stderr)
        return 2
    print('%s %s · export %s · NW %s · %s' % ('MATCH' if k['match'] else 'MISMATCH', a.edit, k['export_ts'],
                                              k['nw_export'], k['note']))
    return 0 if k['match'] else 1


if __name__ == '__main__':
    sys.exit(main())
