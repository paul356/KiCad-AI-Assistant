"""Regression tests for the custom-drawn panel icons (issue #225).

KiCad embeds wxPython in its own C++ ``wxApp``, so wxPython's
``wx.App.OnPreInit()`` never runs and the module-level stock GDI objects
(``wx.TRANSPARENT_BRUSH``, ``wx.WHITE``, ...) keep the null placeholders
created at import time: ``TRANSPARENT_BRUSH = Brush()`` with no reference
data, hence ``IsOk()`` is false.  On wxGTK3 a ``wx.MemoryDC`` is a
cairo-backed ``wxGCDC``; ``wxGCDCImpl::DoDrawEllipticArc()`` calls
``m_brush.GetStyle()``, and ``wxBrush::GetStyle()`` asserts ``IsOk()`` --
which terminates KiCad while the panel builds its toolbar icons.

A stub wx cannot reproduce the GTK assert itself.  These tests pin the
contract that keeps it unreachable: every brush handed to a DC is a valid
solid brush with an explicit RGB colour, and the arc geometry/pen of the
paperclip icon is unchanged.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

_BG = (245, 247, 252)

_FONTFAMILY_DEFAULT = 70
_FONTSTYLE_NORMAL = 90
_FONTWEIGHT_BOLD = 92
_BRUSHSTYLE_TRANSPARENT = 0
_BRUSHSTYLE_SOLID = 1
_PENSTYLE_SOLID = 100


class _Colour:
    """Explicit RGB colour; equality is by channel values."""

    def __init__(self, red: int = 0, green: int = 0, blue: int = 0) -> None:
        self._rgb = (red, green, blue)

    def IsOk(self) -> bool:
        return True

    def Red(self) -> int:
        return self._rgb[0]

    def Green(self) -> int:
        return self._rgb[1]

    def Blue(self) -> int:
        return self._rgb[2]

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Colour) and self._rgb == other._rgb

    def __hash__(self) -> int:
        return hash(self._rgb)

    def __repr__(self) -> str:
        return f"Colour{self._rgb}"


class _NullStockColour:
    """Stand-in for ``wx.WHITE`` when no Python ``wx.App`` initialized it."""

    def IsOk(self) -> bool:
        return False

    def Red(self) -> int:
        raise AssertionError("wx.Colour is not Ok (stock object uninitialised)")


class _Brush:
    def __init__(self, colour: _Colour | None = None, style: int | None = None) -> None:
        self._colour = colour
        if style is not None:
            self.style = style
        else:
            self.style = _BRUSHSTYLE_SOLID if colour is not None else None

    def IsOk(self) -> bool:
        return self._colour is not None and self._colour.IsOk() and self.style is not None

    def GetColour(self) -> _Colour | None:
        return self._colour

    def __repr__(self) -> str:
        return f"Brush({self._colour!r}, style={self.style!r})"


class _Pen:
    def __init__(
        self, colour: _Colour | None = None, width: int = 1, style: int | None = None
    ) -> None:
        self._colour = colour
        self.width = width
        if style is not None:
            self.style = style
        else:
            self.style = _PENSTYLE_SOLID if colour is not None else None

    def IsOk(self) -> bool:
        return self._colour is not None and self._colour.IsOk() and self.style is not None

    def GetColour(self) -> _Colour | None:
        return self._colour

    def __repr__(self) -> str:
        return f"Pen({self._colour!r}, width={self.width}, style={self.style!r})"


class _Path:
    def MoveToPoint(self, *args) -> None:
        pass

    def AddLineToPoint(self, *args) -> None:
        pass

    def CloseSubpath(self) -> None:
        pass

    def AddRoundedRectangle(self, *args) -> None:
        pass


class _GraphicsContext:
    def __init__(self, dc: object) -> None:
        self.dc = dc
        self.brushes: list[_Brush] = []
        self.pens: list[_Pen] = []
        self.events: list[tuple[str, object]] = []

    @staticmethod
    def Create(dc: object) -> _GraphicsContext:
        gc = _GraphicsContext(dc)
        _records.gcs.append(gc)
        return gc

    def SetBrush(self, brush: _Brush) -> None:
        self.brushes.append(brush)
        self.events.append(("brush", brush))

    def SetPen(self, pen: _Pen) -> None:
        self.pens.append(pen)
        self.events.append(("pen", pen))

    def CreatePath(self) -> _Path:
        return _Path()

    def DrawPath(self, path: _Path) -> None:
        self.events.append(("draw", path))

    def StrokePath(self, path: _Path) -> None:
        self.events.append(("stroke", path))

    def DrawEllipse(self, *args) -> None:
        self.events.append(("draw", args))

    def SetFont(self, *args) -> None:
        pass

    def DrawText(self, *args) -> None:
        self.events.append(("draw", args))


class _MemoryDC:
    def __init__(self) -> None:
        # wxGCDCImpl::Init() starts with the C++ stock white brush, which is
        # always valid (unlike the Python-level placeholder objects).
        self.brush = _Brush(_Colour(255, 255, 255))
        self.brushes: list[_Brush] = []
        self.pens: list[_Pen] = []
        self.arcs: list[tuple[tuple, _Brush]] = []
        self.lines: list[tuple] = []
        self.events: list[tuple[str, object]] = []
        _records.memory_dcs.append(self)

    def SelectObject(self, bitmap: object) -> None:
        self.bitmap = bitmap

    def SetBrush(self, brush: _Brush) -> None:
        self.brushes.append(brush)
        self.brush = brush
        self.events.append(("brush", brush))

    def SetPen(self, pen: _Pen) -> None:
        self.pens.append(pen)
        self.events.append(("pen", pen))

    def DrawEllipticArc(self, *args) -> None:
        # Mirrors wxGCDCImpl::DoDrawEllipticArc() -> m_brush.GetStyle().
        if not self.brush.IsOk():
            raise AssertionError("wxBrush::GetStyle(): invalid brush")
        self.arcs.append((args, self.brush))
        self.events.append(("draw", args))

    def DrawLine(self, *args) -> None:
        self.lines.append(args)
        self.events.append(("draw", args))


class _Image:
    def __init__(self, width: int, height: int, data: bytes) -> None:
        self.width = width
        self.height = height
        self.data = data


class _Bitmap:
    def __init__(self, image: _Image) -> None:
        self.width = image.width
        self.height = image.height


class _Font:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args


class _Dummy:
    def __init__(self, *args, **kwargs) -> None:
        pass


class _Records:
    def __init__(self) -> None:
        self.memory_dcs: list[_MemoryDC] = []
        self.gcs: list[_GraphicsContext] = []


_records = _Records()


def _build_fake_wx() -> types.ModuleType:
    global _records
    _records = _Records()

    fake_wx = types.ModuleType("wx")
    fake_wx.__kcaa_fake_wx__ = True  # type: ignore[attr-defined]
    fake_wx.BRUSHSTYLE_TRANSPARENT = _BRUSHSTYLE_TRANSPARENT  # type: ignore[attr-defined]
    fake_wx.BRUSHSTYLE_SOLID = _BRUSHSTYLE_SOLID  # type: ignore[attr-defined]
    fake_wx.PENSTYLE_SOLID = _PENSTYLE_SOLID  # type: ignore[attr-defined]
    fake_wx.FONTFAMILY_DEFAULT = _FONTFAMILY_DEFAULT  # type: ignore[attr-defined]
    fake_wx.FONTSTYLE_NORMAL = _FONTSTYLE_NORMAL  # type: ignore[attr-defined]
    fake_wx.FONTWEIGHT_BOLD = _FONTWEIGHT_BOLD  # type: ignore[attr-defined]
    fake_wx.Colour = _Colour  # type: ignore[attr-defined]
    fake_wx.Brush = _Brush  # type: ignore[attr-defined]
    fake_wx.Pen = _Pen  # type: ignore[attr-defined]
    fake_wx.Font = _Font  # type: ignore[attr-defined]
    fake_wx.Image = _Image  # type: ignore[attr-defined]
    fake_wx.Bitmap = _Bitmap  # type: ignore[attr-defined]
    fake_wx.MemoryDC = _MemoryDC  # type: ignore[attr-defined]
    fake_wx.GraphicsContext = _GraphicsContext  # type: ignore[attr-defined]
    fake_wx.Frame = _Dummy  # type: ignore[attr-defined]
    fake_wx.FileDropTarget = _Dummy  # type: ignore[attr-defined]
    # wxPython exposes these as null placeholders until a wx.App initializes
    # them; model them as invalid so any use fails the contract.
    fake_wx.TRANSPARENT_BRUSH = _Brush()  # type: ignore[attr-defined]
    fake_wx.TRANSPARENT_PEN = _Pen()  # type: ignore[attr-defined]
    fake_wx.WHITE = _NullStockColour()  # type: ignore[attr-defined]
    fake_wx.__getattr__ = lambda name: _Dummy  # type: ignore[attr-defined]

    fake_html = types.ModuleType("wx.html")
    fake_html2 = types.ModuleType("wx.html2")
    fake_wx.html = fake_html  # type: ignore[attr-defined]
    fake_wx.html2 = fake_html2  # type: ignore[attr-defined]
    return fake_wx


@pytest.fixture
def panel_mod():
    """Import panel.py with a recording fake wx module installed."""
    fake_wx = _build_fake_wx()
    records = _records
    module_names = ("wx", "wx.html", "wx.html2", "kicad_plugin.ui.panel")
    missing = object()
    saved_modules = {name: sys.modules.get(name, missing) for name in module_names}
    ui_pkg = sys.modules.get("kicad_plugin.ui")
    saved_panel_attr = getattr(ui_pkg, "panel", None) if ui_pkg is not None else None
    try:
        sys.modules["wx"] = fake_wx
        sys.modules["wx.html"] = types.ModuleType("wx.html")
        sys.modules["wx.html2"] = types.ModuleType("wx.html2")
        sys.modules.pop("kicad_plugin.ui.panel", None)
        panel = importlib.import_module("kicad_plugin.ui.panel")
        yield panel, fake_wx, records
    finally:
        for name, previous in saved_modules.items():
            if previous is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous  # type: ignore[assignment]
        current_ui_pkg = sys.modules.get("kicad_plugin.ui")
        if current_ui_pkg is not None:
            if saved_panel_attr is None:
                current_ui_pkg.__dict__.pop("panel", None)
            else:
                current_ui_pkg.panel = saved_panel_attr  # type: ignore[attr-defined]


def _build_icon(panel, name: str, bg) -> None:
    builders = {
        "attach": lambda: panel.AssistantPanel._make_attach_bitmap(bg=bg),
        "send": lambda: panel.AssistantPanel._make_send_bitmap(bg=bg),
        "stop": lambda: panel.AssistantPanel._make_stop_bitmap(bg=bg),
        "status": lambda: panel.AssistantPanel._make_status_dot_bitmap((140, 140, 140), bg=bg),
        "pdf": lambda: panel.AssistantPanel._make_pdf_thumbnail(bg=bg),
    }
    builders[name]()


@pytest.mark.parametrize("name", ["attach", "send", "stop", "status", "pdf"])
def test_icon_brushes_are_valid_and_explicit(panel_mod, name: str) -> None:
    panel, fake_wx, records = panel_mod
    bg = fake_wx.Colour(*_BG)

    _build_icon(panel, name, bg)

    brushes = [brush for dc in records.memory_dcs for brush in dc.brushes]
    brushes += [brush for gc in records.gcs for brush in gc.brushes]
    assert brushes, f"{name} icon drew no filled shape"
    for brush in brushes:
        assert brush is not fake_wx.TRANSPARENT_BRUSH
        assert brush.IsOk()
        assert brush.style == fake_wx.BRUSHSTYLE_SOLID
        colour = brush.GetColour()
        assert colour.IsOk()
        assert all(isinstance(v, int) for v in (colour.Red(), colour.Green(), colour.Blue()))


@pytest.mark.parametrize("name", ["attach", "send", "stop", "status", "pdf"])
def test_icon_pens_are_valid_and_explicit(panel_mod, name: str) -> None:
    panel, fake_wx, records = panel_mod

    _build_icon(panel, name, fake_wx.Colour(*_BG))

    drawing_targets = [
        target
        for target in records.memory_dcs + records.gcs
        if any(event[0] == "draw" for event in target.events)
    ]
    assert drawing_targets, f"{name} icon drew nothing"
    for target in drawing_targets:
        first_draw = next(i for i, event in enumerate(target.events) if event[0] == "draw")
        assert any(event[0] == "pen" for event in target.events[:first_draw]), (
            f"{name} icon drew before setting a pen"
        )
        assert target.pens, f"{name} icon never set a pen"
        for pen in target.pens:
            assert pen is not fake_wx.TRANSPARENT_PEN
            assert pen.IsOk()
            assert pen.style == fake_wx.PENSTYLE_SOLID
            colour = pen.GetColour()
            assert colour.IsOk()
            assert all(isinstance(v, int) for v in (colour.Red(), colour.Green(), colour.Blue()))


def test_jump_bitmaps_are_36x18_stroke_only(panel_mod) -> None:
    panel, fake_wx, records = panel_mod
    bg = fake_wx.Colour(*_BG)

    for direction in ("up", "down"):
        bmp = panel.AssistantPanel._make_jump_bitmap(direction, bg=bg)
        assert (bmp.width, bmp.height) == (36, 18)

    assert len(records.gcs) == 2
    for gc in records.gcs:
        assert gc.brushes == [], "chevron must be stroke-only"
        assert len(gc.pens) == 1
        pen = gc.pens[0]
        assert pen.width == 3
        colour = pen.GetColour()
        assert colour is not None
        assert (colour.Red(), colour.Green(), colour.Blue()) == (91, 100, 112)
        assert [event[0] for event in gc.events] == ["pen", "stroke"]


@pytest.mark.parametrize("rgb", [(140, 140, 140), (0, 140, 0)])
def test_status_dot_is_16px_ring_and_fill(panel_mod, rgb: tuple[int, int, int]) -> None:
    panel, fake_wx, records = panel_mod
    bmp = panel.AssistantPanel._make_status_dot_bitmap(rgb, bg=fake_wx.Colour(*_BG))
    assert (bmp.width, bmp.height) == (16, 16)

    gc = records.gcs[-1]
    draws = [event[1] for event in gc.events if event[0] == "draw"]
    assert draws == [(1, 1, 14, 14), (2, 2, 12, 12)]

    assert len(gc.pens) == 2
    assert len(gc.brushes) == 2
    ring_pen = gc.pens[0].GetColour()
    fill_pen = gc.pens[1].GetColour()
    ring_brush = gc.brushes[0].GetColour()
    fill_brush = gc.brushes[1].GetColour()
    for colour in (ring_pen, ring_brush):
        assert colour is not None
        assert (colour.Red(), colour.Green(), colour.Blue()) == (214, 218, 224)
    for colour in (fill_pen, fill_brush):
        assert colour is not None
        assert (colour.Red(), colour.Green(), colour.Blue()) == rgb


def test_paperclip_arcs_fill_with_background_brush(panel_mod) -> None:
    panel, fake_wx, records = panel_mod
    bg = fake_wx.Colour(*_BG)

    bmp = panel.AssistantPanel._make_attach_bitmap(bg=bg)

    assert (bmp.width, bmp.height) == (28, 28)
    dc = records.memory_dcs[-1]
    # Every arc is drawn against a valid, explicitly coloured solid brush --
    # never the stock transparent/null brush that aborts wxGCDC on GTK.
    assert [args for args, _brush in dc.arcs] == [
        (8, 14, 12, 12, 180, 360),
        (8, 4, 8, 8, 0, 180),
        (12, 18, 4, 4, 180, 360),
    ]
    for _args, brush in dc.arcs:
        assert brush.IsOk()
        assert brush.style == fake_wx.BRUSHSTYLE_SOLID
        assert brush.GetColour() == bg
    # The visible stroke is unchanged: 2 px grey pen, 3 arcs + 4 lines.
    assert dc.lines == [(8, 20, 8, 8), (12, 20, 12, 8), (16, 20, 16, 8), (20, 20, 20, 8)]
    assert len(dc.pens) == 1
    assert dc.pens[0].GetColour() == fake_wx.Colour(90, 90, 90)
    assert dc.pens[0].width == 2


def test_icons_without_background_use_explicit_white(panel_mod) -> None:
    panel, fake_wx, records = panel_mod
    white = fake_wx.Colour(255, 255, 255)

    for name in ("attach", "send", "stop", "status", "pdf"):
        start = len(records.memory_dcs)
        _build_icon(panel, name, None)  # bg omitted
        dcs = records.memory_dcs[start:]
        assert dcs, f"{name} icon created no DC"
        for brush in [brush for dc in dcs for brush in dc.brushes]:
            assert brush.IsOk()
        for _args, brush in [arc for dc in dcs for arc in dc.arcs]:
            assert brush.GetColour() == white
