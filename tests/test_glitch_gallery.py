#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_glitch_algorithm_gallery as gallery  # noqa: E402


def main() -> int:
    catalog = gallery.build_catalog()
    assert catalog["schema"] == "glic-glitch-gallery-presets-v1"
    assert catalog["algorithm_count"] == 147
    assert catalog["video_count"] == 441
    assert catalog["family_counts"] == {
        "original": 14,
        "spatial": 14,
        "codec_realtime": 36,
        "codec_lab": 20,
        "native_syntax": 12,
        "offline_packet": 8,
        "structured": 6,
        "transport": 3,
        "metadata": 2,
        "generation": 32,
    }
    ids = [row["id"] for row in catalog["algorithms"]]
    assert len(ids) == len(set(ids))
    assert all(len(row["presets"]) == 3 for row in catalog["algorithms"])
    assert all(
        tuple(preset["id"] for preset in row["presets"])
        == gallery.VARIANT_NAMES
        for row in catalog["algorithms"]
    )
    assert all(
        0.0 <= preset["parameters"]["amount"] <= 1.0
        and 0.0 <= preset["parameters"]["rate"] <= 1.0
        and 0.0 <= preset["parameters"]["feedback"] <= 1.0
        and 0.0 <= preset["parameters"]["scale"] <= 1.0
        and 0.0 <= preset["parameters"]["strength"] <= 2.0
        and preset["parameters"]["wet_mix"] in {0.62, 0.82, 1.0}
        and preset["parameters"]["generations"] in {1, 2, 3}
        for row in catalog["algorithms"]
        for preset in row["presets"]
    )

    source_catalog = json.loads(
        (ROOT / "resources" / "codec-lab-effects.json").read_text()
    )
    generation_rows = [
        row for row in catalog["algorithms"] if row["family"] == "generation"
    ]
    assert {
        (row["codec"], row["effect"]) for row in generation_rows
    } == {
        (codec, effect)
        for codec in source_catalog["generation_codecs"]["codecs"]
        for effect in source_catalog["generation_codecs"]["effect_names"]
    }
    assert all(
        preset["parameters"]["generations"] == 1
        for row in generation_rows
        if row["codec"] == "av2"
        for preset in row["presets"]
    )
    assert all(
        tuple(
            preset["parameters"]["wet_mix"] for preset in row["presets"]
        )
        == (0.62, 0.82, 1.0)
        for row in catalog["algorithms"]
    )
    selected_original = {
        item["effect"]
        for item in json.loads(
            (ROOT / "resources" / "selected-presets.json").read_text()
        )["mixed_glitch_patterns"]
        if item["category"] == "original"
    }
    assert {
        row["effect"] for row in catalog["algorithms"] if row["family"] == "original"
    } == selected_original
    print("PASS glitch gallery catalog: 147 algorithms, 441 videos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
