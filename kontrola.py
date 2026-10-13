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


def kontrola(server, uprava, export_ts=None):
    telo = json.dumps({'export_ts': export_ts} if export_ts else {}).encode('utf-8')
    r = urllib.request.Request('%s/api/uprava/%s/kontrola-exportem' % (server.rstrip('/'), uprava), data=telo,
                               method='POST', headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(r, timeout=60) as f:
        return json.loads(f.read().decode('utf-8'))


def check(server, uprava):
    r = urllib.request.Request('%s/api/uprava/%s/check' % (server.rstrip('/'), uprava), data=b'{}', method='POST',
                               headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(r, timeout=900) as f:
        return json.loads(f.read().decode('utf-8'))


def postup(server, uprava):
    with urllib.request.urlopen('%s/api/uprava/%s/postup' % (server.rstrip('/'), uprava), timeout=60) as f:
        return json.loads(f.read().decode('utf-8'))


def main(argv=None):
    ap = argparse.ArgumentParser(description='TIAC_Editor: kontrola exportu po zápisu proti návrhu')
    ap.add_argument('--server', default='http://localhost:8765')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--uprava', help='zkontroluje export po zápisu proti návrhu')
    g.add_argument('--postup', metavar='UPRAVA', help='vytiskne postup skládání pro TIA (markdown)')
    g.add_argument('--check', metavar='UPRAVA', help='Check: Reader exportuje blok, kontrola, při shodě Checked')
    ap.add_argument('--export-ts', help='timestamp exportu (jinak nejnovější s blokem)')
    a = ap.parse_args(argv)
    try:
        if a.postup:
            if hasattr(sys.stdout, 'reconfigure'):
                sys.stdout.reconfigure(encoding='utf-8')     # konzole Windows (cp1250) by spadla na „⟦
            print(postup(a.server, a.postup)['markdown'], end='')
            return 0
        if a.check:
            if hasattr(sys.stdout, 'reconfigure'):
                sys.stdout.reconfigure(encoding='utf-8')
            c = check(a.server, a.check)
            print('%s %s · stav %s · export %s · %s' % ('CHECKED' if c['shoda'] else 'NESHODA' if c['export_ts'] else 'CHYBA',
                                                      a.check, c['uprava']['stav'], c['export_ts'] or '—', c['zprava']))
            return 0 if c['shoda'] else 1 if c['export_ts'] else 2
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
