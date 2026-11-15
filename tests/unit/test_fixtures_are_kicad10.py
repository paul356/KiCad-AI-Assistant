"""Every ``.kicad_pcb`` fixture must be a board real KiCad 10 accepts (#309).

The fixture set used to carry names KiCad never writes (``B.Courtyard``),
internal layer ids instead of the file-format ids, an odd copper-layer count
and ``;`` comment lines, so KiCad 10 refused most of them.  Tests over such a
file prove what the code does with a byte sequence KiCad cannot open.

This guard is text-only: CI has no KiCad.  To re-prove loadability by hand,
run the real parser/plotter in the official image:

    docker run --rm -v "$PWD:/work" -w /work kicad/kicad:10.0.8-full \
        python3 -c "import pcbnew; assert pcbnew.LoadBoard('/work/<fixture>')"

and export the copper layers to force a full parse:

    docker run --rm -v "$PWD:/work" -w /work kicad/kicad:10.0.8-full \
        kicad-cli pcb export svg --layers F.Cu,B.Cu --output /tmp/out.svg \
        /work/<fixture>

A load -> save -> load -> save round-trip must also be byte-identical; that is
how the fixtures in this tree were produced.
"""

from pathlib import Path

import pytest
import sexpdata

from kcaa.utils.sexp_utils import sym_str

_FIXTURE_DIRS = (
    Path(__file__).parent / "tools" / "fixtures",
    Path(__file__).parent.parent / "integration" / "fixtures",
)

FIXTURES = sorted(path for directory in _FIXTURE_DIRS for path in directory.glob("*.kicad_pcb"))

# File-level layer ids KiCad 10.0.8 writes in the ``(layers ...)`` block
# (PCB_LAYER_ID: front layers odd, back layers even, inner copper 4..30).
# Probed by saving empty boards with 1/2/4/6 copper layers in
# ``kicad/kicad:10.0.8-full``; do not extend from memory.
LAYER_IDS = {
    "F.Cu": 0,
    "B.Cu": 2,
    "F.Mask": 1,
    "B.Mask": 3,
    "F.SilkS": 5,
    "B.SilkS": 7,
    "F.Adhes": 9,
    "B.Adhes": 11,
    "F.Paste": 13,
    "B.Paste": 15,
    "Dwgs.User": 17,
    "Cmts.User": 19,
    "Eco1.User": 21,
    "Eco2.User": 23,
    "Edge.Cuts": 25,
    "Margin": 27,
    "B.CrtYd": 29,
    "F.CrtYd": 31,
    "B.Fab": 33,
    "F.Fab": 35,
    "User.1": 39,
    "User.2": 41,
    "User.3": 43,
    "User.4": 45,
    "User.5": 47,
    "User.6": 49,
    "User.7": 51,
    "User.8": 53,
    "User.9": 55,
}
LAYER_IDS.update({f"In{n}.Cu": 2 * n + 2 for n in range(1, 31)})

COPPER_TYPES = frozenset({"signal", "power", "mixed", "jumper"})
# Policy floor for the KiCad 10 migration, not a parser limit: 10.0.8 accepts
# old dates (20241229, 20251027, 20251028) and rejects future ones (20260606),
# and writes 20260206 itself.  20251028 is the floor #309 pins for fixtures.
MIN_VERSION = 20251028


def _top_level(tree, key):
    for node in tree:
        if isinstance(node, list) and node and sym_str(node[0]) == key:
            return node
    return None


@pytest.mark.parametrize("path", FIXTURES, ids=lambda path: path.name)
def test_fixture_is_a_kicad10_board(path):
    text = path.read_text(encoding="utf-8")
    tree = sexpdata.loads(text)

    assert isinstance(tree, list) and sym_str(tree[0]) == "kicad_pcb", (
        f"{path.name}: root node is not kicad_pcb"
    )

    version = _top_level(tree, "version")
    assert version is not None, f"{path.name}: no (version ...) header"
    assert int(version[1]) >= MIN_VERSION, (
        f"{path.name}: (version {version[1]}) predates KiCad 10 (minimum {MIN_VERSION})"
    )

    generator_version = _top_level(tree, "generator_version")
    assert generator_version is not None, f"{path.name}: no (generator_version ...)"
    assert sym_str(generator_version[1]).startswith("10."), (
        f"{path.name}: (generator_version {generator_version[1]!r}) is not KiCad 10"
    )

    layers = _top_level(tree, "layers")
    assert layers is not None, f"{path.name}: no (layers ...) block"
    copper_layers = 0
    for entry in layers[1:]:
        layer_id, name, layer_type = entry[0], sym_str(entry[1]), sym_str(entry[2])
        assert name in LAYER_IDS, (
            f"{path.name}: {name!r} is not a layer name KiCad 10 writes; "
            f"canonical names are {sorted(LAYER_IDS)}"
        )
        assert layer_id == LAYER_IDS[name], (
            f"{path.name}: layer {name!r} is declared as {layer_id!r}, "
            f"KiCad 10 writes {LAYER_IDS[name]}"
        )
        if layer_type in COPPER_TYPES:
            copper_layers += 1

    # KiCad rejects a one-copper-layer board (LoadBoard returns None) and any
    # odd count: copper layers exist as front/back pairs around inner pairs.
    assert copper_layers >= 2 and copper_layers % 2 == 0, (
        f"{path.name}: {copper_layers} copper layers is not a legal KiCad stack (2, 4, 6, ...)"
    )

    comment_lines = [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if line.lstrip().startswith((";", "#"))
    ]
    assert not comment_lines, (
        f"{path.name}: comment lines {comment_lines} are not valid s-expression "
        "syntax; KiCad's parser rejects them"
    )
