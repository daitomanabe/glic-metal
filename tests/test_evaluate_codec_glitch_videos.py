#!/usr/bin/env python3
"""Integration and policy checks for codec-glitch video ranking."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import evaluate_codec_glitch_videos as evaluator  # noqa: E402


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True)


def make_video_fixtures(directory: Path, ffmpeg: str) -> tuple[Path, Path, Path]:
    control = directory / "control.mp4"
    color = directory / "color.mp4"
    blocks = directory / "blocks.mp4"
    run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=24:duration=1",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(control),
        ]
    )
    run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(control),
            "-vf",
            "hue=h=70:s=1.7",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(color),
        ]
    )
    run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(control),
            "-vf",
            "scale=40:22:flags=area,scale=160:90:flags=neighbor",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(blocks),
        ]
    )
    return control, color, blocks


def write_report(path: Path, effect: str, *, fallback: int = 0) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "glic-video-process-v1",
                "processing_mode": "codec_glitch",
                "codec_effect": effect,
                "codec_amount": 0.6,
                "codec_rate": 0.3,
                "codec_feedback": 0.5,
                "output_fps": 24.0,
                "processed_frames": 24,
                "output_frame_count": 24,
                "frame_count_preserved": True,
                "end_to_end_observed_fps": 24.0,
                "end_to_end_average_20fps_passed": True,
                "codec_realtime_20fps_passed": True,
                "filter_stream_realtime_20fps_passed": True,
                "codec_latency_p95_milliseconds": 35.0,
                "codec_fallback_frames": fallback,
                "codec_intentional_repeat_frames": 0,
                "codec_processing_errors": 0,
                "codec_watchdog_recoveries": 0,
                "codec_reliability_passed": fallback == 0,
                "codec_hardware_encoder": True,
                "codec_hardware_decoder": True,
                "filter": {
                    "frames": 24,
                    "submitted_frames": 24,
                    "emitted_frames": 24,
                    "processing_fps": 24.0,
                    "realtime_20fps_passed": True,
                    "fallback_frames": fallback,
                    "intentional_repeat_frames": 0,
                    "codec_errors": 0,
                    "watchdog_recoveries": 0,
                    "backpressure_drops": 0,
                    "poll_queue_drops": 0,
                    "reliability_passed": fallback == 0,
                    "hardware_encoder": True,
                    "hardware_decoder": True,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    assert ffmpeg is not None
    assert ffprobe is not None
    assert evaluator.run_selftest() == 0

    with tempfile.TemporaryDirectory(prefix="glic-codec-evaluator-test-") as raw:
        directory = Path(raw)
        control, color, blocks = make_video_fixtures(directory, ffmpeg)
        color_report = directory / "color.json"
        blocks_report = directory / "blocks.json"
        write_report(color_report, "chroma_codec_echo", fallback=0)
        write_report(blocks_report, "resolution_hop", fallback=2)
        output_json = directory / "ranking.json"
        output_md = directory / "ranking.md"
        args = evaluator.parse_args(
            [
                "--control",
                str(control),
                "--candidate",
                str(color),
                "--candidate",
                str(blocks),
                "--report",
                str(color_report),
                "--report",
                str(blocks_report),
                "--label",
                "color",
                "--label",
                "blocks",
                "--output-json",
                str(output_json),
                "--output-md",
                str(output_md),
                "--sample-fps",
                "6",
                "--max-frames",
                "8",
                "--analysis-width",
                "96",
                "--analysis-height",
                "54",
                "--required-width",
                "160",
                "--required-height",
                "90",
                "--minimum-frames",
                "20",
                "--ffmpeg",
                ffmpeg,
                "--ffprobe",
                ffprobe,
            ]
        )
        input_hashes = {
            path: evaluator.sha256_file(path) for path in (control, color, blocks)
        }
        payload = evaluator.evaluate_candidates(args)
        assert input_hashes == {
            path: evaluator.sha256_file(path) for path in (control, color, blocks)
        }
        assert payload["schema"] == evaluator.SCHEMA
        assert payload["summary"]["candidate_count"] == 2
        assert payload["summary"]["eligible_count"] == 1
        assert payload["summary"]["pairwise_distance_count"] == 1
        assert payload["pairwise_distances"][0]["distance"] > 0.01
        by_label = {record["label"]: record for record in payload["ranking"]}
        assert by_label["color"]["metrics"]["changed_ratio"] > 0.1
        assert by_label["blocks"]["metrics"]["edge_difference"] > 0.01
        assert (
            by_label["blocks"]["performance"]["reliability_penalty"]
            > by_label["color"]["performance"]["reliability_penalty"]
        )
        assert not by_label["blocks"]["performance"]["hard_gate_passed"]
        assert "non-intentional fallback frames=2" in by_label["blocks"][
            "performance"
        ]["gate_reasons"]
        markdown = evaluator.render_markdown(payload)
        assert "Codec glitch video ranking" in markdown
        assert "resolution_hop" in markdown
        evaluator.atomic_write_text(output_json, json.dumps(payload) + "\n")
        evaluator.atomic_write_text(output_md, markdown)
        assert json.loads(output_json.read_text())["summary"]["eligible_count"] == 1
        assert output_md.read_text().endswith("\n")

        failed_report = json.loads(color_report.read_text())
        failed_report["end_to_end_average_20fps_passed"] = False
        gate = evaluator.evaluate_performance_gate(
            failed_report, {"fps": 24.0}, 20.0
        )
        assert not gate["hard_gate_passed"]
        assert "end_to_end_average_20fps_passed=false" in gate["gate_reasons"]

    print("PASS codec glitch video evaluator")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
