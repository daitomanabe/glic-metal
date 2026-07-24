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
    offline_catalog = json.loads(
        (ROOT / "resources/offline-codec-effects.json").read_text()
    )
    codec_lab_catalog = json.loads(
        (ROOT / "resources/codec-lab-effects.json").read_text()
    )

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
    distribution = manifest["distribution"]
    assert distribution["macos_sdk"] == {
        "library": "GlicMetal.xcframework",
        "resources": "GlicMetalResources.bundle",
        "documentation_directory": "Documentation",
        "offline_tools_directory": "Tools",
        "python_requirements": "Tools/requirements.txt",
    }
    assert (
        distribution["cmake_package"]["offline_tools_variable"]
        == "GLIC_METAL_TOOLS_DIR"
    )
    assert (
        distribution["cmake_package"]["python_requirements_variable"]
        == "GLIC_METAL_PYTHON_REQUIREMENTS"
    )

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
        "dual_codec_crossbreed", "codec_pingpong", "gop_accordion",
        "bframe_braid", "plane_split_codec", "roi_quality_islands",
        "codec_phase_mosaic", "encoder_hot_swap", "pts_rubberband",
        "bitrate_raster", "plane_time_split", "reference_atlas",
        "flow_lattice", "scan_order_fold", "regional_gop_clock",
        "entropy_feedback", "rolling_time_shutter",
        "asymmetric_plane_codec",
    ]
    assert lanes["codec"]["effect_count"] == len(expected_codec_effects) == 36
    assert lanes["codec"]["effect_names"] == expected_codec_effects
    assert (
        lanes["codec"]["implementation_level"]
        == "glic_codec_glitch_effect_implementation_level"
    )

    packet_workflow = manifest["offline_workflows"]["packet_glitch_lab"]
    assert packet_workflow["execution"] == "offline_isolated_process"
    assert packet_workflow["realtime_claim"] is False
    assert packet_workflow["may_produce_invalid_bitstream"] is True
    assert packet_workflow["catalog"] == "resources/offline-codec-effects.json"
    assert packet_workflow["sdk_entrypoint"].startswith("Tools/")
    assert packet_workflow["sdk_evaluator"].startswith("Tools/")
    assert offline_catalog["schema"] == "glic-codec-effect-catalog-v1"
    assert packet_workflow["effects"] == [
        effect["name"] for effect in offline_catalog["offline_effects"]
    ]
    assert set(packet_workflow["codecs"]) == {
        codec
        for effect in offline_catalog["offline_effects"]
        for codec in effect["codecs"]
    }
    assert manifest["runtime_resources"]["offline-codec-effects.json"]
    assert manifest["runtime_resources"]["codec-lab-effects.json"]
    assert (
        manifest["offline_workflows"]["codec_syntax_lab"]["effect_count"]
        == len(codec_lab_catalog["syntax_lab"]["effect_names"])
        == 13
    )
    assert (
        manifest["offline_workflows"]["analysis_and_evolutionary_lab"][
            "effect_count"
        ]
        == len(codec_lab_catalog["analysis_and_search"]["effect_names"])
        == 8
    )
    assert (
        codec_lab_catalog["realtime_crossbreed"]["effect_names"]
        == expected_codec_effects[18:28]
    )
    assert (
        codec_lab_catalog["realtime_native_expansion"]["effect_names"]
        == expected_codec_effects[-8:]
    )
    workflow_catalog_sections = {
        "structured_bitstream_lab": "structured_bitstream_lab",
        "transport_glitch_lab": "transport_lab",
        "metadata_glitch_lab": "metadata_lab",
    }
    for workflow_name, catalog_name in workflow_catalog_sections.items():
        workflow = manifest["offline_workflows"][workflow_name]
        catalog_section = codec_lab_catalog[catalog_name]
        assert workflow["effect_count"] == len(catalog_section["effect_names"])
        assert workflow["realtime_claim"] is False
    assert set(
        manifest["offline_workflows"]["codec_generations"]["codecs"]
    ) == set(codec_lab_catalog["generation_codecs"]["codecs"])
    assert manifest["offline_workflows"]["transport_glitch_lab"][
        "network_capture"
    ] is False
    for workflow in manifest["offline_workflows"].values():
        assert workflow["sdk_entrypoint"].startswith("Tools/")

    print("PASS AI integration manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
