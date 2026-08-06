"""PDF text extraction helper.

Runs inside the plugin venv (which has PyMuPDF installed) as a subprocess
invoked from the UI panel.  Extracts text page-by-page and prefixes each
page's content with a ``[P{n}]`` marker so the LLM can cite specific pages.

Usage::

    python -m kicad_plugin.pdf_extractor <pdf_path> [--max-pages N]

Prints the extracted text to stdout (UTF-8).  If PyMuPDF is not installed,
prints a JSON error object to stderr and exits with code 1.
"""

from __future__ import annotations

import argparse
import json
import sys


def extract_text(pdf_path: str, max_pages: int = 0) -> str:
    """Extract text from *pdf_path* with page-number prefixes.

    Args:
        pdf_path: Path to the PDF file.
        max_pages: Maximum number of pages to extract (0 = all pages).

    Returns:
        Extracted text with ``[P1]``, ``[P2]`` … prefixes.
    """
    import fitz  # PyMuPDF

    doc = fitz.open(pdf_path)
    try:
        total = len(doc)
        limit = total if max_pages <= 0 else min(max_pages, total)
        parts: list[str] = []
        for i in range(limit):
            page = doc.load_page(i)
            text = page.get_text("text").strip()
            if text:
                parts.append(f"[P{i + 1}] {text}")
        if limit < total:
            parts.append(f"\n[... {total - limit} more page(s) omitted]")
        return "\n\n".join(parts)
    finally:
        doc.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract text from a PDF.")
    parser.add_argument("pdf_path", help="Path to the PDF file")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Maximum pages to extract (0 = all)",
    )
    args = parser.parse_args()

    try:
        result = extract_text(args.pdf_path, args.max_pages)
    except ImportError:
        json.dump(
            {"error": "PyMuPDF (fitz) not installed in the plugin venv."},
            sys.stderr,
        )
        return 1
    except Exception as e:  # noqa: BLE001 — report any extraction failure
        json.dump({"error": str(e)}, sys.stderr)
        return 1

    sys.stdout.write(result)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
