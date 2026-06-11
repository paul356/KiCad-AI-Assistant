---
name: drc-rules
priority: 45
description: "Design rules, net classes, custom DRC constraints, net-to-class assignment, and DRC checker"
---
# Tools overview
- **get_effective_design_rules** — Read all design constraints: board minimums, net classes (with assigned nets), and custom rules.
- **set_design_rules** — Update board-level minimums (clearance, track width, via sizes, etc.). Global hard floor — checked against ALL objects.
- **set_net_class_rules** — Create or update a net class with per-net working values. Auto-creates the class from Default if it doesn't exist.
- **assign_nets_to_class** — Add nets to a net class so they receive its constraints. Moves nets from other classes automatically.
- **remove_nets_from_class** — Take nets out of a net class, reverting them to Default.
- **delete_net_class** — Delete a net class definition entirely. Associated net_patterns are cleaned up (nets revert to Default). The Default class cannot be deleted.
- **add_custom_rule** — Add a conditional DRC rule using KiCad's constraint DSL (e.g. `"A.NetClass == 'HV'"`).
- **del_custom_rule** — Remove a custom DRC rule by name.
- **run_drc_check** — Open the DRC checker dialog in KiCad via IPC. The user must click **Run DRC** to execute.

# Three-layer DRC model
KiCad checks **all three layers independently** during DRC — violating ANY one triggers an error:

| Layer | Tool to read | Tool to write | Scope |
|---|---|---|---|
| `design_rules` | `get_effective_design_rules` | `set_design_rules` | Global minimums — apply to every object on the board |
| `net_classes` | `get_effective_design_rules` | `set_net_class_rules` / `delete_net_class` | Per-net working values — override board minimums for specific nets |
| `custom_rules` | `get_effective_design_rules` | `add_custom_rule` / `del_custom_rule` | Conditional constraints — apply only when DSL expression matches |

Net class values MUST be stricter (not looser) than board-level minimums to take effect. A net class with 0.3 mm clearance where the board minimum is 0.5 mm will still use 0.5 mm for that net.

# Recommended workflow

## 1. Audit current rules
Call **get_effective_design_rules(project_path)** first. It returns:
- `design_rules` — dict of board-level minimums (mm).
- `net_classes` — list of `{name, clearance, track_width, via_diameter, ..., nets: [...]}`.
  The `nets` field shows which nets are assigned to each class.
- `custom_rules` — list of `{name, condition, constraint_type, value, severity}`.

Read this output before making any changes so you understand the current state.

## 2. Set board-level minimums
Call **set_design_rules(project_path, rules)** with only the fields you want to change.
Valid fields: `min_clearance`, `min_track_width`, `min_via_size`, `min_through_drill`,
`min_microvia_size`, `min_microvia_drill`, `copper_edge_clearance`, `hole_clearance`,
`hole_to_hole_min`, `silk_clearance`, `min_resolved_spokes`, `min_silk_text_height`,
`min_silk_text_thickness`, `min_groove_width`, `min_connection_width`, `min_via_annular_width`.

Values are in **millimetres**. Creates `.bak` backup automatically.

## 3. Create or update net classes
Call **set_net_class_rules(project_path, class_name, updates)**. If the class doesn't
exist, it's created from Default values with your overrides applied on top.

Valid fields: `clearance`, `track_width`, `via_diameter`, `via_drill`,
`microvia_diameter`, `microvia_drill`, `diff_pair_width`, `diff_pair_gap`, `diff_pair_via_gap`.

Example: creating a "VBUS" class with 0.6 mm clearance and 0.5 mm track width:
```
set_net_class_rules(project_path, "VBUS", {"clearance": 0.6, "track_width": 0.5})
```

## 4. Assign nets to classes
After creating a net class, use **assign_nets_to_class(project_path, class_name, nets)**
to add nets to it. Nets are matched by exact string (`"/tp4056/VBUS"`, `"VCC_SYS"`).

- If a net was in a different class, it's **moved** automatically (old pattern removed).
- Nets already in the target class are returned in `existing` (skipped).
- After assigning, call `get_effective_design_rules` to verify the `nets` list updated.

## 5. Remove nets from classes
Use **remove_nets_from_class(project_path, class_name, nets)** to take nets out.
After removal, the nets fall back to the Default class. Nets not in the class are
returned in `not_found` (silently skipped).

## 6. Delete unused net classes
Use **delete_net_class(project_path, class_name)** to remove a net class definition
entirely.  All nets previously assigned to the class are cleaned up and revert to
Default.  The `"Default"` class cannot be deleted.

Only delete a class after moving or removing all its member nets (use
`remove_nets_from_class` first if you want explicit control over where nets go).

## 7. Add custom rules for edge cases
Use **add_custom_rule(project_path, name, condition, constraint_type, value, severity)**
for rules that can't be expressed via board minimums or net classes.

Common constraint types: `clearance`, `track_width`, `hole_size`, `annular_width`,
`courtyard_clearance`.

Example: extra clearance only for nets in the HV class:
```
add_custom_rule(project_path, "HV clearance", "A.NetClass == 'HV'", "clearance", 1.0)
```

Severity: `"error"` (default), `"warning"`, `"ignore"`, or `"exclusion"`.

## 8. Run DRC
Call **run_drc_check(project_path)** to open the DRC dialog. The user clicks **Run DRC**
to execute and sees results in the dialog list view.

## 9. Reopen after .kicad_pro changes
After `set_net_class_rules` or `assign_nets_to_class`, tell the user to reopen the
project in KiCad for the changes to take effect. The `.bak` backup is created
automatically.

# Caveats & gotchas
- All mutation tools create a `.bak` backup before writing.
- `set_design_rules` and `set_net_class_rules` operate on the `.kicad_pro` project file.
  `add_custom_rule` and `del_custom_rule` operate on the `.kicad_pcb` file.
- Net class values can be **stricter** than board minimums, but not **looser**.
  KiCad always enforces the most restrictive constraint.
- `assign_nets_to_class` requires the target class to already exist — create it with
  `set_net_class_rules` first.
- `remove_nets_from_class` does NOT require the class to exist; nets not found in
  the class are returned in `not_found`.
- `delete_net_class` cannot delete the `"Default"` net class. All nets assigned
  to the deleted class automatically revert to Default.
- `.kicad_pro` changes require reopening the project in KiCad to take effect.
- The `get_effective_design_rules` output includes a `note` field explaining the
  three-layer DRC model — surface this to the user when reading rules for the first time.
