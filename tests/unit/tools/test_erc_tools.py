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
# Path trust roots (mocked CLI, real PathValidator)
# ---------------------------------------------------------------------------


@pytest.fixture
def state_project(tmp_path, monkeypatch):
    """Publish a tmp project via the plugin state file and reset server globals.

    Returns (project_dir, schematic_path).
    """
    from kcaa.utils.config import config
    import kcaa.utils.path_validator as path_validator_mod
    import kcaa.utils.secure_subprocess as secure_subprocess_mod

    state_file = tmp_path / "active_project.json"
    project = tmp_path / "proj"
    project.mkdir()
    schematic = project / "board.kicad_sch"
    schematic.write_text("(kicad_sch)", encoding="utf-8")
    state_file.write_text(json.dumps({"project_root": str(project)}), encoding="utf-8")

    monkeypatch.setenv("KCAA_PROJECT_STATE_FILE", str(state_file))
    # Server temp files go to a scratch data dir, not the user's config dir.
    monkeypatch.setattr(config, "get_kcaa_data_dir", lambda: str(tmp_path / "kcaa_data"))
    monkeypatch.setattr(path_validator_mod, "_default_validator", None)
    monkeypatch.setattr(secure_subprocess_mod, "_subprocess_runner", None)
    return project, schematic


def _fake_kicad_cli(captured: dict):
    """Stand-in for SecureSubprocessRunner._run_subprocess that writes the report."""

    def _run(_self, command, working_dir=None, timeout=None, capture_output=True):
        captured["argv"] = list(command)
        out_path = command[command.index("--output") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(SAMPLE_REPORT, f)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return _run


class TestRunErcTrustRoots:
    def test_schematic_under_published_project_root_succeeds(self, tools, state_project):
        from kcaa.utils.secure_subprocess import SecureSubprocessRunner

        project, schematic = state_project
        captured: dict = {}
        with (
            patch(
                "kcaa.utils.secure_subprocess.get_kicad_cli_path",
                return_value="/usr/bin/kicad-cli",
            ),
            patch.object(SecureSubprocessRunner, "_run_subprocess", new=_fake_kicad_cli(captured)),
        ):
            result = asyncio.run(tools["run_erc"](schematic_path=str(schematic)))

        assert result["success"] is True, result
        argv = captured["argv"]
        assert argv[0] == "/usr/bin/kicad-cli"
        assert argv[1:3] == ["sch", "erc"]
        assert argv[3] == str(schematic)
        assert "--output" in argv
        assert "--severity-error" in argv and "--severity-warning" in argv

    def test_schematic_outside_every_root_fails(self, tools, state_project):
        from kcaa.utils.secure_subprocess import SecureSubprocessRunner

        project, _ = state_project
        outside = project.parent / "outside.kicad_sch"
        outside.write_text("(kicad_sch)", encoding="utf-8")
        called: dict = {}
        with (
            patch(
                "kcaa.utils.secure_subprocess.get_kicad_cli_path",
                return_value="/usr/bin/kicad-cli",
            ),
            patch.object(SecureSubprocessRunner, "_run_subprocess", new=_fake_kicad_cli(called)),
        ):
            result = asyncio.run(tools["run_erc"](schematic_path=str(outside)))

        assert result["success"] is False
        assert "outside trusted directories" in result["error"]
        assert called == {}


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
