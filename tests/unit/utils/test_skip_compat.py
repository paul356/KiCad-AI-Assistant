"""Regression tests for the UTF-8 ``open()`` patch in ``skip_compat``."""

from __future__ import annotations

import builtins
from pathlib import Path
import threading

from kcaa.utils.skip_compat import utf8_open_context

TEXT = "µΩ中文"


def test_concurrent_contexts_do_not_leak_or_degrade(tmp_path: Path) -> None:
    """Two threads must not interleave the process-global open() patch."""
    original_open = builtins.open
    barrier = threading.Barrier(2)
    a_inside = threading.Event()
    b_inside = threading.Event()
    a_exited = threading.Event()
    errors: list[BaseException] = []

    def _write(path: Path) -> None:
        # No explicit encoding: the context must provide UTF-8.
        with open(path, "w") as fh:
            fh.write(TEXT)

    def worker_a() -> None:
        try:
            barrier.wait(timeout=5)
            with utf8_open_context():
                a_inside.set()
                # A lock serialises the contexts, so B cannot arrive here.
                b_inside.wait(timeout=0.5)
                _write(tmp_path / "a.kicad_sch")
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)
        finally:
            a_exited.set()

    def worker_b() -> None:
        try:
            barrier.wait(timeout=5)
            a_inside.wait(timeout=5)
            with utf8_open_context():
                b_inside.set()
                # Without the lock A has already restored open() by now,
                # while this thread is still inside its context.
                a_exited.wait(timeout=5)
                _write(tmp_path / "b.kicad_sch")
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)

    threads = [
        threading.Thread(target=worker_a),
        threading.Thread(target=worker_b),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert (tmp_path / "a.kicad_sch").read_text(encoding="utf-8") == TEXT
        assert (tmp_path / "b.kicad_sch").read_text(encoding="utf-8") == TEXT
        # The last exit must restore the real builtins.open, not a wrapper.
        assert builtins.open is original_open
    finally:
        # Keep a failing assertion from leaking the patch into other tests.
        builtins.open = original_open
