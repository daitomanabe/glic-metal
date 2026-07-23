#!/usr/bin/env python3
"""Unit checks for video-wrapper rate parsing and the explicit 30 fps gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "process_video.py"
SPEC = importlib.util.spec_from_file_location("glic_process_video", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def main() -> int:
    assert abs(MODULE.parse_frame_rate("30000/1001") - 29.97002997) < 1e-6
    assert MODULE.parse_frame_rate("30/1") == 30.0
    assert MODULE.parse_frame_rate("24") == 24.0
    assert MODULE.parse_frame_rate("0/0") == 0.0
    assert MODULE.parse_frame_rate("invalid") == 0.0
    assert MODULE.parse_frame_count("166") == 166
    assert MODULE.parse_frame_count("N/A") is None
    assert MODULE.parse_frame_count("0") is None
    assert MODULE.parse_frame_count("2000001") is None
    assert MODULE.first_valid_duration("N/A", "5.533333") == 5.533333
    assert MODULE.first_valid_duration(None, "nan", "0", "5.0") == 5.0
    assert MODULE.first_valid_duration("N/A", None) is None
    assert MODULE.estimate_frame_capacity("5.533333", 30.0) == 168
    assert MODULE.estimate_frame_capacity("N/A", 30.0) is None
    assert MODULE.select_frame_capacity("166", "5.533333", 30.0, False) == 168
    assert MODULE.select_frame_capacity("166", "N/A", 30.0, False) == 166
    assert MODULE.select_frame_capacity("166", "5.533333", 60.0, True) == 334
    assert MODULE.resolve_backend("original_visual", None, "darwin") == "metal"
    assert MODULE.resolve_backend("original_visual", "auto", "darwin") == "metal"
    assert MODULE.resolve_backend("original_visual", "auto", "linux") == "cpu"
    assert MODULE.resolve_backend("original_visual", "cpu", "darwin") == "cpu"
    assert MODULE.resolve_backend("compat_realtime", "auto", "linux") == "auto"
    assert MODULE.resolve_backend("codec_glitch", None, "darwin") == "videotoolbox"
    assert MODULE.filter_binary_name("compat_realtime") == "glic_realtime_filter"
    assert (
        MODULE.filter_binary_name("original_visual")
        == "glic_original_visual_filter"
    )
    assert MODULE.filter_binary_name("codec_glitch") == "glic_codec_glitch_filter"

    expected_codec_effects = (
        "qp_pump",
        "bitrate_crush",
        "slice_dropout",
        "slice_transplant",
        "pframe_loss",
        "idr_starvation",
        "payload_xor",
        "reference_timewarp",
        "codec_feedback",
        "generation_cascade",
        "resolution_hop",
        "chroma_codec_echo",
        "temporal_polyphony",
        "intra_cannibalism",
        "residual_rift",
        "codec_grain_synth",
        "recursive_codec_skin",
        "concealment_choreography",
    )
    assert MODULE.CODEC_GLITCH_EFFECTS == expected_codec_effects
    for effect in expected_codec_effects:
        parsed = MODULE.parse_args(
            [
                "input.mov",
                "output.mp4",
                "--processing-mode",
                "codec_glitch",
                "--codec-effect",
                effect,
            ]
        )
        assert parsed.processing_mode == "codec_glitch"
        assert parsed.codec_effect == effect

    assert MODULE.codec_glitch_filter_options(
        codec_effect="slice_transplant",
        codec_amount=0.625,
        codec_rate=0.25,
        codec_feedback=0.75,
        seed=123,
        frame_rate=29.97002997,
    ) == [
        "--fps",
        "30",
        "--effect",
        "slice_transplant",
        "--amount",
        "0.625",
        "--rate",
        "0.25",
        "--feedback",
        "0.75",
        "--seed",
        "123",
    ]

    codec_args = MODULE.parse_args(
        [
            "input.mov",
            "output.mp4",
            "--processing-mode",
            "codec_glitch",
            "--codec-effect",
            "payload_xor",
            "--codec-amount",
            "0.7",
            "--codec-rate",
            "0.4",
            "--codec-feedback",
            "0.2",
        ]
    )
    codec_fields = MODULE.codec_glitch_report_fields(
        codec_args,
        {
            "effect_family": "payload_xor",
            "amount": 0.7,
            "rate": 0.4,
            "feedback": 0.2,
            "hardware_encoder": True,
            "latency_p50_ms": 8.0,
            "latency_p95_ms": 18.0,
            "fallback_frames": 4,
            "realtime_20fps_passed": True,
            "realtime_30fps_passed": False,
            "target_fps": 30,
            "processing_fps": 87.5,
            "statistics": {
                "hardware_decoder": True,
                "encoded_frames": 160,
                "decoded_frames": 152,
                "intentional_packet_drops": 8,
                "codec_errors": 2,
                "watchdog_recoveries": 1,
                "average_latency_milliseconds": 12.5,
            },
        },
    )
    assert codec_fields["codec_effect"] == "payload_xor"
    assert codec_fields["codec_amount"] == 0.7
    assert codec_fields["codec_rate"] == 0.4
    assert codec_fields["codec_feedback"] == 0.2
    assert codec_fields["codec_hardware_encoder"] is True
    assert codec_fields["codec_hardware_decoder"] is True
    assert codec_fields["codec_encoded_frames"] == 160
    assert codec_fields["codec_decoded_frames"] == 152
    assert codec_fields["codec_intentional_packet_drops"] == 8
    assert codec_fields["codec_processing_errors"] == 2
    assert codec_fields["codec_watchdog_recoveries"] == 1
    assert codec_fields["codec_average_latency_milliseconds"] == 12.5
    assert codec_fields["codec_latency_p50_milliseconds"] == 8.0
    assert codec_fields["codec_latency_p95_milliseconds"] == 18.0
    assert codec_fields["codec_fallback_frames"] == 4
    assert codec_fields["codec_realtime_20fps_passed"] is True
    assert codec_fields["codec_realtime_30fps_passed"] is False
    assert codec_fields["codec_engine_fps"] == 87.5
    non_codec_args = MODULE.parse_args(["input.mov", "output.mp4"])
    assert MODULE.codec_glitch_report_fields(non_codec_args, {}) == {}

    common = {
        "width": 960,
        "height": 540,
        "frames": 166,
        "elapsed_seconds": 5.0,
    }
    assert MODULE.passes_end_to_end_average_30fps(
        **common, target_fps=30.0, output_fps=30.0
    )
    assert not MODULE.passes_end_to_end_average_30fps(
        **common, target_fps=24.0, output_fps=24.0
    )
    assert not MODULE.passes_end_to_end_average_30fps(
        **common, target_fps=30.0, output_fps=29.97002997
    )
    assert not MODULE.passes_end_to_end_average_30fps(
        **{**common, "elapsed_seconds": 6.0}, target_fps=30.0, output_fps=30.0
    )
    assert MODULE.passes_end_to_end_average_20fps(
        **{**common, "elapsed_seconds": 8.0},
        target_fps=20.0,
        output_fps=20.0,
    )
    assert not MODULE.passes_end_to_end_average_20fps(
        **{**common, "elapsed_seconds": 9.0},
        target_fps=20.0,
        output_fps=20.0,
    )
    print("PASS process_video frame-rate helpers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
