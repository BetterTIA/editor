# LADEditor — ANET.md

Lokální prohlížeč (a později editor) LAD networků z exportu Readeru (`.s7dcl` + `.s7res`). Bez instalací: Python 3 stdlib, vanilla JS + SVG, headless Chrome na snímky.

## Spuštění
```
python server.py --port 8765 --export-root "C:\TIA\export" --data "data\ladeditor"
```
- `--export-root` = složka `<projekt>\<timestamp YYYYMMDD-HHMMSS>\<PLC>\Blocks\…\<blok>.s7dcl`. Blok se hledá podle jména souboru v **nejnovějším** exportu, který ho obsahuje (dílčí exporty nevadí).
- `--data` = data úprav (`stav.json`), použije až M2; v M1 se nic nezapisuje.
- Nad vlastními vzorky: `--export-root tests\vzorky`.
- Server poslouchá jen na `127.0.0.1`.

## Stránka
`http://localhost:8765/?projekt=<projekt>&blok=<blok>&nw=<n>[&snimek=1][&tema=svetly|tmavy]`
- bez `projekt`/`blok` = výběr bloků ze všech exportů,
- vlevo seznam networků bloku (číslo + titulek, jazyk mimo LAD), vpravo network jako v TIA,
- `snimek=1` je značka pro snímek (M1 ještě nemá paletu, kterou by skrýval), `tema` přepne motiv (jinak podle systému, výchozí tmavý).

## API (M1)
| GET | Vrací |
|---|---|
| `/api/exporty` | projekty, jejich exporty (ts, počet bloků) a bloky s nejnovějším ts |
| `/api/blok?projekt=&blok=[&ts=]` | druh, číslo, jazyk, `networky: [{nw, jazyk, titulek}]` |
| `/api/network?projekt=&blok=&nw=[&ts=][&sirka=px]` | model networku: `prvky`, `rungy`, `vodice`, `rozlozeni {id: {r, c}}`, `kresba` (geometrie pro SVG), `varovani` |

Prvek (`prvky[]`): `{id, typ, …}` — `typ` ∈ `kontakt`, `kontakt_nc`, `kontakt_p/n` (+`bit`), `porovnani` (`op`, `in1`, `in2`, `sablona`), `civka`, `civka_s/r/neg/p/n`, `ret`, `not`, `box` (`volani` fb|instrukce, `nazev`, `instance`, `sablona`, `napajeni` [EN,ENO]/[IN,Q]…, `vstupy`/`vystupy` `[{pin, operand}]`, P_TRIG `pod`). Pin s operandem `wire#wN` = přívod z jiného rungu. `rungy` = syrový tvar z `.s7dcl` (`{z, cesta, do}`), `vodice` = hrany grafu (`{z, do}`, `do: null` = otevřený konec).

`sirka` = šířka plátna v px; cívky se zarovnají k jeho pravému okraji (stránka posílá šířku okna).

## Jak si Claude bere stav a snímek
```
curl -s "http://localhost:8765/api/network?projekt=Demo_Linka&blok=FB_Dopravnik&nw=2"
python snimek.py --url "http://localhost:8765/?projekt=Demo_Linka&blok=FB_Dopravnik&nw=2&snimek=1" --out C:\…\nw2.png [--width 1400 --height 900] [--svetly]
```
`snimek.py` spustí `chrome.exe --headless=new --disable-gpu --window-size --screenshot --virtual-time-budget=4000 --force-dark-mode` s dočasným profilem (nekoliduje s otevřeným Chromem); záloha Edge. PNG pak Claude otevře nástrojem Read. Snímky zakázkových bloků **neukládat do repa**.

## Vzorky a testy
- `tests\vzorky\Demo_Linka\20261012-120000\PLC_1\Blocks\…` — vlastní, smyšlené (žádná zakázková data):
  `FB_Dopravnik` NW1 série · NW2 samodrž se 2 větvemi + NC · NW3 TON · NW4 volání FB `"DB_Pas1"` s piny (`:=`, `=>`, prázdný pin, pin napájený rungem `wire#w1`) · NW5 `NE_Contact`/`GT_Contact` se `S7_Templates` · NW6 MOVE · NW7 S/R cívky, výstupní větev, P_TRIG · NW8 prázdný; `FC_Vetve` vnořená větev, výstupní větve s boxem, FBD network, můstek (nerozložitelný), dva rungy s řetězem boxů; `FB_Pas` + `DB_Pas1` (dohledání typu FB z instance).
- `python -m unittest discover tests` — parser, rozložení (pozice prvků), API přes `http.client` proti serveru na volném portu, snímek (přeskočí se bez Chrome).
- `tests\snimky\` — snímky všech vzorků (tmavý motiv, 1300×520) + `FB_Dopravnik_NW4_svetly.png`.

## Gramatika `.s7dcl` (zjištěno na exportech V21, 12. 10. 2026)
- Network: `{ S7_Language := "LAD"; S7_NetworkTitle := "MLC_x"; S7_NetworkComment := "MLC_y" }` + `NETWORK … END_NETWORK`; atributy mohou chybět (`{ S7_Language := "LAD" }`). Texty v `.s7res` (`- id: MLC_x` / `en-US: …`, hodnoty i v `'…'` a blocích `|`, `|-`, `|+`).
- `RUNG wire#powerrail … END_RUNG` = rung od lišty s otevřeným koncem; prvky v rungu jdou sériově.
- `wire#wN` **uvnitř** rungu = pojmenovaný uzel na hlavní větvi. Jiný rung `RUNG wire#powerrail|wire#wK … END_RUNG wire#wN` = paralelní větev z uzlu (lišta / wK) do uzlu wN → **OR v LAD není prvek, je to víc rungů do téhož uzlu** (samodrž: `Start wire#w1 …` + `RUNG wire#powerrail Motor END_RUNG wire#w1`).
- `RUNG wire#wN … END_RUNG` (bez cíle) = výstupní větev (další cívky/boxy z uzlu wN).
- Víc `RUNG wire#powerrail … END_RUNG` v jednom networku = víc samostatných rungů (v TIA pod sebou od lišty).
- `pin := wire#wN` = vstup boxu napájený rungem, který končí `END_RUNG wire#wN` (ve všech exportech jen od lišty).
- `O( … )`, `A( … )` a `RUNG <operand>` se vyskytují **jen ve FBD** networkech; LAD je nepoužívá.
- `"RLO=1"` / `"RLO=0"` jsou **obyčejné globální tagy** (jsou v tabulce tagů), ne syntaxe napájení.
- Časovač/instrukce s instancí: `#inst.TON( pt := …, et => … )`, `"DB".TP(…)` — IN/Q jdou rungem, nejsou v argumentech; typ v `{ S7_Templates := "time_type := Time" }`.
- Volání FB: `"InstDB"( … )` / `#multiinstance( … )` — **název FB v exportu není**; doplní se z `DATA_BLOCK "InstDB" : "FB"` nebo z rozhraní bloku (`jmeno : Typ;`).
- Prázdný pin: `pin :=  ,` / `pin =>  `. Uvozovkovaná jména mohou obsahovat čárky (`"…,,,~…"`).

## Co M1 umí / neumí
- ✅ série, paralelní větve (i vnořené, rekurzivně), výstupní větve, víc rungů, boxy v hlavní větvi i ve výstupních větvích, řetězy boxů EN/ENO, přívody pinů od lišty, porovnání, P/N kontakty, S/R/negované cívky, RET, P_TRIG; cívky k pravému okraji; tmavý/světlý motiv.
- ✅ Ověřeno ad hoc (bez uložení čehokoli) na všech 5501 LAD networcích obou exportů: parser bez chyby, rozložení bez výjimky. Vizuálně kontrolovány jen vlastní vzorky.
- ⚠️ Nerozložitelné zapojení (můstek, přívod pinu odjinud než z lišty) → seznam rungů s varováním. FBD/SCL/STL → varování + text.
- ⚠️ Šířka textu je odhad (7,2 px/znak, Consolas 12 px); operand > 30 znaků se zkrátí „…" (plný v tooltipu). TIA dlouhé názvy zalamuje.
- ⚠️ Přívod pinu u boxu, který není v sérii od lišty, může křížit svislici výstupní větve (v exportech nenalezeno).
- ❌ Strom úprav, stavy, editace, katalog (`katalog.py` je stub) — M2/M3.
