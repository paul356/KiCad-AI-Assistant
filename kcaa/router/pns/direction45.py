"""
45-degree polyline skeleton generation (KiCad DIRECTION_45 port).

Port of ``libs/kimath/src/geometry/direction_45.cpp::BuildInitialTrace``:
given two points and a corner mode, build the skeleton polyline for a
45-degree (or 90-degree) route.  Rounded modes insert an arc fillet.

Coordinate convention matches the rest of the router: **Y-down screen
coordinates** (0 deg = right, 90 deg = down), same as KiCad PCB files.

Angle convention (verified against KiCad's ``qa/tests/libs/kimath/geometry/
test_shape_arc.cpp`` golden data):
* positive sweep = **counter-clockwise in the standard math sense** applied
  to the (x, y) values, i.e. KiCad's EDA_ANGLE;
* KiCad's ``RotatePoint`` rotates the *other* way (positive angle decreases
  the atan2 angle); we mirror that with ``_rot_cw``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math

_TAU = 2.0 * math.pi
_DEG = math.pi / 180.0
_ANGLE_45 = 45 * _DEG
_MIN_PRECISION = 1e-6


class CornerMode(str, Enum):
    """Corner style for the skeleton trace (KiCad DIRECTION_45::CORNER_MODE)."""

    MITERED_45 = "mitered45"
    ROUNDED_45 = "rounded45"
    ROUNDED_90 = "rounded90"
    MITERED_90 = "mitered90"


@dataclass(frozen=True)
class ArcSeg:
    """A circular-arc segment in KiCad's 3-point form (start/mid/end).

    ``mid`` is a point the arc passes through (KiCad's ``(arc (start)
    (mid) (end))`` S-expression form): start/end/mid together determine
    center, radius and direction.
    """

    start: tuple[float, float]
    mid: tuple[float, float]
    end: tuple[float, float]
    radius: float

    def center(self) -> tuple[float, float]:
        """Reconstruct the arc center from the 3-point form."""
        x1, y1 = self.start
        x2, y2 = self.mid
        x3, y3 = self.end
        den = 2.0 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
        if abs(den) < 1e-12:
            raise ValueError("collinear arc points")
        ux = (
            (x1 * x1 + y1 * y1) * (y2 - y3)
            + (x2 * x2 + y2 * y2) * (y3 - y1)
            + (x3 * x3 + y3 * y3) * (y1 - y2)
        ) / den
        uy = (
            (x1 * x1 + y1 * y1) * (x3 - x2)
            + (x2 * x2 + y2 * y2) * (x1 - x3)
            + (x3 * x3 + y3 * y3) * (x2 - x1)
        ) / den
        return (ux, uy)

    def sweep(self) -> float:
        """Signed central sweep in radians (positive = math CCW)."""
        cx, cy = self.center()
        a0 = math.atan2(self.start[1] - cy, self.start[0] - cx)
        a1 = math.atan2(self.end[1] - cy, self.end[0] - cx)
        sweep = a1 - a0
        while sweep > math.pi:
            sweep -= _TAU
        while sweep < -math.pi:
            sweep += _TAU
        return sweep

    def total_sweep(self) -> float:
        """Unsigned sweep in (0, 2*pi) going through ``mid``.

        Mirrors KiCad's ``SHAPE_ARC::GetCentralAngle`` (angle measured via
        the mid point, so a near-full-circle arc reports > 180 deg), and
        returns what ``ArcHull`` uses for its octagon fallback test.
        """
        cx, cy = self.center()
        a1 = math.atan2(self.start[1] - cy, self.start[0] - cx)
        am = math.atan2(self.mid[1] - cy, self.mid[0] - cx)
        a2 = math.atan2(self.end[1] - cy, self.end[0] - cx)
        ccw = (a2 - a1) % _TAU
        mid_ccw = (am - a1) % _TAU
        if 0.0 < mid_ccw < ccw:
            return ccw
        if mid_ccw > ccw:
            return ccw - _TAU
        return ccw

    def length(self) -> float:
        return abs(self.sweep()) * self.radius

    def as_polyline(self, n: int = 32) -> list[tuple[float, float]]:
        """Sample to ``n`` points including both endpoints."""
        cx, cy = self.center()
        a0 = math.atan2(self.start[1] - cy, self.start[0] - cx)
        sweep = self.sweep()
        return [
            (
                cx + self.radius * math.cos(a0 + sweep * k / n),
                cy + self.radius * math.sin(a0 + sweep * k / n),
            )
            for k in range(n + 1)
        ]


@dataclass
class Trace:
    """Skeleton polyline with optional arc fillets.

    ``points`` are anchor vertices in order; ``arcs[i]``, when not None,
    replaces the straight segment ``points[i] -> points[i+1]``.
    """

    points: list[tuple[float, float]] = field(default_factory=list)
    arcs: list[ArcSeg | None] = field(default_factory=list)

    def start(self) -> tuple[float, float]:
        return self.points[0]

    def end(self) -> tuple[float, float]:
        return self.points[-1]

    def length(self) -> float:
        total = 0.0
        for i in range(len(self.points) - 1):
            arc = self.arcs[i] if i < len(self.arcs) else None
            if arc is not None:
                total += arc.length()
            else:
                p, q = self.points[i], self.points[i + 1]
                total += math.hypot(q[0] - p[0], q[1] - p[1])
        return total

    def as_polyline(self, arc_pts: int = 32) -> list[tuple[float, float]]:
        """Sample arcs and return a plain polyline (for DRC / rendering)."""
        out = [self.points[0]]
        for i in range(len(self.points) - 1):
            arc = self.arcs[i] if i < len(self.arcs) else None
            if arc is None:
                out.append(self.points[i + 1])
            else:
                out.extend(arc.as_polyline(n=arc_pts)[1:])
        return out


# --------------------------------------------------------------------------
# Arc construction helpers (KiCad SHAPE_ARC ports)
# --------------------------------------------------------------------------


def _rot_cw(v: tuple[float, float], theta: float) -> tuple[float, float]:
    """KiCad RotatePoint: positive angle rotates the point in the math-CW
    direction (the atan2 angle of the vector decreases by theta)."""
    c, s = math.cos(theta), math.sin(theta)
    return (v[0] * c + v[1] * s, -v[0] * s + v[1] * c)


def _resize(v: tuple[float, float], length: float) -> tuple[float, float]:
    L = math.hypot(v[0], v[1])
    if L == 0:
        return (0.0, 0.0)
    return (v[0] * length / L, v[1] * length / L)


def arc_from_start_end_angle(
    start: tuple[float, float],
    end: tuple[float, float],
    a_angle: float,
) -> ArcSeg:
    """Arc from start/end with total central sweep ``a_angle`` (radians,
    positive = math CCW).  Port of KiCad ConstructFromStartEndAngle."""
    x1, y1 = start
    x2, y2 = end
    if a_angle == 0:
        raise ValueError("zero sweep arc")
    chord = math.hypot(x2 - x1, y2 - y1)
    if chord < _MIN_PRECISION:
        raise ValueError("degenerate arc (start == end)")
    half = a_angle / 2.0
    radius = chord / (2.0 * abs(math.sin(half)))
    dx, dy = (x2 - x1) / chord, (y2 - y1) / chord
    nx, ny = -dy, dx  # math-CCW normal to the chord
    h = radius * math.cos(half)
    sgn = 1.0 if a_angle > 0 else -1.0
    cx = (x1 + x2) / 2.0 + nx * h * sgn
    cy = (y1 + y2) / 2.0 + ny * h * sgn
    # mid = start rotated by a_angle/2 in the sweep direction
    v = (x1 - cx, y1 - cy)
    mv = _rot_cw(v, -half)  # positive half => math CCW
    return ArcSeg(start=start, mid=(cx + mv[0], cy + mv[1]), end=end, radius=radius)


def arc_from_start_end_center(
    start: tuple[float, float],
    end: tuple[float, float],
    center: tuple[float, float],
    clockwise: bool,
) -> ArcSeg:
    """Arc from start/end about a given center.  Port of KiCad
    ConstructFromStartEndCenter.  ``clockwise`` rotates toward the math-CW
    direction (positive aAngle in KiCad)."""
    cx, cy = center
    radius = math.hypot(start[0] - cx, start[1] - cy)
    a0 = math.atan2(start[1] - cy, start[0] - cx)
    a1 = math.atan2(end[1] - cy, end[0] - cx)
    angle = (math.degrees(a1 - a0) % 360.0) * _DEG
    if clockwise:
        angle -= _TAU
    v = (start[0] - cx, start[1] - cy)
    mv = _rot_cw(v, -angle / 2.0)
    return ArcSeg(start=start, mid=(cx + mv[0], cy + mv[1]), end=end, radius=radius)


# --------------------------------------------------------------------------
# BuildInitialTrace port
# --------------------------------------------------------------------------


def _sign(x: float) -> int:
    return 1 if x >= 0 else -1


def build_initial_trace(
    p0: tuple[float, float],
    p1: tuple[float, float],
    mode: str | CornerMode = CornerMode.MITERED_45,
    start_diagonal: bool = False,
) -> Trace:
    """Build the skeleton polyline from P0 to P1 (KiCad BuildInitialTrace port).

    Args:
        p0: Start point ``(x, y)``.
        p1: End point ``(x, y)``.
        mode: Corner mode for the direction change; a CornerMode member or
            its plain string value (``"mitered45"`` …).
        start_diagonal: when True the first leg is the diagonal one
            (45 modes) / the arc-lead arrangement (90 modes), matching
            KiCad's ``aStartDiagonal``.
    """
    aP0, aP1 = p0, p1
    w = abs(aP1[0] - aP0[0])
    h = abs(aP1[1] - aP0[1])
    sw = _sign(aP1[0] - aP0[0])
    sh = _sign(aP1[1] - aP0[1])
    is90mode = mode in (CornerMode.ROUNDED_90, CornerMode.MITERED_90)

    # Shortcut: single segment for axis-aligned or (45-mode) square spans.
    if w == 0 or h == 0 or (not is90mode and h == w):
        return Trace(points=[aP0, aP1])

    if is90mode:
        if start_diagonal == (h >= w):
            mp0 = (w * sw, 0.0)  # direction E
        else:
            mp0 = (0.0, sh * h)  # direction N
        mp1: tuple[float, float] | None = None
        tangent = 0.0
    else:
        if w > h:
            mp0 = ((w - h) * sw, 0.0)  # direction E
            mp1 = (h * sw, h * sh)  # direction NE
            tangent = (w - h) - math.hypot(*mp1)
        else:
            mp0 = (0.0, sh * (h - w))  # direction N
            mp1 = (sw * w, sh * w)  # direction NE
            tangent = (h - w) - math.hypot(*mp1)

    trace = Trace(points=[aP0])

    if mode == CornerMode.MITERED_45:
        assert mp1 is not None  # nosec B101 -- invariant: 45 modes compute mp1 above
        mid = mp1 if start_diagonal else mp0
        trace.points.append((aP0[0] + mid[0], aP0[1] + mid[1]))
        trace.points.append(aP1)
        return trace

    if mode == CornerMode.MITERED_90:
        trace.points.append((aP0[0] + mp0[0], aP0[1] + mp0[1]))
        trace.points.append(aP1)
        return trace

    if mode == CornerMode.ROUNDED_45:
        assert mp1 is not None  # nosec B101 -- invariant: 45 modes compute mp1 above
        if w == h:
            return Trace(points=[aP0, aP1])
        rotation_sign = (sw * sh * -1) if (w > h) else (sw * sh)
        if start_diagonal:
            if tangent >= 0:
                # Arc at the start: straight arc-end -> aP1 follows mp0.
                arc_end = (
                    aP1[0] - _resize(mp0, tangent)[0],
                    aP1[1] - _resize(mp0, tangent)[1],
                )
                arc = arc_from_start_end_angle(aP0, arc_end, _ANGLE_45 * rotation_sign)
                trace.points.append(arc_end)
                trace.arcs.append(arc)
                trace.arcs.append(None)
                trace.points.append(aP1)
            else:
                # Arc at the end: straight aP0 -> arc_start follows mp1.
                arc_start = (
                    aP0[0] + _resize(mp1, abs(tangent))[0],
                    aP0[1] + _resize(mp1, abs(tangent))[1],
                )
                arc = arc_from_start_end_angle(arc_start, aP1, _ANGLE_45 * rotation_sign)
                trace.arcs.append(None)
                trace.points.append(arc_start)
                trace.arcs.append(arc)
                trace.points.append(aP1)
        else:
            if tangent >= 0:
                # Arc at the end: straight aP0 -> arc_start follows mp0.
                arc_start = (
                    aP0[0] + _resize(mp0, tangent)[0],
                    aP0[1] + _resize(mp0, tangent)[1],
                )
                arc = arc_from_start_end_angle(arc_start, aP1, -_ANGLE_45 * rotation_sign)
                trace.arcs.append(None)
                trace.points.append(arc_start)
                trace.arcs.append(arc)
                trace.points.append(aP1)
            else:
                # Arc at the start, constructed about a center derived from
                # mp0 rotated 90 deg (KiCad constructs the endpoint this way
                # to guarantee tangency, then snaps it onto aP1's x or y).
                center_dir = _rot_cw(mp0, 90.0 * _DEG * rotation_sign)
                diag_len = math.sqrt(
                    2.0 * math.hypot(*mp0) ** 2
                    - 2.0 * math.hypot(*mp0) ** 2 * math.cos(3 * math.pi / 4)
                )
                arc_radius = diag_len / (2.0 * math.cos(67.5 * _DEG))
                arc_center = (
                    aP0[0] + _resize(center_dir, arc_radius)[0],
                    aP0[1] + _resize(center_dir, arc_radius)[1],
                )
                # SHAPE_ARC(center, aP0, -45deg * rotSign): endpoint from the
                # center construction.
                v = (aP0[0] - arc_center[0], aP0[1] - arc_center[1])
                ev = _rot_cw(v, -(-_ANGLE_45 * rotation_sign))
                endpoint = (arc_center[0] + ev[0], arc_center[1] + ev[1])
                # Snap endpoint onto aP1's x or y when very close (KiCad
                # fixup). Never triggered in the exact float math normally,
                # but keeps the port faithful.
                if abs(endpoint[1] - aP1[1]) < _MIN_PRECISION:
                    endpoint = (endpoint[0], aP1[1])
                elif abs(endpoint[0] - aP1[0]) < _MIN_PRECISION:
                    endpoint = (aP1[0], endpoint[1])
                if endpoint == aP0:
                    trace.points.append(aP0)
                    trace.arcs.append(None)
                else:
                    arc = arc_from_start_end_angle(aP0, endpoint, -_ANGLE_45 * rotation_sign)
                    trace.points.append(endpoint)
                    trace.arcs.append(arc)
                trace.arcs.append(None)
                trace.points.append(aP1)
        return trace

    # ROUNDED_90
    if w == h:
        mp0e = (w * sw, 0.0) if start_diagonal == (h >= w) else (0.0, sh * h)
        center = (aP1[0] - mp0e[0], aP1[1] - mp0e[1])
        cw = (sh == sw) != start_diagonal
        arc = arc_from_start_end_center(aP0, aP1, center, cw)
        trace.arcs.append(arc)
        trace.points.append(aP1)
    elif start_diagonal:
        # Arc first, then a straight leg.
        if h > w:
            y = aP0[1] + w * sh
            arc_end = (aP1[0], y)
            arc_center = (aP0[0], y)
            cw = sh != sw
        else:
            x = aP0[0] + h * sw
            arc_end = (x, aP1[1])
            arc_center = (x, aP0[1])
            cw = sh == sw
        arc = arc_from_start_end_center(aP0, arc_end, arc_center, cw)
        trace.points.append(arc_end)
        trace.arcs.append(arc)
        trace.arcs.append(None)
        trace.points.append(aP1)
    else:
        # Straight leg first, then the arc.
        if w > h:
            x = aP1[0] - h * sw
            arc_end = (x, aP0[1])
            arc_center = (x, aP1[1])
            cw = sh != sw
        else:
            y = aP1[1] - w * sh
            arc_end = (aP0[0], y)
            arc_center = (aP1[0], y)
            cw = sh == sw
        trace.arcs.append(None)
        trace.points.append(arc_end)
        arc = arc_from_start_end_center(arc_end, aP1, arc_center, cw)
        trace.arcs.append(arc)
        trace.points.append(aP1)
    return trace
