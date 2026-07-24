from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import sys

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import process_codec_lab as lab  # noqa: E402
import evolutionary_codec_search as search  # noqa: E402
import native_syntax_glitch as native_syntax  # noqa: E402


def main() -> int:
    assert len(lab.SYNTAX_EFFECTS) == 13
    assert len(lab.ANALYSIS_EFFECTS) == 7
    assert len(lab.EFFECTS) == 20
    catalog = json.loads(
        (ROOT / "resources" / "codec-lab-effects.json").read_text()
    )
    assert catalog["schema"] == "glic-codec-lab-effect-catalog-v1"
    assert catalog["realtime_crossbreed"]["effect_names"] == [
        "dual_codec_crossbreed",
        "codec_pingpong",
        "gop_accordion",
        "bframe_braid",
        "plane_split_codec",
        "roi_quality_islands",
        "codec_phase_mosaic",
        "encoder_hot_swap",
        "pts_rubberband",
        "bitrate_raster",
    ]
    assert catalog["realtime_native_expansion"]["effect_names"] == [
        "plane_time_split",
        "reference_atlas",
        "flow_lattice",
        "scan_order_fold",
        "regional_gop_clock",
        "entropy_feedback",
        "rolling_time_shutter",
        "asymmetric_plane_codec",
    ]
    assert catalog["syntax_lab"]["effect_names"] == list(lab.SYNTAX_EFFECTS)
    assert catalog["native_compressed_syntax_lab"]["effect_names"] == list(
        native_syntax.EFFECTS
    )
    assert (
        catalog["native_compressed_syntax_lab"]["implementation_levels"]
        == native_syntax.IMPLEMENTATION_LEVEL
    )
    assert set(
        catalog["native_compressed_syntax_lab"]["codec_support"][
            "mpeg4_part2"
        ]
    ) == set(native_syntax.MOTION_EFFECTS)
    assert catalog["native_compressed_syntax_lab"]["token_free_ranking"] is True
    assert (
        set(catalog["analysis_and_search"]["effect_names"])
        == set(lab.ANALYSIS_EFFECTS) | {"evolutionary_codec_search"}
    )

    height, width = 48, 64
    x = np.arange(width, dtype=np.uint8)[None, :]
    y = np.arange(height, dtype=np.uint8)[:, None]
    base = np.empty((height, width, 3), dtype=np.uint8)
    base[..., 0] = x
    base[..., 1] = y
    base[..., 2] = x // 2 + y // 2
    history = deque(
        [np.roll(base, shift, axis=1) for shift in range(8)],
        maxlen=12,
    )
    frozen = None
    for index, effect in enumerate(lab.EFFECTS):
        output, frozen = lab.transform_frame(
            effect,
            np.roll(base, index + 1, axis=0),
            history,
            np.flip(base, axis=1),
            frozen,
            index + 10,
            0.72,
            0.56,
            0.68,
            1234,
            0.74,
        )
        assert output.shape == base.shape, effect
        assert output.dtype == np.uint8, effect

    decoder_vectors = [
        {
            "w": 16,
            "h": 16,
            "dst_x": 16,
            "dst_y": 16,
            "motion_x": 24,
            "motion_y": -8,
            "motion_scale": 4,
        },
        {
            "w": 16,
            "h": 16,
            "dst_x": 48,
            "dst_y": 32,
            "motion_x": -16,
            "motion_y": 12,
            "motion_scale": 4,
        },
    ]
    field = lab.decoder_motion_field(base.shape, decoder_vectors)
    assert field.shape == (height, width, 2)
    assert np.max(np.abs(field)) > 0
    mirrored, _ = lab.rewrite_decoder_motion_field(
        "motion_vector_mirror", field, 0.72, 0.56, 11, None
    )
    assert np.allclose(mirrored[..., 0], -field[..., 0])
    quantized, _ = lab.rewrite_decoder_motion_field(
        "motion_vector_quantizer", field, 0.72, 0.56, 11, None
    )
    quantum = 2.0 + 0.72 * 14.0
    assert np.allclose(quantized / quantum, np.round(quantized / quantum))
    assert all(
        lab.IMPLEMENTATION_LEVEL[effect]
        == "decoder_exported_motion_vector_field_rewrite"
        for effect in lab.SYNTAX_EFFECTS[:4]
    )
    assert all(
        lab.IMPLEMENTATION_LEVEL[effect]
        == "decoded_block_dct_coefficient_rewrite"
        for effect in (
            "residual_sign_flip",
            "residual_band_gate",
            "transform_block_transplant",
            "transform_scan_fold",
        )
    )
    assert len(lab.zigzag_indices()) == 64
    assert len(set(lab.zigzag_indices())) == 64
    assert (
        lab.IMPLEMENTATION_LEVEL["decoder_disagreement_amplifier"]
        == "real_h264_hevc_prores_decoder_pixel_disagreement_amplification"
    )
    assert (
        lab.IMPLEMENTATION_LEVEL["audio_packet_resonance"]
        == "real_opus_packet_size_timestamp_driven_video_reconstruction"
    )

    generated = [search.candidate(index, 1234) for index in range(36)]
    assert len({entry["name"] for entry in generated}) == 36
    assert set(lab.EFFECTS).issubset({entry["effect"] for entry in generated})
    assert all(
        entry["codec"] in {"h264", "hevc"}
        for entry in generated
        if entry["effect"].startswith("motion_vector_")
    )
    print("PASS codec lab effects, catalog, and evolutionary recipes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
