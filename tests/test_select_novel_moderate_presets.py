#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "select_novel_moderate_presets",
    ROOT / "scripts" / "select_novel_moderate_presets.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def candidate(index: int, family: str, base: float, novelty: float) -> dict:
    return {
        "recipe_hash": f"{index:016x}",
        "mechanism_family": family,
        "_base_score": base,
        "_nearest_reference_distance": novelty,
        "perceptual": {
            "phash": f"{index:016x}",
            "dhash": f"{index * 3:016x}",
            "hsv_hist": [0.5, 0.5],
            "luma_grid": [index / 20.0] * 4,
            "edge_grid": [index / 30.0] * 4,
            "color_grid": [index / 40.0] * 4,
            "residual_reference": "unavailable",
        },
    }


def main() -> int:
    assert MODULE.quantile([0.0, 10.0], 0.5) == 5.0
    assert MODULE.normalized_entropy([0.5, 0.5]) > 0.99
    assert MODULE.normalized_entropy([1.0, 0.0]) == 0.0

    pool = [
        candidate(1, "line", 0.80, 0.20),
        candidate(2, "line", 0.90, 0.30),
        candidate(3, "shear", 0.70, 0.40),
        candidate(4, "sync", 0.60, 0.50),
    ]
    selected = MODULE.select_maxmin(pool, 3)
    assert len(selected) == 3
    assert len({row["mechanism_family"] for row in selected}) == 3
    assert all("_selection_gain" in row for row in selected)
    assert MODULE.select_maxmin([candidate(5, "line", 1.0, 1.0)], 1)[0][
        "_nearest_selected_distance"
    ] == 1.0
    print("novel moderate preset selector tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
