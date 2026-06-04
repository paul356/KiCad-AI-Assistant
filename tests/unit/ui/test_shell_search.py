"""Pytest wrapper for the shell.js search/find unit tests.

Runs the Node.js test suite and reports results via pytest.
Requires Node.js and jsdom (installed via npm).
"""

import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(
    sys.platform == "win32" and not os.environ.get("CI"),
    reason="Node.js may not be available",
)
def test_shell_search_functions() -> None:
    """Run the Node.js unit tests for shell.js search functions."""
    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    test_script = os.path.join(repo_root, "tests", "unit", "ui", "test_shell_search.js")

    # Check that Node.js is available
    try:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            pytest.skip("Node.js not available")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("Node.js not available")

    # Check that jsdom is installed (required by the test script)
    try:
        result = subprocess.run(
            ["node", "-e", "require('jsdom')"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=repo_root,
        )
        if result.returncode != 0:
            pytest.skip("jsdom not installed (run: npm install)")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("Node.js not available")

    # Run the test script
    result = subprocess.run(
        ["node", test_script],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=repo_root,
    )

    # Print output for visibility in pytest logs
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    assert result.returncode == 0, f"shell.js search tests failed (exit code {result.returncode})"
