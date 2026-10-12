"""LADEditor — lokální server (Python stdlib). M1 prohlížeč networků z exportu Readeru, M2 strom úprav a stavy.

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
import rozdil
import s7dcl
import stav

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


# ---------- M2: úpravy a stavy ----------

def _model(net):
    return {k: net[k] for k in ('prvky', 'rungy', 'vodice', 'rozlozeni')}


def over_model(po):
    """Model „po" z API (hranice důvěry): prvky s id/typ, boxy s piny, rungy jen z existujících id a wire#."""
    chyba = stav.ChybaStavu('po musí být model networku {prvky:[{id, typ, …}], rungy:[{z, cesta, do}]}', 400)
    if not isinstance(po, dict) or not isinstance(po.get('prvky'), list) or not isinstance(po.get('rungy'), list):
        raise chyba
    ids = set()
    for p in po['prvky']:
        if not isinstance(p, dict) or not isinstance(p.get('id'), str) or not isinstance(p.get('typ'), str) \
                or p['id'].startswith('x:') or p['id'] in ids:
            raise chyba
        if p['typ'] == 'box':
            nap = p.get('napajeni')
            if not (isinstance(nap, list) and len(nap) == 2 and all(isinstance(x, str) for x in nap)):
                raise chyba
            for k in ('vstupy', 'vystupy'):
                if not isinstance(p.get(k), list) or not all(
                        isinstance(x, dict) and isinstance(x.get('pin'), str) and isinstance(x.get('operand'), str)
                        for x in p[k]):
                    raise chyba
        ids.add(p['id'])
    for r in po['rungy']:
        if not isinstance(r, dict) or not isinstance(r.get('z'), str) or not isinstance(r.get('cesta'), list) \
                or not (r.get('do') is None or isinstance(r.get('do'), str)):
            raise chyba
        if not all(isinstance(it, str) and (it.startswith('wire#') or it in ids) for it in r['cesta']):
            raise chyba


def _rozloz(model, sirka=0):
    try:
        return layout.rozloz(model['rungy'], model['prvky'], sirka)
    except layout.Nerozlozitelne:
        return None, None


def api_stav(data, projekt=None):
    if projekt:
        return stav.nacti(data, projekt)
    return {'projekty': [{'projekt': p, 'upravy': stav.nacti(data, p)['upravy']} for p in stav.projekty(data)]}


def api_stav_put(root, data, projekt, obj):
    if projekt not in index_exportu(root):
        raise KeyError('neznámý projekt')
    with stav.ZAMEK:
        stav.uloz(data, projekt, obj)
    return obj


def api_uprava_nova(root, data, body):
    if not isinstance(body, dict):
        raise stav.ChybaStavu('tělo musí být JSON objekt', 400)
    projekt, blok = str(body.get('projekt', '')), str(body.get('blok', ''))
    stav._soubor(data, projekt)                      # kontrola --data a jména projektu
    net = api_network(root, projekt, blok, int(body.get('nw', 0)))
    if net['jazyk'] != 'LAD':
        raise stav.ChybaStavu('úprava jen pro LAD network', 400)
    over_model(body.get('po'))
    po = {'prvky': body['po']['prvky'], 'rungy': body['po']['rungy']}
    po['vodice'] = s7dcl.vodice(po)
    po['rozlozeni'] = body['po'].get('rozlozeni') or _rozloz(po)[0]
    cas = stav.ted()
    with stav.ZAMEK:
        s = stav.nacti(data, projekt)
        u = {'id': stav.nove_id(data), 'projekt': projekt, 'kolo': str(body.get('kolo') or '—'),
             'blok': net['soubor'], 'druh': net['druh'], 'cislo_bloku': net['cislo'], 'nw': net['nw'],
             'titulek': net['titulek'], 'autor': 'ANet', 'cas': cas, 'stav': 'Proposal',
             'poznamka': str(body.get('poznamka') or ''),
             'historie': [{'stav': 'Proposal', 'cas': cas, 'kdo': 'ANet'}],
             'pred': dict(_model(net), export_ts=net['ts']), 'po': po, 'kontroly': []}
        s['upravy'].append(u)
        stav.uloz(data, projekt, s)
    return u


def api_uprava_stav(data, uid, body):
    if not isinstance(body, dict):
        raise stav.ChybaStavu('tělo musí být JSON objekt', 400)
    with stav.ZAMEK:
        projekt, s, u = stav.najdi(data, uid)
        stav.zmen_stav(u, body.get('stav'), body.get('kdo'))
        stav.uloz(data, projekt, s)
    return u


def api_kontrola(data, body):
    if not isinstance(body, dict) or not isinstance(body.get('shoda'), bool):
        raise stav.ChybaStavu('kontrola: {uprava_id, shoda: true|false, export_ts, poznamka}', 400)
    with stav.ZAMEK:
        projekt, s, u = stav.najdi(data, str(body.get('uprava_id', '')))
        u.setdefault('kontroly', []).append({
            'cas': stav.ted(), 'shoda': body['shoda'], 'export_ts': str(body.get('export_ts') or ''),
            'poznamka': str(body.get('poznamka') or '')})
        stav.uloz(data, projekt, s)
    return u


def api_uprava_detail(data, uid, sirka=0):
    """Úprava + kresba modelu „po" se smazanými prvky jako přízraky a značkami rozdílu."""
    _, _, u = stav.najdi(data, uid)
    zmeny, smazane = rozdil.porovnej(u['pred'], u['po'])
    prvky, rungy, nevlozene = rozdil.slouc(u['pred'], u['po'], smazane)
    varovani = []
    rozl, kresba = _rozloz({'prvky': prvky, 'rungy': rungy}, sirka)
    if kresba is None:                                # přízraky rozbily rozložení -> jen „po"
        prvky, rungy, nevlozene = u['po']['prvky'], u['po']['rungy'], list(smazane)
        rozl, kresba = _rozloz(u['po'], sirka)
        if kresba is None:
            varovani.append('Model „po" nejde rozložit — zobrazen jako seznam rungů.')
    znacky = dict(zmeny)
    znacky.update({'x:' + i: 'smazany' for i in smazane if i not in nevlozene})
    if nevlozene:
        P = {p['id']: p for p in u['pred']['prvky']}
        varovani.append('Smazáno (bez místa v obraze): ' + ', '.join(
            '%s %s' % (P[i]['typ'], P[i].get('operand') or P[i].get('nazev') or '') for i in nevlozene))
    dalsi, predchozi = stav.prechody(u)
    return {'uprava': u, 'prvky': prvky, 'rungy': rungy, 'rozlozeni': rozl, 'kresba': kresba,
            'rozdil': znacky, 'varovani': varovani, 'dalsi': dalsi, 'predchozi': predchozi,
            'signed_povolen': stav.signed_povolen(u)}


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

    def _telo(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n > 5_000_000:
            raise ValueError('tělo je příliš velké')
        return json.loads(self.rfile.read(n).decode('utf-8') or 'null')

    def _obsluz(self, metoda):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        c = u.path
        try:
            if metoda == 'GET' and c in ('/', '/index.html'):
                with open(os.path.join(WEB, 'index.html'), 'rb') as f:
                    b = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(b)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(b)
            elif metoda == 'GET' and c == '/api/exporty':
                self._json(api_exporty(self.root))
            elif metoda == 'GET' and c == '/api/blok':
                self._json(api_blok(self.root, q.get('projekt', ''), q.get('blok', ''), q.get('ts')))
            elif metoda == 'GET' and c == '/api/network':
                self._json(api_network(self.root, q.get('projekt', ''), q.get('blok', ''),
                                       int(q.get('nw', '1')), q.get('ts'), int(q.get('sirka', '0'))))
            elif metoda == 'GET' and c == '/api/stav':
                self._json(api_stav(self.data, q.get('projekt')))
            elif metoda == 'PUT' and c == '/api/stav':
                self._json(api_stav_put(self.root, self.data, q.get('projekt', ''), self._telo()))
            elif metoda == 'POST' and c == '/api/uprava':
                self._json(api_uprava_nova(self.root, self.data, self._telo()), 201)
            elif metoda == 'POST' and re.fullmatch(r'/api/uprava/[\w-]+/stav', c):
                self._json(api_uprava_stav(self.data, c.split('/')[3], self._telo()))
            elif metoda == 'GET' and re.fullmatch(r'/api/uprava/[\w-]+', c):
                self._json(api_uprava_detail(self.data, c.split('/')[3], int(q.get('sirka', '0'))))
            elif metoda == 'POST' and c == '/api/kontrola':
                self._json(api_kontrola(self.data, self._telo()))
            else:
                self._json({'chyba': 'nenalezeno'}, 404)
        except stav.ChybaStavu as e:
            self._json({'chyba': str(e)}, e.kod)
        except KeyError as e:
            self._json({'chyba': str(e).strip("'")}, 404)
        except (ValueError, TypeError, s7dcl.ChybaParseru) as e:
            self._json({'chyba': str(e)}, 400)

    def do_GET(self):
        self._obsluz('GET')

    def do_POST(self):
        self._obsluz('POST')

    def do_PUT(self):
        self._obsluz('PUT')


def vytvor_server(port, root, data, host='127.0.0.1'):
    h = type('H', (Handler,), {'root': root, 'data': data})
    return ThreadingHTTPServer((host, port), h)


def main():
    ap = argparse.ArgumentParser(description='LADEditor server')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--export-root', required=True)
    ap.add_argument('--data', required=True, help='složka dat úprav: <data>\\<projekt>\\stav.json')
    a = ap.parse_args()
    srv = vytvor_server(a.port, a.export_root, a.data)
    print('LADEditor na http://localhost:%d  (export root %s, data %s)' % (a.port, a.export_root, a.data), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    sys.exit(main())
