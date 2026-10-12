# LADEditor — ANET.md

Lokální prohlížeč a editor LAD networků z exportu Readeru (`.s7dcl` + `.s7res`). Bez instalací: Python 3 stdlib, vanilla JS + SVG, headless Chrome na snímky.

## Spuštění
```
python server.py --port 8765 --export-root "C:\TIA\export" --data "data\ladeditor"
```
- `--export-root` = složka `<projekt>\<timestamp YYYYMMDD-HHMMSS>\<PLC>\Blocks\…\<blok>.s7dcl`. Blok se hledá podle jména souboru v **nejnovějším** exportu, který ho obsahuje (dílčí exporty nevadí).
- `--data` (povinné) = složka dat úprav, `<data>\<projekt>\stav.json` (viz M2).
- Nad vlastními vzorky: `--export-root tests\vzorky`.
- Server poslouchá jen na `127.0.0.1`.

## Stránka
`http://localhost:8765/?projekt=<projekt>&blok=<blok>&nw=<n>[&snimek=1][&tema=svetly|tmavy]`
- bez `projekt`/`blok` = výběr bloků ze všech exportů,
- vlevo seznam networků bloku (číslo + titulek, jazyk mimo LAD), vpravo network jako v TIA,
- `?uprava=<id>` otevře úpravu (M2), `?uprava=<id>&upravit=1` editor (M3); `snimek=1` je značka pro snímek, `tema` přepne motiv (jinak podle systému, výchozí tmavý).

## API — prohlížení exportu (M1)
| GET | Vrací |
|---|---|
| `/api/exporty` | projekty, jejich exporty (ts, počet bloků) a bloky s nejnovějším ts |
| `/api/blok?projekt=&blok=[&ts=]` | druh, číslo, jazyk, `networky: [{nw, jazyk, titulek}]` |
| `/api/network?projekt=&blok=&nw=[&ts=][&sirka=px]` | model networku: `prvky`, `rungy`, `vodice`, `rozlozeni {id: {r, c}}`, `kresba` (geometrie pro SVG), `varovani` |

Prvek (`prvky[]`): `{id, typ, …}` — `typ` ∈ `kontakt`, `kontakt_nc`, `kontakt_p/n` (+`bit`), `porovnani` (`op`, `in1`, `in2`, `sablona`), `civka`, `civka_s/r/neg/p/n`, `ret`, `not`, `box` (`volani` fb|instrukce, `nazev`, `instance`, `sablona`, `napajeni` [EN,ENO]/[IN,Q]…, `vstupy`/`vystupy` `[{pin, operand}]`, P_TRIG `pod`). Pin s operandem `wire#wN` = přívod z jiného rungu. `rungy` = syrový tvar z `.s7dcl` (`{z, cesta, do}`), `vodice` = hrany grafu (`{z, do}`, `do: null` = otevřený konec).

`sirka` = šířka plátna v px; cívky se zarovnají k jeho pravému okraji (stránka posílá šířku okna).

## M2 — úpravy a stavy
**Úložiště:** `<--data>\<projekt>\stav.json` (např. `data\ladeditor\Demo_Linka\stav.json`). Zapisuje **jen server** (zámek + dočasný soubor + `os.replace`); když server běží, soubor ručně neupravovat — použij `PUT /api/stav`.

Úprava: `{id (U001…, jedinečné přes projekty), projekt, kolo, blok, druh, cislo_bloku, nw, titulek, autor, cas, stav, poznamka, historie: [{stav, cas, kdo}], pred: {prvky, rungy, vodice, rozlozeni, export_ts}, po: {…}, kontroly: [{cas, shoda, export_ts, poznamka}]}`. Číslo bloku, titulek networku a `pred` doplní server z aktuálního exportu — nikdy se nezadávají ručně (co pošle klient, ignoruje se).

| Metoda | Cesta | Účel |
|---|---|---|
| GET | `/api/stav[?projekt=]` | celý `stav.json` projektu; bez projektu `{projekty: [{projekt, upravy}]}` |
| PUT | `/api/stav?projekt=` | přepíše celý soubor (kontrola struktury, id, stavů) |
| POST | `/api/uprava` | nová úprava `{projekt, kolo, blok, nw, po, poznamka}` → 201, stav Proposal, autor ANet |
| GET | `/api/uprava/<id>[?sirka=]` | úprava + kresba „po" se značkami `rozdil` (`pridany`/`zmeneny`, přízraky `x:<id>` = `smazany`), `dalsi`, `predchozi`, `signed_povolen` |
| POST | `/api/uprava/<id>/stav` | `{stav, kdo}` — jen o krok vpřed/zpět v Proposal → Approved → Signed → Uploaded → Tested; Signed jen s kontrolou `shoda: true`, jinak **409** s důvodem |
| POST | `/api/kontrola` | `{uprava_id, shoda, export_ts, poznamka}` — výsledek porovnání exportu po zápisu s návrhem |

Model `po` má stejný tvar jako `/api/network` (`prvky` + `rungy`; `vodice` a `rozlozeni` dopočítá server). **Prvky se párují podle `id`**: zachovaný prvek nech s původním id, nový dostane nové (např. `n1`), smazaný vyhoď z `prvky` i `rungy`. Změna operandu/pinů = oranžová, nový prvek = zelený, smazaný = červený čárkovaný přízrak na původním místě (když pro něj v „po" není místo, vypíše se jako varování).

```
# návrh (JSON v souboru, ať se shell nehádá s uvozovkami)
curl -s -X POST -H "Content-Type: application/json" --data-binary @navrh.json http://localhost:8765/api/uprava
#   navrh.json: {"projekt":"Demo_Linka","kolo":"Kolo 1","blok":"FB_Dopravnik","nw":2,"poznamka":"…","po":{"prvky":[…],"rungy":[…]}}
#   základ „po" = výstup GET /api/network (zkopírovat prvky + rungy a upravit)
# kontrola exportem po zápisu
curl -s -X POST -H "Content-Type: application/json" -d '{"uprava_id":"U001","shoda":true,"export_ts":"20261012-130000","poznamka":"export = návrh"}' http://localhost:8765/api/kontrola
# změna stavu (UI Honzy volá totéž)
curl -s -X POST -H "Content-Type: application/json" -d '{"stav":"Approved","kdo":"Honza"}' http://localhost:8765/api/uprava/U001/stav
```
Stránka `?uprava=U001`: vlevo strom `projekt → kolo → blok → NW → úprava (stav barevně)` nad seznamem networků bloku, vpravo hlavička úpravy, tlačítka „← předchozí / další →" (Signed zašedlé s tooltipem, dokud není kontrola se shodou), kontroly, legenda a plátno s rozdílem.

## M3 — editor, katalog, pravidla LAD
**Editor:** u úpravy ve stavu Proposal tlačítko „Upravit" (`?uprava=<id>&upravit=1`); z prohlížení networku „Navrhnout úpravu" (založí Proposal s `po = pred` v zadaném kole a otevře editor). Vpravo **paleta** (instrukce + bloky projektu, hledání). Klik na položku palety zvýrazní **platná místa vložení** (kroužky na vodičích před prvky a za posledním prvkem úseku) → klik na místo, nebo položku **přetáhni** na vodič (přichytí se k nejbližšímu platnému místu do 45 px). **Větev**: tlačítko → klik na začátek a konec úseku v jednom rungu; prvek = vybraný v paletě, jinak spínací kontakt; konec za posledním prvkem otevřeného rungu = výstupní větev. Klik na prvek = výběr + panel vlastností (operand / tabulka pinů, instance, typ) s našeptáváním; **Delete** smaže, **Ctrl+Z / Ctrl+Y** (i Ctrl+Shift+Z), **Esc** zruší vkládání/větev/výběr. Po každé změně server model přepočítá (rozložení + rozdíl proti „před" + chyby). „Uložit návrh" = `POST /api/uprava/<id>/po` (i s chybami; hlavička úpravy je pak označí „⚠ N chyb LAD").

**Pravidla LAD** (`model.validuj`, pod plátnem + oranžový rámeček u prvku): box (FB/FC/instrukce) uvnitř zavřené paralelní větve; cívka/RET jinde než na konci rungu; chybějící operand, bit hrany, instance, vstup instrukce (PT, IN…; u FB jsou vstupy volitelné); porovnání bez obou operandů; nenapájený rung (začíná v uzlu, do kterého nic nevede); nerozložitelné zapojení.

**Katalog** `GET /api/katalog?projekt=` → `{instrukce: [{klic, nazev, skupina, prvek}], bloky: [{klic 'fb:X'|'fc:X', nazev, druh, cislo, rozhrani, instance: ['"DB"'], prvek}], db: [{nazev, instance_typ, cleny}], tagy: [{jmeno, typ, adresa, tabulka}]}`. `prvek` = šablona ve tvaru modelu (bez `id`). Klíče instrukcí: `NO NC P N C S R NEG RET EQ NE GT GE LT LE TON TOF TP CTU CTD MOVE SWAP CONV ADD SUB MUL DIV IN_RANGE P_TRIG N_TRIG`. Zdroj: nejnovější `.s7dcl` každého bloku (rozhraní `VAR_INPUT/VAR_OUTPUT/VAR_IN_OUT`, IN_OUT jako vstupy), `DATA_BLOCK "X" : "FB"` = instance, tag tabulky `<PLC>\Tags\**\*.xml` (SimaticML `SW.Tags.PlcTag`).

**Operace a zkouška rozložení (Claude, nic neukládá):** `POST /api/rozlozeni` `{projekt, po, pred?, ops?, sirka?}` → `{po, kresba, rozlozeni, rozdil, mista, chyby, varovani, posledni}`. Operace (`ops` se provedou postupně):
| op | tvar |
|---|---|
| vložit do série | `{"typ":"vloz","misto":{"r":0,"k":2},"prvek":"TON"}` — `r` = index rungu, `k` = pozice v `cesta` (před položku k; `k = len` = konec) |
| větev | `{"typ":"vetev","od":{"r":0,"k":1},"do":{"r":0,"k":2},"prvek":"fb:FB_Pas"}` |
| smazat | `{"typ":"smaz","id":"p3"}` (u boxu i rungy přívodů jeho pinů) |
| změnit | `{"typ":"zmen","id":"n1","hodnoty":{"operand":"\"Kvitace\""}}` — pole `operand bit in1 in2 op sablona instance pod vstupy vystupy` |
`prvek` je klíč palety nebo celý prvek (dict). Nové prvky dostávají `n1, n2…`, nové uzly `wire#wN` (rozdíl páruje podle id). Typický postup: `GET /api/network` → `POST /api/rozlozeni` s `po = {prvky, rungy}` a `ops` → zkontrolovat `chyby` → `POST /api/uprava` (nová) nebo `POST /api/uprava/<id>/po` (Proposal) s vráceným `po`.

**Scénář v URL** (snímky, headless nemá myš): `?uprava=<id>&upravit=1&ops=<URL-kódovaný JSON seznam operací>&vyber=<id>&paleta=<klíč>&vetev=<r>,<k>` — operace se provedou jako v UI (bez uložení). Snímky `tests\snimky\M3_a_paleta_mista.png` (`&paleta=fb:FB_Pas`), `M3_b_FB_Pas_vetev.png` (výstupní větev s FB_Pas a piny), `M3_c_box_ve_vetvi.png` (chyba „box v paralelní větvi").

M3 změny M2: název kola se ve stromu slučuje bez ohledu na velikost písmen a okrajové mezery; Signed odemyká **poslední** kontrola (pozdější neshoda zamkne; u úpravy už Signed+ se ukáže varování, stav se nevrací).

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
- `tests\snimky\` — snímky všech vzorků (tmavý motiv, 1300×520) + `FB_Dopravnik_NW4_svetly.png`; M2: `M2_U001_Proposal.png`, `M2_U001_Approved.png`; M3: `M3_a…c` (1500×820).
- Vzorky M3: `DBs\DB_Stroj.s7dcl` (globální DB se STRUCT), `PLC_1\Tags\Default tag table.xml` (tagy pro našeptávání).

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

## Co umí / neumí
- ✅ série, paralelní větve (i vnořené, rekurzivně), výstupní větve, víc rungů, boxy v hlavní větvi i ve výstupních větvích, řetězy boxů EN/ENO, přívody pinů od lišty, porovnání, P/N kontakty, S/R/negované cívky, RET, P_TRIG; cívky k pravému okraji; tmavý/světlý motiv.
- ✅ Ověřeno ad hoc (bez uložení čehokoli) na všech 5501 LAD networcích obou exportů: parser bez chyby, rozložení bez výjimky. Vizuálně kontrolovány jen vlastní vzorky.
- ⚠️ Nerozložitelné zapojení (můstek, přívod pinu odjinud než z lišty) → seznam rungů s varováním. FBD/SCL/STL → varování + text.
- ⚠️ Šířka textu je odhad (7,2 px/znak, Consolas 12 px); operand > 30 znaků se zkrátí „…" (plný v tooltipu). TIA dlouhé názvy zalamuje.
- ⚠️ Přívod pinu u boxu, který není v sérii od lišty, může křížit svislici výstupní větve (v exportech nenalezeno).
- ✅ M2: strom úprav, stavy, kontroly, rozdíl před/po (viz výše). Prázdný network má u lišty popisek „(prázdný network)“.
- ✅ M3: editor (paleta, vložení klikem i přetažením, větev, smazání, operandy/piny s našeptáváním, Undo/Redo, Esc), katalog z exportu, pravidla LAD živě.
- ⚠️ Katalog čte jen `.s7dcl` (globální DB jako `.db` v SCL tvaru ne), členy DB jen nejvyšší úrovně; FC s návratovou hodnotou se nabízí bez `Ret_Val`.
- ❌ Přesun prvku tažením po plátně (jen smazat + vložit), ruční posun rozložení.
