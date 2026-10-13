"""Falešný TIAC_Writer / TIAC_Reader (MCP přes stdio) pro testy a ukázku M5 — s TIA Portalem nemluví.

python tests\\falesny_mcp.py writer [--stav DIR] [--vzorek DIR] [--vice-instanci] [--pomalu S]
python tests\\falesny_mcp.py reader --export-root DIR --projekt Demo_Linka [--stav DIR] [--vzorek DIR]

„Projekt TIA" = složka --stav (výchozí %TEMP%\\tiac_falesny_tia): blok tam je po prvním importu; dokud není,
čte se z --vzorek (výchozí vzorky Demo_Linka 20261012-120000\\PLC_1\\Blocks).
Writer: Connect, ResolveObjectPath, ExportBlocksAsDocuments (kopie .s7dcl/.s7res do exportPath), PreviewImport,
ImportFromDocuments (zkopíruje dokumenty do --stav a připíše řádek do <stav>\\importy.jsonl).
Reader: export_to_folder — nová složka <export-root>\\<projekt>\\<RRRRMMDD-HHMMSS>\\<PLC>\\Blocks\\… s blokem
z --stav (nebo ze vzorku), odpověď JSON {exportFolder, complete, manifest}.
"""
import argparse
import glob
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta

KOREN = os.path.dirname(os.path.abspath(__file__))
VZOREK = os.path.join(KOREN, 'samples', 'Demo_Linka', '20261012-120000', 'PLC_1', 'Blocks')


def najdi(a, blok):
    """-> (složka s <blok>.s7dcl, skupina relativně k Blocks)"""
    for zdroj in (a.state, a.vzorek):
        x = glob.glob(os.path.join(zdroj, '**', blok + '.s7dcl'), recursive=True)
        if x:
            d = os.path.dirname(x[0])
            return d, os.path.relpath(d, zdroj).replace('\\', '/').strip('.')
    raise ValueError('blok %s v projektu není' % blok)


def kopiruj(z, do, blok):
    os.makedirs(do, exist_ok=True)
    for p in ('.s7dcl', '.s7res'):
        if os.path.exists(os.path.join(z, blok + p)):
            shutil.copyfile(os.path.join(z, blok + p), os.path.join(do, blok + p))


def writer(a, jm, arg):
    if jm == 'Connect':
        if a.vice_instanci:
            raise ValueError('More than one TIA Portal instance is running — pass portalId')
        return 'Connected to TIA Portal V21 (falešný)'
    if 'softwarePath' not in arg:
        raise ValueError('softwarePath is required')
    if jm == 'ResolveObjectPath':
        _, skup = najdi(a, arg['name'])
        return json.dumps({'path': (skup + '/' if skup else '') + arg['name']})
    if jm == 'ExportBlocksAsDocuments':
        blok = arg['regexName'].strip('^$')
        kopiruj(najdi(a, blok)[0], arg['exportPath'], blok)
        return 'Exported 1 block(s) to %s' % arg['exportPath']
    if jm == 'PreviewImport':
        bloky = [os.path.basename(x)[:-6] for x in glob.glob(os.path.join(arg['importPath'], '*.s7dcl'))]
        return json.dumps({'groupPath': arg['groupPath'], 'overwrite': bloky})
    if jm == 'ImportFromDocuments':
        blok = arg['fileNameWithoutExtension']
        cil = os.path.join(a.state, *[x for x in arg['groupPath'].split('/') if x])
        kopiruj(arg['importPath'], cil, blok)
        with open(os.path.join(a.state, 'importy.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(dict(arg, soubor=os.path.join(cil, blok + '.s7dcl')), ensure_ascii=False) + '\n')
        return 'Imported %s (%s)' % (blok, arg.get('importOption'))
    raise ValueError('nástroj %s falešný Writer nezná' % jm)


def reader(a, jm, arg):
    if jm != 'export_to_folder':
        raise ValueError('nástroj %s falešný Reader nezná' % jm)
    prefix = arg['pathPrefixes'][0].strip('/')
    casti = prefix.split('/')                            # <PLC>/Blocks/<Skupina…>/<blok>
    plc, blok = casti[0], casti[-1]
    z, skup = najdi(a, blok)
    t = datetime.now()
    while True:                                          # jedinečné časové razítko složky
        ts = t.strftime('%Y%m%d-%H%M%S')
        slozka = os.path.join(a.export_root, a.projekt, ts)
        if not os.path.exists(slozka):
            break
        t += timedelta(seconds=1)
    kopiruj(z, os.path.join(slozka, plc, 'Blocks', *[x for x in skup.split('/') if x]), blok)
    with open(os.path.join(slozka, 'manifest.jsonl'), 'w', encoding='utf-8') as f:
        f.write(json.dumps({'path': prefix, 'format': 'documents'}) + '\n')
    return json.dumps({'exportFolder': slozka, 'complete': True, 'manifest': os.path.join(slozka, 'manifest.jsonl'),
                       'exported': 1})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('role', choices=['writer', 'reader'])
    ap.add_argument('--state', default=os.path.join(tempfile.gettempdir(), 'tiac_falesny_tia'))
    ap.add_argument('--vzorek', default=VZOREK)
    ap.add_argument('--export-root')
    ap.add_argument('--projekt', default='Demo_Linka')
    ap.add_argument('--vice-instanci', action='store_true')
    ap.add_argument('--pomalu', type=float, default=0)
    a = ap.parse_args()
    os.makedirs(a.state, exist_ok=True)
    fce = writer if a.role == 'writer' else reader
    for radek in sys.stdin.buffer:
        if not radek.strip():
            continue
        z = json.loads(radek.decode('utf-8'))
        if 'id' not in z:
            continue                                     # notifikace
        if z['method'] == 'initialize':
            v = {'protocolVersion': z['params'].get('protocolVersion'), 'capabilities': {'tools': {}},
                 'serverInfo': {'name': 'falesny-' + a.role, 'version': '0'}}
        elif z['method'] == 'tools/call':
            time.sleep(a.pomalu)
            try:
                v = {'content': [{'type': 'text', 'text': fce(a, z['params']['name'], z['params'].get('arguments') or {})}]}
            except Exception as e:                       # chyba nástroje = isError, ne pád serveru
                v = {'content': [{'type': 'text', 'text': str(e)}], 'isError': True}
        else:
            sys.stdout.buffer.write((json.dumps({'jsonrpc': '2.0', 'id': z['id'],
                                                 'error': {'code': -32601, 'message': 'neznámá metoda'}}) + '\n').encode())
            sys.stdout.buffer.flush()
            continue
        sys.stdout.buffer.write((json.dumps({'jsonrpc': '2.0', 'id': z['id'], 'result': v}, ensure_ascii=False) + '\n')
                                .encode('utf-8'))
        sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
