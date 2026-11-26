# Changelog — BTIA Editor

## Unreleased

- LAD rule "box inside a parallel branch of contacts" no longer fires for a box that TIA already holds there unchanged (same element in the "before" model); new or changed boxes are still reported (D9).
- HMI export: a rich-text line break (`<br/>`, `<br />`, `<br>`) is read as `\n` instead of being dropped; the export check also reads `<br/>` in a proposal as `\n`, so a proposal text with line breaks checks equal (D7b).
- Signed → Checked by hand with a reason, also when the last automatic check is a MISMATCH: `POST /api/edits/<id>/state {state: "Checked", reason}`, MCP `set_state(..., reason)`, UI "Record a check by hand… → ✓ Match → Checked"; recorded as a matching check `by hand: <reason>` and in the history (D20c).
- Retention of Tested edits: `older_than_days` filter of `POST /api/cleanup` / MCP `cleanup_edits`, and the server flag `--tested-retention-days N` (default off) that deletes Tested edits N days after Tested at start and every hour, with the backup and tombstones of a cleanup (D6).
- Project version at Uploaded and Tested: Uploaded records `V<1>.<2>.<3>` (proposed: 3rd number + 1, or 2nd + 1; always passed explicitly, never guessed), Tested records its time and carries the same version. `version` parameter of `POST /api/edits|groups/<id>/state` and MCP `set_state`; shown in the edit, the tree, the group page and the changelog; returned by `get_edit`, `list_edits`, `get_changelog`.
