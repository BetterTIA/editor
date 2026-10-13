# TIAC_Editor — ANET.md

Lokální prohlížeč a editor LAD networků (FBD od F1/F2: prohlížení, editor, kontrola, postup pro TIA) z exportu Readeru (`.s7dcl` + `.s7res`). Bez instalací: Python 3 stdlib, vanilla JS + SVG, headless Chrome na snímky.

## Spuštění
```
python server.py --port 8765 --export-root "C:\TIA\export" --data "data\tiac-editor"
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
**Úložiště:** `<--data>\<projekt>\stav.json` (např. `data\tiac-editor\Demo_Linka\stav.json`). Zapisuje **jen server** (zámek + dočasný soubor + `os.replace`); když server běží, soubor ručně neupravovat — použij `PUT /api/stav`.

Úprava: `{id (U001…, jedinečné přes projekty), projekt, kolo, blok, druh, cislo_bloku, nw, titulek, autor, cas, stav, poznamka, historie: [{stav, cas, kdo}], pred: {prvky, rungy, vodice, rozlozeni, export_ts}, po: {…}, kontroly: [{cas, shoda, export_ts, poznamka}]}`. Číslo bloku, titulek networku a `pred` doplní server z aktuálního exportu — nikdy se nezadávají ručně (co pošle klient, ignoruje se).

| Metoda | Cesta | Účel |
|---|---|---|
| GET | `/api/stav[?projekt=]` | celý `stav.json` projektu; bez projektu `{projekty: [{projekt, upravy}]}` |
| PUT | `/api/stav?projekt=` | přepíše celý soubor (kontrola struktury, id, stavů) |
| POST | `/api/uprava` | nová úprava `{projekt, kolo, blok, nw, po, poznamka}` → 201, stav Proposal, autor ANet |
| GET | `/api/uprava/<id>[?sirka=]` | úprava + kresba „po" se značkami `rozdil` (`pridany`/`zmeneny`, přízraky `x:<id>` = `smazany`), `dalsi`, `predchozi`, `signed_povolen` |
| POST | `/api/uprava/<id>/stav` | `{stav, kdo}` — jen o krok vpřed/zpět v Proposal → Approved → Signed → **Checked** → Uploaded → Tested (od M5); Signed volný, **Checked** jen když poslední kontrola je `shoda: true`, jinak **409** s důvodem |
| POST | `/api/kontrola` | `{uprava_id, shoda, export_ts, poznamka}` — výsledek porovnání exportu po zápisu s návrhem (ručně; automaticky viz M4) |

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

M3 změny M2: název kola se ve stromu slučuje bez ohledu na velikost písmen a okrajové mezery; Signed (od M5 **Checked**) odemyká **poslední** kontrola (pozdější neshoda zamkne; u úpravy už Checked+ se ukáže varování, stav se nevrací).

## M4 — kontrola „export po zápisu = návrh"
Po zápisu úpravy do projektu (ručně nebo Writerem) a exportu Readerem do export rootu serveru:
```
python kontrola.py --server http://localhost:8765 --uprava U003 [--export-ts 20261012-140000]
```
Exit **0 = shoda, 1 = neshoda, 2 = chyba** (server neběží, úprava / export / blok nenalezen). Vypíše jeden řádek `SHODA|NESHODA <id> · export <ts> · NW <n> · <poznámka>`. Výsledek se vždy zapíše k úpravě jako kontrola (stejně jako `POST /api/kontrola`), takže poslední kontrola se shodou odemkne Checked (do M5 Signed).

**Honza z UI:** u úpravy ve stavu Approved a dál tlačítko **„Zkontrolovat exportem"** (bere nejnovější export); výsledek se objeví v „Kontroly exportem", neshoda i nahoře jako varování.

**Server:** `POST /api/uprava/<id>/kontrola-exportem` `{export_ts?}` → `{shoda, export_ts, nw_export, poznamka, rozdily, uprava}`. Postup: nejnovější (nebo zadaný) export, který blok obsahuje → network podle **titulku** (posun čísla se zapíše do poznámky: „network se posunul: NW 1 → NW 2"), bez titulku podle čísla → porovnání s modelem „po" **podle obsahu** (`rozdil.shoda_obsahu`): prvky bez `id`, `volani`, `napajeni` a prázdných hodnot (prázdné piny se ignorují, `wire#wN` = `wire#`), jako multimnožina → čitelné „chybí v exportu / navíc v exportu / jiný operand / jiné piny"; když prvky sedí, porovná se topologie (strom série / větve z `layout.strom`, pořadí větví nehraje roli) → „liší se zapojení". Když je export tentýž jako „před", poznámka to řekne.

Instanční DB a volání FC se dohledávají v nejnovější kopii bloku do daného exportu (i když je export dílčí); `"FC"(…)` je teď `volani: fc` s názvem FC.

**Pravidlo LAD navíc:** paralelní úsek, jehož hlavní cesta je jen vodič (např. po smazání kontaktu) → „Větev bez prvku v hlavní cestě" u každé přemostěné větve.

## F1 — FBD (prohlížení + návrh přes API)
**Model** (`fbd.py`, vlastní, ne převod na LAD): `{jazyk: "FBD", prvky: [box], signaly: [{z, do, vstup}]}`.
- box = `{id, typ, vstupy: [{n, pin, operand, signal, neg}], vystupy: [{pin, operand, neg}], operand?, nazev?, instance?, sablona?, atributy?}`; `typ` ∈ `and or xor assign s r sr rs ptrig ntrig volani instrukce`.
- 1. vstup = vstup řetězu (`in1`, `CLK`, `S`/`R`, u volání/instrukce `EN`; F-blok bez napájení EN nemá). `signal` = id boxu, jehož hlavní výstup vstup napájí (operand je pak `''`); `neg` = kroužek negace.
- `operand` = operand nad boxem (`=`, S, R, SR/RS, bit paměti hrany); `volani`: `nazev` (typ FB / F-instrukce), `instance`, `atributy` (`f_user_card`…).
- Id z exportu `b1, b2…` (pořadí v textu), nové prvky `n1…`. `signaly` jsou odvozené — server je dopočítá ze vstupů, posílat stačí `prvky`.

**Gramatika FBD v `.s7dcl`** (V21): `RUNG <operand>|wire#wN|true` + prvky za sebou (výstup předchozího = 1. vstup dalšího): `A(in2 := …, in3 := …)` &, `O(…)` >=1, `X(…)` x (neověřeno exportem), `Coil(x)` =, `S_Coil`/`R_Coil`, `S_SR(r1 := …, operand := …)` (neověřeno), `P_Trig(pamet)`/`N_Trig`, `Move(…)`, volání `#inst.TYP(…)` / `"Inst"(…)` s atributy `{ f_user_card := "1"; … }`. `NOT` před prvkem = negovaný 1. vstup, `pin := NOT x` = negovaný vstup. `wire#wN` v rungu = odbočka za boxem, `END_RUNG wire#wN` = výstup rungu; `in2 := wire#wN` / `RUNG wire#wN` = napájení z nich. Víc `Coil` za sebou = řetěz přiřazení. `RUNG` + `END_RUNG` = prázdný network.

**Rozložení** (`fbd.rozloz`): strom podle hlavního spotřebiče (první v pořadí exportu); zdroje vlevo, přiřazení vpravo, výstup boxu v lince jeho 1. vstupu (řetěz = rovná čára), box napájející další vstup se položí na linku toho vstupu; další spotřebiče téhož výstupu = odbočka (svislice v kanálu sloupce, uzel). Box = obdélník s titulkem (`&`, `>=1`, `x`, `=`, `S`, `R`, `SR`, `P_TRIG`, název FB), operand / instance nad boxem, piny jen u SR/hran/volání/instrukcí. Cyklus nebo signál z neexistujícího boxu → `Nerozlozitelne` → stránka ukáže text networku s varováním.

**API pro FBD:**
| Metoda | Cesta | FBD |
|---|---|---|
| GET | `/api/network?…` | `jazyk: "FBD"`, `prvky`, `signaly`, `rozlozeni {id: {r, c}}`, `kresba {fbd: true, boxy, cary, uzly}` |
| POST | `/api/rozlozeni` | `{projekt, po: {jazyk: "FBD", prvky}, pred?, ops?}` → `po`, `kresba`, `rozdil`, `rozdil_piny`, `chyby` (nic neukládá) |
| POST | `/api/uprava` | `{projekt, kolo, blok, nw, po: {prvky}, poznamka}` — u FBD networku je `po` FBD model; číslo, titulek, `pred` doplní server |
| POST | `/api/uprava/<id>/po` | uloží FBD „po" (jen Proposal; jazyk návrhu se měnit nesmí → 400) |
| GET | `/api/uprava/<id>` | `jazyk: "FBD"`, kresba s rozdílem, `rozdil_piny {id: {vstupy: [n], vystupy: [pin], nad}}`, `chyby` (pravidla FBD) |

Operace FBD (`ops` v `/api/rozlozeni`): `{"typ":"pridej_vstup","id":"b2","operand":"#X"}` (jen &, >=1, x) · `{"typ":"zmen_operand","id":"b3","operand":"#Y"}` (operand nad boxem / instance) nebo s `"vstup": n` / `"vystup": "PIN"` (signál vstupu se nahradí operandem) · `{"typ":"negace","id":"b2","vstup":2}` (přepne).
```
# 1) network -> 2) zkouška operací -> 3) návrh (JSON v souborech, ať se shell nehádá s uvozovkami)
curl -s "http://localhost:8765/api/network?projekt=Demo_Linka&blok=FB_Bezpecnost&nw=1" > net.json
#   rozl.json: {"projekt":"Demo_Linka","pred":{"jazyk":"FBD","prvky":<net.prvky>},"po":{"jazyk":"FBD","prvky":<net.prvky>},
#               "ops":[{"typ":"pridej_vstup","id":"b2","operand":"#Zamek_OK"},{"typ":"zmen_operand","id":"b3","operand":"#Povoleni_Pohon"}]}
curl -s -X POST -H "Content-Type: application/json" --data-binary @rozl.json http://localhost:8765/api/rozlozeni > rozl_out.json
#   navrh.json: {"projekt":"Demo_Linka","kolo":"F1 demo","blok":"FB_Bezpecnost","nw":1,"poznamka":"…","po":<rozl_out.po>}
curl -s -X POST -H "Content-Type: application/json" --data-binary @navrh.json http://localhost:8765/api/uprava
```
Rozdíl: nový box zeleně, změněný oranžově — **u změněného boxu jsou oranžové jen změněné vstupy / výstupy / operand** (`rozdil_piny`), smazaný box červeně čárkovaně jako samostatný přízrak (visí na signálu, ze kterého byl napájen).

**Pravidla FBD (minimum F1, `fbd.validuj`):** chybí operand `=`/S/R/SR/hrany, chybí instance volání, nezapojený vstup &/>=1/x (1. vstup `=`/S/R se nehlásí — export má `RUNG` + `Coil(x)` bez vstupu, viz níže).

**Umí / neumí (F1):**
- ✅ prohlížení všech konstrukcí výše, rozdíl před/po, návrh přes API, operace přidat vstup / změnit operand / negace; vzorek `FB_Bezpecnost` (9 networků).
- ✅ Ad hoc (nic neuloženo) na všech FBD networcích obou exportů: 43 networků (jen jeden projekt, 2 bloky; druhý export FBD nemá) — parser 43/43, rozložení 43/43, 0 varování, 5 prázdných.
- ⚠️ `RUNG` bez operandu + `Coil(x)` (v exportu 2×) se kreslí jako `=` s otevřeným vstupem `...` — v TIA nejspíš „vždy 1"; neověřeno pohledem do TIA. `NOT A(…)` se čte jako negace 1. vstupu (ne výstupu předchozího boxu) — neověřeno.
- ⚠️ Signál vedený z výstupního pinu volání (`Q => wire#…`) model nezná (v exportech není); signál = vždy hlavní výstup boxu.
- ✅ Editor FBD, kontrola exportem, postup pro TIA a „zobrazit jako LAD" — viz F2.

Snímky F1: `tests\snimky\F1_a_bitova_logika.png` (NW1: >=1 → & s negovaným vstupem → =), `F1_b_volani_F_bloku.png` (NW4: F-blok s piny, negovaný pin, `ACK` z P_TRIG přes `wire#`), `F1_c_navrh_rozdil.png` (U001 založená přes API: & + 4. vstup, změněný operand přiřazení).

## F2 — editor FBD, kontrola exportem, postup pro TIA
**Operace FBD** (`fbd.proved`, `ops` v `POST /api/rozlozeni` — Claude i editor; nové boxy `n1…`):
| op | tvar |
|---|---|
| vložit box | `{"typ":"vloz_box","prvek":"AND","do":{"id":"b2","vstup":1}}` — box převezme signál/operand vstupu na svůj 1. vstup a výstupem ho napájí; `vstup` = počet+1 u &/>=1/x = nový vstup; bez `do` = nová logická cesta. Klíče palety FBD: `AND OR XOR ASSIGN S R SR RS P_TRIG N_TRIG`, z katalogu `MOVE`, `ADD`… a `fb:X` / `fc:X` |
| vložit volání | `{"typ":"vloz_volani","prvek":"fb:FB_Pas","instance":"\"DB_Pas1\"","do":…}` — v F-bloku (`f: true` v těle) bez EN |
| vstupy | `{"typ":"pridej_vstup","id":"b2","operand":"#X"}` · `{"typ":"odeber_vstup","id":"b2","vstup":3}` (jen &/>=1/x s víc než 2 vstupy) |
| negace | `{"typ":"negace","id":"b2","vstup":2}` · `"vystup":"Q"` (pin volání) · `"vystup":"hlavni"` (hlavní výstup boxu → `neg_vystup`) |
| operandy | `{"typ":"zmen_operand","id":"b3","operand":"#Y"}` (nad boxem / instance) nebo s `"vstup": n` · `{"typ":"zmen_pin","id":"b1","pin":"ACK","operand":"#Z","neg":false}` |
| smazat | `{"typ":"smaz","id":"b2"}` — vstupy, které box napájel, převezmou jeho 1. vstup (řetěz); volání/instrukce bez EN je nechá otevřené |
Odpověď `/api/rozlozeni` pro FBD nese i `mista` (`{x, y, do}` pro editor) a `chyby` (`varovani: true` = jen varování).

**Pravidla FBD** (`fbd.validuj`): chybí operand (=, S, R, SR/RS, bit hrany), chybí instance, nezapojený vstup bez operandu, box &/>=1/x/hrana bez výstupu nikam (varování); v **F-bloku** (`S7_Safety := "TRUE"` v hlavičce → `f` v úpravě, ANet může poslat `f: true` v `POST /api/uprava`): volání bloku, který není F (katalog `bloky[].f`), a zapojený EN = chyba.

**Editor FBD v UI** (`?uprava=<id>&upravit=1`, stejný rámec jako LAD): paleta (bitová logika, přiřazení, paměti, hrany, instrukce, bloky projektu s `F-`), místa vložení (kroužek na každém vstupu, pod posledním vstupem &/>=1/x = nový vstup, vlevo dole = nová logická cesta), klik na box nebo na jeho vstup → vlastnosti (operand s našeptáváním, NOT u vstupu/výstupu, operand nad boxem, instance), tlačítka **+ vstup / − vstup / Negovat vstup**, Delete, Ctrl+Z/Y, Esc, Uložit návrh. Z prohlížení FBD networku „Navrhnout úpravu" jako u LAD. Scénář pro snímek: `&ops=…&vyber=n1&vstup=2&paleta=AND`.

**Kontrola exportem FBD** (`fbd.shoda_obsahu`, tlačítko i `kontrola.py --uprava`): boxy podle obsahu (typ, operand, název, instance, vstupy operand/negace/„signál"; prázdné piny volání se ignorují), u **&, >=1, x pořadí vstupů nehraje roli**, u ostatních ano; když boxy sedí, porovná se zapojení (stromy od výstupů) → „boxy sedí, ale liší se zapojení signálů". Výstup jako u LAD (shoda / neshoda + poznámka, poslední shoda odemkne Checked).

**Postup pro TIA** (`postup.py`, LAD i FBD): `GET /api/uprava/<id>/postup` → `{hlavicka, kroky, markdown, zdroje}`; hlavička `FB30 "FB_Bezpecnost" · NW 1: <titulek>`; kroky česky s povely ověřenými v nápovědě V21 (nota §5b, tabulka `postup.POVELY` se zdrojem): **Insert input** `Ctrl+Shift+3`, **Create a ones complement** `Ctrl+Shift+4`, **Delete**, Replace přes trojúhelník (`Alt+F2`), `F9`/`F10`/`Shift+F7`, Empty box `F8`, **Call options** Single/Multi-instance, **Change instance**; ostatní slovy s ⟦neověřeno⟧ (gesto pin→pin, převzetí signálu při vložení, zrušení negace, LAD gesta). U F-bloku na konci „Přelož safety program a zkontroluj F-signaturu" (heslo F-CPU / Safety Administration). V UI panel „Postup pro TIA" pod kontrolami s tlačítkem **kopírovat** (markdown).
```
python kontrola.py --postup U003            # vytiskne markdown postupu (relace ho vloží do odpovědi Honzovi)
curl -s http://localhost:8765/api/uprava/U003/postup
```

**Zobrazit jako LAD** (jen ke čtení, rozhodnutí b): network výhradně z &, >=1, negací a = → `jako_lad {prvky, rungy, kresba}` v `/api/network`; tlačítko „Zobrazit jako LAD" (`&lad=1`). & = série, >=1 = paralelní větve, negovaný operand = rozpínací kontakt, negovaný výsledek = NOT; každé přiřazení je vlastní rung od lišty (sdílené části se opakují). Exporty: 27 ze 43 FBD networků.

**Vzorek F2:** `Demo_Linka\20261012-150000\` = export po zápisu (jen `FB_Bezpecnost`, NW1: & + `in4 := #Zamek_OK`, `Coil( #Povoleni_Pohon )`) — nejnovější export bloku; testy FBD jsou připnuté na `20261012-120000` (`TS0`), `TS2` = tento. Testy `tests\test_fbd_f2.py`. Snímky `F2_a_editor_fbd.png` (paleta, místa vložení, vložený P_TRIG), `F2_b_postup.png` (panel postupu), `F2_c_signed_fbd.png` (neshoda proti starému exportu, shoda proti novému, Signed).

**Skutečné kliky myší** (`tests	est_klikani.py`): headless Chrome přes DevTools protokol (mini WebSocket klient ze stdlib), `Input.dispatchMouseEvent` = důvěryhodná sekvence pointerdown/mousedown → pointerup/mouseup → click na `rect.hit` boxu, „+ vstup", položku palety a místo vložení — FBD i LAD editor. Pozor při živém testu přes rozšíření `claude-in-chrome`: když je karta skrytá (`document.visibilityState === "hidden"`, snímek obrazovky vyprší), **první** kliknutí po načtení stránky do stránky vůbec nedorazí (ověřeno i na prosté JSON stránce `/api/stav`) — druhé už ano. Není to chyba aplikace.

**Neumí (F2):** tažení boxu po plátně / přepojení signálu myší (jen vložení na vstup a smazání); `pin → wire#` z výstupního pinu volání; časovače/čítače ve FBD mají EN/ENO místo IN/Q; negace hlavního výstupu se nekanonizuje proti negaci vstupu následníka (dva zápisy téže logiky = neshoda).

## M5 — Upload (TIAC_Writer) a Check (TIAC_Reader), šest stavů
**Stavy (rozhodnutí Honzy 13. 10. 2026):** `Proposal → Approved → Signed → Checked → Uploaded → Tested`. **Signed** = zapsáno v projektu TIA (Honza ručně, nebo **Upload**), volný přechod. **Checked** = Reader exportem ověřil export = návrh — jen když **poslední** kontrola je shoda, jinak 409 (pravidlo, které do M4 hlídalo Signed; `checked_povolen` v `GET /api/uprava/<id>`). Uploaded / Tested odklikává Honza. Starší `stav.json` (pět stavů) se načte beze změny. Varování „poslední kontrola je NESHODA" se ukazuje od Checked dál.

**Upload** — `POST /api/uprava/<id>/upload` (jen **Approved** a ne-F úprava; F-blok, jiný stav, chybějící `tia`, běžící jiný Upload/Check → **409**). Server spustí TIAC_Writer (MCP přes stdio, `mcp_klient.py`) a provede:
1. `Connect {}` (bez `portalId`; víc instancí TIA = chyba → „TIA zaneprázdněné…", nic dalšího se nedělá),
2. `ResolveObjectPath {softwarePath, name: blok, kind: "block"}` → `groupPath` (přeskočí se, když je `tia.groupPath`),
3. `ExportBlocksAsDocuments {softwarePath, exportPath: <temp>, regexName: "^blok$", preservePath: false}`,
4. náhrada networku v `.s7dcl` (`s7dcl_zapis.py`): network podle **titulku** (jinak čísla), pojistky: blok s `S7_Safety` se nepřepíše, jazyk networku musí sedět, text po náhradě se znovu přečte a musí obsahem odpovídat návrhu (jinak stop před importem); `.s7res` se nemění, BOM a CRLF se zachovají,
5. `PreviewImport {softwarePath, importPath, kind: "block", groupPath}` (text se uloží do záznamu `preview`),
6. `ImportFromDocuments {softwarePath, groupPath, importPath, fileNameWithoutExtension: blok, importOption: "Override"}`.

Výsledek → `u['zapisy']` = `[{cas, nastroj: "TIAC_Writer", kroky: [{krok, ok, zprava}], ok, zprava, preview?}]`; při úspěchu Signed s `kdo: "TIAC_Writer"`. Odpověď `200 {ok, zprava, zapis, uprava}` — i při chybě spojení (`ok: false`, zpráva „TIA zaneprázdněné nebo Writer/Reader nejde spustit: …", stav se nemění). **SaveProject Writer nevolá** — změna je v otevřeném projektu TIA, uložení je na Honzovi.

**Check** — `POST /api/uprava/<id>/check` (jen **Signed**, `tia` povinné, jinak 409). TIAC_Reader `export_to_folder {pathPrefixes: ["<PLC>/Blocks/<Skupina>/<blok>"], include: ["blocks"], formats: ["documents"], plcName}` (PLC a skupina z dosavadního exportu bloku v export rootu editoru, `tia.groupPath` je přebije; `complete: false` → opakuje s `runId` + `nextOffset`). Nová složka exportu = časové razítko z odpovědi, které v export rootu přibylo, jinak nejnovější nová složka projektu po volání. Pak dnešní kontrola exportem (`kontrola-exportem` nad tím exportem) → shoda = **Checked** (`kdo: "TIAC_Reader"`), neshoda = zůstane Signed, kontrola se zapíše jako neshoda. Záznam i do `u['zapisy']` (`nastroj: "TIAC_Reader"`). **Reader musí exportovat do stejného export rootu, nad kterým běží editor** (`--export-root` = `--export-root` Readeru).
```
python kontrola.py --check U003      # CHECKED|NESHODA|CHYBA <id> · stav … · export <ts> · zpráva ; exit 0 / 1 / 2
```
**Zámek:** v jednu chvíli jeden Upload / Check (druhý dostane 409). Timeout jednoho volání nástroje `--mcp-timeout` (300 s).

**Nastavení TIA per projekt** (v `stav.json` vedle `upravy`): `"tia": {"softwarePath": "PLC_1", "plcName": "PLC_1", "groupPath": "Dopravnik"}` (`groupPath` volitelné — jinak ho dohledá Writer). Nastavuje se `PUT /api/stav?projekt=` nebo v UI rozbalovacím „Nastavení TIA projektu" u úpravy. Bez něj jsou tlačítka zašedlá a API vrací 409 s nápovědou.

**Odkud server bere Writer a Reader:** `--mcp-json` (výchozí `.mcp.json`, jen se čte) — klíče `TIAC_Writer` a `tia-portal` (příkaz, argumenty, `env`); přebíjí `--writer-cmd "…"` / `--reader-cmd "…"` (celý příkaz jako text, cesty s mezerami v uvozovkách).

**UI:** v hlavičce úpravy **Upload →** (Approved, ne-F; u F-bloku místo něj „F-blok: zapisuje Honza podle Postupu pro TIA"), **Check →** (Signed); během běhu jsou tlačítka zamčená a vedle je text průběhu; výsledek v panelu „Upload / Check (Writer, Reader)" s kroky (chybný krok červeně se zprávou), chyba nahoře jako varování (stránka nepadá). Řádek „Stavy: …" ukazuje šest stavů s aktuálním zvýrazněným.

**Falešné servery (testy a ukázka, bez TIA):** `tests\falesny_mcp.py writer|reader` — MCP přes stdio, umí jen nástroje výše. „Projekt TIA" = složka `--stav` (výchozí `%TEMP%\tiac_falesny_tia`): Writer exportuje blok odtud (před prvním importem z `--vzorek`, výchozí vzorky `20261012-120000`), import zapíše dokumenty do `--stav` a řádek do `<stav>\importy.jsonl`; Reader `--export-root <root> --projekt <p>` vytvoří novou složku `<root>\<p>\<RRRRMMDD-HHMMSS>\<PLC>\Blocks\…` s blokem z `--stav`. `--vice-instanci` = Connect vrátí chybu, `--pomalu S` = zpoždění odpovědí.
```
rem ukázka: export root = KOPIE vzorků (falešný Reader do něj přidává složky exportu)
robocopy tests\vzorky data\tiac-editor-export-demo /E
python server.py --port 8765 --export-root "data\tiac-editor-export-demo" --data "data\tiac-editor" --writer-cmd "python tests\falesny_mcp.py writer" --reader-cmd "python tests\falesny_mcp.py reader --export-root data\tiac-editor-export-demo --projekt Demo_Linka"
```
⚠ Falešný Reader zapisuje nové složky exportu do svého `--export-root`; kdyby to byl `tests\vzorky`, přibyl by tam nejnovější export bloku a testy (`TS1`/`TS2` = nejnovější export) by padaly — proto kopie vzorků mimo repo.

**Serializace** (`s7dcl_zapis.py`): `text_networku(model)` → tělo networku (LAD: rungy 1:1, `wire#wN`, `Contact`/`I_Contact`/`P_Contact`, `XX_Contact` + `S7_Templates`, cívky, `NOT Coil`, volání `"Inst"(…)` / `#inst.TON(…)` / `"FC"(…)` / instrukce `Move(…)` s piny malými písmeny; FBD: řetěz boxů podle 1. vstupu, odbočky `wire#wN` v rungu nebo `END_RUNG wire#wN`, `NOT` před boxem = negovaný 1. vstup, `pin := NOT x`, atributy volání F-bloku). `nahrad_network(text, n, novy_text)` vymění jen tělo N-tého `NETWORK … END_NETWORK`. Test: `tests\test_s7dcl_zapis.py` — `parse(serialize(parse(x)))` = shoda obsahu na **všech** networcích vzorků (LAD i FBD) a nahrazení networku sebou samým dá blok, který parser čte stejně (FB_Dopravnik NW4 znak po znaku).

**Testy M5:** `tests\test_m5.py` (Upload F-bloku 409, bez `tia` 409, Upload OK → Signed + importovaný `.s7dcl` obsahuje network „po", Check → Checked, neshoda → zůstane Signed + Checked 409, CLI `--check`, „TIA zaneprázdněné", nespustitelný Writer, zámek, FBD ne-safety network), `tests\test_mcp_klient.py` (handshake, isError, nespustitelný příkaz, timeout, konec procesu), `tests\test_stav.py` (šest stavů, starý `stav.json`). Snímky `tests\snimky\M5_a_bez_nastaveni_tia.png`, `M5_b_approved_upload.png`, `M5_c_f_blok_bez_uploadu.png`, `M5_d_signed_check.png`, `M5_e_checked.png`.

**⟦Neověřeno proti živé TIA⟧ (vše výše běží jen proti falešným serverům):**
- tvar odpovědí Readeru `export_to_folder` (klíče složky, `complete`, `runId`, `nextOffset`) — parsuje se tolerantně, záloha = nová složka v export rootu;
- tvar odpovědi `ResolveObjectPath` (JSON `path` / prostý text) a zda `ExportBlocksAsDocuments` dá soubory přímo do `exportPath`;
- chování `ImportFromDocuments` Override u existujícího bloku (instance DB, rozhraní, čísla networků, komentáře), zda TIA po importu network přečísluje nebo změní titulky `MLC_x`;
- kódování `.s7dcl` z Writeru (BOM, CRLF) — obojí se zachovává tak, jak přišlo;
- serializace: pořadí pinů volání (vstupy, pak výstupy), klíče `S7_Templates` mimo porovnání a TON (`SrcType`/`DestType` odhad), tvar `P_Contact`/`N_Contact`, `ReturnTrue()`, negovaný výstup volání `Q => NOT x`, prázdný FBD rung bez operandu;
- F-bloky Upload nemají (409 + pojistka `S7_Safety` v exportu) — F-signaturu nic z M5 nemění;
- `Connect` bez `portalId` může TIA **spustit**, když žádná instance neběží (popis nástroje Writeru).

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
- Vzorek M4: `Demo_Linka\20261012-140000\` = dílčí „export po zápisu" jen s `FB_Dopravnik` (NW1 má navíc `Contact( "Pojistka_OK" )` před cívkou) — je to **nejnovější** export bloku; testy prohlížeče a rozložení jsou připnuté na `20261012-120000` (`tests\spolecne.py` `TS0`). Varianty (jiný operand, posunutý network) vznikají v testu v dočasném export rootu.
- M4 snímky: `M4_a_kontroly_signed_odemceno.png` (neshoda + shoda, Signed odemčené), `M4_b_signed.png`.
- Vzorek F1: `20261012-120000\PLC_1\Blocks\Bezpecnost\FB_Bezpecnost.s7dcl` (F-FB, `S7_Safety`): NW1 >=1 + & s negací + =, NW2 & s `wire#` vstupem z jiného rungu, NW3 odbočka za & + řetěz přiřazení + `NOT Coil`, NW4 F-blok `#NouzStop.ESTOP1` s atributy, negovaným pinem a `ACK` z `P_Trig`, NW5 F-blok s negovanými `QBAD`, NW6 S/R/SR, NW7 `RUNG true` + MOVE, NW8 x + N_TRIG, NW9 prázdný. Testy `tests	est_fbd.py`.

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
- ⚠️ Nerozložitelné zapojení (můstek, přívod pinu odjinud než z lišty) → seznam rungů s varováním. SCL/STL → varování + text. FBD viz F1.
- ⚠️ Šířka textu je odhad (7,2 px/znak, Consolas 12 px); operand > 30 znaků se zkrátí „…" (plný v tooltipu). TIA dlouhé názvy zalamuje.
- ⚠️ Přívod pinu u boxu, který není v sérii od lišty, může křížit svislici výstupní větve (v exportech nenalezeno).
- ✅ M2: strom úprav, stavy, kontroly, rozdíl před/po (viz výše). Prázdný network má u lišty popisek „(prázdný network)“.
- ✅ M3: editor (paleta, vložení klikem i přetažením, větev, smazání, operandy/piny s našeptáváním, Undo/Redo, Esc), katalog z exportu, pravidla LAD živě.
- ⚠️ Katalog čte jen `.s7dcl` (globální DB jako `.db` v SCL tvaru ne), členy DB jen nejvyšší úrovně; FC s návratovou hodnotou se nabízí bez `Ret_Val`.
- ❌ Přesun prvku tažením po plátně (jen smazat + vložit), ruční posun rozložení.
