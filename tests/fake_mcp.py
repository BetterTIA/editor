"""Falešný TIAC_Writer / TIAC_Reader (MCP přes stdio) pro testy a ukázku M5 — s TIA Portalem nemluví.

python tests\\falesny_mcp.py writer [--stav DIR] [--vzorek DIR] [--vice-instanci] [--pomalu S]
python tests\\falesny_mcp.py reader --export-root DIR --projekt Demo_Line [--stav DIR] [--vzorek DIR]

„Projekt TIA" = složka --stav (výchozí %TEMP%\\tiac_falesny_tia): blok tam je po prvním importu; dokud není,
čte se z --vzorek (výchozí vzorky Demo_Line 20261012-120000\\PLC_1\\Blocks).
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

ROOT = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(ROOT, 'samples', 'Demo_Line', '20261012-120000', 'PLC_1', 'Blocks')


def find(a, block):
    """-> (složka s <blok>.s7dcl, skupina relativně k Blocks)"""
    for source in (a.state, a.sample):
        x = glob.glob(os.path.join(source, '**', block + '.s7dcl'), recursive=True)
        if x:
            d = os.path.dirname(x[0])
            return d, os.path.relpath(d, source).replace('\\', '/').strip('.')
    raise ValueError('block %s v project není' % block)


def copy_tree(z, dst, block):
    os.makedirs(dst, exist_ok=True)
    for p in ('.s7dcl', '.s7res'):
        if os.path.exists(os.path.join(z, block + p)):
            shutil.copyfile(os.path.join(z, block + p), os.path.join(dst, block + p))


def writer(a, nm, arg):
    if nm == 'Connect':
        if a.multi_instances:
            raise ValueError('More than one TIA Portal instance is running — pass portalId')
        return 'Connected to TIA Portal V21 (falešný)'
    if 'softwarePath' not in arg:
        raise ValueError('softwarePath is required')
    if nm == 'ResolveObjectPath':
        _, group = find(a, arg['name'])
        return json.dumps({'path': (group + '/' if group else '') + arg['name']})
    if nm == 'ExportBlocksAsDocuments':
        block = arg['regexName'].strip('^$')
        copy_tree(find(a, block)[0], arg['exportPath'], block)
        return 'Exported 1 block(s) to %s' % arg['exportPath']
    if nm == 'PreviewImport':
        blocks = [os.path.basename(x)[:-6] for x in glob.glob(os.path.join(arg['importPath'], '*.s7dcl'))]
        return json.dumps({'groupPath': arg['groupPath'], 'overwrite': blocks})
    if nm == 'ImportFromDocuments':
        block = arg['fileNameWithoutExtension']
        target = os.path.join(a.state, *[x for x in arg['groupPath'].split('/') if x])
        copy_tree(arg['importPath'], target, block)
        with open(os.path.join(a.state, 'imports.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(dict(arg, file=os.path.join(target, block + '.s7dcl')), ensure_ascii=False) + '\n')
        return 'Imported %s (%s)' % (block, arg.get('importOption'))
    raise ValueError('nástroj %s falešný Writer nezná' % nm)


def reader(a, nm, arg):
    if nm != 'export_to_folder':
        raise ValueError('nástroj %s falešný Reader nezná' % nm)
    prefix = arg['pathPrefixes'][0].strip('/')
    parts = prefix.split('/')                            # <PLC>/Blocks/<Skupina…>/<blok>
    plc, block = parts[0], parts[-1]
    z, group = find(a, block)
    t = datetime.now()
    while True:                                          # jedinečné časové razítko složky
        ts = t.strftime('%Y%m%d-%H%M%S')
        folder = os.path.join(a.export_root, a.project, ts)
        if not os.path.exists(folder):
            break
        t += timedelta(seconds=1)
    copy_tree(z, os.path.join(folder, plc, 'Blocks', *[x for x in group.split('/') if x]), block)
    with open(os.path.join(folder, 'manifest.jsonl'), 'w', encoding='utf-8') as f:
        f.write(json.dumps({'path': prefix, 'format': 'documents'}) + '\n')
    return json.dumps({'exportFolder': folder, 'complete': True, 'manifest': os.path.join(folder, 'manifest.jsonl'),
                       'exported': 1})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('role', choices=['writer', 'reader'])
    ap.add_argument('--state', default=os.path.join(tempfile.gettempdir(), 'tiac_fake_tia'))
    ap.add_argument('--sample', default=SAMPLE)
    ap.add_argument('--export-root')
    ap.add_argument('--project', default='Demo_Line')
    ap.add_argument('--multi-instances', action='store_true')
    ap.add_argument('--slow', type=float, default=0)
    a = ap.parse_args()
    os.makedirs(a.state, exist_ok=True)
    fn = writer if a.role == 'writer' else reader
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        z = json.loads(line.decode('utf-8'))
        if 'id' not in z:
            continue                                     # notifikace
        if z['method'] == 'initialize':
            v = {'protocolVersion': z['params'].get('protocolVersion'), 'capabilities': {'tools': {}},
                 'serverInfo': {'name': 'fake-' + a.role, 'version': '0'}}
        elif z['method'] == 'tools/call':
            time.sleep(a.slow)
            try:
                v = {'content': [{'type': 'text', 'text': fn(a, z['params']['name'], z['params'].get('arguments') or {})}]}
            except Exception as e:                       # chyba nástroje = isError, ne pád serveru
                v = {'content': [{'type': 'text', 'text': str(e)}], 'isError': True}
        else:
            sys.stdout.buffer.write((json.dumps({'jsonrpc': '2.0', 'id': z['id'],
                                                 'error': {'code': -32601, 'message': 'neznámá method'}}) + '\n').encode())
            sys.stdout.buffer.flush()
            continue
        sys.stdout.buffer.write((json.dumps({'jsonrpc': '2.0', 'id': z['id'], 'result': v}, ensure_ascii=False) + '\n')
                                .encode('utf-8'))
        sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
