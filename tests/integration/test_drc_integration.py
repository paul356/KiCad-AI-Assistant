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


# ---------------------------------------------------------------------------
# Integration: Design Rules → Constraint Extraction
# ---------------------------------------------------------------------------


class TestDesignRulesToAutorouter:
    """End-to-end: read design rules from PCB file, extract clearance for autorouter."""

    def test_extract_clearance_for_autorouter(self, tmp_path):
        from kcaa.tools.drc_impl.pcb_design_rules import get_design_rules_from_file

        pcb_path = tmp_path / "test.kicad_pcb"
        tree = _make_pcb_with_design_rules(
            min_clearance=0.25, min_track_width=0.15, copper_edge_clearance=0.5
        )
        # Write directly (save_pcb requires existing file for backup)
        pcb_path.write_text(sexpdata.dumps(tree))

        result = get_design_rules_from_file(str(pcb_path))
        assert result["success"]
        rules = result["rules"]
        assert rules["min_clearance"] == 0.25
        assert rules["min_track_width"] == 0.15
        assert rules["copper_edge_clearance"] == 0.5

    def test_no_design_rules_still_succeeds(self, tmp_path):
        from kcaa.tools.drc_impl.pcb_design_rules import (
            get_design_rules_from_file,
        )

        pcb_path = tmp_path / "empty.kicad_pcb"
        pcb_path.write_text("(kicad_pcb (version 20240108))\n")

        result = get_design_rules_from_file(str(pcb_path))
        assert result["success"]
        # No plugin-exported defaults file in test → empty rules
        assert result["rules"] == {}

    def test_null_clearance_does_not_crash_autorouter(self, tmp_path):
        """When design rules exist but min_clearance is missing, the autorouter
        gets None and does NOT pass -dr flag (safe default behavior)."""
        from kcaa.tools.drc_impl.pcb_design_rules import get_design_rules_from_file

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
        result = get_design_rules_from_file(str(pcb_path))
        assert result["success"]
        # No min_clearance → autorouter will pass clearance_mm=None
        assert "min_clearance" not in result["rules"]


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
            "get_drc_history_tool",
            "get_design_rules",
            "set_design_rules",
            "list_custom_rules",
            "add_custom_rule",
            "restore_design_rules",
        }
        assert expected_tools <= set(mcp.tools.keys())

    def test_get_design_rules_tool_signature(self):
        from kcaa.tools.drc_tools import register_drc_tools

        mcp = _make_mcp()
        register_drc_tools(mcp)

        fn = mcp.tools["get_design_rules"]
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
# Integration: Design Rules Round-Trip
# ---------------------------------------------------------------------------


class TestDesignRulesRoundTrip:
    """Full parse → update → parse again cycle for design rules."""

    def test_update_then_read(self, tmp_path):
        from kcaa.tools.drc_impl.pcb_design_rules import (
            get_design_rules_from_file,
            update_design_rules_in_file,
        )

        pcb_path = tmp_path / "roundtrip.kicad_pcb"
        tree = _make_pcb_with_design_rules(min_clearance=0.2)
        pcb_path.write_text(sexpdata.dumps(tree))

        # Update
        result = update_design_rules_in_file(str(pcb_path), {"min_clearance": 0.3})
        assert result["success"]
        assert result["backup_path"].endswith(".bak")

        # Read back
        result2 = get_design_rules_from_file(str(pcb_path))
        assert result2["success"]
        assert result2["rules"]["min_clearance"] == 0.3

    def test_backup_is_created(self, tmp_path):
        from kcaa.tools.drc_impl.pcb_design_rules import update_design_rules_in_file

        pcb_path = tmp_path / "roundtrip.kicad_pcb"
        tree = _make_pcb_with_design_rules()
        pcb_path.write_text(sexpdata.dumps(tree))

        result = update_design_rules_in_file(str(pcb_path), {"min_track_width": 0.25})
        assert result["success"]
        bak_path = result["backup_path"]
        assert os.path.isfile(bak_path)

    def test_multiple_updates_accumulate(self, tmp_path):
        from kcaa.tools.drc_impl.pcb_design_rules import (
            get_design_rules_from_file,
            update_design_rules_in_file,
        )

        pcb_path = tmp_path / "roundtrip.kicad_pcb"
        tree = _make_pcb_with_design_rules(min_clearance=0.2, min_track_width=0.1)
        pcb_path.write_text(sexpdata.dumps(tree))

        # Update both
        result = update_design_rules_in_file(
            str(pcb_path), {"min_clearance": 0.3, "min_track_width": 0.2}
        )
        assert result["success"]
        assert len(result["updated"]) == 2

        # Read back both
        result2 = get_design_rules_from_file(str(pcb_path))
        rules = result2["rules"]
        assert rules["min_clearance"] == 0.3
        assert rules["min_track_width"] == 0.2


# ---------------------------------------------------------------------------
# Integration: Custom Rules
# ---------------------------------------------------------------------------


class TestCustomRulesRoundTrip:
    """Full cycle for custom design rules."""

    def test_add_then_read_custom_rule(self, tmp_path):
        from kcaa.tools.drc_impl.pcb_design_rules import (
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
        from kcaa.tools.drc_impl.pcb_design_rules import get_custom_rules_from_file

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
        from kcaa.tools.drc_impl.pcb_design_rules import get_design_rules_from_file
        from kicad_plugin.autorouter import _run_subprocess

        # 1. Create PCB with design rules
        pcb_path = tmp_path / "pipeline.kicad_pcb"
        tree = _make_pcb_with_design_rules(min_clearance=0.25)
        pcb_path.write_text(sexpdata.dumps(tree))

        # 2. Read design rules (as the plugin would)
        dr_result = get_design_rules_from_file(str(pcb_path))
        assert dr_result["success"]
        clearance_mm = dr_result["rules"].get("min_clearance")
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
        from kcaa.tools.drc_impl.pcb_design_rules import get_design_rules_from_file
        from kicad_plugin.autorouter import _run_subprocess

        pcb_path = tmp_path / "noclear.kicad_pcb"
        # PCB without a (setup ...) section
        pcb_path.write_text("(kicad_pcb (version 20240108))\n")

        dr_result = get_design_rules_from_file(str(pcb_path))
        assert dr_result["success"]
        clearance_mm = dr_result["rules"].get("min_clearance")
        assert clearance_mm is None  # no defaults file → None

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
            assert "-dr" not in cmd  # no defaults → no -dr flag


# ---------------------------------------------------------------------------
# Integration: Rollback / Restore
# ---------------------------------------------------------------------------


class TestDesignRulesRollback:
    """End-to-end rollback: update → restore from backup → verify original."""

    def test_restore_tool_registered(self):
        from kcaa.tools.drc_tools import register_drc_tools

        mcp = _make_mcp()
        register_drc_tools(mcp)

        assert "restore_design_rules" in mcp.tools

        import inspect

        sig = inspect.signature(mcp.tools["restore_design_rules"])
        params = list(sig.parameters.keys())
        assert "backup_path" in params

    def test_round_trip_update_restore(self, tmp_path):
        from kcaa.tools.drc_impl.pcb_design_rules import (
            get_design_rules_from_file,
            restore_design_rules_from_backup,
            update_design_rules_in_file,
        )

        pcb_path = tmp_path / "rt.kicad_pcb"
        tree = _make_pcb_with_design_rules(min_clearance=0.25)
        pcb_path.write_text(sexpdata.dumps(tree))

        # Read original
        original = get_design_rules_from_file(str(pcb_path))
        assert original["rules"]["min_clearance"] == 0.25

        # Update (creates .bak)
        update_result = update_design_rules_in_file(str(pcb_path), {"min_clearance": 0.4})
        assert update_result["success"]
        bak_path = update_result["backup_path"]

        # Verify update took effect
        after_update = get_design_rules_from_file(str(pcb_path))
        assert after_update["rules"]["min_clearance"] == 0.4

        # Restore from backup
        restore_result = restore_design_rules_from_backup(bak_path)
        assert restore_result["success"]

        # Verify restored to original value
        after_restore = get_design_rules_from_file(str(pcb_path))
        assert after_restore["rules"]["min_clearance"] == 0.25

        # Safety backup exists
        assert os.path.isfile(restore_result["safety_backup"])
