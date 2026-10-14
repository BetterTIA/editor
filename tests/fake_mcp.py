"""Fake TIAC_Writer / TIAC_Reader (MCP over stdio) for tests and demos — it never talks to TIA Portal.

python tests\\fake_mcp.py writer [--state DIR] [--sample DIR] [--multi-instances | --no-portal] [--slow S]
python tests\\fake_mcp.py reader --export-root DIR --project Demo_Line [--state DIR] [--sample DIR] [--no-portal]

"TIA project" = the --state folder (default %TEMP%\\tiac_fake_tia): a block appears there after its first import;
until then it is read from --sample (default: the Demo_Line sample 20261012-120000\\PLC_1\\Blocks).
Writer: Connect, ResolveObjectPath, ExportBlocksAsDocuments (copies .s7dcl/.s7res to exportPath), PreviewImport,
ImportFromDocuments (copies the documents to --state and appends a line to <state>\\imports.jsonl), SetNetworkText
(title / comment of network n in --state: MLC ids in .s7dcl, en-US texts in .s7res).
Writer, TIA side: GetPortals (default one instance with Demo_Line open, --no-portal = TIA is not running,
--multi-instances = two), Connect, GetProject, GetDevices, GetProjectTree, GetSoftwareTree, GetState, OpenTiaProject,
CloseProject. Every tool call is appended to <state>\\calls.jsonl (tests check that Connect was not called).
Reader: get_project_status; export_hmi_to_folder = the HMI sample (HMI_1, screens changed by the Writer taken from
<state>\\HMI_1) in chunks of 3 files into <export-root>\\<project>\\<runId>\\HMI_1 + manifest-hmi.jsonl (--no-hmi = no
Unified HMI in the project); export_to_folder without pathPrefixes = the whole PLC from the sample in 2 chunks
(runId + offset=nextOffset). With pathPrefixes — a new folder <export-root>\\<project>\\<YYYYMMDD-HHMMSS>\\<PLC>\\Blocks\\… with the block
from --state (or from the sample), answers JSON {exportFolder, complete, manifest}.
"""
import argparse
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(ROOT, 'samples', 'Demo_Line', '20261012-120000', 'PLC_1', 'Blocks')
SAMPLE_HMI = os.path.join(ROOT, 'samples', 'Demo_Line', '20261012-120000', 'HMI_1')


def find(a, block):
    """-> (folder with <block>.s7dcl, group relative to Blocks)"""
    for source in (a.state, a.sample):
        x = glob.glob(os.path.join(source, '**', block + '.s7dcl'), recursive=True)
        if x:
            d = os.path.dirname(x[0])
            return d, os.path.relpath(d, source).replace('\\', '/').strip('.')
    raise ValueError('block %s is not in the project' % block)


def copy_tree(z, dst, block):
    os.makedirs(dst, exist_ok=True)
    for p in ('.s7dcl', '.s7res'):
        if os.path.exists(os.path.join(z, block + p)):
            shutil.copyfile(os.path.join(z, block + p), os.path.join(dst, block + p))


PROJECT_PATH = 'C:\\TIA\\Demo_Line\\Demo_Line.ap21'
DEVICES = [{'name': 'PLC_1', 'type': 'CPU 1516F-3 PN/DP', 'kind': 'PlcSoftware', 'softwarePath': 'PLC_1'},
           {'name': 'HMI_1', 'type': 'MTP1200 Unified Comfort', 'kind': 'HmiSoftware', 'softwarePath': 'HMI_1'}]


def portals(a):
    if a.no_portal:
        return []
    p = [{'portalId': 4711, 'projectPath': PROJECT_PATH, 'isConnected': False}]
    if a.multi_instances:
        p.append({'portalId': 4712, 'projectPath': 'C:\\TIA\\Other_Line\\Other_Line.ap21', 'isConnected': False})
    return p


def writer(a, nm, arg):
    if nm == 'GetPortals':
        return json.dumps({'portalId': '', 'portals': portals(a)})
    if nm == 'Connect':
        if a.multi_instances and not arg.get('portalId'):
            raise ValueError('More than one TIA Portal instance is running — pass portalId')
        if a.no_portal and not arg.get('portalId'):
            return 'Started a new TIA Portal V21 instance (fake)'
        if arg.get('portalId') and arg['portalId'] not in [x['portalId'] for x in portals(a)]:
            raise ValueError('no TIA Portal instance with portalId %s' % arg['portalId'])
        return 'Connected to TIA Portal V21 (fake), portalId %s' % (arg.get('portalId') or 4711)
    if nm == 'GetProject':
        return json.dumps({'projects': [{'name': 'Demo_Line', 'path': PROJECT_PATH, 'kind': 'project'}]})
    if nm in ('GetDevices', 'GetProjectTree'):
        return json.dumps({'devices': DEVICES})
    if nm == 'GetState':
        return json.dumps({'portalId': 4711, 'connected': True, 'project': PROJECT_PATH, 'writeMode': True})
    if nm == 'OpenTiaProject':
        if not str(arg.get('path', '')).lower().endswith(('.ap21', '.als21')):
            raise ValueError('not a TIA Portal V21 project: %s' % arg.get('path'))
        return json.dumps({'portalId': 4711, 'project': {'name': os.path.splitext(os.path.basename(arg['path']))[0],
                                                          'path': arg['path']},
                           'devices': DEVICES, 'softwarePaths': ['PLC_1']})
    if nm == 'CloseProject':
        return 'Project closed (fake)'
    if nm == 'GetSoftwareTree':
        return json.dumps({'softwarePath': arg['softwarePath'], 'blocks': sorted(
            os.path.basename(x)[:-6] for x in glob.glob(os.path.join(a.sample, '**', '*.s7dcl'), recursive=True))})
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
    if nm == 'SetNetworkText':
        return set_network_text(a, arg)
    raise ValueError('unknown tool %s for the fake Writer' % nm)


def set_network_text(a, arg):
    """Title / comment of network n: the attribute ids in .s7dcl (a new MLC_fake_N when missing) and their en-US text in
    .s7res. The block is copied into --state first (= written to the "TIA project")."""
    block = arg['blockPath'].strip('/').split('/')[-1]
    z, group = find(a, block)
    target = os.path.join(a.state, *[x for x in group.split('/') if x])
    if os.path.normcase(os.path.abspath(z)) != os.path.normcase(os.path.abspath(target)):
        copy_tree(z, target, block)
    p = os.path.join(target, block + '.s7dcl')
    with open(p, 'rb') as f:
        t = f.read().decode('utf-8-sig')
    if 'S7_Safety := "TRUE"' in t and not arg.get('allowFailsafe'):
        raise ValueError('fail-safe block: pass allowFailsafe')
    texts = {k: arg.get(v) for k, v in (('S7_NetworkTitle', 'title'), ('S7_NetworkComment', 'comment')) if arg.get(v) is not None}
    if arg.get('dryRun', True):
        return 'dry run: NW %s of %s would get %s' % (arg['network'], block, texts)
    crlf, t = '\r\n' in t, t.replace('\r\n', '\n')
    nets = list(re.finditer(r'(?:^([ \t]*)\{([^{}]*)\}[ \t]*\n)?^[ \t]*NETWORK[ \t]*\n', t, re.M))
    m = nets[int(arg['network']) - 1]
    attrs = dict(re.findall(r'(S7_\w+)\s*:=\s*"([^"]*)"', m.group(2) or ''))
    rp = p[:-6] + '.s7res'
    res = open(rp, encoding='utf-8-sig').read().replace('\r\n', '\n') if os.path.exists(rp) else 'MultiLingualTexts:\n'
    for k, v in texts.items():
        ident = attrs.get(k)
        if not ident:
            n = 1
            while 'MLC_fake_%d' % n in res:
                n += 1
            ident = attrs[k] = 'MLC_fake_%d' % n
        res = re.sub(r'(?m)^  - id: %s\n(?:    .*\n?)*' % re.escape(ident), '', res)        # drop the old entry
        res = res.rstrip('\n') + "\n  - id: %s\n    en-US: '%s'\n" % (ident, v.replace("'", "''").replace('\n', ' '))
    head = '    {\n' + ';\n'.join('        %s := "%s"' % kv for kv in attrs.items()) + '\n    }\n'
    t = t[:m.start()] + head + re.search(r'^[ \t]*NETWORK[ \t]*\n', m.group(0), re.M).group(0) + t[m.end():]
    with open(p, 'wb') as f:
        f.write((t.replace('\n', '\r\n') if crlf else t).encode('utf-8'))
    with open(rp, 'wb') as f:
        f.write((res.replace('\n', '\r\n') if crlf else res).encode('utf-8'))
    return 'NW %s of %s: %s set (fake)' % (arg['network'], block, ', '.join(texts))


RUNS = {}


def new_folder(a):
    t = datetime.now()
    while True:                                          # unique folder timestamp
        folder = os.path.join(a.export_root, a.project, t.strftime('%Y%m%d-%H%M%S'))
        if not os.path.exists(folder):
            os.makedirs(folder)
            return folder
        t += timedelta(seconds=1)


def export_plc(a, arg):
    """Whole PLC: the sample PLC folder (blocks taken from --state when imported there) in 2 chunks."""
    plc_dir = os.path.dirname(a.sample)                  # …\PLC_1
    files = sorted(os.path.relpath(os.path.join(d, f), plc_dir) for d, _, fs in os.walk(plc_dir) for f in fs)
    if arg.get('runId'):
        folder, start = RUNS[arg['runId']], int(arg.get('offset') or 0)
    else:
        folder, start = new_folder(a), 0
        RUNS[os.path.basename(folder)] = folder
    end = len(files) if start else len(files) // 2
    plc = arg.get('plcName') or 'PLC_1'
    for rel in files[start:end]:
        src = os.path.join(a.state, os.path.relpath(rel, 'Blocks')) if rel.startswith('Blocks') else ''
        dst = os.path.join(folder, plc, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src if src and os.path.exists(src) else os.path.join(plc_dir, rel), dst)
    with open(os.path.join(folder, 'manifest.jsonl'), 'a', encoding='utf-8') as f:
        for rel in files[start:end]:
            f.write(json.dumps({'path': plc + '/' + rel.replace(os.sep, '/'), 'format': 'documents'}) + '\n')
    res = {'exportFolder': folder, 'manifest': os.path.join(folder, 'manifest.jsonl'), 'exported': end - start,
           'complete': end == len(files), 'runId': os.path.basename(folder)}
    if end < len(files):
        res['nextOffset'] = end
    return json.dumps(res)


def export_hmi(a, arg):
    """export_hmi_to_folder: the HMI sample (a screen the fake Writer changed comes from --state) in chunks of 3 files."""
    if a.no_hmi:
        raise ValueError('No WinCC Unified HMI software was found in the project.')
    hmi = os.path.basename(SAMPLE_HMI)
    files = sorted(os.path.relpath(os.path.join(d, f), SAMPLE_HMI).replace(os.sep, '/') for d, _, fs in os.walk(SAMPLE_HMI)
                   for f in fs)
    start = int(arg.get('offset') or 0)
    if arg.get('runId'):
        folder = os.path.join(a.export_root, a.project, arg['runId'])
        os.makedirs(folder, exist_ok=True)
    elif start:
        raise ValueError('offset greater than 0 requires the runId returned by the first call.')
    else:
        folder = new_folder(a)
    end = min(len(files), start + 3)
    with open(os.path.join(folder, 'manifest-hmi.jsonl'), 'a', encoding='utf-8') as f:
        for rel in files[start:end]:
            src = os.path.join(a.state, hmi, rel)
            dst = os.path.join(folder, hmi, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src if os.path.exists(src) else os.path.join(SAMPLE_HMI, rel), dst)
            kind = 'screen' if rel.startswith('Screens/') else 'tagTable' if rel.startswith('Tags/') else rel[:-5]
            f.write(json.dumps({'kind': kind, 'path': rel[:-5], 'file': rel, 'bytes': os.path.getsize(dst)}) + '\n')
    res = {'exportFolder': folder, 'hmiFolder': os.path.join(folder, hmi), 'manifestPath': os.path.join(folder, 'manifest-hmi.jsonl'),
           'runId': os.path.basename(folder), 'hmiName': hmi, 'totalItems': len(files), 'offset': start,
           'processedItems': end - start, 'complete': end == len(files)}
    if end < len(files):
        res['nextOffset'] = end
    return json.dumps(res)


def reader(a, nm, arg):
    if nm == 'export_hmi_to_folder':
        return export_hmi(a, arg)
    if nm == 'get_project_status':
        if a.no_portal:
            return json.dumps({'connected': False, 'message': 'TIA Portal is not running'})
        return json.dumps({'connected': True, 'projectName': a.project, 'projectPath': PROJECT_PATH})
    if nm != 'export_to_folder':
        raise ValueError('unknown tool %s for the fake Reader' % nm)
    if not arg.get('pathPrefixes'):
        return export_plc(a, arg)
    prefix = arg['pathPrefixes'][0].strip('/')
    parts = prefix.split('/')                            # <PLC>/Blocks/<Group…>/<block>
    plc, block = parts[0], parts[-1]
    z, group = find(a, block)
    folder = new_folder(a)
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
    ap.add_argument('--no-portal', action='store_true', help='TIA Portal is not running')
    ap.add_argument('--no-hmi', action='store_true', help='Reader: the project has no Unified HMI')
    ap.add_argument('--slow', type=float, default=0)
    a = ap.parse_args()
    os.makedirs(a.state, exist_ok=True)
    fn = writer if a.role == 'writer' else reader
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        z = json.loads(line.decode('utf-8'))
        if 'id' not in z:
            continue                                     # notification
        if z['method'] == 'initialize':
            v = {'protocolVersion': z['params'].get('protocolVersion'), 'capabilities': {'tools': {}},
                 'serverInfo': {'name': 'fake-' + a.role, 'version': '0'}}
        elif z['method'] == 'tools/call':
            time.sleep(a.slow)
            with open(os.path.join(a.state, 'calls.jsonl'), 'a', encoding='utf-8') as f:
                f.write(json.dumps({'role': a.role, 'tool': z['params']['name'],
                                    'arguments': z['params'].get('arguments') or {}}) + '\n')
            try:
                v = {'content': [{'type': 'text', 'text': fn(a, z['params']['name'], z['params'].get('arguments') or {})}]}
            except Exception as e:                       # tool error = isError, not a server crash
                v = {'content': [{'type': 'text', 'text': str(e)}], 'isError': True}
        else:
            sys.stdout.buffer.write((json.dumps({'jsonrpc': '2.0', 'id': z['id'],
                                                 'error': {'code': -32601, 'message': 'unknown method'}}) + '\n').encode())
            sys.stdout.buffer.flush()
            continue
        sys.stdout.buffer.write((json.dumps({'jsonrpc': '2.0', 'id': z['id'], 'result': v}, ensure_ascii=False) + '\n')
                                .encode('utf-8'))
        sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
