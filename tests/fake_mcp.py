"""Fake TIAC_Writer / TIAC_Reader (MCP over stdio) for tests and demos — it never talks to TIA Portal.

python tests\\fake_mcp.py writer [--state DIR] [--sample DIR] [--multi-instances | --no-portal] [--slow S]
python tests\\fake_mcp.py reader --export-root DIR --project Demo_Line [--state DIR] [--sample DIR] [--no-portal]

"TIA project" = the --state folder (default %TEMP%\\tiac_fake_tia): a block appears there after its first import;
until then it is read from --sample (default: the Demo_Line sample 20261012-120000\\PLC_1\\Blocks).
Writer: Connect, ResolveObjectPath, ExportBlocksAsDocuments (copies .s7dcl/.s7res to exportPath), PreviewImport,
ImportFromDocuments (copies the documents to --state and appends a line to <state>\\imports.jsonl), SetNetworkText
(title / comment of network n in --state: MLC ids in .s7dcl, en-US texts in .s7res), SetHmiItemAttribute,
SetHmiDynamizationTag, SetHmiItemDynamization, CreateHmiScreenItems, DeleteHmiScreenItems (a screen of the HMI sample
copied to <state>\\HMI_1\\Screens and changed there; dry runs answer like the Writer, --fail-hmi NAME refuses item NAME), GetHmiTags,
RenameHmiScreen / SetHmiTagPlcTags (only answer), CompileHmi.
Writer, SimaticML (E4/E5/E7): ExportXmlBlock copies <block>.xml (from --state after an import, else from --xml-sample) and
answers {contentHash}; ImportXmlBlock checks expectedContentHash against the current file ('state_changed'), copies the file
to --state and appends to imports.jsonl; GetBlockInfo answers the OB attributes (Number, SecondaryType, CyclicTime) of the
current file. --fail-xml-import / --fail-compile fail ImportXmlBlock / CompileBlock for a file that differs from the
sample (a restore of the original passes); --ob-renumbered reports another OB number after an edited import.
Upload safety (round 9): SaveProject (--fail-save fails it); --fail-import fails ImportFromDocuments only for edited
documents; --crash-compile: CompileBlock of an edited block "takes TIA down" (GetPortals then lists no instance, every other
call fails); --fail-compile-always: CompileBlock of any imported block fails until CompileSoftware ran (--fail-compile-software
fails that too).
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
import hashlib
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
SAMPLE_XML = os.path.join(ROOT, 'samples', 'simaticml', 'tia')


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
TREE = '''```
Demo_Line
├── Devices [Collection]
│   ├── S7-1500/ET200MP station_1 [Device: System:Device.S71500]
│   │   ├── Rail_0 [DeviceItem]
│   │   │   └── PLC_1 [Hardware Component]
│   │   └── PLC_1 [DeviceItem]
│   │       ├── PlcSoftware: PLC_1 [PLC Program]
│   │       ├── PROFINET interface_1 [Hardware Component]
│   │       └── PROFINET interface_1 [DeviceItem]
│   │           └── Port_1 [DeviceItem]
│   └── HMI_1 [Device: OrderNumber:6AV2128-3MB06-0AX1/V19.0.0.0]
│       └── HMI_1 [DeviceItem]
│           └── HMI_RT_1 [DeviceItem]
│               └── HmiSoftware: HMI_RT_1 [HMI Program]
└── UngroupedDevicesGroup: Ungrouped devices [System Group]
    └── IO device_1 [System:Device.ET200SP]
```'''


def xml_file(a, block):
    """Current SimaticML of a block: --state (after an import) else --xml-sample."""
    for source in (a.state, a.xml_sample):
        x = glob.glob(os.path.join(source, '**', block + '.xml'), recursive=True)
        if x:
            return x[0]
    raise ValueError('block %s is not in the project (no SimaticML)' % block)


def xml_hash(fpath):
    with open(fpath, 'rb') as f:
        return 'xml:sha256:' + hashlib.sha256(f.read()).hexdigest()


def xml_edited(a, block):
    """Does the block's current file differ from the sample (= an edited import)?"""
    cur = xml_file(a, block)
    x = glob.glob(os.path.join(a.xml_sample, '**', block + '.xml'), recursive=True)
    return bool(x) and xml_hash(cur) != xml_hash(x[0])


def edited(a, block):
    """Was the block imported (it is in --state) with content that differs from its sample (SimaticML or documents)?"""
    for ext, sample in (('.xml', a.xml_sample), ('.s7dcl', a.sample)):
        cur = glob.glob(os.path.join(a.state, '**', block + ext), recursive=True)
        if cur:
            x = glob.glob(os.path.join(sample, '**', block + ext), recursive=True)
            return not x or xml_hash(cur[0]) != xml_hash(x[0])
    return False


def imported(a, block):
    return any(glob.glob(os.path.join(a.state, '**', block + ext), recursive=True) for ext in ('.xml', '.s7dcl'))


def xml_writer(a, nm, arg):
    if nm == 'ExportXmlBlock':
        block = arg['blockPath'].strip('/').split('/')[-1]
        src = xml_file(a, block)
        os.makedirs(arg['exportPath'], exist_ok=True)
        shutil.copyfile(src, os.path.join(arg['exportPath'], block + '.xml'))
        return json.dumps({'contentHash': xml_hash(src), 'message': "Block exported from '%s' (fake)" % arg['blockPath']})
    if nm == 'ImportXmlBlock':
        with open(arg['importPath'], encoding='utf-8-sig') as f:
            block = re.search(r'<Name>([^<]+)</Name>', f.read()).group(1)
        try:
            cur = xml_file(a, block)
        except ValueError:
            cur = None
        if arg.get('expectedContentHash') and cur and xml_hash(cur) != arg['expectedContentHash']:
            raise ValueError('state_changed: the block %s changed since its export' % block)
        sample = glob.glob(os.path.join(a.xml_sample, '**', block + '.xml'), recursive=True)
        if a.fail_xml_import and not (sample and xml_hash(sample[0]) == xml_hash(arg['importPath'])):
            raise ValueError("Failed importing '%s' (fake) errorDetail: %s"      # the Writer's (round 5) format
                             % (block, json.dumps(['Import failed', 'Network 2: the wire 37 has no target (fake)'])))
        target = os.path.join(a.state, *[x for x in arg['groupPath'].split('/') if x])
        os.makedirs(target, exist_ok=True)
        shutil.copyfile(arg['importPath'], os.path.join(target, block + '.xml'))
        with open(os.path.join(a.state, 'imports.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(dict(arg, file=os.path.join(target, block + '.xml')), ensure_ascii=False) + '\n')
        return json.dumps({'message': "Block imported from '%s' (fake)" % arg['importPath']})
    if nm == 'GetBlockInfo':
        block = arg['blockPath'].strip('/').split('/')[-1]
        with open(xml_file(a, block), encoding='utf-8-sig') as f:
            t = f.read()
        head = t[:t.find('<ObjectList>')]
        attrs = [{'name': k, 'value': v, 'accessMode': 'ReadWrite'}
                 for k, v in re.findall(r'<(Number|SecondaryType|CyclicTime|PhaseOffset)>([^<]*)<', re.sub(r'<Interface>.*</Interface>', '', head, flags=re.S))]
        if a.ob_renumbered and xml_edited(a, block):
            attrs = [dict(x, value=str(int(x['value']) + 100)) if x['name'] == 'Number' else x for x in attrs]
        return json.dumps({'name': block, 'typeName': 'OB' if 'SW.Blocks.OB' in t else 'FB', 'attributes': attrs})
    return None


def portals(a):
    if a.no_portal:
        return []
    p = [{'portalId': 4711, 'projectPath': PROJECT_PATH, 'isConnected': False}]
    if a.multi_instances:
        p.append({'portalId': 4712, 'projectPath': 'C:\\TIA\\Other_Line\\Other_Line.ap21', 'isConnected': False})
    return p


def writer(a, nm, arg):
    crashed = os.path.exists(os.path.join(a.state, 'crashed.txt'))
    if nm == 'GetPortals':
        return json.dumps({'portalId': '', 'portals': [] if crashed else portals(a)})
    if crashed:                                       # --crash-compile: TIA Portal is gone, every call fails
        raise ValueError('The TIA Portal instance is no longer available (fake)')
    if nm == 'SaveProject':
        if a.fail_save:
            raise ValueError('Failed to save project (fake)')
        return json.dumps({'message': 'Local project saved (fake)'})
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
    if nm == 'GetProjectTree':                        # the Writer's real shape: a text tree (seen on a V21 project)
        return json.dumps({'tree': TREE, 'message': 'Project tree retrieved'})
    if nm == 'GetDevices':
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
        if a.inconsistent and not os.path.exists(os.path.join(a.state, 'compiled.txt')):   # TIA exports no inconsistent block
            return json.dumps({'items': [], 'message': 'Document export completed: 0 blocks exported', 'meta': {'exportedBlocks': 0}})
        copy_tree(find(a, block)[0], arg['exportPath'], block)
        return 'Exported 1 block(s) to %s' % arg['exportPath']
    if nm == 'PreviewImport':
        blocks = [os.path.basename(x)[:-6] for x in glob.glob(os.path.join(arg['importPath'], '*.s7dcl'))]
        return json.dumps({'groupPath': arg['groupPath'], 'overwrite': blocks})
    if nm == 'ImportFromDocuments':
        block = arg['fileNameWithoutExtension']
        sample = glob.glob(os.path.join(a.sample, '**', block + '.s7dcl'), recursive=True)
        if a.fail_import and not (sample and xml_hash(sample[0]) == xml_hash(os.path.join(arg['importPath'], block + '.s7dcl'))):
            raise ValueError("Failed importing '%s' from documents" % block)    # the real Writer says no more than this
        target = os.path.join(a.state, *[x for x in arg['groupPath'].split('/') if x])
        copy_tree(arg['importPath'], target, block)
        with open(os.path.join(a.state, 'imports.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(dict(arg, file=os.path.join(target, block + '.s7dcl')), ensure_ascii=False) + '\n')
        return 'Imported %s (%s)' % (block, arg.get('importOption'))
    if nm == 'SetNetworkText':
        return set_network_text(a, arg)
    if nm in ('SetHmiItemAttribute', 'SetHmiDynamizationTag', 'SetHmiItemDynamization'):
        return set_hmi(a, nm, arg)
    if nm in ('CreateHmiScreenItems', 'DeleteHmiScreenItems'):
        return hmi_batch(a, nm, arg)
    if nm == 'GetHmiTags':
        with open(os.path.join(SAMPLE_HMI, 'Tags', 'Default tag table.json'), encoding='utf-8') as f:
            return json.dumps({'tags': [dict(t, table='Default tag table') for t in json.load(f)['tags']]})
    if nm in ('RenameHmiScreen', 'SetHmiTagPlcTags'):
        return '%s%s (fake): %s' % ('dry run: ' if arg.get('dryRun', True) else '', nm, json.dumps(arg))
    if nm == 'CompileHmi':
        return json.dumps({'compiled': arg['softwarePath'], 'state': 'Success', 'errors': 0, 'warnings': 0})
    if nm in ('ExportXmlBlock', 'ImportXmlBlock', 'GetBlockInfo'):
        return xml_writer(a, nm, arg)
    if nm == 'CompileBlock':                          # the real answer: {state, errorCount, warningCount, messages: [...]}
        block = arg['path'].split('/')[-1]
        if a.crash_compile and edited(a, block):     # D4: TIA Portal V21 went down in CompileBlock
            open(os.path.join(a.state, 'crashed.txt'), 'w').close()
            raise ValueError('Unexpected exception - no exception message available (fake)')
        if a.fail_compile and edited(a, block) or a.fail_compile_always and imported(a, block) \
                and not os.path.exists(os.path.join(a.state, 'software.txt')):
            raise ValueError("Compiling '%s' failed with 1 error(s). %s: Invalid operand (fake)" % (arg['path'], block))
        open(os.path.join(a.state, 'compiled.txt'), 'a').write(arg['path'] + '\n')
        return json.dumps({'state': 'Success', 'errorCount': 0, 'warningCount': 0, 'messages': [
            {'path': '%s (FC)' % arg['path'].split('/')[-1], 'state': 'Success', 'description': 'Block was successfully compiled.'}]})
    if nm == 'CompileSoftware':                       # --fail-compile-always: a compile of the whole software repairs it
        if a.fail_compile_software:
            raise ValueError("Compiling '%s' failed with 1 error(s). FB_Other: Invalid operand (fake)" % arg['softwarePath'])
        open(os.path.join(a.state, 'software.txt'), 'a').write(json.dumps(arg) + '\n')
        return json.dumps({'state': 'Success', 'errorCount': 0, 'warningCount': 0, 'messages': []})
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


def _screen(a, arg):
    """-> (path, screen) of <state>\\HMI_1\\Screens\\<screen>.json (copied from the sample first); the fake Reader's
    export_hmi_to_folder takes the screen from there."""
    hmi = os.path.basename(SAMPLE_HMI)
    if arg['softwarePath'] != hmi:
        raise ValueError('no WinCC Unified HMI software %s (fake: %s)' % (arg['softwarePath'], hmi))
    p = os.path.join(a.state, hmi, 'Screens', arg['screenName'] + '.json')
    if not os.path.exists(p):
        src = os.path.join(SAMPLE_HMI, 'Screens', arg['screenName'] + '.json')
        if not os.path.exists(src):
            raise ValueError('screen %s does not exist' % arg['screenName'])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        shutil.copyfile(src, p)
    with open(p, encoding='utf-8') as f:
        return p, json.load(f)


def _save(p, s):
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def _mtext(v, culture=None):
    return {'$type': 'MultilingualText', 'items': [{'$type': 'MultilingualTextItem', 'language': culture or 'en-US', 'text': v}]}


def _value(v):
    return json.loads(v) if re.fullmatch(r'-?\d+(\.\d+)?|true|false', v) else v


def set_hmi(a, nm, arg):
    """SetHmiItemAttribute (dotted Font.X too) / SetHmiDynamizationTag / SetHmiItemDynamization (tag only) on the screen
    of <state>. --fail-hmi NAME: a dry run of a call on item NAME is refused (isError)."""
    p, s = _screen(a, arg)
    x = next((i for i in s['screenItems'] if i['name'] == arg['itemName']), None) if arg.get('itemName') else s
    if x is None:
        raise ValueError('screen %s has no item %s' % (arg['screenName'], arg['itemName']))
    if a.fail_hmi and arg.get('itemName') == a.fail_hmi:
        raise ValueError("'%s': Hmi%s has no property '%s' (fake)" % (a.fail_hmi, x.get('$type', ''), arg.get('attribute') or arg.get('property')))
    if nm == 'SetHmiDynamizationTag':
        d = next((d for d in x.get('dynamizations') or [] if d.get('propertyName') == arg['property']), None)
        if d is None:
            raise ValueError('%s has no tag dynamization of %s' % (arg['itemName'], arg['property']))
        k, old, new, target = 'tag', d['tag'], arg['tag'], d
    elif nm == 'SetHmiItemDynamization':
        dyn = arg['dynamization']
        dyn = json.loads(dyn) if dyn.startswith('{') else {'tag': dyn}
        ds = x.setdefault('dynamizations', [])
        old = next((d for d in ds if d.get('propertyName') == arg['property']), None)
        k, new, target = 'dynamization ' + arg['property'], {'$type': 'TagDynamization', 'propertyName': arg['property'],
                                                              'tag': dyn['tag'], 'useIndirectAddressing': bool(dyn.get('indirect'))}, None
        if not arg.get('dryRun', True):
            ds[:] = [d for d in ds if d is not old] + [new]
    else:
        path = arg['attribute'].split('.')
        target = x
        for seg in path[:-1]:
            target = target.setdefault(seg[:1].lower() + seg[1:], {})
        k, v = path[-1][:1].lower() + path[-1][1:], arg['value']
        old = target.get(k)
        new = _mtext(v, arg.get('culture')) if k == 'text' else _value(v)
    if arg.get('dryRun', True):
        return json.dumps({'dryRun': True, 'applied': False, 'changeCount': 1, 'message': 'dry run: %s.%s %s -> %s' % (
            arg.get('itemName') or arg['screenName'], k, json.dumps(old), json.dumps(new))})
    if target is not None:
        target[k] = new
    _save(p, s)
    return '%s.%s set (fake)' % (arg.get('itemName') or arg['screenName'], k)


def hmi_batch(a, nm, arg):
    """CreateHmiScreenItems / DeleteHmiScreenItems in the shape of the Writer's ResponseHmiBatch (items that fail are
    reported and skipped, a batch where none goes through fails). --fail-hmi NAME: item NAME fails its check."""
    p, s = _screen(a, arg)
    names = {i['name'].lower() for i in s['screenItems']}
    if nm == 'DeleteHmiScreenItems':
        want = [n for n in arg['names'].split(';') if n]
        res = [{'name': n, 'ok': n.lower() in names and n != a.fail_hmi,
                'message': '' if n.lower() in names else 'no item %s on the screen' % n} for n in want]
        keep = [i for i in s['screenItems'] if i['name'] not in [r['name'] for r in res if r['ok']]]
    else:
        items = json.loads(arg['items'])
        res = [{'name': i['name'], 'ok': i['name'].lower() not in names and i['name'] != a.fail_hmi,
                'message': '' if i['name'].lower() not in names else 'the name is taken'} for i in items]
        made = []
        for i, r in zip(items, res):
            if r['ok']:
                x = dict({'$type': 'Hmi' + i['type'], 'name': i['name']}, **{k: i[k] for k in ('left', 'top', 'width', 'height')})
                if 'text' in i:
                    x['text'] = _mtext(i['text'], arg.get('culture'))
                for k, v in (i.get('properties') or {}).items():
                    seg = k.split('.')
                    t = x
                    for z in seg[:-1]:
                        t = t.setdefault(z[:1].lower() + z[1:], {})
                    t[seg[-1][:1].lower() + seg[-1][1:]] = _value(v)
                x['dynamizations'] = [{'$type': 'TagDynamization', 'propertyName': k, 'tag': v if isinstance(v, str) else v['tag'],
                                       'useIndirectAddressing': not isinstance(v, str) and bool(v.get('indirect'))}
                                      for k, v in (i.get('dynamizations') or {}).items()]
                x['eventHandlers'] = [{'eventType': k, 'script': {'scriptCode': v}} for k, v in (i.get('events') or {}).items()]
                made.append(x)
        keep = s['screenItems'] + made
    for r in res:
        if not r['ok'] and not r['message']:
            r['message'] = 'refused by the check (fake)'
    ok = sum(r['ok'] for r in res)
    if not ok:
        raise ValueError('No item went through: %s' % '; '.join('%s: %s' % (r['name'], r['message']) for r in res))
    dry = arg.get('dryRun', True)
    if not dry:
        s['screenItems'] = keep
        _save(p, s)
    return json.dumps({'dryRun': dry, 'applied': not dry, 'okCount': ok, 'failedCount': len(res) - ok, 'items': res,
                       'message': '%s%d screen item(s)' % ('Dry run: ' if dry else '', ok)})


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
    res = {'exportFolder': folder, 'plcFolder': os.path.join(folder, plc), 'manifestPath': os.path.join(folder, 'manifest.jsonl'),
           'runId': os.path.basename(folder), 'plcName': plc, 'totalItems': len(files), 'offset': start,
           'processedItems': end - start, 'complete': end == len(files)}
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


def wrap(res):
    """The real Reader wraps every answer: {"success": true, "payload": "<JSON text>"} (seen on a V21 project)."""
    return json.dumps({'success': True, 'payload': res})


def reader(a, nm, arg):
    if nm == 'export_hmi_to_folder':
        return wrap(export_hmi(a, arg))
    if nm == 'get_project_status':
        if a.no_portal:
            return json.dumps({'connected': False, 'message': 'TIA Portal is not running'})
        return json.dumps({'connected': True, 'projectName': a.project, 'projectPath': PROJECT_PATH})
    if nm != 'export_to_folder':
        raise ValueError('unknown tool %s for the fake Reader' % nm)
    if not arg.get('pathPrefixes'):
        return wrap(export_plc(a, arg))
    prefix = arg['pathPrefixes'][0].strip('/')
    parts = prefix.split('/')                            # <PLC>/Blocks/<Group…>/<block>
    plc, block = parts[0], parts[-1]
    z, group = find(a, block)
    folder = new_folder(a)
    copy_tree(z, os.path.join(folder, plc, 'Blocks', *[x for x in group.split('/') if x]), block)
    with open(os.path.join(folder, 'manifest.jsonl'), 'w', encoding='utf-8') as f:
        f.write(json.dumps({'path': prefix, 'format': 'documents'}) + '\n')
    return wrap(json.dumps({'exportFolder': folder, 'plcFolder': os.path.join(folder, plc), 'complete': True,
                            'manifestPath': os.path.join(folder, 'manifest.jsonl'), 'processedItems': 1, 'totalItems': 1}))


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
    ap.add_argument('--inconsistent', action='store_true', help='Writer: blocks export only after CompileBlock')
    ap.add_argument('--slow', type=float, default=0)
    ap.add_argument('--fail-import', action='store_true', help='Writer: ImportFromDocuments fails')
    ap.add_argument('--xml-sample', default=SAMPLE_XML)
    ap.add_argument('--fail-xml-import', action='store_true', help='Writer: ImportXmlBlock of an edited file fails')
    ap.add_argument('--fail-compile', action='store_true', help='Writer: CompileBlock of an edited SimaticML block fails')
    ap.add_argument('--ob-renumbered', action='store_true', help='Writer: GetBlockInfo reports another OB number after an edit')
    ap.add_argument('--fail-save', action='store_true', help='Writer: SaveProject fails')
    ap.add_argument('--fail-hmi', help='Writer: every HMI call on this screen item is refused (dry run included)')
    ap.add_argument('--crash-compile', action='store_true', help='Writer: CompileBlock of an edited block takes TIA down')
    ap.add_argument('--fail-compile-always', action='store_true',
                    help='Writer: CompileBlock of any imported block fails (also the original) until CompileSoftware')
    ap.add_argument('--fail-compile-software', action='store_true', help='Writer: CompileSoftware fails')
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
