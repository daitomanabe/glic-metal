#!/usr/bin/env python3
"""Focused stream and fail-closed tests for glic_original_visual_filter."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", type=Path, required=True)
    parser.add_argument("--presets", type=Path, required=True)
    return parser.parse_args()


def fixture(width: int, height: int, frames: int) -> bytes:
    output = bytearray()
    for frame in range(frames):
        for y in range(height):
            for x in range(width):
                output.extend(
                    (
                        (x * 31 + y * 7 + frame * 13) & 0xFF,
                        (x * 11 + y * 23 + frame * 17) & 0xFF,
                        (x * 5 + y * 37 + frame * 19) & 0xFF,
                        255,
                    )
                )
    return bytes(output)


def main() -> int:
    args = parse_args()
    filter_path = args.filter.resolve()
    presets = args.presets.resolve()
    width = 4
    height = 4
    frames = 3
    source = fixture(width, height, frames)

    with tempfile.TemporaryDirectory(prefix="glic-original-filter-test-") as text:
        stats_path = Path(text) / "stats.json"
        command = [
            str(filter_path),
            "--width",
            str(width),
            "--height",
            str(height),
            "--target-fps",
            "30",
            "--expected-frames",
            str(frames),
            "--preset",
            "default",
            "--presets-dir",
            str(presets),
            "--stats-json",
            str(stats_path),
        ]
        result = subprocess.run(command, input=source, capture_output=True)
        if result.returncode != 0:
            raise AssertionError(result.stderr.decode(errors="replace"))
        if len(result.stdout) != len(source):
            raise AssertionError("filter did not emit one complete frame per input frame")

        stats = json.loads(stats_path.read_text())
        assert stats["schema"] == "glic-original-visual-filter-v1"
        assert stats["processing_mode"] == "original_visual"
        assert stats["preset_semantics"] == "original"
        assert stats["backend"] == "cpu-original-visual"
        assert stats["unsupported_policy"] == "fail-closed"
        assert stats["processing_pixel_exact"] is False
        assert stats["frames"] == frames
        assert stats["expected_frames"] == frames
        assert stats["initial_timing_capacity"] == frames
        assert stats["timing_capacity_growth_events"] == 0
        assert stats["warmup_frames"] == frames
        assert stats["measured_frames"] == 0
        assert stats["kernel_timing_scope"] == "post-warmup-lane-process-call-only"
        assert (
            stats["stream_wall_timing_scope"]
            == "post-warmup-pre-read-through-completed-write"
        )
        assert stats["all_frames_mean_process_ms"] > 0
        assert stats["all_frames_stream_wall_mean_ms"] > 0
        assert stats["mean_process_ms"] == 0
        assert stats["p95_process_ms"] == 0
        assert stats["stream_wall_mean_ms"] == 0
        assert stats["stream_observed_fps"] == 0
        assert stats["realtime_policy"]["minimum_measured_frames"] == 120
        assert stats["kernel_realtime_30fps_passed"] is False
        assert stats["realtime_30fps_passed"] is False

        if sys.platform == "darwin":
            metal_stats_path = Path(text) / "metal-stats.json"
            metal_command = command[:-2] + [
                "--backend",
                "metal",
                "--stats-json",
                str(metal_stats_path),
            ]
            metal_result = subprocess.run(
                metal_command, input=source, capture_output=True
            )
            if metal_result.returncode != 0:
                raise AssertionError(
                    metal_result.stderr.decode(errors="replace")
                )
            if len(metal_result.stdout) != len(source):
                raise AssertionError("Metal filter did not emit complete frames")
            metal_stats = json.loads(metal_stats_path.read_text())
            assert metal_stats["backend"] == "metal-original-visual"
            assert (
                metal_stats["execution_mode"]
                == "hybrid_cpu_colorspace_segmentation_gpu_reconstruction"
            )
            assert metal_stats["cdf97_precision"] == "float32-safe-math"
            assert metal_stats["mean_gpu_dispatches"] >= 1
            assert metal_stats["command_buffers_per_frame"] == 1
            assert metal_stats["cpu_gpu_waits_per_frame"] == 1
            assert metal_stats["mapped_buffer_copies_per_frame"] == 0
            assert metal_stats["all_frames_mean_process_ms"] > 0

        checked = subprocess.run(command[:-2] + ["--check"], capture_output=True)
        if checked.returncode != 0 or b"supported original_visual preset" not in checked.stderr:
            raise AssertionError("supported preset preflight failed")

        unsupported = subprocess.run(
            [
                str(filter_path),
                "--width",
                str(width),
                "--height",
                str(height),
                "--preset",
                "0ddangl3",
                "--presets-dir",
                str(presets),
                "--check",
            ],
            input=b"",
            capture_output=True,
        )
        if unsupported.returncode != 4:
            raise AssertionError(
                f"unsupported preset returned {unsupported.returncode}, expected 4"
            )
        message = unsupported.stderr.decode(errors="replace")
        if "Unsupported original_visual preset" not in message or "wavelet" not in message:
            raise AssertionError(f"missing fail-closed reason: {message}")

        partial = subprocess.run(command, input=source[:-1], capture_output=True)
        if partial.returncode != 5 or b"Incomplete BGRA frame" not in partial.stderr:
            raise AssertionError("partial stream did not fail closed")

    print("PASS original_visual dedicated stream filter")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
