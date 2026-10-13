"""TIAC_Editor — lokální server (Python stdlib). M1 prohlížeč networků z exportu Readeru, M2 strom úprav a stavy.

python server.py --port 8765 --export-root "C:\\TIA\\export" --data "<dir>"
"""
import argparse
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import fbd
import katalog
import layout
import mcp_klient
import model
import postup
import rozdil
import s7dcl
import s7dcl_zapis
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
    """-> (ts, cesta, bloky) nejnovějšího exportu s blokem; bloky = nejnovější kopie všech bloků projektu do ts
    (dílčí export pak dohledá instanční DB i FC ze staršího). Jména jen z indexu (žádné skládání cest z URL)."""
    exp = index_exportu(root).get(projekt)
    if not exp:
        raise KeyError('neznámý projekt')
    for t in sorted(exp, reverse=True):
        if (ts is None or t == ts) and blok in exp[t]:
            bloky = {}
            for x in sorted(exp):
                if x <= t:
                    bloky.update(exp[x])
            return t, exp[t][blok], bloky
    raise KeyError('blok v exportu není')


def _nacti(root, projekt, blok, ts=None):
    t, cesta, bloky = najdi_blok(root, projekt, blok, ts)
    b = s7dcl.nacti_blok(cesta)

    fc = set()

    def najdi_db(jmeno):
        c = bloky.get(jmeno)
        if c:
            try:
                x = s7dcl.parse_blok(s7dcl.cti(c))
            except s7dcl.ChybaParseru:
                return None
            if x['druh'] == 'FC':
                fc.add(jmeno)
            return x.get('instance_typ')
    s7dcl.dopln_typy_fb(b, najdi_db)
    for nw in b['networky']:                          # "FC"(…) je volání FC, ne instance FB
        for p in nw.get('prvky', []):
            if p.get('volani') == 'fb' and (p['instance'] or '').strip('"') in fc:
                p.update(volani='fc', nazev=p['instance'].strip('"'), instance=None)
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
           'f': b['atributy'].get('S7_Safety') == 'TRUE',
           'komentar': n['komentar'], 'prvky': n.get('prvky', []), 'rungy': n.get('rungy', []),
           'vodice': s7dcl.vodice(n) if n['jazyk'] == 'LAD' else [],
           'rozlozeni': None, 'kresba': None, 'varovani': []}
    if n['jazyk'] == 'FBD' and 'prvky' in n:
        vys.update(rungy=[], signaly=fbd.signaly(n['prvky']))
        try:
            vys['rozlozeni'], vys['kresba'] = fbd.rozloz(n['prvky'], sirka)
        except fbd.Nerozlozitelne as e:
            vys['varovani'].append('Rozložení FBD se nepodařilo (%s) — zobrazen text.' % e)
            vys['text'] = textwrap.dedent(n['text']).strip(chr(10))
        lad = fbd.jako_lad(n['prvky'])                  # jen ke čtení: čistá bitová logika
        if lad:
            try:
                vys['jako_lad'] = {'prvky': lad[1], 'rungy': lad[0], 'kresba': layout.rozloz(lad[0], lad[1], sirka)[1]}
            except layout.Nerozlozitelne:
                pass
        return vys
    if n['jazyk'] != 'LAD':
        vys['varovani'].append('Network v jazyce %s — kreslí se jen LAD a FBD.' % (n['jazyk'] or '?')
                               if not n.get('chyba') else 'FBD network nejde přečíst (%s) — zobrazen text.' % n['chyba'])
        vys['text'] = textwrap.dedent(n['text']).strip(chr(10))
        return vys
    try:
        vys['rozlozeni'], vys['kresba'] = layout.rozloz(n['rungy'], n['prvky'], sirka)
    except layout.Nerozlozitelne as e:
        vys['varovani'].append('Rozložení se nepodařilo (%s) — zobrazeno jako seznam rungů.' % e)
    return vys


# ---------- M2: úpravy a stavy ----------

def _model(net):
    if net['jazyk'] == 'FBD':
        return {'jazyk': 'FBD', 'prvky': net['prvky'], 'signaly': net['signaly'], 'rozlozeni': net['rozlozeni']}
    return {k: net[k] for k in ('prvky', 'rungy', 'vodice', 'rozlozeni')}


def _fbd_po(po, sirka=0):
    """FBD model z API -> {jazyk, prvky, signaly, rozlozeni}; chybný tvar = 400."""
    try:
        fbd.over_model(po)
    except ValueError as e:
        raise stav.ChybaStavu(str(e), 400)
    m = fbd.model(po['prvky'])
    try:
        m['rozlozeni'] = fbd.rozloz(m['prvky'], sirka)[0]
    except fbd.Nerozlozitelne:
        m['rozlozeni'] = None
    return m


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
    if net['jazyk'] == 'FBD' and net.get('signaly') is not None:
        if not isinstance(body.get('po'), dict):
            raise stav.ChybaStavu('po musí být FBD model {jazyk: "FBD", prvky: [...]}', 400)
        po = _fbd_po(dict(body['po'], jazyk='FBD'))
    elif net['jazyk'] != 'LAD':
        raise stav.ChybaStavu('úprava jen pro LAD nebo FBD network (tento je %s)' % (net['jazyk'] or '?'), 400)
    else:
        over_model(body.get('po'))
        po = {'prvky': body['po']['prvky'], 'rungy': body['po']['rungy']}
        po['vodice'] = s7dcl.vodice(po)
        po['rozlozeni'] = body['po'].get('rozlozeni') or _rozloz(po)[0]
    cas = stav.ted()
    with stav.ZAMEK:
        s = stav.nacti(data, projekt)
        u = {'id': stav.nove_id(data), 'projekt': projekt, 'kolo': str(body.get('kolo') or '—'),
             'blok': net['soubor'], 'druh': net['druh'], 'cislo_bloku': net['cislo'], 'nw': net['nw'],
             'titulek': net['titulek'], 'jazyk': net['jazyk'], 'f': bool(net.get('f') or body.get('f')), 'autor': 'ANet', 'cas': cas, 'stav': 'Proposal',
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


def _kresba_rozdilu(pred, po, sirka=0):
    """Kresba modelu „po" se smazanými prvky jako přízraky a značkami rozdílu proti „pred"."""
    zmeny, smazane = rozdil.porovnej(pred, po)
    if fbd.je_fbd(po):
        return _kresba_rozdilu_fbd(pred, po, zmeny, smazane, sirka)
    prvky, rungy, nevlozene = rozdil.slouc(pred, po, smazane)
    varovani = []
    rozl, kresba = _rozloz({'prvky': prvky, 'rungy': rungy}, sirka)
    if kresba is None:                                # přízraky rozbily rozložení -> jen „po"
        prvky, rungy, nevlozene = po['prvky'], po['rungy'], list(smazane)
        rozl, kresba = _rozloz(po, sirka)
        if kresba is None:
            varovani.append('Model „po" nejde rozložit — zobrazen jako seznam rungů.')
    znacky = dict(zmeny)
    znacky.update({'x:' + i: 'smazany' for i in smazane if i not in nevlozene})
    if nevlozene:
        P = {p['id']: p for p in pred['prvky']}
        varovani.append('Smazáno (bez místa v obraze): ' + ', '.join(
            '%s %s' % (P[i]['typ'], P[i].get('operand') or P[i].get('nazev') or '') for i in nevlozene))
    return {'prvky': prvky, 'rungy': rungy, 'rozlozeni': rozl, 'kresba': kresba, 'rozdil': znacky,
            'varovani': varovani}


def _kresba_rozdilu_fbd(pred, po, zmeny, smazane, sirka):
    """FBD: smazané boxy jako přízraky (samostatné stromy; signály ze zachovaných boxů zůstanou)."""
    varovani, prvky = [], fbd.slouc(pred, po, smazane)
    try:
        rozl, kresba = fbd.rozloz(prvky, sirka)
        znacky = dict(zmeny, **{'x:' + i: 'smazany' for i in smazane})
    except fbd.Nerozlozitelne:
        prvky, znacky = po['prvky'], dict(zmeny)
        try:
            rozl, kresba = fbd.rozloz(prvky, sirka)
        except fbd.Nerozlozitelne as e:
            rozl, kresba = None, None
            varovani.append('Model „po" nejde rozložit (%s).' % e)
        if smazane:
            P = {p['id']: p for p in pred['prvky']}
            varovani.append('Smazáno: ' + ', '.join(fbd.jmeno(P[i]) for i in smazane))
    return {'jazyk': 'FBD', 'prvky': prvky, 'rungy': [], 'signaly': fbd.signaly(prvky), 'rozlozeni': rozl,
            'kresba': kresba, 'rozdil': znacky, 'varovani': varovani,
            'rozdil_piny': fbd.zmeny_pinu(pred, po, {i for i, z in zmeny.items() if z == 'zmeneny'})}


def _ne_f(root, projekt):
    """Jména bloků projektu, které nejsou F (pro pravidlo „v F-bloku jen F")."""
    try:
        return {b['nazev'] for b in api_katalog(root, projekt)['bloky'] if not b.get('f')}
    except KeyError:
        return set()


def _validuj(po, f=False, root=None, projekt=None):
    if fbd.je_fbd(po):
        return fbd.validuj(po, f, _ne_f(root, projekt) if f and root else ())
    return model.validuj(po)


def api_uprava_detail(data, uid, sirka=0, root=None):
    """Úprava + kresba „po" s rozdílem, chyby pravidel LAD, přechody stavů."""
    _, s, u = stav.najdi(data, uid)
    vys = _kresba_rozdilu(u['pred'], u['po'], sirka)
    vys['tia'] = s.get('tia')
    k = u.get('kontroly') or []
    if stav.STAVY.index(u['stav']) >= stav.STAVY.index('Checked') and k and k[-1].get('shoda') is False:
        vys['varovani'].insert(0, 'Poslední kontrola exportem je NESHODA, ale úprava už je %s — stav se sám nevrací, '
                                  'zkontroluj zápis v projektu.' % u['stav'])
    dalsi, predchozi = stav.prechody(u)
    vys.update({'uprava': u, 'dalsi': dalsi, 'predchozi': predchozi, 'checked_povolen': stav.checked_povolen(u),
                'chyby': _validuj(u['po'], u.get('f'), root, u['projekt'])})
    return vys


def api_postup(data, uid, root=None):
    _, _, u = stav.najdi(data, uid)
    if 'f' not in u and root:                         # úpravy z doby před F2 příznak F-bloku nemají -> z exportu
        try:
            u = dict(u, f=_nacti(root, u['projekt'], u['blok'])[1]['atributy'].get('S7_Safety') == 'TRUE')
        except KeyError:
            pass
    return dict(postup.postup(u), uprava=uid)


# ---------- M3: katalog, editor ----------

def api_katalog(root, projekt):
    if projekt not in index_exportu(root):
        raise KeyError('neznámý projekt')
    return katalog.katalog(root, projekt)


def api_rozlozeni(root, body):
    """Editor / zkouška Clauda: {po, pred?, ops?: [op], projekt?, sirka?} -> „po" po operacích, kresba s rozdílem
    proti „pred", místa vložení, chyby pravidel LAD. Nic neukládá."""
    if not isinstance(body, dict):
        raise stav.ChybaStavu('tělo musí být JSON objekt', 400)
    if fbd.je_fbd(body.get('po')):
        return _rozlozeni_fbd(body, root)
    over_model(body.get('po'))
    po = {'prvky': body['po']['prvky'], 'rungy': body['po']['rungy']}
    pred = body.get('pred')
    if pred is not None:
        over_model(pred)
    kat = []

    def sablona(klic):                                # klíč palety ('NO', 'fb:FB_Pas') -> prvek z katalogu
        if not kat:
            kat.append(api_katalog(root, str(body.get('projekt', ''))))
        try:
            return katalog.prvek(kat[0], klic)
        except KeyError as e:
            raise model.ChybaOperace(str(e).strip("'"))
    ops = body.get('ops') or []
    if not isinstance(ops, list):
        raise stav.ChybaStavu('ops musí být seznam operací', 400)
    posledni = None
    for op in ops:
        po, posledni = model.proved(po, op, sablona)
        over_model(po)
    vys = _kresba_rozdilu(pred or po, po, int(body.get('sirka') or 0))
    vys.update({'po': po, 'mista': model.mista(po, vys['kresba']), 'chyby': model.validuj(po), 'posledni': posledni})
    if vys['kresba'] is None:
        vys['chyby'].append({'id': None, 'chyba': 'Zapojení nejde rozložit (není sériově-paralelní).'})
    return vys


def _rozlozeni_fbd(body, root=None):
    """FBD: operace (fbd.proved; klíče palety FBD i katalogu), kresba s rozdílem proti pred, místa vložení, pravidla."""
    po = _fbd_po(body['po'])
    pred = _fbd_po(dict(body['pred'], jazyk='FBD')) if isinstance(body.get('pred'), dict) else None
    ops = body.get('ops') or []
    if not isinstance(ops, list):
        raise stav.ChybaStavu('ops musí být seznam operací', 400)
    posledni, kat, f = None, [], bool(body.get('f'))

    def z_katalogu(klic):
        if not kat:
            kat.append(api_katalog(root, str(body.get('projekt', ''))))
        return katalog.prvek(kat[0], klic)
    for op in ops:
        try:
            po, posledni = fbd.proved(po, op, z_katalogu, f)
        except fbd.ChybaOperace as e:
            raise stav.ChybaStavu(str(e), 400)
    vys = _kresba_rozdilu(pred or po, po, int(body.get('sirka') or 0))
    if vys['kresba'] is not None:
        po['rozlozeni'] = fbd.rozloz(po['prvky'])[0]
    vys.update({'po': po, 'mista': fbd.mista(po, vys['kresba']), 'posledni': posledni,
                'chyby': _validuj(po, f, root, str(body.get('projekt', '')))})
    return vys


def api_kontrola_exportem(root, data, uid, body):
    """M4: porovná network úpravy v exportu (nejnovějším / zadaném) s modelem „po" podle obsahu
    a zapíše výsledek jako kontrolu (POST /api/kontrola). Network se hledá podle titulku, pak podle čísla."""
    if not isinstance(body, dict):
        raise stav.ChybaStavu('tělo musí být JSON objekt', 400)
    _, _, u = stav.najdi(data, uid)
    t, b = _nacti(root, u['projekt'], u['blok'], str(body['export_ts']) if body.get('export_ts') else None)
    pozn = []
    kand = [n for n in b['networky'] if u.get('titulek') and n['titulek'] == u['titulek']]
    if kand:
        n = next((x for x in kand if x['nw'] == u['nw']), kand[0])
        if n['nw'] != u['nw']:
            pozn.append('network se posunul: NW %d → NW %d (podle titulku „%s")' % (u['nw'], n['nw'], u['titulek']))
        if len(kand) > 1:
            pozn.append('titulek je v bloku %d×, porovnán NW %d' % (len(kand), n['nw']))
    else:
        n = b['networky'][u['nw'] - 1] if u['nw'] <= len(b['networky']) else None
        if u.get('titulek'):
            pozn.append('titulek „%s" v exportu není — porovnáno podle čísla NW %d' % (u['titulek'], u['nw']))
    if n is None:
        shoda, rozd = False, ['network %d v exportu není' % u['nw']]
    elif fbd.je_fbd(u['po']):
        if n['jazyk'] != 'FBD' or 'prvky' not in n:
            shoda, rozd = False, ['NW %d je v exportu %s' % (n['nw'], 'v jazyce %s' % n['jazyk'] if n['jazyk'] != 'FBD'
                                                              else 'nečitelný (%s)' % n.get('chyba'))]
        else:
            shoda, rozd = fbd.shoda_obsahu(u['po'], n)
    elif n['jazyk'] != 'LAD':
        shoda, rozd = False, ['NW %d je v exportu v jazyce %s' % (n['nw'], n['jazyk'] or '?')]
    else:
        shoda, rozd = rozdil.shoda_obsahu(u['po'], n)
    if t == u['pred'].get('export_ts'):
        pozn.append('pozor: export je tentýž jako před úpravou')
    poznamka = ('export = návrh' if shoda else 'NESHODA: ' + '; '.join(rozd)) + ''.join(' · ' + x for x in pozn)
    u = api_kontrola(data, {'uprava_id': uid, 'shoda': shoda, 'export_ts': t, 'poznamka': poznamka})
    return {'shoda': shoda, 'export_ts': t, 'nw_export': n['nw'] if n else None, 'poznamka': poznamka,
            'rozdily': rozd, 'uprava': u}


def api_uprava_po(data, uid, body):
    """Uloží model „po" k úpravě (editor „Uložit návrh"). Jen ve stavu Proposal; chyby pravidel uložení nebrání."""
    if not isinstance(body, dict):
        raise stav.ChybaStavu('tělo musí být JSON objekt', 400)
    if fbd.je_fbd(body.get('po')):
        po = _fbd_po(body['po'])
    else:
        over_model(body.get('po'))
        po = {'prvky': body['po']['prvky'], 'rungy': body['po']['rungy']}
        po['vodice'] = s7dcl.vodice(po)
        po['rozlozeni'] = _rozloz(po)[0]
    with stav.ZAMEK:
        projekt, s, u = stav.najdi(data, uid)
        if fbd.je_fbd(po) != fbd.je_fbd(u['po']):
            raise stav.ChybaStavu('jazyk návrhu (LAD/FBD) musí zůstat stejný', 400)
        if u['stav'] != 'Proposal':
            raise stav.ChybaStavu('návrh lze měnit jen ve stavu Proposal (teď %s)' % u['stav'])
        u['po'] = po
        u['upraveno'] = stav.ted()
        stav.uloz(data, projekt, s)
    return {'uprava': u, 'chyby': _validuj(po, u.get('f'))}


# ---------- M5: Upload (TIAC_Writer) a Check (TIAC_Reader) ----------

ZAMEK_TIA = threading.Lock()          # jedna instance TIA = v jednu chvíli jeden Upload / Check
CHYBA_SPOJENI = 'TIA zaneprázdněné nebo Writer/Reader nejde spustit: '


def _tia(s, projekt):
    t = s.get('tia') or {}
    if not t.get('softwarePath'):
        raise stav.ChybaStavu('projekt %s nemá nastavení TIA (tia: {softwarePath, plcName, groupPath?}) — '
                              'nastav ho v UI u úpravy („Nastavení TIA") nebo přes PUT /api/stav' % projekt)
    return t


def _klient(mcp, role, timeout):
    cfg = (mcp or {}).get(role)
    if not cfg:
        raise mcp_klient.ChybaMCP('%s není nastaven (--mcp-json / --%s-cmd)' % (role, role))
    return mcp_klient.Klient(cfg[0], cfg[1], timeout, 'TIAC_Writer' if role == 'writer' else 'TIAC_Reader')


def _bez_nazvu_volani(m):
    """Pro kontrolu serializace: volání FB/FC porovnat podle instance (parser bez exportu název FB nezná)."""
    m = json.loads(json.dumps(m))
    for p in m['prvky']:
        if p.get('volani') == 'fc' and not p.get('instance'):
            p['instance'] = '"%s"' % p.get('nazev')
        if p.get('volani') in ('fb', 'fc'):
            p['nazev'] = None
            p['volani'] = 'fb'
    return m


def _nahrad_v_souboru(soubor, u):
    """Nahradí network úpravy (podle titulku, jinak čísla) v exportovaném .s7dcl modelem „po" -> popis.
    Pojistky: F-blok ani jiný jazyk networku se nepřepíše; text po náhradě se znovu přečte a musí odpovídat návrhu."""
    with open(soubor, 'rb') as f:
        surove = f.read()
    bom = surove.startswith(b'\xef\xbb\xbf')
    text = surove.decode('utf-8-sig')
    b = s7dcl.nacti_blok(soubor)
    if b['atributy'].get('S7_Safety') == 'TRUE':
        raise s7dcl_zapis.ChybaZapisu('blok je F-blok (S7_Safety) — Upload je jen pro ne-safety, zapisuje Honza')
    kand = [n for n in b['networky'] if u.get('titulek') and n['titulek'] == u['titulek']]
    n = next((x for x in kand if x['nw'] == u['nw']), kand[0]) if kand else \
        (b['networky'][u['nw'] - 1] if u['nw'] <= len(b['networky']) else None)
    if n is None:
        raise s7dcl_zapis.ChybaZapisu('network %d („%s") v exportovaném bloku není' % (u['nw'], u.get('titulek') or ''))
    jazyk = 'FBD' if fbd.je_fbd(u['po']) else 'LAD'
    if n['jazyk'] != jazyk:
        raise s7dcl_zapis.ChybaZapisu('NW %d je v projektu %s, návrh je %s' % (n['nw'], n['jazyk'] or '?', jazyk))
    novy = s7dcl_zapis.nahrad_network(text, n['nw'], s7dcl_zapis.text_networku(u['po']))
    zpet = s7dcl.parse_blok(novy.replace('\r\n', '\n'))['networky'][n['nw'] - 1]
    if jazyk == 'FBD':
        shoda, rozd = fbd.shoda_obsahu(_bez_nazvu_volani(u['po']), _bez_nazvu_volani(zpet))
    else:
        shoda, rozd = rozdil.shoda_obsahu(_bez_nazvu_volani(u['po']), _bez_nazvu_volani(zpet))
    if not shoda:
        raise s7dcl_zapis.ChybaZapisu('serializace návrhu nesedí s návrhem: ' + '; '.join(rozd))
    with open(soubor, 'wb') as f:
        f.write((b'\xef\xbb\xbf' if bom else b'') + novy.encode('utf-8'))
    return 'NW %d „%s" nahrazen modelem „po"%s' % (n['nw'], n['titulek'], '' if n['nw'] == u['nw'] else
                                                    ' (v návrhu NW %d — network se posunul)' % u['nw'])


def _cesta_z_textu(t):
    """Odpověď ResolveObjectPath -> 'Skupina/Blok' (JSON s path/objectPath… nebo prostý text)."""
    try:
        j = json.loads(t)
        if isinstance(j, dict):
            t = next((str(j[k]) for k in ('path', 'objectPath', 'resolvedPath', 'rootRelativePath') if j.get(k)), t)
        elif isinstance(j, str):
            t = j
    except ValueError:
        pass
    return t.strip().strip('"').replace('\\', '/')


def _zapis(data, uid, zapis, novy_stav, kdo):
    """Uloží záznam Upload/Check do u['zapisy'] a při úspěchu změní stav (když ho mezitím nikdo nezměnil)."""
    with stav.ZAMEK:
        projekt, s, u = stav.najdi(data, uid)
        u.setdefault('zapisy', []).append(zapis)
        if novy_stav:
            try:
                stav.zmen_stav(u, novy_stav, kdo)
            except stav.ChybaStavu as e:
                zapis['zprava'] += ' · stav nezměněn: %s' % e
        stav.uloz(data, projekt, s)
    return u


def _f_uprava(root, u):
    if 'f' in u:
        return bool(u['f'])
    try:                                              # úpravy před F2: příznak z exportu; neznámý = bereme jako F
        return _nacti(root, u['projekt'], u['blok'])[1]['atributy'].get('S7_Safety') == 'TRUE'
    except KeyError:
        return True


def api_upload(root, data, uid, mcp, timeout=300):
    """Approved ne-safety úprava -> TIAC_Writer: Connect, ResolveObjectPath, export bloku jako dokumenty,
    náhrada networku, PreviewImport, ImportFromDocuments Override -> Signed (kdo TIAC_Writer)."""
    _, s, u = stav.najdi(data, uid)
    if u['stav'] != 'Approved':
        raise stav.ChybaStavu('Upload jen ve stavu Approved (teď %s)' % u['stav'])
    if _f_uprava(root, u):
        raise stav.ChybaStavu('F-blok: Upload není — zapisuje Honza podle Postupu pro TIA')
    tia = _tia(s, u['projekt'])
    if not ZAMEK_TIA.acquire(blocking=False):
        raise stav.ChybaStavu('právě běží jiný Upload / Check — počkej na jeho konec')
    kroky, prac = [], tempfile.mkdtemp(prefix='tiac_upload_')

    def krok(nazev, fce):
        try:
            v = fce()
        except Exception as e:
            kroky.append({'krok': nazev, 'ok': False, 'zprava': str(e)[:1000]})
            raise
        kroky.append({'krok': nazev, 'ok': True, 'zprava': str(v)[:1000]})
        return v
    try:
        sp, blok = tia['softwarePath'], u['blok']
        try:
            with _klient(mcp, 'writer', timeout) as k:
                krok('Connect', lambda: k.zavolej('Connect', {}))
                grp = tia.get('groupPath')
                if not grp:
                    cesta = _cesta_z_textu(krok('ResolveObjectPath', lambda: k.zavolej(
                        'ResolveObjectPath', {'softwarePath': sp, 'name': blok, 'kind': 'block'})))
                    grp = cesta.rsplit('/', 1)[0] if '/' in cesta else ''
                krok('ExportBlocksAsDocuments', lambda: k.zavolej('ExportBlocksAsDocuments', {
                    'softwarePath': sp, 'exportPath': prac, 'regexName': '^%s$' % re.escape(blok), 'preservePath': False}))
                soubory = glob.glob(os.path.join(prac, '**', blok + '.s7dcl'), recursive=True)
                if not soubory:
                    raise s7dcl_zapis.ChybaZapisu('Writer nevyexportoval %s.s7dcl' % blok)
                slozka = os.path.dirname(soubory[0])
                krok('Náhrada networku v .s7dcl', lambda: _nahrad_v_souboru(soubory[0], u))
                nahled = krok('PreviewImport', lambda: k.zavolej('PreviewImport', {
                    'softwarePath': sp, 'importPath': slozka, 'kind': 'block', 'groupPath': grp}))
                krok('ImportFromDocuments', lambda: k.zavolej('ImportFromDocuments', {
                    'softwarePath': sp, 'groupPath': grp, 'importPath': slozka, 'fileNameWithoutExtension': blok,
                    'importOption': 'Override'}))
            ok, zprava = True, 'zapsáno do projektu (%s, skupina %s) — projekt v TIA ulož (SaveProject Writer nevolá)'                 % (sp, grp or '/')
        except mcp_klient.ChybaMCP as e:
            ok, zprava, nahled = False, CHYBA_SPOJENI + str(e), None
        except (s7dcl_zapis.ChybaZapisu, s7dcl.ChybaParseru, OSError, ValueError) as e:
            ok, zprava, nahled = False, 'Upload zastaven: %s' % e, None
        zapis = {'cas': stav.ted(), 'nastroj': 'TIAC_Writer', 'kroky': kroky, 'ok': ok, 'zprava': zprava}
        if nahled is not None:
            zapis['preview'] = nahled[:4000]
        u = _zapis(data, uid, zapis, 'Signed' if ok else None, 'TIAC_Writer')
    finally:
        shutil.rmtree(prac, ignore_errors=True)
        ZAMEK_TIA.release()
    return {'ok': ok, 'zprava': zapis['zprava'], 'zapis': zapis, 'uprava': u}


def _json_nebo_nic(t):
    try:
        j = json.loads(t)
        return j if isinstance(j, dict) else {}
    except ValueError:
        return {}


def api_check(root, data, uid, mcp, timeout=300):
    """Signed úprava -> TIAC_Reader export_to_folder (blok do nové složky export rootu) -> kontrola exportem
    -> při shodě Checked (kdo TIAC_Reader); při neshodě zůstane Signed a kontrola se zapíše jako neshoda."""
    projekt, s, u = stav.najdi(data, uid)
    if u['stav'] != 'Signed':
        raise stav.ChybaStavu('Check jen ve stavu Signed (teď %s)' % u['stav'])
    tia = _tia(s, projekt)
    try:                                              # PLC a skupina z dosavadního exportu bloku
        t0, cesta, _ = najdi_blok(root, projekt, u['blok'])
        casti = os.path.relpath(cesta, os.path.join(root, projekt, t0))[:-len('.s7dcl')].replace('\\', '/').split('/')
    except KeyError:
        casti = [tia.get('plcName') or tia['softwarePath'], 'Blocks', u['blok']]
    if tia.get('groupPath'):
        casti = casti[:2] + [x for x in tia['groupPath'].split('/') if x] + [u['blok']]
    if not ZAMEK_TIA.acquire(blocking=False):
        raise stav.ChybaStavu('právě běží jiný Upload / Check — počkej na jeho konec')
    kroky, k_vys = [], None
    try:
        pred = set(index_exportu(root).get(projekt, {}))
        try:
            with _klient(mcp, 'reader', timeout) as k:
                arg = {'pathPrefixes': ['/'.join(casti)], 'include': ['blocks'], 'formats': ['documents'],
                       'plcName': tia.get('plcName') or casti[0]}
                texty = []
                for _ in range(50):                   # complete:false -> pokračovat runId + nextOffset
                    t = k.zavolej('export_to_folder', arg)
                    texty.append(t)
                    j = _json_nebo_nic(t)
                    kroky.append({'krok': 'export_to_folder', 'ok': True, 'zprava': t[:1000]})
                    if j.get('complete', True) is not False:
                        break
                    arg.update(runId=j.get('runId'), nextOffset=j.get('nextOffset'))
            nove = set(index_exportu(root).get(projekt, {})) - pred
            ts = [x for x in re.findall(r'\d{8}-\d{6}', ' '.join(texty)) if x in nove] or sorted(nove)
            if not ts:
                raise s7dcl_zapis.ChybaZapisu('Reader export v export rootu editoru nenašel (%s\\%s) — odpověď: %s'
                                              % (root, projekt, texty[-1][:300] if texty else ''))
            k_vys = api_kontrola_exportem(root, data, uid, {'export_ts': ts[-1]})
            kroky.append({'krok': 'Kontrola exportem %s' % ts[-1], 'ok': k_vys['shoda'], 'zprava': k_vys['poznamka']})
            ok, zprava = k_vys['shoda'], ('export = návrh' if k_vys['shoda'] else k_vys['poznamka'])
        except mcp_klient.ChybaMCP as e:
            kroky.append({'krok': 'export_to_folder', 'ok': False, 'zprava': str(e)[:1000]})
            ok, zprava = False, CHYBA_SPOJENI + str(e)
        except (s7dcl_zapis.ChybaZapisu, s7dcl.ChybaParseru, KeyError, OSError) as e:
            ok, zprava = False, 'Check zastaven: %s' % str(e).strip("'")
        zapis = {'cas': stav.ted(), 'nastroj': 'TIAC_Reader', 'kroky': kroky, 'ok': ok, 'zprava': zprava}
        u = _zapis(data, uid, zapis, 'Checked' if ok else None, 'TIAC_Reader')
    finally:
        ZAMEK_TIA.release()
    return {'ok': ok, 'shoda': bool(k_vys and k_vys['shoda']), 'export_ts': k_vys and k_vys['export_ts'],
            'zprava': zapis['zprava'], 'zapis': zapis, 'uprava': u}


class Handler(BaseHTTPRequestHandler):
    root = ''
    data = ''
    mcp = None                       # {'writer': (příkaz, env) | None, 'reader': …}
    mcp_timeout = 300

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
                self._json(api_uprava_detail(self.data, c.split('/')[3], int(q.get('sirka', '0')), self.root))
            elif metoda == 'GET' and re.fullmatch(r'/api/uprava/[\w-]+/postup', c):
                self._json(api_postup(self.data, c.split('/')[3], self.root))
            elif metoda == 'GET' and c == '/api/katalog':
                self._json(api_katalog(self.root, q.get('projekt', '')))
            elif metoda == 'POST' and c == '/api/rozlozeni':
                self._json(api_rozlozeni(self.root, self._telo()))
            elif metoda in ('POST', 'PUT') and re.fullmatch(r'/api/uprava/[\w-]+/po', c):
                self._json(api_uprava_po(self.data, c.split('/')[3], self._telo()))
            elif metoda == 'POST' and re.fullmatch(r'/api/uprava/[\w-]+/kontrola-exportem', c):
                self._json(api_kontrola_exportem(self.root, self.data, c.split('/')[3], self._telo()))
            elif metoda == 'POST' and re.fullmatch(r'/api/uprava/[\w-]+/upload', c):
                self._json(api_upload(self.root, self.data, c.split('/')[3], self.mcp, self.mcp_timeout))
            elif metoda == 'POST' and re.fullmatch(r'/api/uprava/[\w-]+/check', c):
                self._json(api_check(self.root, self.data, c.split('/')[3], self.mcp, self.mcp_timeout))
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


def vytvor_server(port, root, data, host='127.0.0.1', mcp=None, mcp_timeout=300):
    h = type('H', (Handler,), {'root': root, 'data': data, 'mcp': mcp, 'mcp_timeout': mcp_timeout})
    return ThreadingHTTPServer((host, port), h)


def mcp_konfigurace(mcp_json, writer_cmd=None, reader_cmd=None):
    """Příkazy Writeru / Readeru: z .mcp.json (klíče TIAC_Writer, tia-portal), přebité --writer-cmd / --reader-cmd."""
    return {'writer': (mcp_klient.prikaz_z_textu(writer_cmd), None) if writer_cmd
            else mcp_klient.z_mcp_json(mcp_json, 'TIAC_Writer'),
            'reader': (mcp_klient.prikaz_z_textu(reader_cmd), None) if reader_cmd
            else mcp_klient.z_mcp_json(mcp_json, 'tia-portal')}


def main():
    ap = argparse.ArgumentParser(description='TIAC_Editor server')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--export-root', required=True)
    ap.add_argument('--data', required=True, help='složka dat úprav: <data>\\<projekt>\\stav.json')
    ap.add_argument('--mcp-json', default=r'.mcp.json',
                    help='odkud vzít příkazy TIAC_Writer a tia-portal (Reader); soubor se jen čte')
    ap.add_argument('--writer-cmd', help='příkaz Writeru místo .mcp.json (např. "python tests\\falesny_mcp.py writer")')
    ap.add_argument('--reader-cmd', help='příkaz Readeru místo .mcp.json')
    ap.add_argument('--mcp-timeout', type=int, default=300, help='timeout jednoho volání nástroje (s)')
    a = ap.parse_args()
    srv = vytvor_server(a.port, a.export_root, a.data, mcp=mcp_konfigurace(a.mcp_json, a.writer_cmd, a.reader_cmd),
                        mcp_timeout=a.mcp_timeout)
    print('TIAC_Editor na http://localhost:%d  (export root %s, data %s)' % (a.port, a.export_root, a.data), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    sys.exit(main())
