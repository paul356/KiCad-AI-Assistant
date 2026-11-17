# Plugin Project State

## 1. Purpose

The plugin runs in KiCad's embedded interpreter; the MCP server runs in a
separate subprocess. Both need to agree on *which project is open* for path
trust, session ownership, context prompts and pre-edit snapshots. Exactly one
file carries that answer — there is no second probe, window-title fallback or
in-memory mirror.

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
not need a scan is free. When the scanned content matches the last published
record, the file is not written at all, so an idle project never churns its
mtime.

At startup the panel writes the state file *before* auto-restoring a session,
so restore judges ownership against this run's record.

## 6. Readers

| Reader | Uses the record for |
|---|---|
| Panel `_current_project_path()` | Session auto-restore, Load dialog, context block, session stamps |
| `kicad_plugin/context_bridge.py` | `## Active KiCad Context` system-prompt block |
| `_project_path_for()` (`kicad_plugin/llm_client.py`) | Pre-edit snapshots and project-relative paths |
| `PathValidator` (`kcaa/utils/path_validator.py`) | The project trust root |

The panel read path has **no fallback**: a missing, corrupt, `null` or invalid
record means "no project". Auto-restore loads no project session, the Load
dialog lists only unowned sessions, the context block is omitted, and session
stamps are empty.

`_project_path_for` is the one exception with a defined fallback: when a
valid record's `profile_file` has gone stale (renamed or deleted), the upward
search may supply the replacement, but only if it lives in the same
`project_root`. A path outside the root is refused even when another valid
project exists there.

## 7. Invariants

- One canonical file; no `null` channel.
- Project switch latency ≤ 1 second.
- Manifest (new/removed schematic or PCB) latency ≤ 5 seconds.
- The server re-reads the file on every path validation; switching projects
  needs no server restart.