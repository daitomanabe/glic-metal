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

    public_items = [
        {
            "selection_rank": 1,
            "name": "first</script>",
            "recipe_hash": "abc123",
            "mechanism_family": "line",
            "preview": "previews/first.png",
            "canonical": "v2|first</script>",
            "complexity_score": 0.5,
            "nearest_prior_distance": 0.4,
            "nearest_selected_distance": 1.0,
        },
        {
            "selection_rank": 2,
            "name": "second",
            "recipe_hash": "def456",
            "mechanism_family": "sync",
            "preview": "previews/second.png",
            "video": "videos/second.mp4",
            "canonical": "v2|second",
            "complexity_score": 0.6,
            "nearest_prior_distance": 0.3,
            "nearest_selected_distance": 0.2,
        },
    ]
    summary = {"selected": 2, "moderate_pool": 4, "eligible": 8}
    review_html = MODULE.build_html(public_items, summary)
    assert review_html.count('class="preset-checkbox"') == 2
    assert 'id="export-json"' in review_html
    assert 'id="export-csv"' in review_html
    assert 'selection_origin: "checked_only"' in review_html
    assert 'download("adopted-presets.json"' in review_html
    assert "URL.revokeObjectURL(url), 1000" in review_html
    assert 'JSON.stringify(adoptedPayload(), null, 2) + "\\n"' in review_html
    assert 'fields.join(",") + "\\n" + rows.join("\\n") + "\\n"' in review_html
    assert "first</script>" not in review_html
    assert "first\\u003c/script\\u003e" in review_html
    assert MODULE.selection_set_id(public_items) == MODULE.selection_set_id(public_items)
    assert MODULE.selection_set_id(public_items) != MODULE.selection_set_id(public_items[:1])
    print("novel moderate preset selector tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
