"""Guard against hand-built .kicad_pro paths in production code."""

import ast
import io
from pathlib import Path
import re
import tokenize

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_DIRS = ("kcaa", "kicad_plugin", "scripts")

# Exact whitelist with the reason each file may spell the suffix itself.
WHITELIST = {
    REPO_ROOT / "kcaa" / "utils" / "project_resolution.py": "canonical project resolver",
    REPO_ROOT / "kicad_plugin" / "project_resolution.py": "vendored byte-for-byte resolver copy",
    REPO_ROOT / "kcaa" / "utils" / "config.py": "extension table definition",
    REPO_ROOT
    / "kcaa"
    / "tools"
    / "kipy_tools.py": "IPC file-type prompt uses the literal extension",
    REPO_ROOT / "kicad_plugin" / "context_bridge.py": "window-title regex names the project suffix",
}

# Additional textual rules, kept alongside the literal scan.
FORBIDDEN = (
    (
        "pcb-to-project replace",
        re.compile(r"""replace\s*\(\s*['"]\.kicad_pcb['"]\s*,\s*['"]\.kicad_pro['"]"""),
    ),
    (
        "project suffix concatenation",
        re.compile(r"""\+\s*['"]\.kicad_pro['"]"""),
    ),
    (
        "extension slice plus project suffix",
        re.compile(r"""\[:-10\]\s*\+\s*['"]\.kicad_pro['"]"""),
    ),
)


def _production_files():
    for directory in PRODUCTION_DIRS:
        for path in (REPO_ROOT / directory).rglob("*.py"):
            if "__pycache__" in path.parts or "tests" in path.parts:
                continue
            yield path


def _docstring_lines(tree: ast.AST) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            lines.add(first.value.lineno)
    return lines


def _literal_text(token_string: str) -> str | None:
    try:
        value = ast.literal_eval(token_string)
    except (SyntaxError, ValueError):
        # f-strings are not literal-eval-able; strip the prefix and quotes so
        # f"{stem}.kicad_pro" is still inspected.
        match = re.match(r"(?is)^[rubf]*(?:'''|\"\"\"|'|\")(.*)(?:'''|\"\"\"|'|\")$", token_string)
        return match.group(1) if match else None
    return value if isinstance(value, str) else None


def _literal_suffix_violations(path: Path, text: str) -> list[str]:
    tree = ast.parse(text)
    docstrings = _docstring_lines(tree)
    violations: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type != tokenize.STRING or token.start[0] in docstrings:
            continue
        literal = _literal_text(token.string)
        if literal is not None and literal.lower().endswith(".kicad_pro"):
            violations.append(
                f"{path.relative_to(REPO_ROOT)}:{token.start[0]}: "
                f"string literal ends with .kicad_pro: {literal!r}"
            )
    return violations


def test_production_code_has_no_manual_project_path_construction():
    violations = []
    for path in _production_files():
        if path in WHITELIST:
            continue
        text = path.read_text(encoding="utf-8")
        violations.extend(_literal_suffix_violations(path, text))
        for line_number, line in enumerate(text.splitlines(), 1):
            for label, pattern in FORBIDDEN:
                if pattern.search(line):
                    violations.append(
                        f"{path.relative_to(REPO_ROOT)}:{line_number}: {label}: {line.strip()}"
                    )
    assert not violations, "manual .kicad_pro construction found:\n" + "\n".join(violations)
