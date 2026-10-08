"""
Tests for the ERC tool in erc_tools.py (run_erc).

The kicad-cli invocation is mocked so these run without KiCad installed; a
single live integration test runs the real CLI when it is available.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SCHEMATIC_PATH = str(Path(__file__).parent / "fixtures" / "tools_test.kicad_sch")

SAMPLE_REPORT = {
    "coordinate_units": "mm",
    "kicad_version": "10.0.3",
    "source": "tools_test.kicad_sch",
    "sheets": [
        {
            "path": "/",
            "uuid_path": "/abc",
            "violations": [
                {
                    "type": "pin_not_connected",
                    "severity": "error",
                    "description": "Pin not connected",
                    "items": [
                        {
                            "description": "Symbol R2 Pin 1 [Passive, Line]",
                            "pos": {"x": 1.0, "y": 0.9746},
                            "uuid": "1d8a8918-9b7c-408d-baea-17b19844e796",
                        }
                    ],
                },
                {
                    "type": "lib_symbol_issues",
                    "severity": "warning",
                    "description": "Symbol differs from library",
                    "items": [],
                },
            ],
        }
    ],
}


class _MockMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _get_tools() -> dict:
    from kcaa.tools.erc_tools import register_erc_tools

    mock = _MockMCP()
    register_erc_tools(mock)
    return mock.tools


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


def _fake_cli(report, returncode=0, stderr="", captured=None):
    """Build an async stand-in for run_kicad_command_async that writes *report*
    to the requested output file and records the command in *captured*."""

    async def _fake(command, input_files=None, output_files=None, timeout=None):
        if captured is not None:
            captured["command"] = command
            captured["timeout"] = timeout
        if returncode == 0 and output_files:
            with open(output_files[0], "w", encoding="utf-8") as f:
                json.dump(report, f)
        return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

    return _fake


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestRunErcValidation:
    def test_non_schematic_path_rejected(self, tools):
        result = asyncio.run(tools["run_erc"](schematic_path="/tmp/foo.txt"))
        assert result["success"] is False and "error" in result

    def test_missing_file_rejected(self, tools):
        result = asyncio.run(tools["run_erc"](schematic_path="/nope/missing.kicad_sch"))
        assert result["success"] is False

    def test_invalid_severity_rejected(self, tools):
        result = asyncio.run(tools["run_erc"](schematic_path=SCHEMATIC_PATH, severity="bogus"))
        assert result["success"] is False
        assert "severity" in result["error"]


# ---------------------------------------------------------------------------
# Parsing & behavior (mocked CLI)
# ---------------------------------------------------------------------------


class TestRunErcParsing:
    def test_success_flattens_and_counts(self, tools):
        with patch(
            "kcaa.tools.erc_tools.run_kicad_command_async",
            new=_fake_cli(SAMPLE_REPORT),
        ):
            result = asyncio.run(tools["run_erc"](schematic_path=SCHEMATIC_PATH))
        assert result["success"] is True
        assert result["violation_count"] == 2
        assert result["error_count"] == 1
        assert result["warning_count"] == 1
        assert result["kicad_version"] == "10.0.3"
        assert result["coordinate_units"] == "mm"

        v0 = result["violations"][0]
        assert v0["severity"] == "error"
        assert v0["type"] == "pin_not_connected"
        assert v0["sheet"] == "/"
        assert v0["items"][0]["x"] == 1.0
        assert v0["items"][0]["uuid"].startswith("1d8a8918")

    def test_severity_maps_to_cli_flags(self, tools):
        captured: dict = {}
        cases = {
            "default": ["--severity-error", "--severity-warning"],
            "all": ["--severity-all"],
            "error": ["--severity-error"],
            "warning": ["--severity-warning"],
        }
        for sev, expected in cases.items():
            with patch(
                "kcaa.tools.erc_tools.run_kicad_command_async",
                new=_fake_cli(SAMPLE_REPORT, captured=captured),
            ):
                asyncio.run(tools["run_erc"](schematic_path=SCHEMATIC_PATH, severity=sev))
            cmd = captured["command"]
            assert cmd[:2] == ["sch", "erc"]
            assert "--exit-code-violations" not in cmd  # violations must not fail the run
            for flag in expected:
                assert flag in cmd

    def test_excluded_counted_separately(self, tools):
        report = {
            "coordinate_units": "mm",
            "kicad_version": "10.0.3",
            "source": "x.kicad_sch",
            "sheets": [
                {
                    "path": "/",
                    "violations": [
                        {"type": "a", "severity": "error", "description": "e", "items": []},
                        {"type": "b", "severity": "warning", "description": "w", "items": []},
                        {
                            "type": "c",
                            "severity": "error",
                            "excluded": True,
                            "comment": "known false positive",
                            "description": "x",
                            "items": [],
                        },
                    ],
                }
            ],
        }
        with patch("kcaa.tools.erc_tools.run_kicad_command_async", new=_fake_cli(report)):
            result = asyncio.run(tools["run_erc"](schematic_path=SCHEMATIC_PATH, severity="all"))
        assert result["violation_count"] == 3
        assert result["error_count"] == 1  # excluded error not counted as live
        assert result["warning_count"] == 1
        assert result["exclusion_count"] == 1
        excluded = [v for v in result["violations"] if v["excluded"]]
        assert len(excluded) == 1
        assert excluded[0]["comment"] == "known false positive"

    def test_nonzero_exit_is_error(self, tools):
        with patch(
            "kcaa.tools.erc_tools.run_kicad_command_async",
            new=_fake_cli(SAMPLE_REPORT, returncode=2, stderr="boom"),
        ):
            result = asyncio.run(tools["run_erc"](schematic_path=SCHEMATIC_PATH))
        assert result["success"] is False
        assert "boom" in result["error"]

    def test_cli_not_found_is_error(self, tools):
        from kcaa.utils.kicad_cli import KiCadCLIError

        async def _raise(*a, **k):
            raise KiCadCLIError("KiCad CLI not found.")

        with patch("kcaa.tools.erc_tools.run_kicad_command_async", new=_raise):
            result = asyncio.run(tools["run_erc"](schematic_path=SCHEMATIC_PATH))
        assert result["success"] is False
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# Live integration (only when kicad-cli is installed)
# ---------------------------------------------------------------------------


def _kicad_cli_available() -> bool:
    from kcaa.utils.kicad_cli import is_kicad_cli_available

    return is_kicad_cli_available()


@pytest.mark.skipif(not _kicad_cli_available(), reason="kicad-cli not installed")
class TestRunErcLive:
    def test_real_erc_on_fixture(self, tools):
        result = asyncio.run(tools["run_erc"](schematic_path=SCHEMATIC_PATH, severity="all"))
        assert result["success"] is True, result
        assert isinstance(result["violation_count"], int)
        # Every violation carries a severity and (usually) located items.
        for v in result["violations"]:
            assert v["severity"] in {"error", "warning", "exclusion"}
            assert "description" in v
