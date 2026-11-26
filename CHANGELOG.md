# Changelog — BTIA Editor

## Unreleased

- Project version at Uploaded and Tested: Uploaded records `V<1>.<2>.<3>` (proposed: 3rd number + 1, or 2nd + 1; always passed explicitly, never guessed), Tested records its time and carries the same version. `version` parameter of `POST /api/edits|groups/<id>/state` and MCP `set_state`; shown in the edit, the tree, the group page and the changelog; returned by `get_edit`, `list_edits`, `get_changelog`.

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
  "BLOCK INCONSISTENT — DO NOT SAVE THE PROJECT", and further Uploads are refused until the TIA settings are saved again.
- Upload without compile (`{"compile": false}`, UI *Upload without compile*): through SimaticML, no compile, the edits go
  Signed "waiting for Rebuild all".
- Documents Upload of a group: one export, one import and one compile per block (was one per member), and a restore of
  the block from before the write when anything fails after the import.
- What the SimaticML way cannot write (e.g. PID_Compact / PID_Temp calls, RET) is a warning of the edit at propose time.
