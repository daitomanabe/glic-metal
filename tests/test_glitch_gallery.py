#!/usr/bin/env python3

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile


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

    algorithm = deepcopy(catalog["algorithms"][0])
    algorithm["variants"] = algorithm.pop("presets")
    for variant in algorithm["variants"]:
        variant.update(
            {
                "status": "PASS",
                "warnings": [],
                "video": None,
                "thumbnail": None,
                "metrics": {},
                "error": None,
            }
        )
    manifest = {
        "schema": "glic-glitch-algorithm-gallery-v1",
        "generated_utc": "2026-07-26T00:00:00Z",
        "source": {},
        "glic_metal_revision": "test-revision",
        "algorithm_count": 1,
        "expected_video_count": 3,
        "rendered_video_count": 0,
        "status_counts": {"PASS": 3, "WARN": 0, "FAIL": 0, "MISSING": 0},
        "family_counts": {"original": 1},
        "algorithms": [algorithm],
    }
    with tempfile.TemporaryDirectory() as temporary:
        site = Path(temporary)
        review_keys = [
            f"{algorithm['id']}::{variant['id']}"
            for variant in algorithm["variants"]
        ]
        review_data = {
            "schema": "glic-metal-gallery-review-v1",
            "scope": "all",
            "counts": {"adopted": 1, "rejected": 1, "pending": 1},
            "items": [
                {"key": review_keys[0], "decision": "adopt"},
                {"key": review_keys[1], "decision": "reject"},
                {"key": review_keys[2], "decision": "pending"},
            ],
        }
        gallery.render_site(manifest, site, review_data)
        page = (site / "index.html").read_text()
        rendered_manifest = json.loads((site / "manifest.json").read_text())
        assert page.count('data-review-key="') == 3
        assert "glic-metal-gallery-review-v1" in page
        assert "window.__GLIC_REVIEW_ITEMS" in page
        assert "window.__GLIC_CURATED_DECISIONS" in page
        assert 'id="copy-adopted"' in page
        assert 'id="download-all"' in page
        assert 'id="review-filter"' in page
        assert '<option value="adopt" selected' in page
        assert "glic-metal-gallery-review-v2" in page
        assert "savedEnvelope.curation_revision === curationRevision" in page
        assert "catch (_) {\n      decisions = {...curatedDecisions};" in page
        assert "decisions = {...curatedDecisions}" in page
        assert "localStorage.setItem(storageKey" in page
        assert "glic-metal-adopted-presets.json" in page
        assert rendered_manifest["curation"]["schema"] == (
            "glic-metal-gallery-review-v1"
        )
        assert len(rendered_manifest["curation"]["revision"]) == 16
        assert rendered_manifest["curation"]["default_filter"] == "adopt"
        assert rendered_manifest["curation"]["counts"] == {
            "adopted": 1,
            "rejected": 1,
            "pending": 1,
        }
        assert rendered_manifest["curation"]["realtime_adopted"] == 1
    print("PASS glitch gallery catalog: 147 algorithms, 441 videos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
