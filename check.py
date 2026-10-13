"""Kontrola „export po zápisu = návrh" (M4) — CLI pro Clauda i Honzu.

python kontrola.py --server http://localhost:8765 --uprava U003 [--export-ts 20261012-140000]
python kontrola.py --postup U003        # vytiskne postup skládání pro TIA (markdown) — F2
python kontrola.py --check U003         # M5: Check = TIAC_Reader export bloku + kontrola; při shodě Signed -> Checked

Server najde v export rootu nejnovější (nebo zadaný) export bloku úpravy, network podle titulku (pak podle čísla),
porovná ho s modelem „po" podle obsahu (typ + operand/piny + série/větve, bez id a rozložení) a zapíše kontrolu.
Exit: 0 = shoda, 1 = neshoda, 2 = chyba.
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
    ap = argparse.ArgumentParser(description='TIAC_Editor: check export after zápisu against návrhu')
    ap.add_argument('--server', default='http://localhost:8765')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--edit', help='zkontroluje export after zápisu against návrhu')
    g.add_argument('--procedure', metavar='UPRAVA', help='vytiskne procedure skládání for TIA (markdown)')
    g.add_argument('--check', metavar='UPRAVA', help='Check: Reader exportuje block, check, při shodě Checked')
    ap.add_argument('--export-ts', help='timestamp export (jinak nejnovější s block)')
    a = ap.parse_args(argv)
    try:
        if a.procedure:
            if hasattr(sys.stdout, 'reconfigure'):
                sys.stdout.reconfigure(encoding='utf-8')     # konzole Windows (cp1250) by spadla na „⟦
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
