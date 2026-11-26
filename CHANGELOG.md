# Changelog — BTIA Editor

## Unreleased

- HMI Upload deletes and creates screen items and sets new dynamizations, visibility and fonts (`DeleteHmiScreenItems`, `CreateHmiScreenItems`, `SetHmiItemDynamization`, `SetHmiDynamizationTag`, `SetHmiItemAttribute`); every Writer call runs as a dry run before the first write, and in a group the HMI dry runs run before the PLC part writes anything. Still by hand: a new screen, a changed type, a removed binding, color or font (D5); a dry run that warns of a tag that is no HMI tag refuses too, and a batch call that skipped an item counts as written.
- LAD rule "box inside a parallel branch of contacts" no longer fires for a box that TIA already holds there unchanged (same element in the "before" model); new or changed boxes are still reported (D9).
- HMI export: a rich-text line break (`<br/>`, `<br />`, `<br>`) is read as `\n` instead of being dropped; the export check also reads `<br/>` in a proposal as `\n`, so a proposal text with line breaks checks equal (D7b).
- Signed → Checked by hand with a reason, also when the last automatic check is a MISMATCH: `POST /api/edits/<id>/state {state: "Checked", reason}`, MCP `set_state(..., reason)`, UI "Record a check by hand… → ✓ Match → Checked"; recorded as a matching check `by hand: <reason>` and in the history (D20c).
- Retention of Tested edits: `older_than_days` filter of `POST /api/cleanup` / MCP `cleanup_edits`, and the server flag `--tested-retention-days N` (default off) that deletes Tested edits N days after Tested at start and every hour, with the backup and tombstones of a cleanup (D6).
- Project version at Uploaded and Tested: Uploaded records `V<1>.<2>.<3>` (proposed: 3rd number + 1, or 2nd + 1; always passed explicitly, never guessed), Tested records its time and carries the same version. `version` parameter of `POST /api/edits|groups/<id>/state` and MCP `set_state`; shown in the edit, the tree, the group page and the changelog; returned by `get_edit`, `list_edits`, `get_changelog`.
- SimaticML Upload: parts in the order of the current flow and the power rail's connections in the same walk, as TIA exports them (D13, D14, D20a); every unconnected FB/FC call parameter gets an `OpenCon`, an FC's `Ret_Val` as an output (D10a, D2); INC is written (`Part Inc` with `DestType`, D3).

### Upload safety
- A group Upload is refused before the first write when Upload cannot make one of its members (an HMI change the Writer
  has no tool for, what the SimaticML way does not write); after the first write nothing raises — every written member is
  recorded. HMI members are written after the PLC members, only when those succeeded.
- `SaveProject` once per Upload, right before the first import, so a TIA crash during the Upload loses nothing (TIA
  setting `save: "no"` / UI *save the project before Upload* turns it off).
- A failure after the first import first checks that the Writer and the TIA Portal instance are still there; if not, the
  result says "TIA PORTAL MAY HAVE CRASHED" and nothing is restored.
- When `CompileBlock` fails or leaves the block inconsistent, `CompileSoftware` follows — only for a PLC without
  F-blocks (Openness V21 has no rebuild all; what it does not fix needs Rebuild all in TIA). The same after a restore; a block that cannot be restored or does not compile is reported as
  "BLOCK INCONSISTENT — DO NOT SAVE THE PROJECT", and further Uploads are refused until the operator confirms *TIA is fixed* (`POST /api/state/tia/fixed`).
- Upload without compile (`{"compile": false}`, UI *Upload without compile*): through SimaticML, no compile, the edits go
  Signed "waiting for Rebuild all".
- Documents Upload of a group: one export, one import and one compile per block (was one per member), and a restore of
  the block from before the write when anything fails after the import.
- What the SimaticML way cannot write (e.g. PID_Compact / PID_Temp calls, RET) is a warning of the edit at propose time.
