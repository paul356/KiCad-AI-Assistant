#!/usr/bin/env python3
"""
Test runner for KiCad MCP project.
"""
import subprocess
import sys
from pathlib import Path


def run_command(cmd: list[str], description: str) -> int:
    """Run a command and return the exit code."""
    print(f"\n🔍 {description}")
    print(f"Running: {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, check=False)
        if result.returncode == 0:
            print(f"✅ {description} passed")
        else:
            print(f"❌ {description} failed with exit code {result.returncode}")
        return result.returncode
    except FileNotFoundError:
        print(f"❌ Command not found: {cmd[0]}")
        return 1


def main():
    """Run all tests and checks."""
    project_root = Path(__file__).parent

    # Change to project directory
    import os

    os.chdir(project_root)

    exit_code = 0

    # Run linting
    exit_code |= run_command(["uv", "run", "ruff", "check", "kcaa/", "tests/"], "Lint check")

    # Run formatting check
    exit_code |= run_command(
        ["uv", "run", "ruff", "format", "--check", "kcaa/", "tests/"], "Format check"
    )

    # Run type checking
    exit_code |= run_command(["uv", "run", "mypy", "kcaa/"], "Type check")

    # Run tests
    exit_code |= run_command(["uv", "run", "python", "-m", "pytest", "tests/", "-v"], "Unit tests")

    if exit_code == 0:
        print("\n🎉 All checks passed!")
    else:
        print(f"\n💥 Some checks failed (exit code: {exit_code})")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
