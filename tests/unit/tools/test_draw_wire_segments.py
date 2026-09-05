"""Tests for draw_wire_segments — the explicit, no-autorouter wire tool.

Every test verifies that the wire endpoints come back at the exact
coordinates the caller specified — never shifted by KiCad's smart router
or auto-junction logic.  The contract is: explicit in, explicit out.
"""

import asyncio
import os
from pathlib import Path
import shutil
import tempfile

import pytest
import skip

from kcaa.utils.skip_compat import safe_schematic


SCHEMATIC_PATH = str(
    Path(__file__).parent / "fixtures" / "tools_test.kicad_sch"
)


def _make_temp_copy() -> str:
    tmp = tempfile.NamedTemporaryFile(
        suffix=".kicad_sch", delete=False, dir=tempfile.gettempdir()
    )
    tmp.close()
    shutil.copy(SCHEMATIC_PATH, tmp.name)
    return tmp.name


class _MockMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _get_tools() -> dict:
    from kcaa.tools.wire_edit_tools import register_wire_edit_tools

    mock = _MockMCP()
    register_wire_edit_tools(mock)
    return mock.tools


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


@pytest.fixture()
def tmp_sch():
    path = _make_temp_copy()
    yield path
    for p in [path, path + ".bak"]:
        if os.path.exists(p):
            os.unlink(p)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _draw(tools, sch_path, segments, **kwargs):
    return asyncio.run(
        tools["draw_wire_segments"](
            schematic_path=sch_path,
            segments=segments,
            **kwargs,
        )
    )


def _all_wires(sch_path: str) -> list[tuple[float, float, float, float]]:
    """Return every wire segment as (ax, ay, bx, by)."""
    sch = skip.Schematic(sch_path)
    wires = []
    for w in sch.wire:
        wires.append(
            (
                float(w.start.value[0]),
                float(w.start.value[1]),
                float(w.end.value[0]),
                float(w.end.value[1]),
            )
        )
    return wires


def _wire_count_at(sch_path: str, ax, ay, bx, by, tol: float = 0.01) -> int:
    """Count wire segments with the exact endpoints (in either direction)."""
    count = 0
    for wax, way, wbx, wby in _all_wires(sch_path):
        if (
            abs(wax - ax) < tol
            and abs(way - ay) < tol
            and abs(wbx - bx) < tol
            and abs(wby - by) < tol
        ):
            count += 1
        elif (
            abs(wax - bx) < tol
            and abs(way - by) < tol
            and abs(wbx - ax) < tol
            and abs(wby - ay) < tol
        ):
            count += 1
    return count


# ===========================================================================
# 1. Endpoint coordinates are written verbatim
# ===========================================================================


class TestVerbatimPlacement:
    def test_horizontal_segment_written_at_exact_coords(self, tools, tmp_sch):
        """A horizontal segment is written with the exact (sx, sy, ex, ey) given."""
        result = _draw(
            tools,
            tmp_sch,
            [{"start_x": 80.0, "start_y": 100.0, "end_x": 120.0, "end_y": 100.0}],
        )
        assert result.get("success") is True, result
        assert result["segments_written"] == 1

        # File contains a wire at the exact coordinates — no L-bend, no shift.
        assert _wire_count_at(tmp_sch, 80.0, 100.0, 120.0, 100.0) == 1

        # No spurious wires were drawn.
        all_w = _all_wires(tmp_sch)
        assert all_w == [(80.0, 100.0, 120.0, 100.0)], all_w

    def test_vertical_segment_written_at_exact_coords(self, tools, tmp_sch):
        result = _draw(
            tools,
            tmp_sch,
            [{"start_x": 100.0, "start_y": 80.0, "end_x": 100.0, "end_y": 130.0}],
        )
        assert result.get("success") is True, result
        assert _wire_count_at(tmp_sch, 100.0, 80.0, 100.0, 130.0) == 1
        assert _all_wires(tmp_sch) == [(100.0, 80.0, 100.0, 130.0)]

    def test_z_shape_kept_as_three_segments(self, tools, tmp_sch):
        """Caller-specified L / Z shapes are preserved — no merging, no smart re-route.

        A 3-segment Z from (10,10) → (40,10) → (40,30) → (70,30) should produce
        three separate wire entries in the schematic file (no diagonal, no
        single straight line, no re-routed L).  This is the core
        "no-autorouter" contract: the tool must do what was asked, no more.
        """
        segments = [
            {"start_x": 10.0, "start_y": 10.0, "end_x": 40.0, "end_y": 10.0},
            {"start_x": 40.0, "start_y": 10.0, "end_x": 40.0, "end_y": 30.0},
            {"start_x": 40.0, "start_y": 30.0, "end_x": 70.0, "end_y": 30.0},
        ]
        result = _draw(tools, tmp_sch, segments)
        assert result.get("success") is True, result
        assert result["segments_written"] == 3

        written = sorted(_all_wires(tmp_sch))
        expected = sorted(
            [
                (10.0, 10.0, 40.0, 10.0),
                (40.0, 10.0, 40.0, 30.0),
                (40.0, 30.0, 70.0, 30.0),
            ]
        )
        assert written == expected, (written, expected)


# ===========================================================================
# 2. Diagonal and zero-length rejected up front
# ===========================================================================


class TestInputValidation:
    def test_diagonal_segment_rejected(self, tools, tmp_sch):
        """Diagonal endpoints are an error — no segment written, file untouched."""
        result = _draw(
            tools,
            tmp_sch,
            [{"start_x": 10.0, "start_y": 10.0, "end_x": 20.0, "end_y": 20.0}],
        )
        assert "error" in result
        assert "diagonal" in result["error"].lower()
        assert _all_wires(tmp_sch) == []

    def test_zero_length_segment_silently_skipped(self, tools, tmp_sch):
        """A zero-length entry is skipped (not an error) so callers can include sentinels."""
        result = _draw(
            tools,
            tmp_sch,
            [
                {"start_x": 10.0, "start_y": 10.0, "end_x": 10.0, "end_y": 10.0},
                {"start_x": 10.0, "start_y": 10.0, "end_x": 30.0, "end_y": 10.0},
            ],
        )
        assert result.get("success") is True, result
        assert result["segments_written"] == 1
        assert result["segments_skipped"] >= 1
        assert _wire_count_at(tmp_sch, 10.0, 10.0, 30.0, 10.0) == 1

    def test_empty_segments_list_is_error(self, tools, tmp_sch):
        result = _draw(tools, tmp_sch, [])
        assert "error" in result

    def test_non_kicad_sch_extension_rejected(self, tools, tmp_sch):
        bad_path = tmp_sch.replace(".kicad_sch", ".txt")
        result = _draw(
            tools, bad_path, [{"start_x": 0, "start_y": 0, "end_x": 10, "end_y": 0}]
        )
        assert "error" in result
        assert ".kicad_sch" in result["error"]


# ===========================================================================
# 3. The router stays out of the way
# ===========================================================================


class TestNoAutorouter:
    def test_no_obstacle_avoidance(self, tools, tmp_sch):
        """A wire can be drawn through a pin's coordinate — no skip, no dodge.

        The fixture places R2..R5 at (100,100)..(160,100).  Drawing a wire
        from (110, 102.54) to (150, 102.54) lays it across pin 2 of R3 / R4.
        The autorouter would normally fail (collision with pins); the
        explicit tool is allowed to draw anyway.  This is the user's
        responsibility, not the tool's.
        """
        result = _draw(
            tools,
            tmp_sch,
            [{"start_x": 110.0, "start_y": 102.54, "end_x": 150.0, "end_y": 102.54}],
        )
        assert result.get("success") is True, result
        # The wire is written — no error.
        assert _wire_count_at(tmp_sch, 110.0, 102.54, 150.0, 102.54) == 1

    def test_no_segment_merging(self, tools, tmp_sch):
        """Two adjacent collinear segments stay as two (wire ...) entries.

        The smart router would merge them into one; the explicit tool must not.
        """
        result = _draw(
            tools,
            tmp_sch,
            [
                {"start_x": 10.0, "start_y": 50.0, "end_x": 30.0, "end_y": 50.0},
                {"start_x": 30.0, "start_y": 50.0, "end_x": 50.0, "end_y": 50.0},
            ],
        )
        assert result.get("success") is True, result
        assert result["segments_written"] == 2

        # Both segments exist in the file as separate wire entries.
        assert _wire_count_at(tmp_sch, 10.0, 50.0, 30.0, 50.0) == 1
        assert _wire_count_at(tmp_sch, 30.0, 50.0, 50.0, 50.0) == 1


# ===========================================================================
# 4. Auto-junction (default ON) and override
# ===========================================================================


class TestJunctions:
    def test_junction_added_at_wire_interior_endpoint(self, tools, tmp_sch):
        """Default: endpoints on a wire's interior place a junction + split the wire.

        Draw a long wire, then a second wire that ends on the first wire's
        interior.  A junction must be placed at the meeting point.
        """
        long_wire = _draw(
            tools,
            tmp_sch,
            [{"start_x": 10.0, "start_y": 80.0, "end_x": 100.0, "end_y": 80.0}],
        )
        assert long_wire.get("success") is True

        cross = _draw(
            tools,
            tmp_sch,
            [{"start_x": 50.0, "start_y": 70.0, "end_x": 50.0, "end_y": 90.0}],
        )
        assert cross.get("success") is True
        assert len(cross.get("junctions_added", [])) == 1

        sch = skip.Schematic(tmp_sch)
        junctions = [j.at.value[:2] for j in sch.junction]
        assert any(abs(jx - 50.0) < 0.01 and abs(jy - 80.0) < 0.01 for jx, jy in junctions)

    def test_auto_junctions_can_be_disabled(self, tools, tmp_sch):
        """auto_junctions=False: no junction placed even when overlapping a wire."""
        _draw(
            tools,
            tmp_sch,
            [{"start_x": 10.0, "start_y": 80.0, "end_x": 100.0, "end_y": 80.0}],
        )

        cross = _draw(
            tools,
            tmp_sch,
            [{"start_x": 50.0, "start_y": 70.0, "end_x": 50.0, "end_y": 90.0}],
            auto_junctions=False,
        )
        assert cross.get("success") is True
        assert cross.get("junctions_added", []) == []

        sch = skip.Schematic(tmp_sch)
        assert len(list(sch.junction)) == 0, "auto_junctions=False should add no junctions"


# ===========================================================================
# 5. fail_on_overlap
# ===========================================================================


class TestFailOnOverlap:
    def test_fail_on_overlap_true_rejects_overlap(self, tools, tmp_sch):
        """fail_on_overlap=True aborts the whole batch when an overlap is detected."""
        _draw(
            tools,
            tmp_sch,
            [{"start_x": 10.0, "start_y": 60.0, "end_x": 50.0, "end_y": 60.0}],
        )

        result = _draw(
            tools,
            tmp_sch,
            [{"start_x": 30.0, "start_y": 50.0, "end_x": 30.0, "end_y": 70.0}],
            fail_on_overlap=True,
        )
        assert "error" in result
        assert "overlap" in result["error"].lower()
        assert "overlaps_found" in result

        # File unchanged from the first call (the cross was rejected).
        assert _wire_count_at(tmp_sch, 10.0, 60.0, 50.0, 60.0) == 1
        assert _wire_count_at(tmp_sch, 30.0, 50.0, 30.0, 70.0) == 0

    def test_fail_on_overlap_false_splits_and_writes(self, tools, tmp_sch):
        """fail_on_overlap=False (default): overlap is split into two wires + junction."""
        _draw(
            tools,
            tmp_sch,
            [{"start_x": 10.0, "start_y": 60.0, "end_x": 50.0, "end_y": 60.0}],
        )

        result = _draw(
            tools,
            tmp_sch,
            [{"start_x": 30.0, "start_y": 50.0, "end_x": 30.0, "end_y": 70.0}],
            fail_on_overlap=False,
        )
        assert result.get("success") is True
        # The original wire was split (now at least two wires on y=60),
        # and the cross wire was added.
        all_w = _all_wires(tmp_sch)
        horizontal_on_60 = [w for w in all_w if w[1] == 60.0 and w[3] == 60.0]
        assert len(horizontal_on_60) >= 2, (
            f"Expected the original wire to be split; got {horizontal_on_60}"
        )
        assert _wire_count_at(tmp_sch, 30.0, 50.0, 30.0, 70.0) == 1


# ===========================================================================
# 6. Idempotence and batching
# ===========================================================================


class TestIdempotence:
    def test_double_call_same_segments_writes_only_first_time(self, tools, tmp_sch):
        """Calling twice with the same segments should be a no-op on the second call."""
        seg = {"start_x": 10.0, "start_y": 50.0, "end_x": 30.0, "end_y": 50.0}
        first = _draw(tools, tmp_sch, [seg])
        second = _draw(tools, tmp_sch, [seg])

        assert first["segments_written"] == 1
        assert second["segments_written"] == 0
        assert _wire_count_at(tmp_sch, 10.0, 50.0, 30.0, 50.0) == 1

    def test_partial_overlap_dedupes_only_drawn_segments(self, tools, tmp_sch):
        """First call draws A. Second call mixes [A, B] — only B should be new."""
        first = _draw(
            tools,
            tmp_sch,
            [{"start_x": 10.0, "start_y": 50.0, "end_x": 30.0, "end_y": 50.0}],
        )
        assert first["segments_written"] == 1

        # Second call: A again + a new B
        second = _draw(
            tools,
            tmp_sch,
            [
                {"start_x": 10.0, "start_y": 50.0, "end_x": 30.0, "end_y": 50.0},
                {"start_x": 30.0, "start_y": 50.0, "end_x": 50.0, "end_y": 50.0},
            ],
        )
        assert second["segments_written"] == 1

        # File has both A and B (one of each), no duplicates.
        assert _wire_count_at(tmp_sch, 10.0, 50.0, 30.0, 50.0) == 1
        assert _wire_count_at(tmp_sch, 30.0, 50.0, 50.0, 50.0) == 1