"""
Unit tests for kcaa/tools/drc_impl/pcb_design_rules.py.

Tests the S-expression-based design rules parser against synthetic
.kicad_pcb file content.  No KiCad process is required.
"""

import os

from kcaa.tools.drc_impl.pcb_design_rules import (
    add_custom_rule_to_file,
    get_custom_rules_from_file,
    get_design_rules_from_file,
    restore_design_rules_from_backup,
    update_design_rules_in_file,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A minimal-but-realistic .kicad_pcb template with a (setup ...) section.
_PCB_TEMPLATE = """(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(general
\t\t(thickness 1.6)
\t)
\t(setup
\t\t(design_rules
\t\t\t(min_clearance 0.2)
\t\t\t(min_track_width 0.15)
\t\t\t(min_via_size 0.6)
\t\t\t(min_through_drill 0.3)
\t\t\t(copper_edge_clearance 0.5)
\t\t\t(hole_clearance 0.25)
\t\t\t(silk_clearance 0.15)
\t\t)
\t\t{custom_rules_block}
\t)
\t(net "VCC")
\t(net "GND")
)
"""

_CUSTOM_RULES_BLOCK = """(custom_rules
\t\t\t(rule "High voltage"
\t\t\t\t(condition "A.NetClass == 'HV'")
\t\t\t\t(constraint clearance min 1.0)
\t\t\t\t(severity error)
\t\t\t)
\t\t\t(rule "Fine tracks"
\t\t\t\t(condition "A.Type == 'track'")
\t\t\t\t(constraint track_width min 0.1)
\t\t\t\t(severity warning)
\t\t\t)
\t\t)"""

# A PCB file with NO setup section at all.
_PCB_NO_SETUP = """(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(net "GND")
)
"""

# A PCB file WITH setup but NO design_rules subsection.
_PCB_SETUP_NO_DR = """(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(setup
\t\t(pad_to_mask_clearance 0.05)
\t)
\t(net "GND")
)
"""

# A PCB file with setup + design_rules but NO custom_rules.
_PCB_NO_CUSTOM = """(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(setup
\t\t(design_rules
\t\t\t(min_clearance 0.3)
\t\t)
\t)
\t(net "GND")
)
"""


def _write_pcb(content: str, dir_: str) -> str:
    """Write *content* to a temp .kicad_pcb file and return its path."""
    path = os.path.join(dir_, "test_board.kicad_pcb")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


# ---------------------------------------------------------------------------
# Tests: get_design_rules_from_file
# ---------------------------------------------------------------------------


class TestGetDesignRules:
    """Tests for get_design_rules_from_file."""

    def test_reads_all_known_fields(self, tmp_path):
        """All design_rule fields defined in the template are parsed."""
        content = _PCB_TEMPLATE.format(custom_rules_block="")
        pcb = _write_pcb(content, str(tmp_path))

        result = get_design_rules_from_file(pcb)

        assert result["success"] is True
        assert result["rules"]["min_clearance"] == 0.2
        assert result["rules"]["min_track_width"] == 0.15
        assert result["rules"]["min_via_size"] == 0.6
        assert result["rules"]["min_through_drill"] == 0.3
        assert result["rules"]["copper_edge_clearance"] == 0.5
        assert result["rules"]["hole_clearance"] == 0.25
        assert result["rules"]["silk_clearance"] == 0.15

    def test_no_setup_section(self, tmp_path):
        """When setup section is missing, returns empty rules."""
        pcb = _write_pcb(_PCB_NO_SETUP, str(tmp_path))

        result = get_design_rules_from_file(pcb)

        assert result["success"] is True
        assert result["rules"] == {}
        assert "No (setup" in result.get("message", "")

    def test_no_design_rules_subsection(self, tmp_path):
        """When design_rules subsection is missing, returns empty."""
        pcb = _write_pcb(_PCB_SETUP_NO_DR, str(tmp_path))

        result = get_design_rules_from_file(pcb)

        assert result["success"] is True
        assert result["rules"] == {}
        assert "No (design_rules" in result.get("message", "")

    def test_file_not_found(self):
        """Non-existent file returns error."""
        result = get_design_rules_from_file("/nonexistent/pcb.kicad_pcb")

        assert result["success"] is False
        assert "error" in result

    def test_ignores_unknown_fields(self, tmp_path):
        """Unknown tags inside design_rules are silently skipped."""
        content = """(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(setup
\t\t(design_rules
\t\t\t(min_clearance 0.2)
\t\t\t(unknown_field 999)
\t\t)
\t)
)
"""
        pcb = _write_pcb(content, str(tmp_path))

        result = get_design_rules_from_file(pcb)

        assert result["success"] is True
        assert result["rules"]["min_clearance"] == 0.2
        assert "unknown_field" not in result["rules"]


# ---------------------------------------------------------------------------
# Tests: update_design_rules_in_file
# ---------------------------------------------------------------------------


class TestUpdateDesignRules:
    """Tests for update_design_rules_in_file."""

    def test_updates_single_field(self, tmp_path):
        """Update one field and verify file was changed."""
        content = _PCB_TEMPLATE.format(custom_rules_block="")
        pcb = _write_pcb(content, str(tmp_path))

        result = update_design_rules_in_file(pcb, {"min_clearance": 0.35})

        assert result["success"] is True
        assert len(result["updated"]) == 1
        assert "min_clearance" in result["updated"][0]
        assert result["backup_path"].endswith(".bak")

        # Re-read and verify
        rules = get_design_rules_from_file(pcb)
        assert rules["rules"]["min_clearance"] == 0.35
        # Unchanged fields stay the same
        assert rules["rules"]["min_track_width"] == 0.15

    def test_updates_multiple_fields(self, tmp_path):
        """Multiple fields can be updated in one call."""
        content = _PCB_TEMPLATE.format(custom_rules_block="")
        pcb = _write_pcb(content, str(tmp_path))

        result = update_design_rules_in_file(pcb, {"min_clearance": 0.5, "min_track_width": 0.3})

        assert result["success"] is True
        assert len(result["updated"]) == 2

        rules = get_design_rules_from_file(pcb)
        assert rules["rules"]["min_clearance"] == 0.5
        assert rules["rules"]["min_track_width"] == 0.3

    def test_rejects_invalid_fields(self, tmp_path):
        """Unknown field names are rejected with an error."""
        pcb = _write_pcb(_PCB_TEMPLATE.format(custom_rules_block=""), str(tmp_path))

        result = update_design_rules_in_file(pcb, {"bad_field": 1.0})

        assert result["success"] is False
        assert "bad_field" in result["error"]

    def test_creates_backup(self, tmp_path):
        """Verify that a .bak file is created on update."""
        content = _PCB_TEMPLATE.format(custom_rules_block="")
        pcb = _write_pcb(content, str(tmp_path))

        update_design_rules_in_file(pcb, {"min_clearance": 0.99})

        bak = pcb + ".bak"
        assert os.path.exists(bak)
        # Backup contains original value
        rules = get_design_rules_from_file(bak)
        assert rules["rules"]["min_clearance"] == 0.2

    def test_no_design_rules_section(self, tmp_path):
        """Error when there's no design_rules to update."""
        pcb = _write_pcb(_PCB_SETUP_NO_DR, str(tmp_path))

        result = update_design_rules_in_file(pcb, {"min_clearance": 0.1})

        assert result["success"] is False
        assert "No (design_rules" in result["error"]


# ---------------------------------------------------------------------------
# Tests: get_custom_rules_from_file
# ---------------------------------------------------------------------------


class TestGetCustomRules:
    """Tests for get_custom_rules_from_file."""

    def test_reads_multiple_rules(self, tmp_path):
        """Two custom rules are parsed with all fields."""
        content = _PCB_TEMPLATE.format(custom_rules_block=_CUSTOM_RULES_BLOCK)
        pcb = _write_pcb(content, str(tmp_path))

        result = get_custom_rules_from_file(pcb)

        assert result["success"] is True
        assert len(result["rules"]) == 2

        r0 = result["rules"][0]
        assert r0["name"] == "High voltage"
        assert r0["condition"] == "A.NetClass == 'HV'"
        assert r0["constraint"]["type"] == "clearance"
        assert r0["constraint"]["min"] == 1.0
        assert r0["severity"] == "error"

        r1 = result["rules"][1]
        assert r1["name"] == "Fine tracks"
        assert r1["severity"] == "warning"

    def test_no_custom_rules(self, tmp_path):
        """When no custom_rules subsection, returns empty list."""
        pcb = _write_pcb(_PCB_NO_CUSTOM, str(tmp_path))

        result = get_custom_rules_from_file(pcb)

        assert result["success"] is True
        assert result["rules"] == []
        assert "No (custom_rules" in result.get("message", "")

    def test_no_setup_section(self, tmp_path):
        """When no setup section, returns empty list."""
        pcb = _write_pcb(_PCB_NO_SETUP, str(tmp_path))

        result = get_custom_rules_from_file(pcb)

        assert result["success"] is True
        assert result["rules"] == []


# ---------------------------------------------------------------------------
# Tests: add_custom_rule_to_file
# ---------------------------------------------------------------------------


class TestAddCustomRule:
    """Tests for add_custom_rule_to_file."""

    def test_adds_rule_to_existing_custom_rules(self, tmp_path):
        """Rule is appended when custom_rules section already exists."""
        content = _PCB_TEMPLATE.format(custom_rules_block=_CUSTOM_RULES_BLOCK)
        pcb = _write_pcb(content, str(tmp_path))

        result = add_custom_rule_to_file(
            pcb, "Test rule", "A.NetName == 'CLK'", "clearance", 0.5, "warning"
        )

        assert result["success"] is True
        assert result["rule"]["name"] == "Test rule"
        assert result["backup_path"].endswith(".bak")

        # Re-read and verify
        rules = get_custom_rules_from_file(pcb)
        assert len(rules["rules"]) == 3  # 2 original + 1 new
        new_rule = rules["rules"][-1]
        assert new_rule["name"] == "Test rule"
        assert new_rule["condition"] == "A.NetName == 'CLK'"
        assert new_rule["severity"] == "warning"

    def test_adds_rule_when_no_custom_rules_section(self, tmp_path):
        """Creates the custom_rules section if it doesn't exist."""
        pcb = _write_pcb(_PCB_NO_CUSTOM, str(tmp_path))

        result = add_custom_rule_to_file(
            pcb, "New rule", "A.NetClass == 'Power'", "track_width", 0.25, "error"
        )

        assert result["success"] is True

        rules = get_custom_rules_from_file(pcb)
        assert len(rules["rules"]) == 1
        assert rules["rules"][0]["name"] == "New rule"

    def test_adds_rule_when_no_setup_section(self, tmp_path):
        """Creates both setup and custom_rules sections."""
        pcb = _write_pcb(_PCB_NO_SETUP, str(tmp_path))

        result = add_custom_rule_to_file(
            pcb, "First rule", "A.Type == 'via'", "annular_width", 0.15, "error"
        )

        assert result["success"] is True

        rules = get_custom_rules_from_file(pcb)
        assert len(rules["rules"]) == 1

    def test_rejects_invalid_severity(self, tmp_path):
        """Invalid severity value is rejected."""
        pcb = _write_pcb(_PCB_TEMPLATE.format(custom_rules_block=""), str(tmp_path))

        result = add_custom_rule_to_file(
            pcb, "Bad rule", "A.NetClass == 'X'", "clearance", 1.0, "critical"
        )  # invalid

        assert result["success"] is False
        assert "severity" in result["error"].lower()


# ---------------------------------------------------------------------------
# Tests for restore_design_rules_from_backup
# ---------------------------------------------------------------------------


class TestRestoreDesignRules:
    """Verify rollback via backup restoration."""

    def test_restore_from_valid_backup(self, tmp_path):
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(_PCB_TEMPLATE.format(custom_rules_block=""))
        # Create a .bak file with different content
        bak = tmp_path / "board.kicad_pcb.bak"
        modified = _PCB_TEMPLATE.replace("(min_clearance 0.2)", "(min_clearance 0.5)")
        bak.write_text(modified.format(custom_rules_block=""))

        result = restore_design_rules_from_backup(str(bak))
        assert result["success"]
        assert os.path.isfile(result["safety_backup"])

        # Check that original was restored (should now have 0.5 clearance)
        dr = get_design_rules_from_file(str(pcb))
        assert dr["rules"]["min_clearance"] == 0.5

    def test_backup_not_found(self):
        result = restore_design_rules_from_backup("/nonexistent/path.bak")
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    def test_not_a_bak_extension(self, tmp_path):
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(_PCB_TEMPLATE.format(custom_rules_block=""))

        result = restore_design_rules_from_backup(str(pcb))
        assert result["success"] is False
        assert ".bak" in result["error"].lower()

    def test_original_missing(self, tmp_path):
        bak = tmp_path / "board.kicad_pcb.bak"
        bak.write_text(_PCB_TEMPLATE.format(custom_rules_block=""))
        # No corresponding .kicad_pcb file

        result = restore_design_rules_from_backup(str(bak))
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    def test_safety_backup_created(self, tmp_path):
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(_PCB_TEMPLATE.format(custom_rules_block=""))
        bak = tmp_path / "board.kicad_pcb.bak"
        modified = _PCB_TEMPLATE.replace("(min_clearance 0.2)", "(min_clearance 0.8)")
        bak.write_text(modified.format(custom_rules_block=""))

        result = restore_design_rules_from_backup(str(bak))
        assert result["success"]

        safety = result["safety_backup"]
        assert os.path.isfile(safety)
        assert ".pre-restore-" in safety
        # Safety backup should contain the pre-restore content (0.2 clearance)
        dr_safety = get_design_rules_from_file(safety)
        assert dr_safety["rules"]["min_clearance"] == 0.2

    def test_invalid_backup_content(self, tmp_path):
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(_PCB_TEMPLATE.format(custom_rules_block=""))
        bak = tmp_path / "board.kicad_pcb.bak"
        bak.write_text("this is not valid sexp data {{{")

        result = restore_design_rules_from_backup(str(bak))
        assert result["success"] is False
        assert "parse" in result["error"].lower()
