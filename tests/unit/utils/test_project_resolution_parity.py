"""Parity between the core resolver and the plugin's duplicated mirror."""

import os
import random

from kcaa.utils import project_resolution
from kicad_plugin import project_resolution as plugin_project_resolution


def _build_random_tree(root, seed=0x315):
    rng = random.Random(seed)
    directories = [root]
    for depth in range(1, 4):
        for index in range(3):
            parent = rng.choice(directories)
            directory = parent / f"d{depth}_{index}"
            directory.mkdir(exist_ok=True)
            directories.append(directory)

    stems = ("aria", "beta", "main_bus", "power", "zeta")
    suffixes = (
        ".kicad_pro",
        ".KICAD_PRO",
        ".Kicad_Pro",
        ".kicad_sch",
        ".kicad_pcb",
        ".txt",
    )
    paths = []
    for directory in directories:
        for _ in range(rng.randrange(0, 4)):
            path = directory / f"{rng.choice(stems)}{rng.choice(suffixes)}"
            if path.exists() or path.is_dir():
                continue
            path.write_text("(kicad)\n")
            paths.append(path)
    return directories, paths


def test_plugin_mirror_matches_core_on_random_trees(tmp_path, isolated_project_search):
    root = tmp_path / "random_tree"
    root.mkdir()
    directories, paths = _build_random_tree(root)

    probes = list(paths)
    probes += [directory / "does_not_exist.kicad_sch" for directory in directories]
    probes += directories

    for probe in probes:
        expected = project_resolution.find_project_file(str(probe))
        actual = plugin_project_resolution.find_project_file(str(probe))
        assert actual == expected, f"parity mismatch for {os.fspath(probe)!r}"
