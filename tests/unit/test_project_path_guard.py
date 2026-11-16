"""Guard against hand-built .kicad_pro paths in production code.

This is a lint for the historical spelling shapes of manual path
construction, not a sandbox.  Fragment assembly that never produces a
literal ending in ``.kicad_pro`` (for example ``".kicad_" + "pro"`` or
``"".join(["x", ".kicad_", "pro"])``) is outside its coverage.
"""

import ast
import io
from pathlib import Path
import re
import tokenize

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_DIRS = ("kcaa", "kicad_plugin", "scripts")

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


def _is_bare_literal_context(parent: ast.AST, child: ast.AST) -> bool:
    """Return True for a standalone extension literal, not a construction."""
    if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is child:
        return True
    if isinstance(parent, ast.Dict) and any(value is child for value in parent.values):
        return True
    if isinstance(parent, ast.Call) and any(arg is child for arg in parent.args):
        return isinstance(parent.func, ast.Attribute) and parent.func.attr == "endswith"
    return False


def _bare_literal_positions(tree: ast.AST) -> set[tuple[int, int]]:
    """Return ``(line, column)`` of exact ``.kicad_pro`` literals used bare."""
    allowed: set[tuple[int, int]] = set()
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            if (
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and child.value.lower() == ".kicad_pro"
                and _is_bare_literal_context(parent, child)
            ):
                allowed.add((child.lineno, child.col_offset))
    return allowed


def _literal_text(token_string: str) -> str | None:
    try:
        value = ast.literal_eval(token_string)
    except (SyntaxError, ValueError):
        # f-strings are not literal-eval-able; strip the prefix and quotes so
        # f"{stem}.kicad_pro" is still inspected.
        match = re.match(r"(?is)^[rubf]*(?:'''|\"\"\"|'|\")(.*)(?:'''|\"\"\"|'|\")$", token_string)
        return match.group(1) if match else None
    return value if isinstance(value, str) else None


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return path.name


def _literal_suffix_violations(path: Path, text: str) -> list[str]:
    tree = ast.parse(text)
    docstrings = _docstring_lines(tree)
    bare_positions = _bare_literal_positions(tree)
    violations: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type != tokenize.STRING or token.start[0] in docstrings:
            continue
        literal = _literal_text(token.string)
        if literal is None or not literal.lower().endswith(".kicad_pro"):
            continue
        if literal.lower() == ".kicad_pro" and token.start in bare_positions:
            continue
        violations.append(
            f"{_display_path(path)}:{token.start[0]}: "
            f"string literal ends with .kicad_pro: {literal!r}"
        )
    return violations


def find_violations(path: Path, text: str | None = None) -> list[str]:
    """Return all guard violations for *path* (literal scan + regex rules)."""
    source = text if text is not None else path.read_text(encoding="utf-8")
    violations = _literal_suffix_violations(path, source)
    for line_number, line in enumerate(source.splitlines(), 1):
        for label, pattern in FORBIDDEN:
            if pattern.search(line):
                violations.append(f"{_display_path(path)}:{line_number}: {label}: {line.strip()}")
    return violations


def test_production_code_has_no_manual_project_path_construction():
    violations = []
    for path in _production_files():
        violations.extend(find_violations(path))
    assert not violations, "manual .kicad_pro construction found:\n" + "\n".join(violations)


def test_literal_scan_flags_fstring_and_percent_with_line_numbers(tmp_path):
    module = tmp_path / "sample.py"
    text = (
        "def build(stem):\n"
        '    first = f"{stem}.kicad_pro"\n'
        '    second = "%s.kicad_pro" % stem\n'
        "    return first, second\n"
    )
    module.write_text(text)

    violations = _literal_suffix_violations(module, text)

    assert len(violations) == 2
    assert ":2:" in violations[0]
    assert ":3:" in violations[1]


def test_literal_scan_skips_docstrings_and_comments(tmp_path):
    module = tmp_path / "sample.py"
    text = (
        "def note():\n"
        '    """Docstring ends with .kicad_pro"""\n'
        "    # Comment mentions .kicad_pro\n"
        "    return 1\n"
    )
    module.write_text(text)

    assert _literal_suffix_violations(module, text) == []


def test_literal_scan_allows_bare_extension_literals(tmp_path):
    module = tmp_path / "sample.py"
    text = (
        'SUFFIX = ".kicad_pro"\n\ndef is_project(path):\n    return path.endswith(".kicad_pro")\n'
    )
    module.write_text(text)

    assert _literal_suffix_violations(module, text) == []
