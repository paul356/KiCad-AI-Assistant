"""Integration tests for DRC IPC tools and autorouter constraint extraction.

Verifies end-to-end flows:
- PCB file → design rules → autorouter clearance mapping
- DRC tool registration and MCP integration
- Design rules round-trip (parse → update → parse again)
- Multiple rule updates with backup verification
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import sexpdata

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _make_mcp():
    class _MockMCP:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn

            return decorator

    return _MockMCP()


# ---------------------------------------------------------------------------
# PCB fixture helpers
# ---------------------------------------------------------------------------


def _make_pcb_with_design_rules(min_clearance=0.2, min_track_width=0.15, copper_edge_clearance=0.3):
    """Build a minimal .kicad_pcb S-expression with design rules."""
    return [
        sexpdata.Symbol("kicad_pcb"),
        [sexpdata.Symbol("version"), 20240108],
        [sexpdata.Symbol("generator"), "pcbnew"],
        [sexpdata.Symbol("general"), [sexpdata.Symbol("thickness"), 1.6]],
        [sexpdata.Symbol("modules")],
        [sexpdata.Symbol("networks")],
        [
            sexpdata.Symbol("setup"),
            [sexpdata.Symbol("stackup"), [sexpdata.Symbol("layer"), "F.Cu"]],
            [
                sexpdata.Symbol("design_rules"),
                [sexpdata.Symbol("min_clearance"), min_clearance],
                [sexpdata.Symbol("min_track_width"), min_track_width],
                [sexpdata.Symbol("copper_edge_clearance"), copper_edge_clearance],
                [sexpdata.Symbol("min_via_size"), 0.4],
            ],
        ],
    ]


def _write_pro_for_pcb(pcb_path: Path | str, **rules) -> str:
    """Write a companion .kicad_pro JSON file for the given .kicad_pcb path.

    Writes board.design_settings.rules using the pro-key format.
    Returns the .kicad_pro path.
    """
    import json

    pro_path = str(pcb_path).replace(".kicad_pcb", ".kicad_pro")
    pro_data = {
        "board": {
            "design_settings": {
                "rules": {
                    "min_clearance": rules.get("min_clearance", 0.2),
                    "min_track_width": rules.get("min_track_width", 0.15),
                    "min_via_diameter": rules.get("min_via_size", 0.6),
                    "min_through_hole_diameter": rules.get("min_through_drill", 0.3),
                    "min_copper_edge_clearance": rules.get("copper_edge_clearance", 0.5),
                    "min_hole_clearance": rules.get("hole_clearance", 0.25),
                    "min_silk_clearance": rules.get("silk_clearance", 0.15),
                }
            }
        },
        "net_settings": {"classes": [{"name": "Default", "clearance": 0.2, "track_width": 0.15}]},
    }
    with open(pro_path, "w", encoding="utf-8") as f:
        json.dump(pro_data, f, indent=2)
    return pro_path


# ---------------------------------------------------------------------------
# Integration: Design Rules → Constraint Extraction
# ---------------------------------------------------------------------------


class TestDesignRulesToAutorouter:
    """End-to-end: read design rules from PCB file, extract clearance for autorouter."""

    def test_extract_clearance_for_autorouter(self, tmp_path):
        from kcaa.utils.pcb_design_rules import get_effective_design_rules_from_file

        pcb_path = tmp_path / "test.kicad_pcb"
        tree = _make_pcb_with_design_rules(
            min_clearance=0.25, min_track_width=0.15, copper_edge_clearance=0.5
        )
        # Write directly (save_pcb requires existing file for backup)
        pcb_path.write_text(sexpdata.dumps(tree))
        _write_pro_for_pcb(
            pcb_path,
            min_clearance=0.25,
            min_track_width=0.15,
            copper_edge_clearance=0.5,
        )

        result = get_effective_design_rules_from_file(str(pcb_path))
        assert result["success"]
        rules = result["design_rules"]
        assert rules["min_clearance"] == 0.25
        assert rules["min_track_width"] == 0.15
        assert rules["copper_edge_clearance"] == 0.5

    def test_no_design_rules_still_succeeds(self, tmp_path):
        from kcaa.utils.pcb_design_rules import (
            get_effective_design_rules_from_file,
        )

        pcb_path = tmp_path / "empty.kicad_pcb"
        pcb_path.write_text("(kicad_pcb (version 20240108))\n")
        _write_pro_for_pcb(pcb_path)

        result = get_effective_design_rules_from_file(str(pcb_path))
        assert result["success"]
        assert "design_rules" in result

    def test_null_clearance_does_not_crash_autorouter(self, tmp_path):
        """When design rules exist but min_clearance is missing, the autorouter
        gets None and does NOT pass -dr flag (safe default behavior)."""
        from kcaa.utils.pcb_design_rules import get_effective_design_rules_from_file

        pcb_path = tmp_path / "test.kicad_pcb"
        tree = _make_pcb_with_design_rules(min_track_width=0.15)  # no min_clearance
        # Remove min_clearance from the sexp
        dr_section = [
            item
            for item in tree
            if isinstance(item, list) and len(item) > 0 and item[0] == sexpdata.Symbol("setup")
        ]
        if dr_section:
            dr_inner = [
                item
                for item in dr_section[0]
                if isinstance(item, list)
                and len(item) > 0
                and item[0] == sexpdata.Symbol("design_rules")
            ]
            if dr_inner:
                filtered = [
                    item
                    for item in dr_inner[0]
                    if not (
                        isinstance(item, list)
                        and len(item) > 0
                        and item[0] == sexpdata.Symbol("min_clearance")
                    )
                ]
                dr_inner[0][:] = filtered

        pcb_path.write_text(sexpdata.dumps(tree))
        # Write .kicad_pro WITHOUT min_clearance
        _write_pro_for_pcb(
            pcb_path,
            min_track_width=0.15,
            min_clearance=None,  # signal: omit this field
        )
        # Overwrite the rules dict to remove min_clearance key
        import json

        pro_path = str(pcb_path).replace(".kicad_pcb", ".kicad_pro")
        with open(pro_path) as f:
            pro_data = json.load(f)
        del pro_data["board"]["design_settings"]["rules"]["min_clearance"]
        with open(pro_path, "w") as f:
            json.dump(pro_data, f, indent=2)

        result = get_effective_design_rules_from_file(str(pcb_path))
        assert result["success"]
        # No min_clearance → autorouter will pass clearance_mm=None
        assert "min_clearance" not in result["design_rules"]


# ---------------------------------------------------------------------------
# Integration: DRC Tool Registration
# ---------------------------------------------------------------------------


class TestDRCToolRegistration:
    """Verify all DRC-related tools register correctly with the MCP server."""

    def test_all_drc_tools_registered(self):
        from kcaa.tools.drc_tools import register_drc_tools

        mcp = _make_mcp()
        register_drc_tools(mcp)

        expected_tools = {
            "run_drc_check",
            "get_effective_design_rules",
            "set_design_rules",
            "set_net_class_rules",
            "assign_nets_to_class",
            "remove_nets_from_class",
            "add_custom_rule",
            "del_custom_rule",
        }
        assert expected_tools <= set(mcp.tools.keys())

    def test_get_effective_design_rules_tool_signature(self):
        from kcaa.tools.drc_tools import register_drc_tools

        mcp = _make_mcp()
        register_drc_tools(mcp)

        fn = mcp.tools["get_effective_design_rules"]
        import inspect

        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        assert "project_path" in params

    def test_set_design_rules_tool_signature(self):
        from kcaa.tools.drc_tools import register_drc_tools

        mcp = _make_mcp()
        register_drc_tools(mcp)

        fn = mcp.tools["set_design_rules"]
        import inspect

        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        assert "project_path" in params
        assert "rules" in params

    def test_add_custom_rule_tool_signature(self):
        from kcaa.tools.drc_tools import register_drc_tools

        mcp = _make_mcp()
        register_drc_tools(mcp)

        fn = mcp.tools["add_custom_rule"]
        import inspect

        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        required = {"project_path", "name", "condition", "constraint_type", "value"}
        assert required <= set(params)


# ---------------------------------------------------------------------------
# Integration: Net Class & Nets Lifecycle
# ---------------------------------------------------------------------------


def _make_pro_json(net_classes=None, netclass_patterns=None):
    """Build minimal .kicad_pro JSON content for net class tests."""
    import json

    data = {
        "board": {"design_settings": {"rules": {}}},
        "net_settings": {
            "classes": net_classes or [{"name": "Default", "clearance": 0.2, "track_width": 0.15}],
        },
    }
    if netclass_patterns is not None:
        data["net_settings"]["netclass_patterns"] = netclass_patterns
    return json.dumps(data, indent=2)


class TestNetClassLifecycle:
    """End-to-end: create net class → assign nets → read back → remove nets."""

    def test_create_net_class_and_assign_nets(self, tmp_path):
        from kcaa.utils.net_settings import (
            assign_nets_to_class_in_pro,
            get_net_classes_from_pro,
            set_net_class_in_pro,
        )

        pro_path = tmp_path / "test.kicad_pro"
        pro_path.write_text(_make_pro_json())

        # 1. Create a new net class "HV" with custom clearance
        result = set_net_class_in_pro(str(pro_path), "HV", {"clearance": 0.5})
        assert result["success"]
        assert result.get("created")

        # 2. Assign nets to the class
        result = assign_nets_to_class_in_pro(str(pro_path), "HV", ["VCC_SYS", "/tp4056/VBUS"])
        assert result["success"]
        assert sorted(result["assigned"]) == ["/tp4056/VBUS", "VCC_SYS"]

        # 3. Read back — verify nets appear in the class
        result = get_net_classes_from_pro(str(pro_path))
        assert result["success"]
        hv = next(c for c in result["classes"] if c["name"] == "HV")
        assert sorted(hv["nets"]) == ["/tp4056/VBUS", "VCC_SYS"]
        assert hv["clearance"] == 0.5

    def test_assign_existing_net_is_skipped(self, tmp_path):
        from kcaa.utils.net_settings import assign_nets_to_class_in_pro

        pro_path = tmp_path / "test.kicad_pro"
        pro_path.write_text(
            _make_pro_json(
                netclass_patterns=[
                    {"netclass": "Default", "pattern": "GND"},
                ]
            )
        )

        result = assign_nets_to_class_in_pro(str(pro_path), "Default", ["GND", "VCC"])
        assert result["success"]
        assert result["existing"] == ["GND"]
        assert result["assigned"] == ["VCC"]

    def test_move_net_between_classes(self, tmp_path):
        from kcaa.utils.net_settings import (
            assign_nets_to_class_in_pro,
            get_net_classes_from_pro,
            set_net_class_in_pro,
        )

        pro_path = tmp_path / "test.kicad_pro"
        pro_path.write_text(
            _make_pro_json(
                netclass_patterns=[
                    {"netclass": "Default", "pattern": "GND"},
                ]
            )
        )

        # Create HV class and move GND into it
        set_net_class_in_pro(str(pro_path), "HV", {"clearance": 0.5})
        result = assign_nets_to_class_in_pro(str(pro_path), "HV", ["GND"])
        assert result["success"]
        assert result["assigned"] == ["GND"]

        # Verify GND is now in HV, not Default
        nc_result = get_net_classes_from_pro(str(pro_path))
        hv = next(c for c in nc_result["classes"] if c["name"] == "HV")
        default = next(c for c in nc_result["classes"] if c["name"] == "Default")
        assert "GND" in hv["nets"]
        assert "GND" not in default["nets"]

    def test_remove_nets_from_class(self, tmp_path):
        from kcaa.utils.net_settings import (
            get_net_classes_from_pro,
            remove_nets_from_class_in_pro,
            set_net_class_in_pro,
        )

        pro_path = tmp_path / "test.kicad_pro"
        pro_path.write_text(_make_pro_json())

        # Create HV class first so it exists in classes
        set_net_class_in_pro(str(pro_path), "HV", {"clearance": 0.5})

        # Now assign nets (using the utility directly to set up state)
        from kcaa.utils.net_settings import assign_nets_to_class_in_pro

        assign_nets_to_class_in_pro(str(pro_path), "HV", ["VBUS", "VCC_SYS"])
        assign_nets_to_class_in_pro(str(pro_path), "Default", ["GND"])

        # Remove VBUS from HV
        result = remove_nets_from_class_in_pro(str(pro_path), "HV", ["VBUS"])
        assert result["success"]
        assert result["removed"] == ["VBUS"]
        assert result["not_found"] == []

        # Verify VBUS is gone from HV, VCC_SYS remains
        nc_result = get_net_classes_from_pro(str(pro_path))
        hv = next(c for c in nc_result["classes"] if c["name"] == "HV")
        assert "VBUS" not in hv["nets"]
        assert "VCC_SYS" in hv["nets"]

    def test_remove_nets_not_found(self, tmp_path):
        from kcaa.utils.net_settings import remove_nets_from_class_in_pro

        pro_path = tmp_path / "test.kicad_pro"
        pro_path.write_text(_make_pro_json())

        result = remove_nets_from_class_in_pro(str(pro_path), "Default", ["NONEXISTENT"])
        assert not result["success"]
        assert "None of the specified nets" in result["error"]

    def test_remove_nets_mixed_found_and_not_found(self, tmp_path):
        from kcaa.utils.net_settings import (
            assign_nets_to_class_in_pro,
            remove_nets_from_class_in_pro,
            set_net_class_in_pro,
        )

        pro_path = tmp_path / "test.kicad_pro"
        pro_path.write_text(_make_pro_json())
        set_net_class_in_pro(str(pro_path), "HV", {"clearance": 0.5})
        assign_nets_to_class_in_pro(str(pro_path), "HV", ["VBUS"])

        result = remove_nets_from_class_in_pro(str(pro_path), "HV", ["VBUS", "NONEXISTENT"])
        assert result["success"]
        assert result["removed"] == ["VBUS"]
        assert result["not_found"] == ["NONEXISTENT"]

    def test_remove_nets_wrong_class_not_removed(self, tmp_path):
        from kcaa.utils.net_settings import (
            assign_nets_to_class_in_pro,
            remove_nets_from_class_in_pro,
            set_net_class_in_pro,
        )

        pro_path = tmp_path / "test.kicad_pro"
        pro_path.write_text(_make_pro_json())
        set_net_class_in_pro(str(pro_path), "HV", {"clearance": 0.5})
        assign_nets_to_class_in_pro(str(pro_path), "HV", ["VBUS"])

        # Try to remove VBUS from Default (it's actually in HV)
        result = remove_nets_from_class_in_pro(str(pro_path), "Default", ["VBUS"])
        assert not result["success"]
        assert "None of the specified nets" in result["error"]

    def test_full_lifecycle_create_assign_remove(self, tmp_path):
        from kcaa.utils.net_settings import (
            assign_nets_to_class_in_pro,
            get_net_classes_from_pro,
            remove_nets_from_class_in_pro,
            set_net_class_in_pro,
        )

        pro_path = tmp_path / "test.kicad_pro"
        pro_path.write_text(_make_pro_json())

        # 1. Create class
        set_net_class_in_pro(str(pro_path), "HV", {"clearance": 0.5})

        # 2. Assign nets
        assign_nets_to_class_in_pro(str(pro_path), "HV", ["VBUS", "VCC_SYS"])

        # 3. Read back — nets present
        nc_result = get_net_classes_from_pro(str(pro_path))
        hv = next(c for c in nc_result["classes"] if c["name"] == "HV")
        assert sorted(hv["nets"]) == ["VBUS", "VCC_SYS"]

        # 4. Remove one net
        remove_nets_from_class_in_pro(str(pro_path), "HV", ["VBUS"])

        # 5. Read back — only VCC_SYS remains
        nc_result = get_net_classes_from_pro(str(pro_path))
        hv = next(c for c in nc_result["classes"] if c["name"] == "HV")
        assert hv["nets"] == ["VCC_SYS"]

    def test_remove_nets_backup_created(self, tmp_path):
        from kcaa.utils.net_settings import (
            assign_nets_to_class_in_pro,
            remove_nets_from_class_in_pro,
            set_net_class_in_pro,
        )

        pro_path = tmp_path / "test.kicad_pro"
        pro_path.write_text(_make_pro_json())
        set_net_class_in_pro(str(pro_path), "HV", {"clearance": 0.5})
        assign_nets_to_class_in_pro(str(pro_path), "HV", ["VBUS"])

        result = remove_nets_from_class_in_pro(str(pro_path), "HV", ["VBUS"])
        assert result["success"]
        bak_path = result["backup_path"]
        assert os.path.isfile(bak_path)


# ---------------------------------------------------------------------------
# Integration: Design Rules Round-Trip
# ---------------------------------------------------------------------------


class TestDesignRulesRoundTrip:
    """Full parse → update → parse again cycle for design rules."""

    def test_update_then_read(self, tmp_path):
        from kcaa.utils.pcb_design_rules import (
            get_effective_design_rules_from_file,
            update_design_rules_in_file,
        )

        pcb_path = tmp_path / "roundtrip.kicad_pcb"
        tree = _make_pcb_with_design_rules(min_clearance=0.2)
        pcb_path.write_text(sexpdata.dumps(tree))
        pro_path = _write_pro_for_pcb(pcb_path, min_clearance=0.2)

        # Update
        result = update_design_rules_in_file(pro_path, {"min_clearance": 0.3})
        assert result["success"]
        assert result["backup_path"].endswith(".bak")

        # Read back
        result2 = get_effective_design_rules_from_file(str(pcb_path))
        assert result2["success"]
        assert result2["design_rules"]["min_clearance"] == 0.3

    def test_backup_is_created(self, tmp_path):
        from kcaa.utils.pcb_design_rules import update_design_rules_in_file

        pcb_path = tmp_path / "roundtrip.kicad_pcb"
        tree = _make_pcb_with_design_rules()
        pcb_path.write_text(sexpdata.dumps(tree))
        pro_path = _write_pro_for_pcb(pcb_path)

        result = update_design_rules_in_file(pro_path, {"min_track_width": 0.25})
        assert result["success"]
        bak_path = result["backup_path"]
        assert os.path.isfile(bak_path)

    def test_multiple_updates_accumulate(self, tmp_path):
        from kcaa.utils.pcb_design_rules import (
            get_effective_design_rules_from_file,
            update_design_rules_in_file,
        )

        pcb_path = tmp_path / "roundtrip.kicad_pcb"
        tree = _make_pcb_with_design_rules(min_clearance=0.2, min_track_width=0.1)
        pcb_path.write_text(sexpdata.dumps(tree))
        pro_path = _write_pro_for_pcb(pcb_path, min_clearance=0.2, min_track_width=0.1)

        # Update both
        result = update_design_rules_in_file(
            pro_path, {"min_clearance": 0.3, "min_track_width": 0.2}
        )
        assert result["success"]
        assert len(result["updated"]) == 2

        # Read back both
        result2 = get_effective_design_rules_from_file(str(pcb_path))
        rules = result2["design_rules"]
        assert rules["min_clearance"] == 0.3
        assert rules["min_track_width"] == 0.2


# ---------------------------------------------------------------------------
# Integration: Custom Rules
# ---------------------------------------------------------------------------


class TestCustomRulesRoundTrip:
    """Full cycle for custom design rules."""

    def test_add_then_read_custom_rule(self, tmp_path):
        from kcaa.utils.pcb_design_rules import (
            add_custom_rule_to_file,
            get_custom_rules_from_file,
        )

        pcb_path = tmp_path / "custom.kicad_pcb"
        tree = _make_pcb_with_design_rules()
        pcb_path.write_text(sexpdata.dumps(tree))

        # Add custom rule
        result = add_custom_rule_to_file(
            str(pcb_path),
            name="HV clearance",
            condition="A.NetClass == 'HV'",
            constraint_type="clearance",
            value=0.8,
            severity="error",
        )
        assert result["success"]
        assert result["rule"]["name"] == "HV clearance"

        # Read back
        result2 = get_custom_rules_from_file(str(pcb_path))
        assert result2["success"]
        assert len(result2["rules"]) == 1
        rule = result2["rules"][0]
        assert rule["name"] == "HV clearance"
        assert rule["severity"] == "error"

    def test_custom_rules_empty_by_default(self, tmp_path):
        from kcaa.utils.pcb_design_rules import get_custom_rules_from_file

        pcb_path = tmp_path / "nocustom.kicad_pcb"
        tree = _make_pcb_with_design_rules()
        pcb_path.write_text(sexpdata.dumps(tree))

        result = get_custom_rules_from_file(str(pcb_path))
        assert result["success"]
        assert result["rules"] == []


# ---------------------------------------------------------------------------
# Integration: Autorouter + Design Rules end-to-end
# ---------------------------------------------------------------------------


class TestAutorouterWithDesignRules:
    """End-to-end: design rules extraction → autorouter command with -dr flag."""

    def test_full_pipeline_clearance_to_dr_flag(self, tmp_path):
        """Simulate the full pipeline from the plugin:
        PCB file → design rules → clearance_mm → autorouter command."""
        from kcaa.utils.pcb_design_rules import get_effective_design_rules_from_file
        from kicad_plugin.autorouter import _run_subprocess

        # 1. Create PCB with design rules
        pcb_path = tmp_path / "pipeline.kicad_pcb"
        tree = _make_pcb_with_design_rules(min_clearance=0.25)
        pcb_path.write_text(sexpdata.dumps(tree))
        _write_pro_for_pcb(pcb_path, min_clearance=0.25)

        # 2. Read design rules (as the plugin would)
        dr_result = get_effective_design_rules_from_file(str(pcb_path))
        assert dr_result["success"]
        clearance_mm = dr_result["design_rules"].get("min_clearance")
        assert clearance_mm == 0.25

        # 3. Pass to autorouter (as the plugin would)
        dsn = tmp_path / "pipeline.dsn"
        ses = tmp_path / "pipeline.ses"
        dsn.write_text("(design)")
        ses.write_text("")

        with (
            patch(
                "kicad_plugin.autorouter.find_freerouting_jar", return_value="/fake/freerouting.jar"
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            _run_subprocess(str(dsn), str(ses), None, None, 50, clearance_mm=clearance_mm)

            cmd = mock_run.call_args[0][0]
            assert "-dr" in cmd
            dr_idx = cmd.index("-dr")
            assert cmd[dr_idx + 1] == "0.25"

    def test_missing_clearance_no_dr_flag(self, tmp_path):
        """When min_clearance is not in the PCB and no defaults file exists,
        autorouter does NOT pass -dr flag (safe default)."""
        from kcaa.utils.pcb_design_rules import get_effective_design_rules_from_file
        from kicad_plugin.autorouter import _run_subprocess

        pcb_path = tmp_path / "noclear.kicad_pcb"
        # PCB without a (setup ...) section — and no .kicad_pro file
        pcb_path.write_text("(kicad_pcb (version 20240108))\n")

        dr_result = get_effective_design_rules_from_file(str(pcb_path))
        # Without .kicad_pro sidecar, this returns an error now
        assert dr_result["success"] is False or "design_rules" in dr_result
        clearance_mm = (
            dr_result.get("design_rules", {}).get("min_clearance")
            if dr_result.get("success")
            else None
        )

        dsn = tmp_path / "noclear.dsn"
        ses = tmp_path / "noclear.ses"
        dsn.write_text("(design)")
        ses.write_text("")

        with (
            patch(
                "kicad_plugin.autorouter.find_freerouting_jar", return_value="/fake/freerouting.jar"
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            _run_subprocess(str(dsn), str(ses), None, None, 50, clearance_mm=clearance_mm)

            cmd = mock_run.call_args[0][0]
            if clearance_mm is not None:
                assert "-dr" in cmd
                assert str(clearance_mm) in cmd
            else:
                assert "-dr" not in cmd


# ---------------------------------------------------------------------------
