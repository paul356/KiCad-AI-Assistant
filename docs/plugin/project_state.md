# Plugin Project State

## 1. Purpose

The plugin runs in KiCad's embedded interpreter; the MCP server runs in a
separate subprocess. Both need to agree on *which project is open* for path
trust, session ownership, context prompts and pre-edit snapshots. Exactly one
file carries that answer — there is no second probe and no window-title
fallback. The panel keeps the last record it published only for change
detection and the session-stamp fallback described below.

## 2. Location

```
<KiCad user config>/<version>/kcaa/project_state.json
```

The path is also exported to the server subprocess as
`KCAA_PROJECT_STATE_FILE`. A standalone MCP server (no such variable) never
sees a project state and behaves as before.

## 3. Record

```json
{
  "version": 2,
  "written_at": "2026-11-17T09:12:33Z",
  "project_root": "/abs/project/dir",
  "profile_file": "name.kicad_pro",
  "pcb_files": ["hardware/main.kicad_pcb"],
  "schematic_files": ["name.kicad_sch", "hardware/main_bus.kicad_sch"]
}
```

Rules:

- `project_root` is absolute; every other path is relative to it and uses
  `/` separators.
- The writer never emits `null` and never writes an empty string. When no
  valid record can be assembled, nothing is written and the previous file is
  left untouched.
- A record is invalid when it is `null`, `version` is not 2, a required field
  is missing or has the wrong type, `project_root` is not absolute, or
  `profile_file` does not exist or resolves outside the root.
- Unknown extra fields are ignored.
- Manifest entries are validated one by one: an entry that no longer exists
  or escapes the root is dropped; the rest of the record survives.

## 4. File discovery

Starting at `project_root`, the plugin recursively collects `*.kicad_pcb` and
`*.kicad_sch` (case-insensitive). It skips `.versions/`,
`<name>-backups/`, directories whose name starts with `.`, and symlinked
directories. Paths are relative, de-duplicated and stably sorted, so an
unchanged project produces an identical record.

## 5. Writing and refresh

The panel probes the open board (`pcbnew.GetBoard()` → owning `.kicad_pro`
via the upward search). A failed probe changes nothing: no scan, no write.

The watch timer ticks every **1000 ms**. A project switch is noticed and
written on the next tick. The project tree is rescanned every **5 ticks**,
which bounds new/removed sub-sheet visibility at 5 seconds. A tick that does
not need a scan is free. When the scanned content matches the record already
on disk, the file is not written at all, so an idle project never churns its
mtime.

At startup the panel writes the state file *before* auto-restoring a session,
so restore judges ownership against this run's record.  A session is restored
only when this run's probe produced a record: a stale record left by an
earlier run (with no board open now) is never used to restore a session.
The same rule applies when a hidden panel is shown again.

When the state file matches the scanned record, it is left untouched.  The
comparison is against the file as it is on disk, so a state file deleted or
replaced externally is recreated by the next scan (within one scan tick on
the watch, five seconds by default).

## 6. Readers

| Reader | Uses the record for |
|---|---|
| Panel `_current_project_path()` | Session auto-restore, Load dialog, context block |
| Panel `_session_project_path()` | Session stamps (with the unreadable-file fallback below) |
| `kicad_plugin/context_bridge.py` | `## Active KiCad Context` system-prompt block (absolute file paths) |
| `_resolve_project()` (`kicad_plugin/llm_client.py`) | Pre-edit and Auto Route snapshots, project-relative paths |
| `save_project_version` (`kcaa/tools/version_tools.py`) | Merging the whole project into an explicit snapshot |
| `PathValidator` (`kcaa/utils/path_validator.py`) | The project trust root |

Every read path **fails closed**: a missing, corrupt, `null` or invalid
record means "no project".  Auto-restore loads no project session, the Load
dialog lists only unowned sessions, the context block is omitted, and session
stamps are empty.

The one exception is the session stamp used when persisting a conversation:
`_session_project_path()` reads the canonical file first and falls back to
the record this panel last published only when the file is missing or
unreadable.  That keeps the stamp available after teardown (when `pcbnew`
no longer returns a board) without letting an in-memory record override a
valid file.

On Windows a reader may hit the same transient sharing race the writer
already retries during `os.replace`.  The read side retries once (50 ms) on
`PermissionError` / `ERROR_SHARING_VIOLATION` before declaring the file
unreadable; a still-failing read returns "no project" and clears the trust
root as before.

The pre-edit resolver has no fallback either.  With no valid record it
returns no project and an actionable error (save the project, open its PCB
editor, then retry); a path outside `project_root` is refused; a recorded
`profile_file` that no longer exists also fails with a retry message.  The
file being edited is checked against `project_root` as well.  There is no
upward filesystem search on the read side.

The canonical upward search in `kcaa/utils/project_resolution.py` (used by
the router, ERC validation and standalone tools) stops when a directory
cannot be read instead of adopting a project higher up.  The plugin's
pre-edit snapshot does **not** use it.

The plugin's pre-edit snapshot and a direct `save_project_version` call read
the same record: when the record names the project being versioned, the tool
merges every `pcb_files`/`schematic_files` entry into the archive, so an
explicit call also covers the whole project.

## 7. Invariants

- One canonical file; no `null` channel.
- Project switch latency ≤ 1 second.
- Manifest (new/removed schematic or PCB) latency ≤ 5 seconds.
- The server re-reads the file on every path validation; switching projects
  needs no server restart.