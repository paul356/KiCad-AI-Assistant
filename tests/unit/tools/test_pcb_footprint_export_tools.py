"""
Unit tests for the PCB → footprint library tools registered in
``kcaa.tools.pcb_library_tools`` and their supporting utils
(``normalize_footprint_for_library`` in ``kcaa.utils.pcb_footprint_utils``,
``fp_lib_table_utils`` registration).
"""

import asyncio
import os
import shutil

import pytest

from kcaa.utils.footprint_index_manager import FootprintIndexManager
from kcaa.utils.fp_lib_table_utils import register_library_in_table
from kcaa.utils.pcb_footprint_utils import (
    normalize_footprint_for_library,
    serialize_footprint_mod,
    split_footprint_header,
)

# Every footprint placed on the test board; used as the explicit
# ``footprints`` argument for add_footprints_to_library.
_BOARD_FOOTPRINTS = ["Sensor_Board_XYZ", "R_0402_1005Metric", "Connector_Odd"]

# Minimal board with three footprints:
#   - CustomLib:Sensor_Board_XYZ  -> missing everywhere (candidate, 45° rotation)
#   - Resistor_SMD:R_0402_1005Metric -> exists in the TestSys library, skipped
#     when exporting elsewhere; failed when the target directory has it
#   - CustomLib:Connector_Odd    -> missing everywhere (candidate)
_BOARD = """\
(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(general
\t\t(thickness 1.6)
\t)
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(40 "B.SilkS" user)
\t\t(49 "F.SilkS" user)
\t)
\t(net "VCC")
\t(net "GND")
\t(footprint "CustomLib:Sensor_Board_XYZ"
\t\t(layer "F.Cu")
\t\t(uuid "11111111-0000-0000-0000-000000000001")
\t\t(at 10.0 20.0 45.0)
\t\t(property "Reference" "U1")
\t\t(property "Value" "Sensor")
\t\t(property "Footprint" "CustomLib:Sensor_Board_XYZ")
\t\t(pad "1" smd rect
\t\t\t(at -0.5 0.0 90.0)
\t\t\t(size 0.5 0.5)
\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net "VCC")
\t\t)
\t\t(pad "2" smd rect
\t\t\t(at 0.5 0.0 90.0)
\t\t\t(size 0.5 0.5)
\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net "GND")
\t\t)
\t\t(fp_text reference "U1" (at 0 2.0 90.0) (layer "F.SilkS"))
\t\t(fp_text value "Sensor" (at 0 -2.0 90.0) (layer "F.Fab"))
\t)
\t(footprint "TestSys:R_0402_1005Metric"
\t\t(layer "F.Cu")
\t\t(uuid "22222222-0000-0000-0000-000000000002")
\t\t(at 20.0 30.0 0.0)
\t\t(property "Reference" "R1")
\t\t(property "Value" "10k")
\t\t(pad "1" smd rect
\t\t\t(at -0.5 0.0)
\t\t\t(size 0.5 0.5)
\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net "VCC")
\t\t)
\t\t(pad "2" smd rect
\t\t\t(at 0.5 0.0)
\t\t\t(size 0.5 0.5)
\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net "GND")
\t\t)
\t)
\t(footprint "CustomLib:Connector_Odd"
\t\t(layer "B.Cu")
\t\t(uuid "33333333-0000-0000-0000-000000000003")
\t\t(at 30.0 40.0 0.0)
\t\t(property "Reference" "J1")
\t\t(property "Value" "Conn")
\t\t(pad "1" thru_hole circle
\t\t\t(at -1.27 0.0)
\t\t\t(size 1.7 1.7)
\t\t\t(drill 1.0)
\t\t\t(layers "*.Cu" "*.Mask")
\t\t\t(net "GND")
\t\t)
\t)
)
"""


def _make_board(tmp_path, name="custom_board.kicad_pcb") -> str:
    path = tmp_path / name
    path.write_text(_BOARD, encoding="utf-8")
    return str(path)


def _make_library(tmp_path, nickname: str, mods: list[str]) -> str:
    """Create a .pretty dir with (empty) .kicad_mod files."""
    lib_dir = tmp_path / f"{nickname}.pretty"
    lib_dir.mkdir()
    for mod in mods:
        (lib_dir / f"{mod}.kicad_mod").write_text(f'(footprint "{mod}")', encoding="utf-8")
    return str(lib_dir)


def _make_fp_lib_table(tmp_path, entries: list[tuple[str, str]]) -> str:
    lines = ["(fp_lib_table", "\t(version 7)"]
    for nick, uri in entries:
        lines.append(
            f'\t(lib (name "{nick}") (type "KiCad") (uri "{uri}") (options "") (descr ""))'
        )
    lines.append(")")
    path = tmp_path / "fp-lib-table"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


class _MockMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _get_tools() -> dict:
    from kcaa.tools.pcb_library_tools import register_pcb_library_tools

    mock = _MockMCP()
    register_pcb_library_tools(mock)  # type: ignore[arg-type]
    return mock.tools


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Fixtures: system lib (TestSys with R_0402), 3rdparty dir, isolated index DB.

    The fp-lib-table lives in the board's directory, so it doubles as the
    project-local table (find_fp_lib_tables checks the PCB directory first).
    """
    sys_lib = _make_library(tmp_path, "TestSys", ["R_0402_1005Metric"])
    table = _make_fp_lib_table(
        tmp_path,
        [("TestSys", sys_lib)],
    )
    third_party = tmp_path / "3rdparty"
    third_party.mkdir()

    index_mgr = FootprintIndexManager(db_path=str(tmp_path / "fp_test.db"))

    from kcaa.utils import pcb_library_utils
    from kcaa.utils.config import config

    monkeypatch.setattr(pcb_library_utils, "_default_kicad_config_dirs", lambda: [str(tmp_path)])
    monkeypatch.setattr(config, "_kicad_3rd_party", str(third_party))
    # `${KICAD10_3RD_PARTY}` URIs are expanded via os.environ (ServerConfig
    # builds a fresh instance), so set the env var — not just the singleton.
    monkeypatch.setenv("KICAD10_3RD_PARTY", str(third_party))
    monkeypatch.setattr(
        "kcaa.tools.pcb_library_tools._3rd_party_footprints_dir",
        lambda: str(third_party / "footprints"),
    )
    # Isolate the footprint index: tools use the module-level singleton, so
    # swap the factory for a temp-DB manager (never the real user DB).
    monkeypatch.setattr(
        "kcaa.tools.pcb_library_tools.get_footprint_index_manager",
        lambda project_path=None: index_mgr,
    )
    return {
        "tmp_path": str(tmp_path),
        "table": table,
        "system_lib": sys_lib,
        "third_party": str(third_party),
        "index_mgr": index_mgr,
    }


# ---------------------------------------------------------------------------
# normalize_footprint_for_library
# ---------------------------------------------------------------------------


class TestNormalizeForLibrary:
    def _node(self):
        import sexpdata

        return [
            n
            for n in sexpdata.loads(_BOARD)
            if isinstance(n, list) and n and str(n[0]) == "footprint"
        ]

    def test_header_and_version(self):
        node = self._node()[0]
        out = normalize_footprint_for_library(node, 20260206, "MyLib")
        assert split_footprint_header(out) == (None, "Sensor_Board_XYZ")
        assert out[1] == "Sensor_Board_XYZ"
        assert str(out[2][0]) == "version"
        assert out[2][1] == 20260206

    def test_strips_instance_data(self):
        node = self._node()[0]
        out = normalize_footprint_for_library(node, 20260206, "MyLib")
        keys = [str(x[0]) if isinstance(x, list) and x else "" for x in out]
        assert "at" not in keys  # placement gone
        assert "uuid" not in keys
        for child in out:
            if isinstance(child, list) and child and str(child[0]) == "pad":
                sub_keys = [str(s[0]) if isinstance(s, list) and s else "" for s in child]
                assert "net" not in sub_keys  # nets stripped
                assert child[1:] == child[1:]  # pad number preserved
        refs = [c[1] for c in out if isinstance(c, list) and c and str(c[0]) == "property"]
        assert "Reference" not in refs
        assert "Value" not in refs

    def test_rotation_inverted_for_pads(self):
        node = self._node()[0]  # fp at rotation 45°
        out = normalize_footprint_for_library(node, 20260206, "MyLib")
        for child in out:
            if isinstance(child, list) and child and str(child[0]) == "pad":
                for sub in child:
                    if isinstance(sub, list) and sub and str(sub[0]) == "at":
                        # pad stored 90° absolute -> local 90 - 45 = 45
                        assert sub[3] == pytest.approx(45.0)

    def test_text_without_explicit_angle_gets_implied_rotation(self):
        """A text whose ``at`` has no angle component reads as axis-aligned
        (implied 0) in board space; re-expressing it in the footprint frame
        yields 0 - fp_rotation, same as a pad's missing angle."""
        import sexpdata as _sx

        node = _sx.loads(
            '(kicad_pcb (footprint "CustomLib:A"'
            '  (layer "F.Cu")'
            "  (at 10.0 20.0 45.0)"
            '  (fp_text reference "U1" (at 0 1.5) (layer "F.SilkS"))'
            ")"
            ")"
        )
        node = next(n for n in node if isinstance(n, list) and n and str(n[0]) == "footprint")
        out = normalize_footprint_for_library(node, 20260206, "MyLib")
        texts = [c for c in out if isinstance(c, list) and c and str(c[0]) == "fp_text"]
        assert len(texts) == 1
        at = next(s for s in texts[0] if isinstance(s, list) and s and str(s[0]) == "at")
        # implied 0 - fp_rotation 45 = -45, normalized to [0, 360) = 315
        assert at[3] == pytest.approx(315.0)

    def test_text_rotation_and_justify_kept(self):
        """Reference/value fp_text keep the board's angle (un-rotated from
        board space: 90° - fp_rotation 45° = 45°) and its effects/justify:
        KiCad's Update Footprints copies library text attributes onto the
        board, so the library must carry exactly what the board displayed."""
        import sexpdata as _sx

        node = _sx.loads(
            '(kicad_pcb (footprint "CustomLib:Sensor_Board_XYZ"'
            '  (layer "F.Cu")'
            "  (at 10.0 20.0 45.0)"
            '  (fp_text reference "U1" (at 0 2.0 90.0) (layer "F.SilkS")'
            "    (effects (font (size 1.524 1.524) (thickness 0.254)) (justify left bottom)))"
            '  (fp_text value "Sensor" (at 0 -2.0 90.0) (layer "F.Fab")'
            "    (effects (font (size 1.3 1.3) (thickness 0.2)) (justify left bottom)))"
            ")"
            ")"
        )
        node = next(n for n in node if isinstance(n, list) and n and str(n[0]) == "footprint")
        out = normalize_footprint_for_library(node, 20260206, "MyLib")
        texts = {str(c[1]): c for c in out if isinstance(c, list) and c and str(c[0]) == "fp_text"}
        assert "reference" in texts
        assert "value" in texts
        for text_type, c in texts.items():
            at = next(s for s in c if isinstance(s, list) and s and str(s[0]) == "at")
            # board-space 90° re-expressed in footprint frame: 90 - 45 = 45
            assert at[3] == pytest.approx(45.0)
            # effects and justify carried over from the board, not dropped
            effects = next(s for s in c if isinstance(s, list) and s and str(s[0]) == "effects")
            assert [
                e
                for e in effects
                if isinstance(e, list)
                and e
                and str(e[0]) == "justify"
                and any(str(t) == "left" for t in e)
                and any(str(t) == "bottom" for t in e)
            ]
            font = next(e for e in effects if isinstance(e, list) and e and str(e[0]) == "font")
            size = next(s for s in font if isinstance(s, list) and s and str(s[0]) == "size")
            assert float(size[1]) == pytest.approx(1.524) or float(size[1]) == pytest.approx(1.3)
            if text_type == "reference":
                assert c[2] == "REF**"
            else:
                assert c[2] == "Sensor_Board_XYZ"
        # position: at x/y are footprint-local and do not follow the
        # footprint's rotation; the library keeps them untouched.
        ref_at = next(
            s for s in texts["reference"] if isinstance(s, list) and s and str(s[0]) == "at"
        )
        assert ref_at[1] == pytest.approx(0.0)
        assert ref_at[2] == pytest.approx(2.0)

    def test_serialize_roundtrip(self):
        node = self._node()[0]
        out = normalize_footprint_for_library(node, 20260206, "MyLib")
        text = serialize_footprint_mod(out)
        import sexpdata

        reparsed = sexpdata.loads(text)
        assert reparsed[0] == "footprint" or str(reparsed[0]) == "footprint"
        assert str(reparsed[1]) == "Sensor_Board_XYZ"

    def test_pad_without_angle_gets_negative_rotation(self):
        """Pads with no explicit angle are axis-aligned in board space (implied
        0) and must be re-expressed in the footprint frame: 0 - fp_rotation.
        Regression: the board rotation was left un-applied to such pads, so
        extracted footprints gained an extra rotation (e.g. an oval pin pad
        ended up vertical instead of horizontal)."""
        import sexpdata

        board = sexpdata.loads(
            '(kicad_pcb (footprint "CustomLib:A"'
            '\t(layer "F.Cu")'
            "\t(at 10.0 20.0 -90.0)"
            '\t(pad "1" smd oval'
            "\t\t(at -3.8 -6.75)"
            "\t\t(size 0.3 1.4)"
            '\t\t(layers "F.Cu" "F.Paste" "F.Mask")'
            "\t)"
            '\t(pad "2" smd rect'
            "\t\t(at 0.0 0.0 270.0)"
            "\t\t(size 3.61 6.35)"
            '\t\t(layers "F.Cu" "F.Paste" "F.Mask")'
            "\t)"
            ")"
            ")"
        )
        node = next(n for n in board if isinstance(n, list) and n and str(n[0]) == "footprint")
        out = normalize_footprint_for_library(node, 20260206, "MyLib")
        pads = {str(c[1]): c for c in out if isinstance(c, list) and c and str(c[0]) == "pad"}
        at1 = next(s for s in pads["1"] if isinstance(s, list) and s and str(s[0]) == "at")
        at2 = next(s for s in pads["2"] if isinstance(s, list) and s and str(s[0]) == "at")
        # implied 0 - (-90) = 90
        assert at1[3] == pytest.approx(90.0)
        # explicit 270 - (-90) = 360 -> 0
        assert at2[3] == pytest.approx(0.0)


class TestNormalizeBackSideFlip:
    """B.Cu board footprints are exported in front-side form: geometry Y
    mirrored, pad angles negated, text angles zeroed, layers flipped F↔B.  Re-placing such a library part on B.Cu
    reproduces the original back-side component (KiCad flips layers/angles
    again on placement)."""

    def _node(self):
        import sexpdata

        board = sexpdata.loads(
            "(kicad_pcb (version 20260830)"
            '	(footprint "CustomLib:BACKFP"'
            '		(layer "B.Cu")'
            "		(at 12.0 34.0 180.0)"
            '		(property "Reference" "R7" (at 0 1.5 270) (layer "B.SilkS")'
            "			(effects (font (size 1.524 1.524) (thickness 0.254)) (justify left bottom)))"
            '		(property "Value" "10k" (at 0 -1.5 270) (layer "B.SilkS")'
            "			(effects (font (size 1.524 1.524) (thickness 0.254)) (justify left bottom)))"
            '		(property "Datasheet" "" (at 0 0 270) (layer "B.Fab") (hide yes))'
            '		(pad "1" smd rect'
            "			(at -1.0 2.0 90.0)"
            "			(size 1.0 2.0)"
            '			(layers "B.Cu" "B.Paste" "B.Mask")'
            '			(net "GND")'
            "		)"
            '		(pad "2" smd rect'
            "			(at 1.0 -2.0 90.0)"
            "			(size 1.0 2.0)"
            '			(layers "B.Cu" "B.Paste" "B.Mask")'
            "		)"
            '		(fp_line (start 2.0 3.0) (end -2.0 3.0) (stroke (width 0.1) (type solid)) (layer "B.SilkS"))'
            '		(fp_arc (start 4.0 5.0) (mid 3.5 3.5) (end 5.0 4.0) (stroke (width 0.1) (type solid)) (layer "B.Courtyard"))'
            '		(fp_poly (pts (xy 6.0 7.0) (xy 7.0 7.0)) (stroke (width 0) (type default)) (layer "B.Fab"))'
            "	)"
            ")"
        )
        return next(n for n in board if isinstance(n, list) and n and str(n[0]) == "footprint")

    def test_top_layer_flipped_to_front(self):
        out = normalize_footprint_for_library(self._node(), 20260830, "MyLib")
        layer = next(s for s in out if isinstance(s, list) and s and str(s[0]) == "layer")
        assert str(layer[1]) == "F.Cu"

    def test_pad_mirrored_and_angle_negated(self):
        out = normalize_footprint_for_library(self._node(), 20260830, "MyLib")
        pads = [c for c in out if isinstance(c, list) and c and str(c[0]) == "pad"]
        assert len(pads) == 2
        for pad in pads:
            at = next(s for s in pad if isinstance(s, list) and s and str(s[0]) == "at")
            assert at[3] == pytest.approx(90.0)
            # layers flipped
            layers = next(s for s in pad if isinstance(s, list) and s and str(s[0]) == "layers")
            assert layers[1] == "F.Cu"
            assert layers[2] == "F.Paste"
            assert layers[3] == "F.Mask"
            # nets stripped
            subs = [str(s[0]) for s in pad if isinstance(s, list) and s]
            assert "net" not in subs

    def test_pad_y_mirrored_positions(self):
        out = normalize_footprint_for_library(self._node(), 20260830, "MyLib")
        pads = {str(c[1]): c for c in out if isinstance(c, list) and c and str(c[0]) == "pad"}
        at1 = next(s for s in pads["1"] if isinstance(s, list) and s and str(s[0]) == "at")
        at2 = next(s for s in pads["2"] if isinstance(s, list) and s and str(s[0]) == "at")
        # source pad1 y=+2.0 -> mirrored -2.0; pad2 y=-2.0 -> +2.0
        assert at1[2] == pytest.approx(-2.0)
        assert at2[2] == pytest.approx(2.0)
        # local rotation after fp_rotation 180: pad stored 90 -> 90-180=-90 -> negate -> 90
        assert at1[3] == pytest.approx(90.0)
        assert at2[3] == pytest.approx(90.0)

    def test_text_rotation_justify_mirror_kept_unlocked(self):
        """Reference/Value properties convert to fp_text, keep their board
        position (Y mirrored for the back side), angle re-expressed
        (270 - fp_rot 180 = 90, then TOP_BOTTOM flip 180 - 90 = 90) with
        effects/justify preserved, unlock keep-upright, and flip layers;
        the board layer may come from source (B.SilkS -> F.SilkS)."""
        out = normalize_footprint_for_library(self._node(), 20260830, "MyLib")
        texts = {str(c[1]): c for c in out if isinstance(c, list) and c and str(c[0]) == "fp_text"}
        assert "reference" in texts
        assert "value" in texts
        ref = texts["reference"]
        at = next(s for s in ref if isinstance(s, list) and s and str(s[0]) == "at")
        # at x/y are footprint-local; the back-side flip only mirrors Y:
        # (0, 1.5) -> (0, -1.5); angle: 270 - 180 = 90, flip -> 90
        assert at[1] == pytest.approx(0.0)
        assert at[2] == pytest.approx(-1.5)
        assert at[3] == pytest.approx(90.0)
        assert ref[2] == "REF**"
        # effects/justify carried over from the board, not dropped
        effects = next(s for s in ref if isinstance(s, list) and s and str(s[0]) == "effects")
        assert [
            e
            for e in effects
            if isinstance(e, list)
            and e
            and str(e[0]) == "justify"
            and any(str(t) == "left" for t in e)
            and any(str(t) == "bottom" for t in e)
        ]
        # keep-upright unlocked
        subs = [s for s in ref if isinstance(s, list) and s and str(s[0]) == "unlocked"]
        assert subs, "unlocked missing"
        assert str(subs[0][1]) == "yes"
        # layer flipped B.SilkS -> F.SilkS
        layer = next(s for s in ref if isinstance(s, list) and s and str(s[0]) == "layer")
        assert str(layer[1]) == "F.SilkS"
        # value text: name + back-side Y mirror (0, -1.5) -> (0, 1.5)
        val = texts["value"]
        vat = next(s for s in val if isinstance(s, list) and s and str(s[0]) == "at")
        assert vat[1] == pytest.approx(0.0)
        assert vat[2] == pytest.approx(1.5)
        assert val[2] == "BACKFP"

    def test_graphics_mirrored_and_layers_flipped(self):
        out = normalize_footprint_for_library(self._node(), 20260830, "MyLib")
        line = next(c for c in out if isinstance(c, list) and c and str(c[0]) == "fp_line")
        start = next(s for s in line if isinstance(s, list) and s and str(s[0]) == "start")
        end = next(s for s in line if isinstance(s, list) and s and str(s[0]) == "end")
        # Y mirrored: +3.0 -> -3.0
        assert start[2] == pytest.approx(-3.0)
        assert end[2] == pytest.approx(-3.0)
        line_layer = next(s for s in line if isinstance(s, list) and s and str(s[0]) == "layer")
        assert str(line_layer[1]) == "F.SilkS"

        arc = next(c for c in out if isinstance(c, list) and c and str(c[0]) == "fp_arc")
        a_start = next(s for s in arc if isinstance(s, list) and s and str(s[0]) == "start")
        a_end = next(s for s in arc if isinstance(s, list) and s and str(s[0]) == "end")
        a_mid = next(s for s in arc if isinstance(s, list) and s and str(s[0]) == "mid")
        # KiCad semantics: values swapped after mirroring, so start holds the
        # mirrored original end (5,-4) and end the mirrored original start (4,-5)
        assert a_start[1] == pytest.approx(5.0)
        assert a_start[2] == pytest.approx(-4.0)
        assert a_end[1] == pytest.approx(4.0)
        assert a_end[2] == pytest.approx(-5.0)
        # mid mirrored
        assert a_mid[2] == pytest.approx(-3.5)
        arc_layer = next(s for s in arc if isinstance(s, list) and s and str(s[0]) == "layer")
        assert str(arc_layer[1]) == "F.Courtyard"

        poly = next(c for c in out if isinstance(c, list) and c and str(c[0]) == "fp_poly")
        pts = next(s for s in poly if isinstance(s, list) and s and str(s[0]) == "pts")
        xy1, xy2 = pts[1], pts[2]
        assert xy1[2] == pytest.approx(-7.0)
        assert xy2[2] == pytest.approx(-7.0)
        poly_layer = next(s for s in poly if isinstance(s, list) and s and str(s[0]) == "layer")
        assert str(poly_layer[1]) == "F.Fab"

    def test_text_mirror_cleared(self):
        import sexpdata

        board = sexpdata.loads(
            "(kicad_pcb (version 20260830)"
            '	(footprint "CustomLib:MIRRORFP"'
            '		(layer "B.Cu")'
            "		(at 1.0 2.0 0.0)"
            '		(fp_text user "LABEL" (at 0.5 0.5 90) (unlocked yes) (layer "B.SilkS")'
            "			(effects (font (size 1.0 1.0)) (justify left bottom mirror)))"
            "	)"
            ")"
        )
        node = next(n for n in board if isinstance(n, list) and n and str(n[0]) == "footprint")
        out = normalize_footprint_for_library(node, 20260830, "MyLib")
        text = next(c for c in out if isinstance(c, list) and c and str(c[0]) == "fp_text")
        dumped = str(text)
        assert "mirror" not in dumped
        assert "unlocked" in dumped
        layer = next(s for s in text if isinstance(s, list) and s and str(s[0]) == "layer")
        assert str(layer[1]) == "F.SilkS"


def _fp_layer(node) -> str:
    layer = next((s for s in node if isinstance(s, list) and s and str(s[0]) == "layer"), None)
    return str(layer[1]) if layer else ""


class TestDeterministicInstanceSelection:
    """Same-named board footprints export deterministically: the front-side
    instance wins when one exists, else the first in board order — never an
    arbitrary last one."""

    def _board(self, layer_a: str, layer_b: str):
        import sexpdata

        return sexpdata.loads(
            "(kicad_pcb (version 20260830)"
            '	(footprint "CustomLib:DUP"'
            f'		(layer "{layer_a}")'
            "		(at 10.0 20.0 0.0)"
            '		(property "Reference" "A1" (at 0 1.5 0) (layer "F.SilkS"))'
            '		(fp_line (start 0 0) (end 1 0) (stroke (width 0.1) (type solid)) (layer "F.SilkS"))'
            "	)"
            '	(footprint "CustomLib:DUP"'
            f'		(layer "{layer_b}")'
            "		(at 30.0 40.0 90.0)"
            '		(property "Reference" "B2" (at 0 -1.5 0) (layer "F.SilkS"))'
            '		(fp_line (start 2 0) (end 3 0) (stroke (width 0.1) (type solid)) (layer "F.SilkS"))'
            "	)"
            ")"
        )

    def _fp_nodes(self, board):
        return [n for n in board if isinstance(n, list) and n and str(n[0]) == "footprint"]

    def _pick(self, nodes) -> list:
        picked = None
        for node in nodes:
            is_front = _fp_layer(node) == "F.Cu"
            if picked is None or (_fp_layer(picked) != "F.Cu" and is_front):
                picked = node
        return picked

    def test_front_side_instance_wins_over_back_side(self):
        """B.Cu instance first, F.Cu second: the F.Cu one is picked, so the
        exported geometry is the second instance's (line start x=2), unflipped."""
        board = self._board("B.Cu", "F.Cu")
        picked = self._pick(self._fp_nodes(board))
        assert _fp_layer(picked) == "F.Cu"
        out = normalize_footprint_for_library(picked, 20260830, "MyLib")
        line = next(c for c in out if isinstance(c, list) and c and str(c[0]) == "fp_line")
        start = next(s for s in line if isinstance(s, list) and s and str(s[0]) == "start")
        assert start[1] == pytest.approx(2.0)  # second instance, not the B.Cu first
        assert start[2] == pytest.approx(0.0)

    def test_back_side_only_takes_first_instance(self):
        """Only B.Cu instances exist: the first in board order is picked
        (line start x=0, not the second's x=2)."""
        board = self._board("B.Cu", "B.Cu")
        picked = self._pick(self._fp_nodes(board))
        assert _fp_layer(picked) == "B.Cu"
        out = normalize_footprint_for_library(picked, 20260830, "MyLib")
        line = next(c for c in out if isinstance(c, list) and c and str(c[0]) == "fp_line")
        start = next(s for s in line if isinstance(s, list) and s and str(s[0]) == "start")
        assert start[1] == pytest.approx(0.0)  # first instance picked


# ---------------------------------------------------------------------------
# fp_lib_table_utils registration
# ---------------------------------------------------------------------------


class TestRegisterLibraryInTable:
    def test_creates_new_table(self, tmp_path):
        table = str(tmp_path / "fp-lib-table")
        result = register_library_in_table(
            table, "MyLib", "${KICAD10_3RD_PARTY}/footprints/MyLib.pretty"
        )
        assert result["registered"] is True
        text = open(table, encoding="utf-8").read()
        assert 'name "MyLib"' in text
        assert "${KICAD10_3RD_PARTY}/footprints/MyLib.pretty" in text

    def test_appends_to_existing_preserving_other_entries(self, tmp_path):
        table = str(tmp_path / "fp-lib-table")
        _make_fp_lib_table(tmp_path, [("TestSys", "/tmp/TestSys.pretty")])
        register_library_in_table(table, "MyLib", "${KICAD10_3RD_PARTY}/footprints/MyLib.pretty")
        text = open(table, encoding="utf-8").read()
        assert 'name "TestSys"' in text
        assert 'name "MyLib"' in text
        # original entry byte-identical (surgical append)
        assert "/tmp/TestSys.pretty" in text

    def test_no_duplicate_registration(self, tmp_path):
        table = str(tmp_path / "fp-lib-table")
        register_library_in_table(table, "MyLib", "uri1")
        r2 = register_library_in_table(table, "MyLib", "uri2")
        assert r2["registered"] is False
        assert r2["reason"] == "already_registered"
        text = open(table, encoding="utf-8").read()
        assert text.count('name "MyLib"') == 1

    def test_creates_backup(self, tmp_path):
        table = str(tmp_path / "fp-lib-table")
        _make_fp_lib_table(tmp_path, [("TestSys", "/tmp/TestSys.pretty")])
        register_library_in_table(table, "MyLib", "uri")
        assert os.path.isfile(table + ".bak")

    def test_sanitizes_nickname(self, tmp_path):
        table = str(tmp_path / "fp-lib-table")
        register_library_in_table(table, "My Lib/With Bads", "uri")
        text = open(table, encoding="utf-8").read()
        assert 'name "My_Lib_With_Bads"' in text


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tools():
    return _get_tools()


class TestFindMissingFootprints:
    def test_consolidates_multiple_references(self, tools, tmp_path, project):
        """Two board instances of the same (library, name) collapse into one
        missing entry carrying both references, not two same-shaped rows."""
        # A second full Sensor_Board_XYZ node (same lib:name, reference U2)
        # inserted before the Connector_Odd node keeps the board valid S-expr.
        u2_node = (
            '\t(footprint "CustomLib:Sensor_Board_XYZ"\n'
            '\t\t(layer "F.Cu")\n'
            '\t\t(uuid "44444444-0000-0000-0000-000000000004")\n'
            "\t\t(at 12.0 22.0 0.0)\n"
            '\t\t(property "Reference" "U2")\n'
            '\t\t(property "Value" "Sensor")\n'
            '\t\t(fp_text reference "U2" (at 0 2.0 0.0) (layer "F.SilkS"))\n'
            '\t\t(fp_text value "Sensor" (at 0 -2.0 0.0) (layer "F.Fab"))\n'
            "\t)\n"
        )
        board_text = _BOARD.replace(
            '\t(footprint "CustomLib:Connector_Odd"',
            u2_node + '\t(footprint "CustomLib:Connector_Odd"',
        )
        board = str(tmp_path / "two_mods.kicad_pcb")
        open(board, "w", encoding="utf-8").write(board_text)
        result = _run(tools["find_footprints_not_in_libraries"](pcb_path=board, ctx=None))
        assert "error" not in result, result
        sensor = [fp for fp in result["missing"] if fp["name"] == "Sensor_Board_XYZ"]
        assert len(sensor) == 1  # merged, not one row per instance
        assert sorted(sensor[0]["references"]) == ["U1", "U2"]
        assert sensor[0]["reference_count"] == 2
        assert result["missing_count"] == 2  # Sensor_Board_XYZ + Connector_Odd

    def test_list_is_read_only(self, tools, tmp_path, project):
        board = _make_board(tmp_path)
        before = open(board, encoding="utf-8").read()
        _run(tools["find_footprints_not_in_libraries"](pcb_path=board, ctx=None))
        assert open(board, encoding="utf-8").read() == before

    def test_does_not_write_footprint_database(self, tools, tmp_path, project):
        """The read path never writes to the index DB (it reads it)."""
        board = _make_board(tmp_path)
        result = _run(tools["find_footprints_not_in_libraries"](pcb_path=board, ctx=None))
        assert "error" not in result, result
        assert project["index_mgr"].get_stats().footprint_count == 0
        assert project["index_mgr"].get_stats().library_count == 0

    def test_uses_index_after_completed_sync_even_if_dir_gone(
        self, tools, tmp_path, project, monkeypatch
    ):
        """After a completed sync for this project, find consumes the index DB:
        an indexed name is NOT missing even when the library directory is no
        longer live-scannable.  (A partially-synced DB must NOT be trusted —
        covered by TestCollectExistingNames.)"""
        import kcaa.tools.pcb_library_tools as _tool_module

        board = _make_board(tmp_path)
        proj_id = _tool_module.normalize_project_id(board)
        lib_dir = project["system_lib"]

        with _tool_module._fp_sync_lock:
            _tool_module._fp_sync_state.last_result = None
            _tool_module._fp_sync_state.error = None
            _tool_module._fp_sync_state.last_project_path = None
        _tool_module._run_fp_sync_in_background(True, board)
        with _tool_module._fp_sync_lock:
            assert _tool_module._fp_sync_state.last_result is not None
            assert _tool_module._fp_sync_state.last_result["success"] is True
            assert _tool_module._fp_sync_state.last_project_path == proj_id

        # Index is complete; the library directory is no longer scannable.
        shutil.rmtree(lib_dir)
        assert not os.path.isdir(lib_dir)

        result = _run(tools["find_footprints_not_in_libraries"](pcb_path=board, ctx=None))
        assert "error" not in result, result
        names = [fp["name"] for fp in result["missing"]]
        assert "R_0402_1005Metric" not in names  # indexed → existing
        assert "Sensor_Board_XYZ" in names
        assert "Connector_Odd" in names
        assert result["missing_count"] == 2


class TestCreate3rdPartyLibrary:
    def test_creates_and_registers(self, tools, tmp_path, project):
        result = _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        assert "error" not in result, result
        assert result["library"] == "MyVendor"
        lib_dir = os.path.join(project["third_party"], "footprints", "MyVendor.pretty")
        assert result["path"] == lib_dir
        assert os.path.isdir(lib_dir)
        final_table = os.path.join(project["tmp_path"], "fp-lib-table")
        assert result["table_path"] == final_table
        assert result["registered"] is True
        table_text = open(final_table, encoding="utf-8").read()
        assert 'name "MyVendor"' in table_text
        assert "${KICAD10_3RD_PARTY}/footprints/MyVendor.pretty" in table_text
        # Only the new library lands in the index DB — never the whole table.
        libs = project["index_mgr"].get_all_libraries()
        assert [lib.library_name for lib in libs] == ["MyVendor"]

    def test_collision_errors(self, tools, tmp_path, project):
        result = _run(tools["create_footprint_library"](name="TestSys", ctx=None))
        assert "error" in result
        assert "already exists" in result["error"]
        assert "TestSys" not in [
            lib.library_name for lib in project["index_mgr"].get_all_libraries()
        ]

    def test_collision_with_indexed_other_project_library(self, tools, tmp_path, project):
        """A nickname indexed under any project blocks creation."""
        project["index_mgr"]._db.save_library(
            "OtherProjLib", "u", "/x", "d", "c", [], project="/other/project"
        )
        result = _run(tools["create_footprint_library"](name="OtherProjLib", ctx=None))
        assert "error" in result
        assert "already exists" in result["error"]

    def test_refuses_existing_directory(self, tools, tmp_path, project):
        """A pre-existing <name>.pretty directory blocks creation."""
        target = os.path.join(project["third_party"], "footprints", "Taken.pretty")
        os.makedirs(target)
        result = _run(tools["create_footprint_library"](name="Taken", ctx=None))
        assert "error" in result
        assert "Directory already exists" in result["error"]
        assert "Taken" in result["error"]
        assert not os.path.isfile(os.path.join(project["tmp_path"], "fp-lib-table.bak"))


class TestCreateProjectLibrary:
    """create_footprint_library's project_dir branch (${KIPRJMOD} scope)."""

    def test_creates_project_local_library(self, tools, tmp_path, project):
        proj_dir = tmp_path / "subproj"
        proj_dir.mkdir()
        result = _run(
            tools["create_footprint_library"](name="ProjLib", project_dir=str(proj_dir), ctx=None)
        )
        assert "error" not in result, result
        lib_dir = proj_dir / "ProjLib.pretty"
        assert result["path"] == str(lib_dir)
        assert os.path.isdir(lib_dir)
        # Registered in the project's own fp-lib-table, created on demand.
        table = proj_dir / "fp-lib-table"
        assert result["table_path"] == str(table)
        assert os.path.isfile(table)
        table_text = table.read_text(encoding="utf-8")
        assert 'name "ProjLib"' in table_text
        assert "${KIPRJMOD}/ProjLib.pretty" in table_text
        # Indexed under the project id (realpath of the project dir), so the
        # library shows up in the project scope but not the global scope.
        libs = project["index_mgr"]._db.get_all_libraries(project=None)
        assert [lib.library_name for lib in libs] == ["ProjLib"]
        assert libs[0].project == os.path.realpath(str(proj_dir))
        global_libs = project["index_mgr"].get_all_libraries()
        assert [lib.library_name for lib in global_libs] == []

    def test_missing_project_dir_errors(self, tools, tmp_path, project):
        result = _run(
            tools["create_footprint_library"](
                name="ProjLib", project_dir=str(tmp_path / "nope"), ctx=None
            )
        )
        assert "error" in result
        assert "Project directory not found" in result["error"]
        # Nothing was created or indexed.
        assert not (tmp_path / "nope").exists()
        assert project["index_mgr"]._db.get_all_libraries(project=None) == []

    def test_project_name_collides_with_global(self, tools, tmp_path, project):
        """Nickname uniqueness is global: a project library can't shadow the
        existing TestSys (registered in the global user fp-lib-table)."""
        proj_dir = tmp_path / "subproj"
        proj_dir.mkdir()
        result = _run(
            tools["create_footprint_library"](name="TestSys", project_dir=str(proj_dir), ctx=None)
        )
        assert "error" in result
        assert "already exists" in result["error"]
        assert not (proj_dir / "TestSys.pretty").exists()


class TestAddFootprints:
    def test_exports_missing_to_3rdparty(self, tools, tmp_path, project):
        # Create the target library first, then export into it.
        created = _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        assert "error" not in created, created
        board = _make_board(tmp_path)
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board, footprints=_BOARD_FOOTPRINTS, library="MyVendor", ctx=None
            )
        )
        assert "error" not in result, result
        assert result["exported_count"] == 2
        target = os.path.join(project["third_party"], "footprints", "MyVendor.pretty")
        assert result["library_path"] == target
        assert os.path.isfile(os.path.join(target, "Sensor_Board_XYZ.kicad_mod"))
        assert os.path.isfile(os.path.join(target, "Connector_Odd.kicad_mod"))
        assert not os.path.exists(os.path.join(target, "R_0402_1005Metric.kicad_mod"))
        # The fixture's fp-lib-table doubles as the project table (board and
        # table share tmp_path), so the add's ownership check tags the
        # library under the project, not the global scope.
        libs = project["index_mgr"]._db.get_all_libraries(project=os.path.realpath(str(tmp_path)))
        assert [lib.library_name for lib in libs] == ["MyVendor"]
        assert libs[0].project == os.path.realpath(str(tmp_path))

    def test_board_untouched(self, tools, tmp_path, project):
        _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        board = _make_board(tmp_path)
        before = open(board, encoding="utf-8").read()
        _run(
            tools["add_footprints_to_library"](
                pcb_path=board, footprints=_BOARD_FOOTPRINTS, library="MyVendor", ctx=None
            )
        )
        assert open(board, encoding="utf-8").read() == before

    def test_no_overwrite_on_second_run(self, tools, tmp_path, project):
        _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        # Real-world premise: the index is built (sync) before exporting.
        project["index_mgr"].index_library("TestSys", project["system_lib"])
        board = _make_board(tmp_path)
        first = _run(
            tools["add_footprints_to_library"](
                pcb_path=board, footprints=_BOARD_FOOTPRINTS, library="MyVendor", ctx=None
            )
        )
        assert first["exported_count"] == 2
        second = _run(
            tools["add_footprints_to_library"](
                pcb_path=board, footprints=_BOARD_FOOTPRINTS, library="MyVendor", ctx=None
            )
        )
        assert second["exported_count"] == 0
        assert second["failed_count"] == 2  # Sensor_Board_XYZ, Connector_Odd
        assert second["skipped_count"] == 1  # R_0402 lives in TestSys
        assert all(s["reason"].startswith("target file already exists") for s in second["failed"])

    def test_exports_only_requested_subset(self, tools, tmp_path, project):
        """Only the explicit footprints list is exported — no full export."""
        _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        board = _make_board(tmp_path)
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board,
                footprints=["Connector_Odd"],
                library="MyVendor",
                ctx=None,
            )
        )
        assert "error" not in result, result
        assert result["exported_count"] == 1
        assert result["exported"] == [
            os.path.join(
                project["third_party"], "footprints", "MyVendor.pretty", "Connector_Odd.kicad_mod"
            )
        ]
        target = os.path.join(project["third_party"], "footprints", "MyVendor.pretty")
        assert os.path.isfile(os.path.join(target, "Connector_Odd.kicad_mod"))
        assert not os.path.exists(os.path.join(target, "Sensor_Board_XYZ.kicad_mod"))

    def test_not_on_board_is_skipped(self, tools, tmp_path, project):
        """A requested name not placed on the board is skipped, never written."""
        _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        board = _make_board(tmp_path)
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board,
                footprints=["NoSuchFootprint"],
                library="MyVendor",
                ctx=None,
            )
        )
        assert "error" not in result, result
        assert result["exported_count"] == 0
        assert result["skipped_count"] == 1
        assert result["skipped"] == [{"name": "NoSuchFootprint", "reason": "not on board"}]
        target = os.path.join(project["third_party"], "footprints", "MyVendor.pretty")
        assert os.listdir(target) == []

    def test_empty_footprints_exports_nothing(self, tools, tmp_path, project):
        """An empty footprints list is valid: no export, no error."""
        _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        board = _make_board(tmp_path)
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board, footprints=[], library="MyVendor", ctx=None
            )
        )
        assert "error" not in result, result
        assert result["exported_count"] == 0
        assert result["failed_count"] == 0
        assert result["skipped_count"] == 0

    def test_export_into_existing_user_library(self, tools, tmp_path, project):
        board = _make_board(tmp_path)
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board, footprints=_BOARD_FOOTPRINTS, library="TestSys", ctx=None
            )
        )
        assert "error" not in result, result
        assert result["library_path"] == project["system_lib"]
        assert os.path.isfile(os.path.join(project["system_lib"], "Sensor_Board_XYZ.kicad_mod"))
        # R_0402 already exists in the target directory: reported as failed,
        # never overwritten.
        assert [f["name"] for f in result["failed"]] == ["R_0402_1005Metric"]
        assert result["failed_count"] == 1
        assert result["skipped_count"] == 0
        r_0402_path = os.path.join(project["system_lib"], "R_0402_1005Metric.kicad_mod")
        mod = open(r_0402_path, encoding="utf-8").read()
        assert "(version" not in mod  # still the bare fixture, not an exported copy
        # indexing re-scans the whole target: R_0402 (fixture) + 2 exported
        assert result["indexed"] == 3
        libs = project["index_mgr"]._db.get_all_libraries(project=os.path.realpath(str(tmp_path)))
        assert [lib.library_name for lib in libs] == ["TestSys"]

    def test_global_target_library_keeps_global_ownership(self, tools, tmp_path, project):
        """A library registered in the global user table stays globally owned.

        The board lives in a subproject directory that has no fp-lib-table,
        so the target library's table (global) differs from the project
        table path — ownership must be "" (global), not the project id.
        """
        proj_dir = tmp_path / "projA"
        proj_dir.mkdir()
        board = _make_board(proj_dir, name="board.kicad_pcb")
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board, footprints=_BOARD_FOOTPRINTS, library="TestSys", ctx=None
            )
        )
        assert "error" not in result, result
        libs = project["index_mgr"]._db.get_all_libraries(project=None)
        assert [lib.library_name for lib in libs] == ["TestSys"]
        assert libs[0].project == ""  # global, not project-owned
        # Still visible in the global scope.
        global_libs = project["index_mgr"]._db.get_all_libraries(project="")
        assert [lib.library_name for lib in global_libs] == ["TestSys"]


class TestExportFileContent:
    def test_exported_mod_is_valid_and_clean(self, tools, tmp_path, project):
        _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        board = _make_board(tmp_path)
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board, footprints=_BOARD_FOOTPRINTS, library="MyVendor", ctx=None
            )
        )
        assert "error" not in result, result


class TestUnsafeFootprintNames:
    """BLOCKER 2: crafted PCB headers must never reach the filesystem."""

    def _board_with_name(self, tmp_path, header_name: str) -> str:
        board = _make_board(tmp_path)
        text = open(board, encoding="utf-8").read()
        text = text.replace(
            '(footprint "CustomLib:Sensor_Board_XYZ"', f'(footprint "{header_name}"'
        )
        path = tmp_path / "evil.kicad_pcb"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_traversal_name_is_failed_not_written(self, tools, tmp_path, project):
        _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        board = self._board_with_name(tmp_path, "../../escape")
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board,
                footprints=["../../escape", "R_0402_1005Metric", "Connector_Odd"],
                library="MyVendor",
                ctx=None,
            )
        )
        assert "error" not in result, result
        # R_0402 lives in TestSys (skipped); Connector_Odd exports fine;
        # the crafted name is failed, not written.
        assert result["exported_count"] == 1
        names = {f["name"] for f in result["failed"]}
        assert "../../escape" in names
        # Nothing was written outside the library directory.
        lib_dir = os.path.join(project["third_party"], "footprints", "MyVendor.pretty")
        assert os.listdir(lib_dir) == ["Connector_Odd.kicad_mod"]

    def test_empty_name_is_failed_not_written(self, tools, tmp_path, project):
        _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        board = self._board_with_name(tmp_path, "CustomLib:")
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board,
                footprints=["", "R_0402_1005Metric", "Connector_Odd"],
                library="MyVendor",
                ctx=None,
            )
        )
        assert "error" not in result, result
        assert "" in {f["name"] for f in result["failed"]}
        lib_dir = os.path.join(project["third_party"], "footprints", "MyVendor.pretty")
        assert os.listdir(lib_dir) == ["Connector_Odd.kicad_mod"]  # no stray ".kicad_mod"

    def test_spaced_name_exports_fine(self, tools, tmp_path, project):
        """Spaces are legal KiCad footprint names — export succeeds, unlike
        path-escaping names which must still be failed."""
        _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        board = self._board_with_name(tmp_path, "M3 Hole")
        result = _run(
            tools["add_footprints_to_library"](
                pcb_path=board,
                footprints=["M3 Hole", "R_0402_1005Metric", "Connector_Odd"],
                library="MyVendor",
                ctx=None,
            )
        )
        assert "error" not in result, result
        assert result["exported_count"] == 2  # M3 Hole + Connector_Odd
        assert result["failed_count"] == 0
        lib_dir = os.path.join(project["third_party"], "footprints", "MyVendor.pretty")
        assert "M3 Hole.kicad_mod" in os.listdir(lib_dir)
        assert "Connector_Odd.kicad_mod" in os.listdir(lib_dir)

    def test_write_footprint_mod_rejects_unsafe_name(self, tmp_path):
        from kcaa.utils.pcb_footprint_utils import is_safe_footprint_name, write_footprint_mod

        lib_dir = tmp_path / "Lib.pretty"
        lib_dir.mkdir()
        with pytest.raises(ValueError):
            write_footprint_mod(str(lib_dir), "../../escape", ["footprint"])
        with pytest.raises(ValueError):
            write_footprint_mod(str(lib_dir), "", ["footprint"])
        assert os.listdir(lib_dir) == []
        # Spaces are legal in KiCad footprint names (e.g. M3 Hole) — only
        # path-escaping constructs are refused.
        assert is_safe_footprint_name("M3 Hole")
        assert is_safe_footprint_name("M3 Spade Hole")
        assert not is_safe_footprint_name("a\\b")
        assert not is_safe_footprint_name("a\x00b")


class TestCreateRollback:
    """MINOR 2: a failed registration must not leave an orphaned .pretty dir."""

    def test_registration_failure_removes_created_dir(self, tools, tmp_path, project, monkeypatch):
        _run = asyncio.run
        from kcaa.tools import pcb_library_tools

        def _boom(*args, **kwargs):
            raise ValueError("fp-lib-table is a single line")

        monkeypatch.setattr(pcb_library_tools, "register_library_in_table", _boom)
        result = _run(tools["create_footprint_library"](name="MyVendor", ctx=None))
        assert "error" in result
        lib_dir = os.path.join(project["third_party"], "footprints", "MyVendor.pretty")
        assert not os.path.exists(lib_dir)
