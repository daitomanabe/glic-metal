#!/usr/bin/env python3

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    manifest = json.loads(
        (ROOT / "resources/integration-manifest.json").read_text()
    )
    selected = json.loads(
        (ROOT / "resources/selected-presets.json").read_text()
    )["mixed_glitch_patterns"]

    assert manifest["schema"] == "glic-metal-integration-v1"
    bank = manifest["selected_preset_bank"]
    assert bank["count"] == len(selected) == 19
    counts = {
        category: sum(item["category"] == category for item in selected)
        for category in ("original", "spatial", "codec")
    }
    assert bank["category_counts"] == counts == {
        "original": 14,
        "spatial": 4,
        "codec": 1,
    }

    for header in manifest["public_headers"]:
        assert (ROOT / "include" / header).is_file(), header
    for path in manifest["documentation"].values():
        assert (ROOT / path).is_file(), path

    lanes = manifest["lanes"]
    assert set(lanes) == set(counts)
    assert lanes["codec"]["execution"] == "asynchronous"
    assert lanes["original"]["execution"] == "synchronous"
    assert lanes["spatial"]["execution"] == "synchronous"
    assert lanes["original"]["apply"] == "glic_glitch_preset_apply_metal"
    assert lanes["spatial"]["apply"] == "glic_glitch_preset_apply_metal"
    assert lanes["codec"]["apply"] == "glic_glitch_preset_apply_codec"
    expected_codec_effects = [
        "qp_pump", "bitrate_crush", "slice_dropout", "slice_transplant",
        "pframe_loss", "idr_starvation", "payload_xor",
        "reference_timewarp", "codec_feedback", "generation_cascade",
        "resolution_hop", "chroma_codec_echo", "temporal_polyphony",
        "intra_cannibalism", "residual_rift", "codec_grain_synth",
        "recursive_codec_skin", "concealment_choreography",
    ]
    assert lanes["codec"]["effect_count"] == len(expected_codec_effects) == 18
    assert lanes["codec"]["effect_names"] == expected_codec_effects

    print("PASS AI integration manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
