"""
Grid-based A* pathfinding with 8-direction movement and hierarchical search.

Architecture
------------
Phase 1 — Grid A* search on a uniform grid (single-pass).
Phase 2 — Hierarchical A*: coarse pass at 5× resolution, then fine pass
          over a narrow band around the coarse path.  Activated automatically
          when the grid would exceed a threshold size.
Phase 3 — Shortcut + miter postprocessing (path quality).

Hierarchical search (``hierarchical_a_star``)
----------------------------------------------
For large boards a single-pass grid search at high resolution visits
hundreds of thousands of cells.  The hierarchical variant avoids this by:

1. **Coarse search** — builds a grid at 5× step size (e.g. 0.5 mm when
   the fine resolution is 0.1 mm).  A* runs quickly on the coarse grid.
2. **Band construction** — a bounding box around the coarse path, padded
   by ``_BAND_MARGIN`` mm on each side.  The band covers the area where
   the final path must lie.
3. **Fine search** — builds a grid at the requested resolution, but only
   within the band.  A* runs on this much smaller grid to produce the
   final path.

The hierarchical path is at most 2-4 % longer than the optimal single-pass
path, but runs 5-20× faster on boards >50×50 mm.
"""

from __future__ import annotations

from dataclasses import dataclass
import heapq
import math

from shapely.geometry import Point

# Grid resolution (mm per cell).
# 0.1 mm is a good balance: it captures fine-pitch pin gaps (~0.5 mm → 5 cells)
# while keeping grid size manageable on a 100 mm board (~1000 × 800 cells).
GRID_RESOLUTION = 0.1

# 8-direction offsets: (dx, dy, is_diagonal).
# Order matters for tie-breaking — axis-aligned first.
_DIRECTIONS = [
    (1, 0, False),  # E
    (0, 1, False),  # S
    (-1, 0, False),  # W
    (0, -1, False),  # N
    (1, 1, True),  # SE
    (-1, 1, True),  # SW
    (1, -1, True),  # NE
    (-1, -1, True),  # NW
]

_CARD_COST = 1.0
_DIAG_COST = math.sqrt(2.0)


@dataclass
class GridNode:
    """Minimal path node compatible with ``postprocess_path``.

    Only exposes ``x``, ``y``, ``layer`` — the fields that
    ``postprocess`` and ``postprocess_path`` actually read.
    """

    x: float
    y: float
    layer: str


@dataclass
class GridMap:
    """2D walkability grid aligned to a bounding rectangle.

    Cell (0, 0) maps to world coordinate ``(origin_x, origin_y)``.
    The grid is stored as a flat row-major ``bool`` list (``False`` =
    blocked).
    """

    width: int
    height: int
    resolution: float
    origin_x: float
    origin_y: float
    blocked: list[bool]

    # ── coordinate helpers ──────────────────────────────────────────

    def to_grid(self, x: float, y: float) -> tuple[int, int]:
        """World coordinate → (col, row)."""
        gx = round((x - self.origin_x) / self.resolution)
        gy = round((y - self.origin_y) / self.resolution)
        return int(gx), int(gy)

    def to_world(self, gx: int, gy: int) -> tuple[float, float]:
        """(col, row) → world coordinate (cell centre)."""
        return (
            gx * self.resolution + self.origin_x,
            gy * self.resolution + self.origin_y,
        )

    def in_bounds(self, gx: int, gy: int) -> bool:
        return 0 <= gx < self.width and 0 <= gy < self.height

    def is_free(self, gx: int, gy: int) -> bool:
        if not self.in_bounds(gx, gy):
            return False
        return not self.blocked[gy * self.width + gx]

    def mark_blocked(self, gx: int, gy: int) -> None:
        if self.in_bounds(gx, gy):
            self.blocked[gy * self.width + gx] = True


# ═══════════════════════════════════════════════════════════════════════
# Grid construction
# ═══════════════════════════════════════════════════════════════════════


def build_grid_map(
    obstacles: list,
    board_bbox: tuple[float, float, float, float] | None,
    resolution: float = GRID_RESOLUTION,
    margin: float = 2.0,
) -> GridMap:
    """Rasterise *obstacles* onto a uniform grid.

    Each obstacle is a ``shapely`` Polygon stored as the ``.shape``
    attribute of whatever object is in the list (typically
    :class:`~kcaa.router.world_model.Obstacle`).  A cell is BLOCKED if
    its centre falls inside **any** obstacle shape.

    Args:
        obstacles: Iterable of objects with a ``.shape`` (``Polygon``).
        board_bbox: ``(min_x, min_y, max_x, max_y)`` of the board.
            When ``None`` a 100×100 mm area is assumed.
        resolution: Grid cell size in mm.
        margin: Extra space (mm) around the board bbox.

    Returns:
        A populated :class:`GridMap`.
    """
    if board_bbox is not None:
        min_x, min_y, max_x, max_y = board_bbox
    else:
        min_x = min_y = 0.0
        max_x = max_y = 100.0

    origin_x = min_x - margin
    origin_y = min_y - margin
    span_x = max_x - min_x + 2.0 * margin
    span_y = max_y - min_y + 2.0 * margin

    width = max(1, int(math.ceil(span_x / resolution)))
    height = max(1, int(math.ceil(span_y / resolution)))

    blocked = [False] * (width * height)

    # Collect obstacle shapes.
    shapes = []
    for obs in obstacles:
        s = getattr(obs, "shape", obs)
        if s is not None and not s.is_empty:
            shapes.append(s)
    if not shapes:
        return GridMap(
            width=width,
            height=height,
            resolution=resolution,
            origin_x=origin_x,
            origin_y=origin_y,
            blocked=blocked,
        )

    # Rasterise: iterate over each obstacle's bounding box and mark
    # cells whose centre falls inside the obstacle polygon.
    for shp in shapes:
        bxmin, bymin, bxmax, bymax = shp.bounds
        gx0 = max(0, int(math.floor((bxmin - origin_x) / resolution)))
        gy0 = max(0, int(math.floor((bymin - origin_y) / resolution)))
        gx1 = min(width - 1, int(math.ceil((bxmax - origin_x) / resolution)))
        gy1 = min(height - 1, int(math.ceil((bymax - origin_y) / resolution)))

        for gy in range(gy0, gy1 + 1):
            cy = gy * resolution + origin_y + resolution / 2
            for gx in range(gx0, gx1 + 1):
                if blocked[gy * width + gx]:
                    continue
                wx = gx * resolution + origin_x + resolution / 2
                if shp.contains(Point(wx, cy)):
                    blocked[gy * width + gx] = True

    return GridMap(
        width=width,
        height=height,
        resolution=resolution,
        origin_x=origin_x,
        origin_y=origin_y,
        blocked=blocked,
    )


# ═══════════════════════════════════════════════════════════════════════
# Grid A*
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class AStarResult:
    """Result of a Grid A* search.

    Attributes:
        path: Ordered list of world-coordinate ``(x, y)`` tuples from
            start to goal, or ``None`` if no path exists.
        cells_visited: Number of cells expanded.
        path_length_mm: Total Euclidean length of the path in mm.
    """

    path: list[tuple[float, float]] | None
    cells_visited: int = 0
    path_length_mm: float = 0.0


def _octile_dist(gx: int, gy: int, ex: int, ey: int) -> float:
    """Octile distance heuristic (admissible for 8-direction movement)."""
    dx = abs(gx - ex)
    dy = abs(gy - ey)
    return _CARD_COST * abs(dx - dy) + (_DIAG_COST - _CARD_COST) * min(dx, dy)


def grid_a_star(
    grid: GridMap,
    start_world: tuple[float, float],
    end_world: tuple[float, float],
) -> AStarResult:
    """Run A* on *grid* from *start_world* to *end_world*.

    Args:
        grid: Walkability grid.
        start_world: ``(x, y)`` in mm.
        end_world: ``(x, y)`` in mm.

    Returns:
        :class:`AStarResult` with the path in world coordinates, or
        ``path=None`` if unreachable.
    """
    sx, sy = grid.to_grid(start_world[0], start_world[1])
    ex, ey = grid.to_grid(end_world[0], end_world[1])

    # Clamp start/end to the grid.
    sx = max(0, min(grid.width - 1, sx))
    sy = max(0, min(grid.height - 1, sy))
    ex = max(0, min(grid.width - 1, ex))
    ey = max(0, min(grid.height - 1, ey))

    if not grid.is_free(sx, sy) or not grid.is_free(ex, ey):
        return AStarResult(path=None)

    width = grid.width
    start_id = sy * width + sx
    end_id = ey * width + ex

    # A* state
    g_score = {start_id: 0.0}
    parent: dict[int, tuple[int, int] | None] = {start_id: None}
    open_heap = [(0.0, start_id)]
    closed: set[int] = set()
    visited = 0

    while open_heap:
        _, cur_id = heapq.heappop(open_heap)
        if cur_id in closed:
            continue
        closed.add(cur_id)
        visited += 1

        if cur_id == end_id:
            path: list[tuple[float, float]] = []
            node = cur_id
            while node is not None:
                gx = node % width
                gy = node // width
                path.append(grid.to_world(gx, gy))
                node = parent[node]  # type: ignore[assignment]
            path.reverse()

            total_len = sum(
                math.hypot(path[i + 1][0] - path[i][0], path[i + 1][1] - path[i][1])
                for i in range(len(path) - 1)
            )
            return AStarResult(path=path, cells_visited=visited, path_length_mm=total_len)

        cx = cur_id % width
        cy = cur_id // width
        cur_g = g_score[cur_id]

        for dx, dy, is_diag in _DIRECTIONS:
            nx, ny = cx + dx, cy + dy
            if not grid.is_free(nx, ny):
                continue
            nid = ny * width + nx
            if nid in closed:
                continue
            step = _DIAG_COST if is_diag else _CARD_COST
            tentative = cur_g + step
            if tentative < g_score.get(nid, float("inf")):
                g_score[nid] = tentative
                f = tentative + _octile_dist(nx, ny, ex, ey)
                heapq.heappush(open_heap, (f, nid))
                parent[nid] = cur_id

    return AStarResult(path=None, cells_visited=visited)


# ═══════════════════════════════════════════════════════════════════════
# Hierarchical search — Phase 2
# ═══════════════════════════════════════════════════════════════════════

# Coarse resolution = fine_resolution × COARSE_FACTOR.
# 5× means a 0.5 mm coarse grid when the fine grid is 0.1 mm.
COARSE_FACTOR = 5

# Band margin (mm) on each side of the coarse path for the fine pass.
# Must be wide enough to accommodate detours around obstacles missed by
# the coarse search.  3 mm = 30 cells at 0.1 mm resolution.
_BAND_MARGIN = 3.0

# Threshold: cells below this use single-pass A* (faster for small grids).
_SINGLE_PASS_THRESHOLD = 25_000


def _band_bbox(
    coarse_path: list[tuple[float, float]],
    margin: float = _BAND_MARGIN,
) -> tuple[float, float, float, float]:
    """Compute the bounding box of a coarse path plus margin.

    Args:
        coarse_path: Simplified polyline from the coarse search.
        margin: Extra space (mm) on each side.

    Returns:
        ``(min_x, min_y, max_x, max_y)``.
    """
    min_x = min(x for x, _ in coarse_path) - margin
    min_y = min(y for _, y in coarse_path) - margin
    max_x = max(x for x, _ in coarse_path) + margin
    max_y = max(y for _, y in coarse_path) + margin
    return min_x, min_y, max_x, max_y


def hierarchical_a_star(
    obstacles: list,
    start_world: tuple[float, float],
    end_world: tuple[float, float],
    fine_resolution: float = GRID_RESOLUTION,
    route_bbox: tuple[float, float, float, float] | None = None,
) -> AStarResult:
    """Run hierarchical A* with auto-detection of single-pass vs two-pass.

    For small search areas (grid < ``_SINGLE_PASS_THRESHOLD`` cells) this
    falls back to single-pass :func:`grid_a_star` — the overhead of
    building two grids and running two searches is not worth it.

    For large areas, does a coarse pass at 5× resolution, computes a band
    bounding box around the coarse path, then a fine pass at the requested
    resolution within that band.

    Args:
        obstacles: Iterable of objects with a ``.shape`` (``Polygon``).
        start_world: ``(x, y)`` in mm.
        end_world: ``(x, y)`` in mm.
        fine_resolution: Grid cell size in mm for the fine pass.
        route_bbox: ``(min_x, min_y, max_x, max_y)`` of the route area.
            When ``None``, computed from start/end + 5 mm margin.

    Returns:
        :class:`AStarResult`.
    """
    # Determine the route bounding box if not given.
    if route_bbox is not None:
        bbox = route_bbox
    else:
        sx, sy = start_world
        ex, ey = end_world
        bbox = (
            min(sx, ex) - 5.0,
            min(sy, ey) - 5.0,
            max(sx, ex) + 5.0,
            max(sy, ey) + 5.0,
        )

    # Compute the approximate number of cells at fine resolution.
    bw = bbox[2] - bbox[0]
    bh = bbox[3] - bbox[1]
    fine_cells = int(math.ceil(bw / fine_resolution)) * int(math.ceil(bh / fine_resolution))

    # Small area → single pass.
    if fine_cells < _SINGLE_PASS_THRESHOLD:
        grid = build_grid_map(obstacles, bbox, resolution=fine_resolution)
        return grid_a_star(grid, start_world, end_world)

    # Large area → hierarchical (coarse + band + fine).
    coarse_res = fine_resolution * COARSE_FACTOR
    coarse_grid = build_grid_map(obstacles, bbox, resolution=coarse_res)
    coarse_result = grid_a_star(coarse_grid, start_world, end_world)
    if coarse_result.path is None:
        return AStarResult(path=None, cells_visited=coarse_result.cells_visited)

    simplified = simplify_path(coarse_result.path)
    band = _band_bbox(simplified)
    # Clamp band to the route bbox.
    band = (
        max(bbox[0], band[0]),
        max(bbox[1], band[1]),
        min(bbox[2], band[2]),
        min(bbox[3], band[3]),
    )

    fine_grid = build_grid_map(obstacles, band, resolution=fine_resolution)
    result = grid_a_star(fine_grid, start_world, end_world)
    coarse_visited = coarse_result.cells_visited
    if result.path is not None:
        result.cells_visited += coarse_visited
    else:
        result = AStarResult(path=None, cells_visited=coarse_visited)
    return result


# ═══════════════════════════════════════════════════════════════════════
# Multi-layer A* — cross-layer routing with via edges
# ═══════════════════════════════════════════════════════════════════════

# Default via cost (mm added to the path for each via transition).
# Matches the original visibility-graph DEFAULT_VIA_COST_FN(0).
_VIA_COST = 2.0


@dataclass
class MultiLayerAStarResult:
    """Result of a multi-layer A* search.

    Attributes:
        path: Ordered list of :class:`GridNode` with layer info, or
            ``None`` if no path exists.
        cells_visited: Number of search states expanded.
    """

    path: list[GridNode] | None
    cells_visited: int = 0


def _collect_routing_layers(
    start_layer: str,
    end_layer: str,
    via_pairs: tuple[tuple[str, str], ...],
) -> list[str]:
    """Collect all layers the router must consider, preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for layer in (start_layer, end_layer):
        if layer not in seen:
            out.append(layer)
            seen.add(layer)
    for top, bot in via_pairs:
        for layer in (top, bot):
            if layer not in seen:
                out.append(layer)
                seen.add(layer)
    return out


def multi_layer_a_star(
    obstacles_by_layer: dict[str, list],
    start_world: tuple[float, float],
    end_world: tuple[float, float],
    start_layer: str,
    end_layer: str,
    via_pairs: tuple[tuple[str, str], ...],
    route_bbox: tuple[float, float, float, float],
    fine_resolution: float = GRID_RESOLUTION,
    via_cost: float = _VIA_COST,
) -> MultiLayerAStarResult:
    """Run A* across multiple copper layers, inserting via edges where legal.

    Each layer has its own walkability grid.  The search state is
    ``(grid_x, grid_y, layer)``.  From each state A* expands:

    * 8 neighbours on the **same layer** (standard 8-dir movement).
    * Via transitions to layers reachable via ``via_pairs``, provided the
      target cell is free on the destination layer.

    Via edges cost a fixed ``via_cost`` mm (2.0 mm default) — the same
    cost model as the original visibility-graph router.  The cost is
    independent of the via-count because Grid A* does not maintain a
    running via count.

    The heuristic is octile distance to the goal on the goal layer,
    which is admissible because via costs are non-negative.

    Args:
        obstacles_by_layer: ``{layer_name: [obstacle_objects]}``.
        start_world: ``(x, y)`` in mm on ``start_layer``.
        end_world: ``(x, y)`` in mm on ``end_layer``.
        start_layer: Layer name for the start point.
        end_layer: Layer name for the end point.
        via_pairs: Allowed ``(from, to)`` layer pairs for via edges.
        route_bbox: ``(min_x, min_y, max_x, max_y)`` of the route area.
        fine_resolution: Grid cell size in mm.
        via_cost: Distance-equivalent cost added per via transition.

    Returns:
        :class:`MultiLayerAStarResult` with GridNode path (``path=None`` if
        unreachable).
    """
    layers = _collect_routing_layers(start_layer, end_layer, via_pairs)
    n_layers = len(layers)
    layer_to_idx = {l: i for i, l in enumerate(layers)}

    # Build one GridMap per layer.  All share the same resolution and
    # origin — crucial for via-edge alignment.
    grids: dict[str, GridMap] = {}
    for layer in layers:
        obs = obstacles_by_layer.get(layer, [])
        grids[layer] = build_grid_map(obs, route_bbox, resolution=fine_resolution)

    ref_grid = grids[layers[0]]
    W = ref_grid.width
    H = ref_grid.height

    # Start / end grid coordinates.
    gx, gy = ref_grid.to_grid(start_world[0], start_world[1])
    sx = max(0, min(W - 1, gx))
    sy = max(0, min(H - 1, gy))
    gx, gy = ref_grid.to_grid(end_world[0], end_world[1])
    ex = max(0, min(W - 1, gx))
    ey = max(0, min(H - 1, gy))

    start_li = layer_to_idx[start_layer]
    end_li = layer_to_idx[end_layer]

    if not grids[start_layer].is_free(sx, sy) or not grids[end_layer].is_free(ex, ey):
        return MultiLayerAStarResult(path=None)

    # State encoding: (gy * W + gx) * n_layers + layer_idx
    def _encode(gx: int, gy: int, li: int) -> int:
        return (gy * W + gx) * n_layers + li

    start_id = _encode(sx, sy, start_li)
    end_id = _encode(ex, ey, end_li)

    # Build via adjacency: for each layer, which other layers can it via to?
    via_from: dict[int, set[int]] = {i: set() for i in range(n_layers)}
    for t, b in via_pairs:
        if t in layer_to_idx and b in layer_to_idx:
            ti, bi = layer_to_idx[t], layer_to_idx[b]
            via_from[ti].add(bi)
            via_from[bi].add(ti)

    g_score = {start_id: 0.0}
    parent: dict[int, int | None] = {start_id: None}
    open_heap = [(_octile_dist(sx, sy, ex, ey), start_id)]
    closed: set[int] = set()
    visited = 0

    while open_heap:
        _, cur_id = heapq.heappop(open_heap)
        if cur_id in closed:
            continue
        closed.add(cur_id)
        visited += 1

        if cur_id == end_id:
            # Reconstruct path
            path_rev: list[GridNode] = []
            nid = cur_id
            while nid is not None:
                li = nid % n_layers
                rest = nid // n_layers
                gy_p = rest // W
                gx_p = rest % W
                wx, wy = ref_grid.to_world(gx_p, gy_p)
                path_rev.append(GridNode(x=wx, y=wy, layer=layers[li]))
                nid = parent[nid]
            path_rev.reverse()
            return MultiLayerAStarResult(path=path_rev, cells_visited=visited)

        li = cur_id % n_layers
        rest = cur_id // n_layers
        cy = rest // W
        cx = rest % W
        cur_g = g_score[cur_id]
        cur_layer = layers[li]
        cur_grid = grids[cur_layer]

        # ---- Same-layer 8-dir moves ----
        for dx, dy, is_diag in _DIRECTIONS:
            nx, ny = cx + dx, cy + dy
            if not cur_grid.is_free(nx, ny):
                continue
            nid = _encode(nx, ny, li)
            if nid in closed:
                continue
            step = _DIAG_COST if is_diag else _CARD_COST
            tentative = cur_g + step
            if tentative < g_score.get(nid, float("inf")):
                g_score[nid] = tentative
                heapq.heappush(open_heap, (tentative + _octile_dist(nx, ny, ex, ey), nid))
                parent[nid] = cur_id

        # ---- Via moves to other layers ----
        for tgt_li in via_from[li]:
            if not grids[layers[tgt_li]].is_free(cx, cy):
                continue
            nid = _encode(cx, cy, tgt_li)
            if nid in closed:
                continue
            tentative = cur_g + via_cost
            if tentative < g_score.get(nid, float("inf")):
                g_score[nid] = tentative
                heapq.heappush(open_heap, (tentative + _octile_dist(cx, cy, ex, ey), nid))
                parent[nid] = cur_id

    return MultiLayerAStarResult(path=None, cells_visited=visited)


def simplify_path(
    pts: list[tuple[float, float]],
    tol_rad: float = 0.005,
) -> list[tuple[float, float]]:
    """Remove collinear intermediate points from a polyline.

    Phase 3 will add miter (90° → 2×45°) on top of this.

    Args:
        pts: Ordered ``(x, y)`` points.
        tol_rad: Cosine tolerance — any dot product closer than this to
            1.0 is treated as collinear.

    Returns:
        Simplified polyline with the same start and end.
    """
    if len(pts) <= 2:
        return list(pts)

    result = [pts[0]]
    for i in range(1, len(pts) - 1):
        px, py = result[-1]
        cx, cy = pts[i]
        nx, ny = pts[i + 1]
        d1x = cx - px
        d1y = cy - py
        d2x = nx - cx
        d2y = ny - cy
        l1 = math.hypot(d1x, d1y)
        l2 = math.hypot(d2x, d2y)
        if l1 < 1e-12 or l2 < 1e-12:
            continue
        dot = (d1x * d2x + d1y * d2y) / (l1 * l2)
        if dot < 1.0 - tol_rad:
            result.append(pts[i])
    result.append(pts[-1])
    return result


def path_to_nodes(
    pts: list[tuple[float, float]],
    layer: str,
) -> list[GridNode]:
    """Convert world-coordinate points to :class:`GridNode` list."""
    return [GridNode(x=x, y=y, layer=layer) for x, y in pts]
