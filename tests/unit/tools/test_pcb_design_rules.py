"""
Unit tests for kcaa/utils/pcb_design_rules.py.

Tests the JSON-based design rules parser against synthetic .kicad_pro files.
No KiCad process is required.
"""

import json
import os

from kcaa.utils.pcb_design_rules import (
    add_custom_rule_to_file,
    get_custom_rules_from_file,
    get_effective_design_rules_from_file,
    update_design_rules_in_file,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A minimal-but-realistic .kicad_pcb template (still needed for custom_rules tests).
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


def _make_pro_rules(**kwargs: float) -> dict:
    """Build board.design_settings.rules dict from user-facing field names."""
    from kcaa.utils.pcb_design_rules import _DESIGN_RULE_FIELD_MAP

    return {_DESIGN_RULE_FIELD_MAP[k]: v for k, v in kwargs.items()}


def _write_pro(
    dir_: str,
    rules: dict | None = None,
    classes: list | None = None,
    stem: str = "test_board",
) -> str:
    """Write a .kicad_pro JSON file and return its path.

    Args:
        dir_: Temp directory.
        rules: Dict of ``board.design_settings.rules`` values (pro-key format).
               If an empty dict, the section is present but empty.
               If None, the section is populated with default values.
        classes: Optional list of net class dicts.
        stem: Base filename without extension.
    """
    if rules is None:
        rules = {
            "min_clearance": 0.2,
            "min_track_width": 0.15,
            "min_via_diameter": 0.6,
            "min_through_hole_diameter": 0.3,
            "min_copper_edge_clearance": 0.5,
            "min_hole_clearance": 0.25,
            "min_silk_clearance": 0.15,
        }
    data: dict = {
        "board": {
            "design_settings": {
                "rules": rules,
            },
        },
        "net_settings": {
            "classes": classes or [{"name": "Default", "clearance": 0.2, "track_width": 0.15}],
        },
    }
    path = os.path.join(dir_, f"{stem}.kicad_pro")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return path


def _write_pro_without_rules(dir_: str, stem: str = "test_board") -> str:
    """Write a .kicad_pro JSON file with NO board.design_settings.rules section."""
    data: dict = {
        "board": {
            "design_settings": {},
        },
        "net_settings": {
            "classes": [{"name": "Default", "clearance": 0.2, "track_width": 0.15}],
        },
    }
    path = os.path.join(dir_, f"{stem}.kicad_pro")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return path


def _write_pcb_and_pro(pcb_content: str, dir_: str, rules: dict | None = None) -> str:
    """Write both .kicad_pcb and .kicad_pro files; return pcb path."""
    pcb = _write_pcb(pcb_content, dir_)
    _write_pro(dir_, rules=rules)
    return pcb


# ---------------------------------------------------------------------------
# Tests: get_effective_design_rules_from_file
# ---------------------------------------------------------------------------


class TestGetEffectiveDesignRules:
    """Tests for get_effective_design_rules_from_file."""

    def test_reads_all_known_fields(self, tmp_path):
        """All design_rule fields from the .kicad_pro JSON are parsed."""
        content = _PCB_TEMPLATE.format(custom_rules_block="")
        pcb = _write_pcb_and_pro(content, str(tmp_path))

        result = get_effective_design_rules_from_file(pcb)

        assert result["success"] is True
        assert result["design_rules"]["min_clearance"] == 0.2
        assert result["design_rules"]["min_track_width"] == 0.15
        assert result["design_rules"]["min_via_size"] == 0.6
        assert result["design_rules"]["min_through_drill"] == 0.3
        assert result["design_rules"]["copper_edge_clearance"] == 0.5
        assert result["design_rules"]["hole_clearance"] == 0.25
        assert result["design_rules"]["silk_clearance"] == 0.15

    def test_no_setup_section(self, tmp_path):
        """When pro file is missing, returns error."""
        pcb = _write_pcb(_PCB_NO_SETUP, str(tmp_path))
        result = get_effective_design_rules_from_file(pcb)
        assert result["success"] is False
        assert "Cannot read project file" in result["error"]

    def test_no_design_rules_subsection(self, tmp_path):
        """When design_settings.rules is missing, returns empty rules."""
        pcb = _write_pcb(_PCB_SETUP_NO_DR, str(tmp_path))
        # Write a .kicad_pro with no rules section
        _write_pro_without_rules(str(tmp_path))
        result = get_effective_design_rules_from_file(pcb)
        assert result["success"] is True
        assert result["design_rules"] == {}
        assert "note" in result

    def test_file_not_found(self):
        """Non-existent file returns error."""
        result = get_effective_design_rules_from_file("/nonexistent/pcb.kicad_pcb")

        assert result["success"] is False
        assert "error" in result

    def test_ignores_unknown_fields(self, tmp_path):
        """Unknown keys in board.design_settings.rules are silently skipped."""
        pcb = _write_pcb(_PCB_NO_SETUP, str(tmp_path))
        _write_pro(
            str(tmp_path),
            rules={
                "min_clearance": 0.2,
                "unknown_field": 999,
            },
        )

        result = get_effective_design_rules_from_file(pcb)

        assert result["success"] is True
        assert result["design_rules"]["min_clearance"] == 0.2
        assert "unknown_field" not in result["design_rules"]


# ---------------------------------------------------------------------------
# Tests: update_design_rules_in_file
# ---------------------------------------------------------------------------


class TestUpdateDesignRules:
    """Tests for update_design_rules_in_file."""

    def test_updates_single_field(self, tmp_path):
        """Update one field and verify file was changed."""
        pro = _write_pro(str(tmp_path))

        result = update_design_rules_in_file(pro, {"min_clearance": 0.35})

        assert result["success"] is True
        assert len(result["updated"]) == 1
        assert "min_clearance" in result["updated"][0]
        assert result["backup_path"].endswith(".bak")

        # Re-read and verify
        rules = get_effective_design_rules_from_file(pro.replace(".kicad_pro", ".kicad_pcb"))
        assert rules["design_rules"]["min_clearance"] == 0.35
        # Unchanged fields stay the same
        assert rules["design_rules"]["min_track_width"] == 0.15

    def test_updates_multiple_fields(self, tmp_path):
        """Multiple fields can be updated in one call."""
        pro = _write_pro(str(tmp_path))

        result = update_design_rules_in_file(pro, {"min_clearance": 0.5, "min_track_width": 0.3})

        assert result["success"] is True
        assert len(result["updated"]) == 2

        rules = get_effective_design_rules_from_file(pro.replace(".kicad_pro", ".kicad_pcb"))
        assert rules["design_rules"]["min_clearance"] == 0.5
        assert rules["design_rules"]["min_track_width"] == 0.3

    def test_rejects_invalid_fields(self, tmp_path):
        """Unknown field names are rejected with an error."""
        pro = _write_pro(str(tmp_path))

        result = update_design_rules_in_file(pro, {"bad_field": 1.0})

        assert result["success"] is False
        assert "bad_field" in result["error"]

    def test_creates_backup(self, tmp_path):
        """Verify that a .bak file is created on update."""
        pro = _write_pro(str(tmp_path))

        update_design_rules_in_file(pro, {"min_clearance": 0.99})

        bak = pro + ".bak"
        assert os.path.exists(bak)
        # Backup contains original value (read JSON directly)
        import json as _json

        with open(bak) as f:
            bak_data = _json.load(f)
        assert bak_data["board"]["design_settings"]["rules"]["min_clearance"] == 0.2

    def test_no_design_rules_section(self, tmp_path):
        """Error when there's no board.design_settings.rules to update."""
        pro = _write_pro_without_rules(str(tmp_path))

        result = update_design_rules_in_file(pro, {"min_clearance": 0.1})

        assert result["success"] is False
        assert "board.design_settings.rules" in result["error"]


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
