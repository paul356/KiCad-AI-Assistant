"""Tests for autorouter constraint extraction and FreeRouting CLI integration."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

# Module under test needs to be importable without wx/pcbnew.
# _run_subprocess and _strip_nets_from_dsn have zero pcbnew deps.
from kicad_plugin.autorouter import (
    _run_subprocess,
    _strip_nets_from_dsn,
    start_freerouting_thread,
)

# ---------------------------------------------------------------------------
# Tests for _strip_nets_from_dsn
# ---------------------------------------------------------------------------


class TestStripNetsFromDsn:
    """Unit tests for the DSN net-stripping helper."""

    def test_removes_single_bare_net(self, tmp_path):
        p = tmp_path / "test.dsn"
        p.write_text("(network\n  (net GND\n    (pins ...))\n  (net VCC\n    (pins ...))\n)")
        _strip_nets_from_dsn(str(p), ["GND"])
        content = p.read_text()
        assert "GND" not in content
        assert "VCC" in content

    def test_removes_quoted_net_with_parens_in_name(self, tmp_path):
        p = tmp_path / "test.dsn"
        p.write_text(
            '(network\n  (net "Net-(U1-PROG)"\n    (pins ...))\n  (net GND\n    (pins ...))\n)'
        )
        _strip_nets_from_dsn(str(p), ["Net-(U1-PROG)"])
        content = p.read_text()
        assert "U1-PROG" not in content
        assert "GND" in content

    def test_nop_when_net_not_found(self, tmp_path):
        original = "(network\n  (net GND\n    (pins ...))\n)"
        p = tmp_path / "test.dsn"
        p.write_text(original)
        _strip_nets_from_dsn(str(p), ["NONEXISTENT"])
        assert p.read_text().strip() == original.strip()

    def test_nop_when_net_names_empty(self, tmp_path):
        original = "(network\n  (net GND\n    (pins ...))\n)"
        p = tmp_path / "test.dsn"
        p.write_text(original)
        _strip_nets_from_dsn(str(p), [])
        assert p.read_text().strip() == original.strip()


# ---------------------------------------------------------------------------
# Tests for _run_subprocess — FreeRouting command construction
# ---------------------------------------------------------------------------

JAR_PATH = "/fake/plugin/freerouting.jar"


class TestRunSubprocessConstraintExtraction:
    """Verify that clearance_mm parameter adds the -dr flag."""

    @pytest.fixture(autouse=True)
    def _patch_deps(self):
        """Mock filesystem and subprocess dependencies."""
        with (
            patch("kicad_plugin.autorouter.find_freerouting_jar", return_value=JAR_PATH),
            patch("os.path.isfile", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "DRC: 0 violations\n"
            mock_result.stderr = ""
            mock_run.return_value = mock_result
            self.mock_run = mock_run
            yield

    def test_no_clearance_produces_no_dr_flag(self, tmp_path):
        dsn = tmp_path / "board.dsn"
        ses = tmp_path / "board.ses"
        dsn.write_text("(design)")
        ses.write_text("")  # pre-create so isfile() check passes

        _run_subprocess(str(dsn), str(ses), None, None, 50)

        cmd = self.mock_run.call_args[0][0]
        assert "-dr" not in cmd

    def test_clearance_adds_dr_flag(self, tmp_path):
        dsn = tmp_path / "board.dsn"
        ses = tmp_path / "board.ses"
        dsn.write_text("(design)")
        ses.write_text("")

        _run_subprocess(str(dsn), str(ses), None, None, 50, clearance_mm=0.25)

        cmd = self.mock_run.call_args[0][0]
        assert "-dr" in cmd
        dr_idx = cmd.index("-dr")
        assert cmd[dr_idx + 1] == "0.25"

    def test_clearance_float_conversion(self, tmp_path):
        dsn = tmp_path / "board.dsn"
        ses = tmp_path / "board.ses"
        dsn.write_text("(design)")
        ses.write_text("")

        _run_subprocess(str(dsn), str(ses), None, None, 50, clearance_mm=0.2)

        cmd = self.mock_run.call_args[0][0]
        dr_idx = cmd.index("-dr")
        assert cmd[dr_idx + 1] == "0.2"

    def test_jar_not_found_short_circuits(self):
        with patch("kicad_plugin.autorouter.find_freerouting_jar", return_value=None):
            success, msg, stdout, stderr = _run_subprocess(
                "/x.dsn", "/x.ses", None, None, 50, clearance_mm=0.25
            )
        assert not success
        assert "not found" in msg.lower()

    def test_java_not_found(self, tmp_path):
        dsn = tmp_path / "board.dsn"
        ses = tmp_path / "board.ses"
        dsn.write_text("(design)")
        ses.write_text("")

        with (
            patch("kicad_plugin.autorouter.find_freerouting_jar", return_value=JAR_PATH),
            patch("subprocess.run", side_effect=FileNotFoundError),
        ):
            success, msg, stdout, stderr = _run_subprocess(str(dsn), str(ses), None, None, 50)
        assert not success
        assert "java" in msg.lower()

    def test_timeout(self, tmp_path):
        dsn = tmp_path / "board.dsn"
        ses = tmp_path / "board.ses"
        dsn.write_text("(design)")
        ses.write_text("")

        with (
            patch("kicad_plugin.autorouter.find_freerouting_jar", return_value=JAR_PATH),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 600)),
        ):
            success, msg, stdout, stderr = _run_subprocess(str(dsn), str(ses), None, None, 50)
        assert not success
        assert "timed out" in msg.lower()

    def test_missing_ses_file(self, tmp_path):
        dsn = tmp_path / "board.dsn"
        ses = tmp_path / "board.ses"
        dsn.write_text("(design)")
        # Don't create ses file

        with (
            patch("kicad_plugin.autorouter.find_freerouting_jar", return_value=JAR_PATH),
            patch("subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.stderr = ""
            mock_run.return_value = mock_result
            # ses file does not exist
            with patch("os.path.isfile", side_effect=lambda p: p != str(ses)):
                success, msg, stdout, stderr = _run_subprocess(str(dsn), str(ses), None, None, 50)
        assert not success
        assert "ses" in msg.lower()

    def test_on_progress_called(self, tmp_path):
        dsn = tmp_path / "board.dsn"
        ses = tmp_path / "board.ses"
        dsn.write_text("(design)")
        ses.write_text("")

        progress_msgs = []

        def on_progress(msg):
            progress_msgs.append(msg)

        with patch("kicad_plugin.autorouter.find_freerouting_jar", return_value=JAR_PATH):
            _run_subprocess(str(dsn), str(ses), on_progress, None, 5, clearance_mm=0.15)

        assert any("FreeRouting" in m for m in progress_msgs)

    def test_ignore_nets_strips_before_routing(self, tmp_path):
        dsn = tmp_path / "board.dsn"
        ses = tmp_path / "board.ses"
        dsn.write_text("(network\n  (net VCC\n    (pins 1))\n  (net GND\n    (pins 2))\n)")
        ses.write_text("")

        with patch("kicad_plugin.autorouter.find_freerouting_jar", return_value=JAR_PATH):
            _run_subprocess(str(dsn), str(ses), None, ["GND"], 50)

        # After _run_subprocess, the DSN should have GND stripped
        content = dsn.read_text()
        assert "GND" not in content
        assert "VCC" in content


# ---------------------------------------------------------------------------
# Tests for start_freerouting_thread
# ---------------------------------------------------------------------------


class TestStartFreeroutingThread:
    """Verify thread creation and parameter passing."""

    def test_thread_is_daemon_and_named(self, tmp_path):
        dsn = tmp_path / "board.dsn"
        ses = tmp_path / "board.ses"
        dsn.write_text("(design)")

        done_called = []

        def on_done(success, msg, stdout, stderr):
            done_called.append(success)

        with (
            patch("kicad_plugin.autorouter.find_freerouting_jar", return_value=JAR_PATH),
            patch("kicad_plugin.autorouter._run_subprocess", return_value=(True, "ok", "", "")),
        ):
            t = start_freerouting_thread(
                str(dsn),
                str(ses),
                on_done,
                max_passes=10,
            )
            t.join(timeout=5)

        assert t.daemon
        assert t.name == "freerouting-worker"
        assert done_called == [True]

    def test_clearance_passed_to_run_subprocess(self, tmp_path):
        dsn = tmp_path / "board.dsn"
        ses = tmp_path / "board.ses"
        dsn.write_text("(design)")

        with (
            patch("kicad_plugin.autorouter.find_freerouting_jar", return_value=JAR_PATH),
            patch(
                "kicad_plugin.autorouter._run_subprocess", return_value=(True, "ok", "", "")
            ) as mock_run,
        ):
            t = start_freerouting_thread(
                str(dsn),
                str(ses),
                on_done=lambda s, m, so, se: None,
                clearance_mm=0.3,
            )
            t.join(timeout=5)

        # _run_subprocess should have been called with clearance_mm
        call_args = mock_run.call_args
        assert call_args is not None
        # positional: dsn_path, ses_path, on_progress, ignore_nets, max_passes, clearance_mm
        assert call_args[0][4] == 50  # max_passes (default)
        assert call_args[0][5] == 0.3  # clearance_mm

    def test_clearance_none_not_passed_explicitly(self, tmp_path):
        dsn = tmp_path / "board.dsn"
        ses = tmp_path / "board.ses"
        dsn.write_text("(design)")

        with (
            patch("kicad_plugin.autorouter.find_freerouting_jar", return_value=JAR_PATH),
            patch(
                "kicad_plugin.autorouter._run_subprocess", return_value=(True, "ok", "", "")
            ) as mock_run,
        ):
            t = start_freerouting_thread(
                str(dsn),
                str(ses),
                on_done=lambda s, m, so, se: None,
            )
            t.join(timeout=5)

        call_args = mock_run.call_args
        # clearance_mm default None should still be position 5
        assert call_args[0][5] is None
