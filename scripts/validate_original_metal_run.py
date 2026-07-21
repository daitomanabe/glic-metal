#!/usr/bin/env python3
"""Fail-closed checks for original-style Metal certification artifacts."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


class ValidationError(RuntimeError):
    pass


METAL_BACKEND = "metal-original-visual"
METAL_EXECUTION_MODE = "hybrid_cpu_colorspace_segmentation_gpu_reconstruction"
METAL_FIDELITY_LANE = (
    "upstream-colorspace-quadtree-fixed-predictor-quantize-"
    "reconstruct-float-float-cdf97-fp32-storage-no-serialization"
)
CPU_FIDELITY_LANE = (
    "upstream-colorspace-quadtree-fixed-predictor-quantize-"
    "reconstruct-exact-cdf97-no-serialization"
)
METAL_CDF97_PRECISION = "float-float-accumulation-fp32-storage-safe-math"
FIDELITY_CLAIM = "original-style-algorithmic-core-not-processing-pixel-exact"
PROCESSING_COMPATIBILITY_FIELDS = (
    "processing_rounding_compatible",
    "processing_raw_plane_pack_compatible",
    "processing_rng_and_cross_channel_order_compatible",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(payload, dict), f"JSON root is not an object: {path}")
    return payload


def valid_fnv1a64(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 16:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def validate_strict_metadata(
    payload: dict[str, Any], *, label: str, metal: bool
) -> None:
    require(
        payload.get("backend") == (METAL_BACKEND if metal else "cpu-reference"),
        f"{label}: wrong backend",
    )
    require(
        payload.get("execution_mode")
        == (METAL_EXECUTION_MODE if metal else "cpu_parallel_channels"),
        f"{label}: wrong execution mode",
    )
    require(
        payload.get("fidelity_lane")
        == (METAL_FIDELITY_LANE if metal else CPU_FIDELITY_LANE),
        f"{label}: stale or wrong fidelity lane",
    )
    require(
        payload.get("fidelity_claim") == FIDELITY_CLAIM,
        f"{label}: wrong fidelity claim",
    )
    require(
        payload.get("processing_pixel_exact") is False,
        f"{label}: invalid Processing pixel-exact claim",
    )
    require(
        payload.get("cdf97_precision")
        == (METAL_CDF97_PRECISION if metal else "float64"),
        f"{label}: stale or wrong CDF97 precision",
    )
    for field in PROCESSING_COMPATIBILITY_FIELDS:
        require(payload.get(field) is True, f"{label}: {field} is not certified")
    deviations = payload.get("known_deviations")
    require(isinstance(deviations, list), f"{label}: missing known deviations")
    require(
        "processing_rng_seed_fixed_to_42_while_original_sketch_default_seed_is_unpinned"
        in deviations,
        f"{label}: deterministic RNG seed deviation is not disclosed",
    )
    if metal:
        require(
            "metal_cdf97_fp32_matrix_storage_differs_from_cpu_float64_reference"
            in deviations,
            f"{label}: fp32 matrix-storage deviation is not disclosed",
        )


def validate_benchmark(
    payload: dict[str, Any],
    *,
    label: str,
    schema: str,
    expected_presets: int,
    require_performance: bool,
    minimum_fps: float,
) -> None:
    require(payload.get("schema") == schema, f"{label}: wrong schema")
    metal = schema == "glic-original-realtime-metal-benchmark-v1"
    validate_strict_metadata(payload, label=label, metal=metal)
    require(int(payload.get("width", 0)) >= 960, f"{label}: width below 960")
    require(int(payload.get("height", 0)) >= 540, f"{label}: height below 540")
    require(int(payload.get("frames", 0)) >= 120, f"{label}: fewer than 120 measured frames")
    require(int(payload.get("warmup_frames", 0)) >= 10, f"{label}: fewer than 10 warm-up frames")
    require(
        finite_number(payload.get("required_fps")) is not None
        and float(payload["required_fps"]) >= minimum_fps,
        f"{label}: required_fps below {minimum_fps:g}",
    )
    require(
        payload.get("certification_evidence_passed") is True,
        f"{label}: certification evidence gate failed",
    )
    require(
        int(payload.get("supported_presets", -1)) == expected_presets,
        f"{label}: expected {expected_presets} supported presets",
    )
    require(
        valid_fnv1a64(payload.get("input_decoded_color_fnv1a64")),
        f"{label}: invalid input pixel hash",
    )
    results = payload.get("results")
    require(isinstance(results, list), f"{label}: results is not a list")
    require(
        len(results) == expected_presets,
        f"{label}: expected {expected_presets} results, found {len(results)}",
    )
    frame_budget = 1000.0 / minimum_fps
    threadgroup_dispatch_total = 0.0
    global_dispatch_total = 0.0
    for row in results:
        preset = row.get("preset") if isinstance(row, dict) else None
        require(isinstance(preset, str) and preset, f"{label}: invalid preset row")
        require(row.get("process_passed") is True, f"{label}/{preset}: processing failed")
        require(
            valid_fnv1a64(row.get("preset_config_fnv1a64")),
            f"{label}/{preset}: invalid preset configuration hash",
        )
        require(
            valid_fnv1a64(row.get("output_preview_file_fnv1a64")),
            f"{label}/{preset}: invalid output preview hash",
        )
        if require_performance:
            mean_ms = finite_number(row.get("mean_ms"))
            p95_ms = finite_number(row.get("p95_ms"))
            require(row.get("timing_passed") is True, f"{label}/{preset}: timing gate failed")
            require(row.get("performance_passed") is True, f"{label}/{preset}: performance gate failed")
            require(
                mean_ms is not None and mean_ms <= frame_budget,
                f"{label}/{preset}: mean exceeds frame budget",
            )
            require(
                p95_ms is not None and p95_ms <= frame_budget,
                f"{label}/{preset}: p95 exceeds frame budget",
            )
        if metal:
            gpu_dispatches = finite_number(row.get("mean_gpu_dispatches"))
            threadgroup_dispatches = finite_number(
                row.get("mean_threadgroup_pipeline_dispatches")
            )
            threadgroup_segments = finite_number(
                row.get("mean_threadgroup_pipeline_segments")
            )
            global_dispatches = finite_number(
                row.get("mean_global_pipeline_dispatches")
            )
            global_segments = finite_number(
                row.get("mean_global_pipeline_segments")
            )
            mean_segments = finite_number(row.get("mean_segments"))
            dependency_levels = finite_number(row.get("mean_dependency_levels"))
            barriers = finite_number(row.get("mean_buffer_barriers"))
            reuse = finite_number(row.get("static_schedule_reuse_ratio"))
            early_nodes = finite_number(row.get("mean_early_terminated_nodes"))
            early_samples = finite_number(row.get("mean_early_skipped_samples"))
            require(
                gpu_dispatches is not None and gpu_dispatches >= 1.0,
                f"{label}/{preset}: invalid GPU dispatch count",
            )
            require(
                threadgroup_dispatches is not None
                and threadgroup_dispatches >= 0.0
                and threadgroup_segments is not None
                and threadgroup_segments >= 0.0
                and global_dispatches is not None
                and global_dispatches >= 0.0
                and global_segments is not None
                and global_segments >= 0.0
                and mean_segments is not None
                and mean_segments >= 1.0
                and dependency_levels is not None
                and dependency_levels >= 1.0
                and early_nodes is not None
                and early_nodes >= 0.0
                and early_samples is not None
                and early_samples >= 0.0
                and valid_fnv1a64(row.get("last_segmentation_rng_state"))
                and valid_fnv1a64(row.get("last_segment_order_fnv1a64")),
                f"{label}/{preset}: invalid local/global pipeline evidence",
            )
            require(
                abs(
                    gpu_dispatches
                    - (threadgroup_dispatches + global_dispatches)
                )
                <= 0.01,
                f"{label}/{preset}: local/global dispatch totals do not match",
            )
            require(
                abs(mean_segments - (threadgroup_segments + global_segments))
                <= 0.005,
                f"{label}/{preset}: local/global segment totals do not match",
            )
            require(
                threadgroup_segments + 0.005 >= threadgroup_dispatches
                and global_segments + 0.005 >= global_dispatches,
                f"{label}/{preset}: a pipeline dispatch has no segment",
            )
            require(
                (threadgroup_dispatches <= 0.005)
                == (threadgroup_segments <= 0.005)
                and (global_dispatches <= 0.005)
                == (global_segments <= 0.005),
                f"{label}/{preset}: zero dispatch/segment evidence is inconsistent",
            )
            require(
                barriers is not None
                and abs(barriers - max(0.0, dependency_levels - 1.0)) <= 0.01,
                f"{label}/{preset}: plane-buffer barrier count is inconsistent",
            )
            require(
                row.get("pipeline_accounting_passed") is True,
                f"{label}/{preset}: runtime pipeline accounting did not pass",
            )
            require(
                reuse is not None and 0.0 <= reuse <= 1.0,
                f"{label}/{preset}: invalid fixed-schedule reuse ratio",
            )
            if row.get("min_block_size") == row.get("max_block_size"):
                require(
                    abs(reuse - 1.0) <= 1e-9,
                    f"{label}/{preset}: fixed schedule was not reused",
                )
            threadgroup_dispatch_total += threadgroup_dispatches
            global_dispatch_total += global_dispatches
    if metal:
        require(
            threadgroup_dispatch_total > 0.0,
            f"{label}: threadgroup pipeline was never exercised",
        )
        require(
            global_dispatch_total > 0.0,
            f"{label}: global pipeline was never exercised",
        )


def validate_comparison(
    payload: dict[str, Any], *, expected_presets: int
) -> None:
    require(
        payload.get("schema") == "glic-original-metal-reference-comparison-v1",
        "reference comparison: wrong schema",
    )
    require(
        payload.get("provenance_match_verified") is True,
        "reference comparison: provenance was not verified",
    )
    require(
        payload.get("segmentation_trace_match_verified") is True,
        "reference comparison: CPU/Metal segmentation trace was not verified",
    )
    require(
        int(payload.get("preset_count", -1)) == expected_presets,
        "reference comparison: wrong preset count",
    )
    require(
        int(payload.get("style_passed_count", -1)) == expected_presets,
        "reference comparison: not every preset passed morphology",
    )
    require(
        payload.get("original_style_match_passed") is True,
        "reference comparison: morphology gate failed",
    )
    numeric_passed = int(payload.get("passed_count", -1))
    require(
        numeric_passed >= max(0, expected_presets - 3),
        "reference comparison: numeric match regressed below the three known fp32 exceptions",
    )
    failures = payload.get("failed_presets")
    require(isinstance(failures, list), "reference comparison: missing numeric failures")
    require(
        numeric_passed + len(failures) == expected_presets,
        "reference comparison: numeric pass/failure counts are inconsistent",
    )
    known_exceptions = {
        "colour_mess2",
        "colour_waves_sharp",
        "colour_waves_sharp2",
    }
    require(
        set(failures).issubset(known_exceptions),
        "reference comparison: unexpected numeric failure preset",
    )


def validate_effect_difference(payload: dict[str, Any]) -> None:
    require(
        payload.get("schema") == "glic-effect-difference-v1",
        "effect difference: wrong schema",
    )
    require(
        int(payload.get("analyzed_frames", 0)) >= 120,
        "effect difference: fewer than 120 aligned frames",
    )
    candidates = payload.get("candidates")
    require(
        isinstance(candidates, list) and len(candidates) == 1,
        "effect difference: expected exactly one candidate",
    )
    candidate = candidates[0]
    require(isinstance(candidate, dict), "effect difference: invalid candidate")
    require(
        candidate.get("verdict") in {"VISIBLE", "STRONG"},
        "effect difference: glitch is not visibly distinct from passthrough",
    )
    require(
        candidate.get("meaningful_glitch_passed") is True,
        "effect difference: meaningful glitch gate failed",
    )


def validate_video(payload: dict[str, Any], *, minimum_fps: float) -> None:
    require(payload.get("schema") == "glic-video-process-v1", "video: wrong schema")
    require(int(payload.get("target_width", 0)) >= 960, "video: width below 960")
    require(int(payload.get("target_height", 0)) >= 540, "video: height below 540")
    require(
        finite_number(payload.get("target_fps")) is not None
        and float(payload["target_fps"]) >= minimum_fps,
        "video: target fps below requirement",
    )
    require(
        finite_number(payload.get("output_fps")) is not None
        and float(payload["output_fps"]) >= minimum_fps,
        "video: encoded fps below requirement",
    )
    for field in (
        "end_to_end_average_30fps_passed",
        "filter_stream_realtime_30fps_passed",
        "filter_kernel_realtime_30fps_passed",
    ):
        require(payload.get(field) is True, f"video: {field} failed")

    stats = payload.get("filter")
    require(isinstance(stats, dict), "video: missing filter statistics")
    validate_strict_metadata(stats, label="video/filter", metal=True)
    require(stats.get("processing_mode") == "original_visual", "video: wrong processing mode")
    frames = int(stats.get("frames", 0))
    expected_frames = int(stats.get("expected_frames", 0))
    initial_capacity = int(stats.get("initial_timing_capacity", 0))
    require(frames >= 120, "video: fewer than 120 frames processed")
    require(int(stats.get("measured_frames", 0)) >= 120, "video: fewer than 120 measured frames")
    require(expected_frames >= frames, "video: expected frame capacity is too small")
    require(initial_capacity >= expected_frames, "video: timing arrays were not preallocated")
    require(
        int(stats.get("timing_capacity_growth_events", -1)) == 0,
        "video: timing arrays grew during streaming",
    )
    counters = (
        ("command_buffers_per_frame", 1.0),
        ("cpu_gpu_waits_per_frame", 1.0),
        ("mapped_buffer_copies_per_frame", 0.0),
    )
    for field, expected in counters:
        value = finite_number(stats.get(field))
        require(
            value is not None and abs(value - expected) <= 1e-9,
            f"video: {field} is not {expected:g}",
        )
    gpu_dispatches = finite_number(stats.get("mean_gpu_dispatches"))
    threadgroup_dispatches = finite_number(
        stats.get("mean_threadgroup_pipeline_dispatches")
    )
    threadgroup_segments = finite_number(
        stats.get("mean_threadgroup_pipeline_segments")
    )
    global_dispatches = finite_number(stats.get("mean_global_pipeline_dispatches"))
    global_segments = finite_number(stats.get("mean_global_pipeline_segments"))
    dependency_levels = finite_number(stats.get("mean_dependency_levels"))
    channel_segments = stats.get("mean_segments_per_channel")
    mean_segments = None
    if isinstance(channel_segments, list) and len(channel_segments) == 3:
        parsed_segments = [finite_number(value) for value in channel_segments]
        if all(value is not None for value in parsed_segments):
            mean_segments = sum(float(value) for value in parsed_segments)
    barriers = finite_number(stats.get("buffer_barriers_per_frame"))
    reuse = finite_number(stats.get("static_schedule_reuse_ratio"))
    early_nodes = finite_number(stats.get("mean_early_terminated_nodes"))
    early_samples = finite_number(stats.get("mean_early_skipped_samples"))
    require(
        gpu_dispatches is not None
        and gpu_dispatches >= 1.0
        and threadgroup_dispatches is not None
        and threadgroup_dispatches >= 0.0
        and threadgroup_segments is not None
        and threadgroup_segments >= 0.0
        and global_dispatches is not None
        and global_dispatches >= 0.0
        and global_segments is not None
        and global_segments >= 0.0
        and dependency_levels is not None
        and dependency_levels >= 1.0
        and mean_segments is not None
        and mean_segments >= 1.0
        and early_nodes is not None
        and early_nodes >= 0.0
        and early_samples is not None
        and early_samples >= 0.0
        and valid_fnv1a64(stats.get("last_segmentation_rng_state"))
        and valid_fnv1a64(stats.get("last_segment_order_fnv1a64")),
        "video: invalid local/global pipeline evidence",
    )
    require(
        abs(gpu_dispatches - (threadgroup_dispatches + global_dispatches))
        <= 0.01,
        "video: local/global dispatch totals do not match",
    )
    require(
        abs(mean_segments - (threadgroup_segments + global_segments)) <= 0.005,
        "video: local/global segment totals do not match",
    )
    require(
        threadgroup_segments + 0.005 >= threadgroup_dispatches
        and global_segments + 0.005 >= global_dispatches,
        "video: a pipeline dispatch has no segment",
    )
    require(
        (threadgroup_dispatches <= 0.005) == (threadgroup_segments <= 0.005)
        and (global_dispatches <= 0.005) == (global_segments <= 0.005),
        "video: zero dispatch/segment evidence is inconsistent",
    )
    require(
        barriers is not None
        and abs(barriers - max(0.0, dependency_levels - 1.0)) <= 0.01,
        "video: plane-buffer barrier count is inconsistent",
    )
    require(
        stats.get("pipeline_accounting_passed") is True,
        "video: runtime pipeline accounting did not pass",
    )
    require(
        reuse is not None and 0.0 <= reuse <= 1.0,
        "video: invalid fixed-schedule reuse ratio",
    )


def validate_qa(payload: dict[str, Any]) -> None:
    summary = payload.get("summary")
    require(isinstance(summary, dict), "video QA: missing summary")
    require(int(summary.get("total", -1)) == 1, "video QA: expected one file")
    require(int(summary.get("pass", -1)) == 1, "video QA: file did not pass")
    require(int(summary.get("warn", -1)) == 0, "video QA: warning is not certification")
    require(int(summary.get("fail", -1)) == 0, "video QA: failure detected")


def selftest() -> None:
    strict_metal = {
        "backend": METAL_BACKEND,
        "execution_mode": METAL_EXECUTION_MODE,
        "fidelity_lane": METAL_FIDELITY_LANE,
        "fidelity_claim": FIDELITY_CLAIM,
        "processing_pixel_exact": False,
        "cdf97_precision": METAL_CDF97_PRECISION,
        "processing_rounding_compatible": True,
        "processing_raw_plane_pack_compatible": True,
        "processing_rng_and_cross_channel_order_compatible": True,
        "known_deviations": [
            "processing_rng_seed_fixed_to_42_while_original_sketch_default_seed_is_unpinned",
            "metal_cdf97_fp32_matrix_storage_differs_from_cpu_float64_reference",
        ],
    }
    row = {
        "preset": "test",
        "process_passed": True,
        "timing_passed": True,
        "performance_passed": True,
        "mean_ms": 10.0,
        "p95_ms": 12.0,
        "preset_config_fnv1a64": "1111111111111111",
        "output_preview_file_fnv1a64": "2222222222222222",
        "mean_gpu_dispatches": 2.0,
        "mean_threadgroup_pipeline_dispatches": 1.0,
        "mean_threadgroup_pipeline_segments": 1.0,
        "mean_global_pipeline_dispatches": 1.0,
        "mean_global_pipeline_segments": 1.0,
        "mean_segments": 2.0,
        "mean_dependency_levels": 1.0,
        "mean_buffer_barriers": 0.0,
        "pipeline_accounting_passed": True,
        "mean_early_terminated_nodes": 1.0,
        "mean_early_skipped_samples": 4.0,
        "last_segmentation_rng_state": "0123456789abcdef",
        "last_segment_order_fnv1a64": "fedcba9876543210",
        "static_schedule_reuse_ratio": 1.0,
        "min_block_size": 4,
        "max_block_size": 4,
    }
    base = {
        **strict_metal,
        "width": 960,
        "height": 540,
        "frames": 120,
        "warmup_frames": 10,
        "required_fps": 30.0,
        "certification_evidence_passed": True,
        "supported_presets": 1,
        "input_decoded_color_fnv1a64": "0123456789abcdef",
        "results": [row],
    }
    validate_benchmark(
        {**base, "schema": "glic-original-realtime-metal-benchmark-v1"},
        label="selftest",
        schema="glic-original-realtime-metal-benchmark-v1",
        expected_presets=1,
        require_performance=True,
        minimum_fps=30.0,
    )
    comparison_fixture = {
        "schema": "glic-original-metal-reference-comparison-v1",
        "provenance_match_verified": True,
        "segmentation_trace_match_verified": True,
        "preset_count": 1,
        "passed_count": 1,
        "failed_presets": [],
        "style_passed_count": 1,
        "original_style_match_passed": True,
    }
    validate_comparison(comparison_fixture, expected_presets=1)
    validate_effect_difference(
        {
            "schema": "glic-effect-difference-v1",
            "analyzed_frames": 166,
            "candidates": [
                {
                    "verdict": "STRONG",
                    "meaningful_glitch_passed": True,
                }
            ],
        }
    )
    validate_video(
        {
            "schema": "glic-video-process-v1",
            "target_width": 960,
            "target_height": 540,
            "target_fps": 30.0,
            "output_fps": 30.0,
            "end_to_end_average_30fps_passed": True,
            "filter_stream_realtime_30fps_passed": True,
            "filter_kernel_realtime_30fps_passed": True,
            "filter": {
                **strict_metal,
                "processing_mode": "original_visual",
                "frames": 166,
                "measured_frames": 156,
                "expected_frames": 168,
                "initial_timing_capacity": 168,
                "timing_capacity_growth_events": 0,
                "command_buffers_per_frame": 1.0,
                "cpu_gpu_waits_per_frame": 1.0,
                "mapped_buffer_copies_per_frame": 0.0,
                "mean_gpu_dispatches": 374.0,
                "mean_threadgroup_pipeline_dispatches": 374.0,
                "mean_threadgroup_pipeline_segments": 97200.0,
                "mean_global_pipeline_dispatches": 0.0,
                "mean_global_pipeline_segments": 0.0,
                "mean_dependency_levels": 374.0,
                "mean_segments_per_channel": [32400.0, 32400.0, 32400.0],
                "buffer_barriers_per_frame": 373.0,
                "static_schedule_reuse_ratio": 1.0,
                "pipeline_accounting_passed": True,
                "mean_early_terminated_nodes": 0.0,
                "mean_early_skipped_samples": 0.0,
                "last_segmentation_rng_state": "0123456789abcdef",
                "last_segment_order_fnv1a64": "fedcba9876543210",
            },
        },
        minimum_fps=30.0,
    )
    validate_qa({"summary": {"total": 1, "pass": 1, "warn": 0, "fail": 0}})
    def expect_failure(callback: Any, message: str) -> None:
        try:
            callback()
        except ValidationError:
            return
        raise ValidationError(message)

    broken = {
        **base,
        "schema": "glic-original-realtime-metal-benchmark-v1",
        "results": [{**row, "performance_passed": False}],
    }
    expect_failure(
        lambda: validate_benchmark(
            broken,
            label="negative selftest",
            schema="glic-original-realtime-metal-benchmark-v1",
            expected_presets=1,
            require_performance=True,
            minimum_fps=30.0,
        ),
        "performance negative selftest did not fail",
    )
    broken_barrier = {
        **base,
        "schema": "glic-original-realtime-metal-benchmark-v1",
        "results": [{**row, "mean_buffer_barriers": 1.0}],
    }
    expect_failure(
        lambda: validate_benchmark(
            broken_barrier,
            label="barrier negative selftest",
            schema="glic-original-realtime-metal-benchmark-v1",
            expected_presets=1,
            require_performance=True,
            minimum_fps=30.0,
        ),
        "frontier barrier negative selftest did not fail",
    )
    expect_failure(
        lambda: validate_comparison(
            {**comparison_fixture, "segmentation_trace_match_verified": False},
            expected_presets=1,
        ),
        "segmentation trace negative selftest did not fail",
    )
    legacy = {
        **base,
        "schema": "glic-original-realtime-metal-benchmark-v1",
        "cdf97_precision": "float32-safe-math",
    }
    for field in PROCESSING_COMPATIBILITY_FIELDS:
        legacy.pop(field, None)
    expect_failure(
        lambda: validate_benchmark(
            legacy,
            label="legacy metadata negative selftest",
            schema="glic-original-realtime-metal-benchmark-v1",
            expected_presets=1,
            require_performance=True,
            minimum_fps=30.0,
        ),
        "legacy metadata negative selftest did not fail",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpu-benchmark", type=Path)
    parser.add_argument("--metal-normal", type=Path)
    parser.add_argument("--comparison", type=Path)
    parser.add_argument("--metal-noise", type=Path)
    parser.add_argument("--video-report", type=Path)
    parser.add_argument("--effect-difference", type=Path)
    parser.add_argument("--qa-report", type=Path)
    parser.add_argument("--expected-presets", type=int, default=37)
    parser.add_argument("--minimum-fps", type=float, default=30.0)
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.selftest:
        selftest()
        print("PASS original Metal validation adapter selftest")
        return 0
    require(
        any(
            (
                args.cpu_benchmark,
                args.metal_normal,
                args.comparison,
                args.metal_noise,
                args.video_report,
                args.effect_difference,
                args.qa_report,
            )
        ),
        "at least one report argument is required",
    )
    if args.cpu_benchmark:
        validate_benchmark(
            load_json(args.cpu_benchmark),
            label="CPU reference",
            schema="glic-original-realtime-cpu-benchmark-v1",
            expected_presets=args.expected_presets,
            require_performance=False,
            minimum_fps=args.minimum_fps,
        )
    for path, label in (
        (args.metal_normal, "Metal normal"),
        (args.metal_noise, "Metal noise"),
    ):
        if path:
            validate_benchmark(
                load_json(path),
                label=label,
                schema="glic-original-realtime-metal-benchmark-v1",
                expected_presets=args.expected_presets,
                require_performance=True,
                minimum_fps=args.minimum_fps,
            )
    if args.comparison:
        validate_comparison(
            load_json(args.comparison), expected_presets=args.expected_presets
        )
    if args.video_report:
        validate_video(load_json(args.video_report), minimum_fps=args.minimum_fps)
    if args.effect_difference:
        validate_effect_difference(load_json(args.effect_difference))
    if args.qa_report:
        validate_qa(load_json(args.qa_report))
    print("PASS original Metal validation reports")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, ValidationError) as error:
        print(f"error: {error}")
        raise SystemExit(1)
