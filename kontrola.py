"""Kontrola „export po zápisu = návrh" (M4) — CLI pro Clauda i Honzu.

python kontrola.py --server http://localhost:8765 --uprava U003 [--export-ts 20261012-140000]

Server najde v export rootu nejnovější (nebo zadaný) export bloku úpravy, network podle titulku (pak podle čísla),
porovná ho s modelem „po" podle obsahu (typ + operand/piny + série/větve, bez id a rozložení) a zapíše kontrolu.
Exit: 0 = shoda, 1 = neshoda, 2 = chyba.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request


def kontrola(server, uprava, export_ts=None):
    telo = json.dumps({'export_ts': export_ts} if export_ts else {}).encode('utf-8')
    r = urllib.request.Request('%s/api/uprava/%s/kontrola-exportem' % (server.rstrip('/'), uprava), data=telo,
                               method='POST', headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(r, timeout=60) as f:
        return json.loads(f.read().decode('utf-8'))


def main(argv=None):
    ap = argparse.ArgumentParser(description='LADEditor: kontrola exportu po zápisu proti návrhu')
    ap.add_argument('--server', default='http://localhost:8765')
    ap.add_argument('--uprava', required=True)
    ap.add_argument('--export-ts', help='timestamp exportu (jinak nejnovější s blokem)')
    a = ap.parse_args(argv)
    try:
        k = kontrola(a.server, a.uprava, a.export_ts)
    except urllib.error.HTTPError as e:
        try:
            zprava = json.loads(e.read().decode('utf-8')).get('chyba')
        except ValueError:
            zprava = str(e)
        print('CHYBA %s: %s' % (e.code, zprava), file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError, ValueError) as e:
        print('CHYBA: %s' % e, file=sys.stderr)
        return 2
    print('%s %s · export %s · NW %s · %s' % ('SHODA' if k['shoda'] else 'NESHODA', a.uprava, k['export_ts'],
                                              k['nw_export'], k['poznamka']))
    return 0 if k['shoda'] else 1


if __name__ == '__main__':
    sys.exit(main())
