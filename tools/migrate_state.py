"""One-off migration of an old `stav.json` (Czech keys, editor versions before the English rename) to `state.json`.

python tools/migrate_state.py <data>\\<project>\\stav.json [<output state.json>]

The server reads only `state.json`. The old file is left untouched; an existing output file is never overwritten.
Keys are renamed recursively; element type values (`typ`) and call kinds (`volani`) are renamed too.
Free text (notes, step messages, network titles) is kept as it is.
"""
import json
import os
import sys

KEYS = {
    'adresa': 'address', 'atributy': 'attributes', 'autor': 'author', 'blok': 'block', 'bloky': 'blocks',
    'boxy': 'boxes', 'cary': 'lines', 'cas': 'time', 'cesta': 'path', 'checked_povolen': 'checked_allowed',
    'chyba': 'error', 'chyby': 'errors', 'cislo': 'number', 'cislo_bloku': 'block_number', 'cleny': 'members',
    'dalsi': 'next', 'deti': 'children', 'do': 'to', 'druh': 'kind', 'historie': 'history', 'hlavicka': 'header',
    'hodnoty': 'values', 'instance_typ': 'instance_type', 'jako_lad': 'as_lad', 'jazyk': 'language', 'jmeno': 'name',
    'kdo': 'by', 'klic': 'key', 'kolo': 'round', 'komentar': 'comment', 'kontroly': 'checks', 'kresba': 'drawing',
    'krok': 'step', 'kroky': 'steps', 'lista': 'rail', 'mista': 'places', 'misto': 'place', 'nad': 'above',
    'napajeni': 'power', 'nastroj': 'tool', 'nazev': 'name', 'neg_vystup': 'neg_output', 'networky': 'networks',
    'novy_vstup': 'new_input', 'od': 'from', 'otevrena': 'open', 'pahyl': 'stub', 'piny_l': 'pins_l',
    'piny_r': 'pins_r', 'po': 'after', 'pod': 'below', 'polozky': 'items', 'popis': 'description', 'posledni': 'last',
    'poznamka': 'note', 'prave': 'right', 'prazdny': 'empty', 'pred': 'before', 'predchozi': 'previous',
    'privody': 'feeds', 'projekt': 'project', 'projekty': 'projects', 'promenne': 'variables', 'prvek': 'element',
    'prvky': 'elements', 'radky': 'rows', 'radky_r': 'rows_r', 'rozdil': 'diff', 'rozdil_piny': 'pin_diff',
    'rozdily': 'diffs', 'rozhrani': 'interface', 'rozlozeni': 'layout', 'rungy': 'rungs', 'sablona': 'template',
    'sekce': 'section', 'shoda': 'match', 'signal': 'signal', 'signaly': 'signals', 'sirka': 'width',
    'skupina': 'group', 'smer': 'direction', 'soubor': 'file', 'stav': 'state', 'tabulka': 'table', 'tagy': 'tags',
    'titul': 'symbol', 'titulek': 'title', 'typ': 'type', 'uprava': 'edit', 'uprava_id': 'edit_id',
    'upraveno': 'edited', 'upravy': 'edits', 'uzly': 'nodes', 'varovani': 'warnings', 'vetve': 'branches',
    'vodice': 'wires', 'volani': 'call', 'vpravo': 'right', 'vstup': 'input', 'vstupy': 'inputs', 'vyska': 'height',
    'vystup': 'output', 'vystupy': 'outputs', 'y_nad': 'y_above', 'y_sablona': 'y_template', 'y_titul': 'y_symbol',
    'z': 'from', 'zapis': 'write', 'zapisy': 'writes', 'zdroje': 'sources', 'zprava': 'message',
}
VALUES = {   # element types (key `typ`) and call kinds (key `volani`)
    'kontakt': 'contact', 'kontakt_nc': 'contact_nc', 'kontakt_p': 'contact_p', 'kontakt_n': 'contact_n',
    'civka': 'coil', 'civka_s': 'coil_s', 'civka_r': 'coil_r', 'civka_neg': 'coil_neg', 'civka_p': 'coil_p',
    'civka_n': 'coil_n', 'porovnani': 'comparison', 'volani': 'call', 'instrukce': 'instruction',
}


def migrate(x, key=None):
    if isinstance(x, dict):
        return {KEYS.get(k, k): migrate(v, k) for k, v in x.items()}
    if isinstance(x, list):
        return [migrate(v) for v in x]
    if key in ('typ', 'volani') and isinstance(x, str):
        return VALUES.get(x, x)
    return x


def main(argv=None):
    a = sys.argv[1:] if argv is None else argv
    if not a or len(a) > 2:
        print(__doc__, file=sys.stderr)
        return 2
    out = a[1] if len(a) > 1 else os.path.join(os.path.dirname(os.path.abspath(a[0])), 'state.json')
    if os.path.exists(out):
        print('ERROR: %s already exists' % out, file=sys.stderr)
        return 2
    with open(a[0], encoding='utf-8') as f:
        old = json.load(f)
    if not isinstance(old, dict) or not isinstance(old.get('upravy'), list):
        print('ERROR: %s is not an old stav.json ({"upravy": [...]})' % a[0], file=sys.stderr)
        return 2
    new = migrate(old)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(new, f, ensure_ascii=False, indent=1)
    print('%s -> %s (%d edits)' % (a[0], out, len(new['edits'])))
    return 0


if __name__ == '__main__':
    sys.exit(main())
