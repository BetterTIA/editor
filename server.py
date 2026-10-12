"""LADEditor — lokální server (Python stdlib). M1: prohlížeč networků z exportu Readeru.

python server.py --port 8765 --export-root "C:\\TIA\\export" --data "<dir>"
"""
import argparse
import json
import os
import re
import sys
import textwrap
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import layout
import s7dcl

TS = re.compile(r'^\d{8}-\d{6}$')
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')


def index_exportu(root):
    """{projekt: {ts: {blok: cesta}}} — jen časové složky, které obsahují .s7dcl."""
    vys = {}
    if not os.path.isdir(root):
        return vys
    for projekt in sorted(os.listdir(root)):
        pd = os.path.join(root, projekt)
        if not os.path.isdir(pd):
            continue
        for ts in sorted(os.listdir(pd)):
            if not TS.match(ts):
                continue
            bloky = {}
            for dp, _, soubory in os.walk(os.path.join(pd, ts)):
                for f in soubory:
                    if f.endswith('.s7dcl'):
                        bloky.setdefault(f[:-6], os.path.join(dp, f))
            if bloky:
                vys.setdefault(projekt, {})[ts] = bloky
    return vys


def najdi_blok(root, projekt, blok, ts=None):
    """-> (ts, cesta, bloky_ts) nejnovějšího exportu s blokem; jména jen z indexu (žádné skládání cest z URL)."""
    exp = index_exportu(root).get(projekt)
    if not exp:
        raise KeyError('neznámý projekt')
    for t in sorted(exp, reverse=True):
        if (ts is None or t == ts) and blok in exp[t]:
            return t, exp[t][blok], exp[t]
    raise KeyError('blok v exportu není')


def _nacti(root, projekt, blok, ts=None):
    t, cesta, bloky = najdi_blok(root, projekt, blok, ts)
    b = s7dcl.nacti_blok(cesta)

    def najdi_db(jmeno):
        c = bloky.get(jmeno)
        if c:
            try:
                return s7dcl.parse_blok(s7dcl.cti(c)).get('instance_typ')
            except s7dcl.ChybaParseru:
                return None
    s7dcl.dopln_typy_fb(b, najdi_db)
    return t, b


def api_exporty(root):
    vys = []
    for projekt, exp in index_exportu(root).items():
        nejnovejsi = {}
        for ts in sorted(exp):
            for blok in exp[ts]:
                nejnovejsi[blok] = ts
        vys.append({'projekt': projekt,
                    'exporty': [{'ts': ts, 'bloku': len(b)} for ts, b in sorted(exp.items(), reverse=True)],
                    'bloky': [{'blok': k, 'ts': v} for k, v in sorted(nejnovejsi.items())]})
    return {'export_root': root, 'projekty': vys}


def api_blok(root, projekt, blok, ts=None):
    t, b = _nacti(root, projekt, blok, ts)
    return {'projekt': projekt, 'ts': t, 'blok': b['nazev'], 'soubor': blok, 'druh': b['druh'],
            'cislo': b['cislo'], 'jazyk': b['jazyk'], 'titulek': b['titulek'],
            'networky': [{'nw': n['nw'], 'jazyk': n['jazyk'], 'titulek': n['titulek']} for n in b['networky']]}


def api_network(root, projekt, blok, nw, ts=None, sirka=0):
    t, b = _nacti(root, projekt, blok, ts)
    if not 1 <= nw <= len(b['networky']):
        raise KeyError('network neexistuje')
    n = b['networky'][nw - 1]
    vys = {'projekt': projekt, 'ts': t, 'blok': b['nazev'], 'soubor': blok, 'druh': b['druh'],
           'cislo': b['cislo'], 'nw': nw, 'jazyk': n['jazyk'], 'titulek': n['titulek'],
           'komentar': n['komentar'], 'prvky': n.get('prvky', []), 'rungy': n.get('rungy', []),
           'vodice': s7dcl.vodice(n) if n['jazyk'] == 'LAD' else [],
           'rozlozeni': None, 'kresba': None, 'varovani': []}
    if n['jazyk'] != 'LAD':
        vys['varovani'].append('Network v jazyce %s — M1 kreslí jen LAD.' % (n['jazyk'] or '?'))
        vys['text'] = textwrap.dedent(n['text']).strip(chr(10))
        return vys
    try:
        vys['rozlozeni'], vys['kresba'] = layout.rozloz(n['rungy'], n['prvky'], sirka)
    except layout.Nerozlozitelne as e:
        vys['varovani'].append('Rozložení se nepodařilo (%s) — zobrazeno jako seznam rungů.' % e)
    return vys


class Handler(BaseHTTPRequestHandler):
    root = ''
    data = ''

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, kod=200):
        b = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(kod)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ('/', '/index.html'):
                with open(os.path.join(WEB, 'index.html'), 'rb') as f:
                    b = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(b)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(b)
            elif u.path == '/api/exporty':
                self._json(api_exporty(self.root))
            elif u.path == '/api/blok':
                self._json(api_blok(self.root, q.get('projekt', ''), q.get('blok', ''), q.get('ts')))
            elif u.path == '/api/network':
                self._json(api_network(self.root, q.get('projekt', ''), q.get('blok', ''),
                                       int(q.get('nw', '1')), q.get('ts'), int(q.get('sirka', '0'))))
            else:
                self._json({'chyba': 'nenalezeno'}, 404)
        except KeyError as e:
            self._json({'chyba': str(e).strip("'")}, 404)
        except (ValueError, s7dcl.ChybaParseru) as e:
            self._json({'chyba': str(e)}, 400)


def vytvor_server(port, root, data, host='127.0.0.1'):
    h = type('H', (Handler,), {'root': root, 'data': data})
    return ThreadingHTTPServer((host, port), h)


def main():
    ap = argparse.ArgumentParser(description='LADEditor server')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--export-root', required=True)
    ap.add_argument('--data', default='', help='složka dat úprav (stav.json) — použije M2')
    a = ap.parse_args()
    srv = vytvor_server(a.port, a.export_root, a.data)
    print('LADEditor na http://localhost:%d  (export root %s)' % (a.port, a.export_root), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    sys.exit(main())
