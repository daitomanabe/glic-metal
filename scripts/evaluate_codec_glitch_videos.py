#!/usr/bin/env python3
"""Headless technical ranking for stateful codec-glitch video candidates.

The evaluator samples a dry/control video and multiple processed videos at the
same cadence through FFmpeg.  It measures visible dry/wet change, temporal
behaviour, codec reliability and pairwise effect morphology.  The 20 fps
requirement is a fail-closed eligibility gate; it is never traded against a
strong visual score.

Only the JSON and Markdown paths passed on the command line are written.  Input
videos and their process reports are opened read-only.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, NamedTuple, Sequence

import numpy as np


SCHEMA = "glic-codec-glitch-video-ranking-v1"
ANALYSIS_WIDTH = 256
ANALYSIS_HEIGHT = 144
DEFAULT_SAMPLE_FPS = 6.0
DEFAULT_MAX_FRAMES = 90
DEFAULT_CHANGED_THRESHOLD = 12.0 / 255.0
DEFAULT_REQUIRED_WIDTH = 960
DEFAULT_REQUIRED_HEIGHT = 540
DEFAULT_MINIMUM_FRAMES = 120
FINGERPRINT_SCHEMA = {
    "residual_luma_grid": 64,
    "residual_edge_grid": 64,
    "signed_color_grid": 48,
    "residual_histogram": 16,
    "temporal_quantiles": 5,
    "summary": 6,
}
FINGERPRINT_WEIGHTS = {
    "residual_luma_grid": 0.30,
    "residual_edge_grid": 0.25,
    "signed_color_grid": 0.20,
    "residual_histogram": 0.15,
    "temporal_quantiles": 0.05,
    "summary": 0.05,
}


class EvaluationError(RuntimeError):
    """Raised when the control or evaluator configuration is invalid."""


class CandidateSpec(NamedTuple):
    video: Path
    report: Path
    label: str


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rank codec-glitch MP4 candidates against a control video."
    )
    parser.add_argument("--control", type=Path)
    parser.add_argument(
        "--candidate",
        action="append",
        type=Path,
        default=[],
        help="Candidate MP4/MOV. Repeat for every candidate.",
    )
    parser.add_argument(
        "--report",
        action="append",
        type=Path,
        default=[],
        help="Candidate JSON process report in the same order as --candidate.",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="Optional candidate label in the same order as --candidate.",
    )
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-md", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--sample-fps", type=float, default=DEFAULT_SAMPLE_FPS)
    parser.add_argument("--max-frames", type=int, default=DEFAULT_MAX_FRAMES)
    parser.add_argument("--analysis-width", type=int, default=ANALYSIS_WIDTH)
    parser.add_argument("--analysis-height", type=int, default=ANALYSIS_HEIGHT)
    parser.add_argument("--min-fps", type=float, default=20.0)
    parser.add_argument("--required-width", type=int, default=DEFAULT_REQUIRED_WIDTH)
    parser.add_argument("--required-height", type=int, default=DEFAULT_REQUIRED_HEIGHT)
    parser.add_argument("--minimum-frames", type=int, default=DEFAULT_MINIMUM_FRAMES)
    parser.add_argument(
        "--changed-threshold",
        type=float,
        default=DEFAULT_CHANGED_THRESHOLD,
        help="Normalized maximum-channel dry/wet difference threshold.",
    )
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args(argv)


def executable_path(value: str) -> str:
    expanded = Path(value).expanduser()
    if expanded.parent != Path(".") or expanded.is_absolute():
        if not expanded.is_file():
            raise EvaluationError(f"executable was not found: {expanded}")
        return str(expanded.resolve())
    resolved = shutil.which(value)
    if resolved is None:
        raise EvaluationError(f"executable was not found on PATH: {value}")
    return resolved


def finite_float(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool) or value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def finite_int(value: Any, default: int = 0) -> int:
    result = finite_float(value)
    if result is None:
        return default
    return max(0, int(result))


def parse_frame_rate(value: Any) -> float:
    if not isinstance(value, str) or not value or value == "N/A":
        return 0.0
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        top = finite_float(numerator, 0.0) or 0.0
        bottom = finite_float(denominator, 0.0) or 0.0
        return top / bottom if bottom > 0.0 else 0.0
    return finite_float(value, 0.0) or 0.0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def probe_video(ffprobe: str, path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise EvaluationError(f"video was not found: {path}")
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,duration:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        payload = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        raise EvaluationError(f"ffprobe failed for {path}: {detail.strip()}") from error
    streams = payload.get("streams")
    if not isinstance(streams, list) or not streams:
        raise EvaluationError(f"video has no decodable video stream: {path}")
    stream = streams[0]
    width = finite_int(stream.get("width"))
    height = finite_int(stream.get("height"))
    fps = parse_frame_rate(stream.get("avg_frame_rate"))
    if fps <= 0.0:
        fps = parse_frame_rate(stream.get("r_frame_rate"))
    duration = finite_float(stream.get("duration"))
    if duration is None:
        duration = finite_float(payload.get("format", {}).get("duration"), 0.0)
    if width <= 0 or height <= 0 or fps <= 0.0:
        raise EvaluationError(f"video stream metadata is incomplete: {path}")
    return {
        "width": width,
        "height": height,
        "fps": fps,
        "duration_seconds": duration or 0.0,
        "declared_frames": finite_int(stream.get("nb_frames")),
    }


def decode_sampled_frames(
    ffmpeg: str,
    path: Path,
    *,
    sample_fps: float,
    max_frames: int,
    width: int,
    height: int,
) -> np.ndarray:
    filter_graph = (
        f"fps={sample_fps:.8f},"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=area,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )
    command = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(path),
        "-an",
        "-sn",
        "-vf",
        filter_graph,
        "-frames:v",
        str(max_frames),
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "-",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True)
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", "replace").strip()
        raise EvaluationError(f"ffmpeg decode failed for {path}: {detail}") from error
    frame_bytes = width * height * 3
    if len(result.stdout) % frame_bytes != 0:
        raise EvaluationError(f"ffmpeg returned a partial RGB frame for {path}")
    count = len(result.stdout) // frame_bytes
    if count < 2:
        raise EvaluationError(f"fewer than two sampled frames decoded from {path}")
    return np.frombuffer(result.stdout, dtype=np.uint8).reshape(
        count, height, width, 3
    )


def _luma(frames: np.ndarray) -> np.ndarray:
    return (
        frames[..., 0] * np.float32(0.2126)
        + frames[..., 1] * np.float32(0.7152)
        + frames[..., 2] * np.float32(0.0722)
    )


def _chroma(frames: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    red, green, blue = frames[..., 0], frames[..., 1], frames[..., 2]
    cb = -0.114572 * red - 0.385428 * green + 0.5 * blue
    cr = 0.5 * red - 0.454153 * green - 0.045847 * blue
    return cb, cr


def _edge_magnitude(luma: np.ndarray) -> np.ndarray:
    gx = np.zeros_like(luma, dtype=np.float32)
    gy = np.zeros_like(luma, dtype=np.float32)
    gx[..., :, 1:-1] = (luma[..., :, 2:] - luma[..., :, :-2]) * 0.5
    gy[..., 1:-1, :] = (luma[..., 2:, :] - luma[..., :-2, :]) * 0.5
    return np.minimum(1.0, np.sqrt(gx * gx + gy * gy) / np.float32(0.70710678))


def _grid_mean(values: np.ndarray, rows: int, columns: int) -> np.ndarray:
    height, width = values.shape[:2]
    y_edges = np.linspace(0, height, rows + 1, dtype=np.int32)
    x_edges = np.linspace(0, width, columns + 1, dtype=np.int32)
    channels = () if values.ndim == 2 else (values.shape[2],)
    result = np.zeros((rows, columns) + channels, dtype=np.float32)
    for row in range(rows):
        for column in range(columns):
            cell = values[
                y_edges[row] : y_edges[row + 1],
                x_edges[column] : x_edges[column + 1],
            ]
            result[row, column] = cell.mean(axis=(0, 1))
    return result


def _rounded_list(values: np.ndarray) -> list[float]:
    return [round(float(value), 7) for value in values.reshape(-1)]


def analyze_frame_pairs(
    control_frames: np.ndarray,
    candidate_frames: np.ndarray,
    changed_threshold: float = DEFAULT_CHANGED_THRESHOLD,
) -> tuple[dict[str, float], dict[str, list[float]]]:
    pair_count = min(len(control_frames), len(candidate_frames))
    if pair_count < 2:
        raise EvaluationError("control/candidate overlap contains fewer than two frames")
    control = control_frames[:pair_count].astype(np.float32) / 255.0
    candidate = candidate_frames[:pair_count].astype(np.float32) / 255.0
    signed_rgb = candidate - control
    absolute_rgb = np.abs(signed_rgb)
    residual_magnitude = np.max(absolute_rgb, axis=-1)

    control_luma = _luma(control)
    candidate_luma = _luma(candidate)
    signed_luma = candidate_luma - control_luma
    absolute_luma = np.abs(signed_luma)
    control_cb, control_cr = _chroma(control)
    candidate_cb, candidate_cr = _chroma(candidate)
    chroma_difference = 0.5 * (
        np.abs(candidate_cb - control_cb) + np.abs(candidate_cr - control_cr)
    )

    control_edges = _edge_magnitude(control_luma)
    candidate_edges = _edge_magnitude(candidate_luma)
    edge_absolute_difference = np.abs(candidate_edges - control_edges)
    edge_mask_difference = np.not_equal(
        candidate_edges >= 0.10, control_edges >= 0.10
    )

    control_pair_delta = np.mean(
        np.abs(np.diff(control_luma, axis=0)), axis=(1, 2)
    )
    candidate_pair_delta = np.mean(
        np.abs(np.diff(candidate_luma, axis=0)), axis=(1, 2)
    )
    residual_pair_delta = np.mean(
        np.abs(np.diff(signed_luma, axis=0)), axis=(1, 2)
    )
    temporal_quantiles = np.quantile(
        residual_pair_delta, [0.0, 0.25, 0.5, 0.75, 1.0]
    ).astype(np.float32)

    mean_residual_luma = absolute_luma.mean(axis=0)
    mean_residual_edge = edge_absolute_difference.mean(axis=0)
    mean_signed_color = signed_rgb.mean(axis=0)
    histogram, _ = np.histogram(
        residual_magnitude, bins=16, range=(0.0, 1.0)
    )
    histogram = histogram.astype(np.float32)
    histogram /= max(1.0, float(histogram.sum()))

    metrics = {
        "paired_frames": float(pair_count),
        "mae_rgb": float(absolute_rgb.mean()),
        "mae_8bit": float(absolute_rgb.mean() * 255.0),
        "changed_ratio": float((residual_magnitude >= changed_threshold).mean()),
        "luminance_difference": float(absolute_luma.mean()),
        "chroma_difference": float(chroma_difference.mean()),
        "edge_difference": float(edge_absolute_difference.mean()),
        "edge_changed_ratio": float(edge_mask_difference.mean()),
        "control_temporal_activity": float(control_pair_delta.mean()),
        "candidate_temporal_activity": float(candidate_pair_delta.mean()),
        "temporal_activity_delta": float(
            np.mean(np.abs(candidate_pair_delta - control_pair_delta))
        ),
        "temporal_residual_activity": float(residual_pair_delta.mean()),
        "control_repeated_pair_ratio": float((control_pair_delta < 0.002).mean()),
        "candidate_repeated_pair_ratio": float(
            (candidate_pair_delta < 0.002).mean()
        ),
    }
    fingerprint = {
        "residual_luma_grid": _rounded_list(
            _grid_mean(mean_residual_luma, 8, 8)
        ),
        "residual_edge_grid": _rounded_list(
            _grid_mean(mean_residual_edge, 8, 8)
        ),
        "signed_color_grid": _rounded_list(
            (_grid_mean(mean_signed_color, 4, 4) + 1.0) * 0.5
        ),
        "residual_histogram": _rounded_list(histogram),
        "temporal_quantiles": _rounded_list(temporal_quantiles),
        "summary": [
            round(metrics["mae_rgb"], 7),
            round(metrics["changed_ratio"], 7),
            round(metrics["luminance_difference"], 7),
            round(metrics["chroma_difference"], 7),
            round(metrics["edge_difference"], 7),
            round(metrics["temporal_residual_activity"], 7),
        ],
    }
    return {key: round(value, 8) for key, value in metrics.items()}, fingerprint


def fingerprint_distance(
    left: dict[str, list[float]], right: dict[str, list[float]]
) -> float:
    distance = 0.0
    for name, expected_length in FINGERPRINT_SCHEMA.items():
        left_values = np.asarray(left.get(name, []), dtype=np.float64)
        right_values = np.asarray(right.get(name, []), dtype=np.float64)
        if left_values.size != expected_length or right_values.size != expected_length:
            raise EvaluationError(f"fingerprint group has invalid length: {name}")
        group_distance = float(np.sqrt(np.mean((left_values - right_values) ** 2)))
        distance += FINGERPRINT_WEIGHTS[name] * group_distance
    return round(distance, 8)


def _report_containers(report: dict[str, Any]) -> list[dict[str, Any]]:
    containers = [report]
    for name in ("filter", "statistics"):
        value = report.get(name)
        if isinstance(value, dict):
            containers.append(value)
    filter_report = report.get("filter")
    if isinstance(filter_report, dict):
        statistics = filter_report.get("statistics")
        if isinstance(statistics, dict):
            containers.append(statistics)
    return containers


def report_value(report: dict[str, Any], *names: str) -> Any:
    for container in _report_containers(report):
        for name in names:
            if name in container:
                return container[name]
    return None


def load_candidate_report(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise EvaluationError(f"candidate report was not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvaluationError(f"candidate report is invalid: {path}: {error}") from error
    if not isinstance(payload, dict):
        raise EvaluationError(f"candidate report root must be an object: {path}")
    processing_mode = report_value(payload, "processing_mode")
    if processing_mode != "codec_glitch":
        raise EvaluationError(
            f"candidate report is not codec_glitch mode: {path}: {processing_mode}"
        )
    return payload


def evaluate_performance_gate(
    report: dict[str, Any],
    video_probe: dict[str, Any],
    min_fps: float,
    required_width: int = DEFAULT_REQUIRED_WIDTH,
    required_height: int = DEFAULT_REQUIRED_HEIGHT,
    minimum_frames: int = DEFAULT_MINIMUM_FRAMES,
) -> dict[str, Any]:
    reasons: list[str] = []
    output_width = finite_int(video_probe.get("width"))
    output_height = finite_int(video_probe.get("height"))
    declared_frames = finite_int(video_probe.get("declared_frames"))
    if output_width < required_width or output_height < required_height:
        reasons.append(
            "decoded output resolution "
            f"{output_width}x{output_height} < {required_width}x{required_height}"
        )
    if declared_frames < minimum_frames:
        reasons.append(
            f"decoded output frames {declared_frames} < {minimum_frames}"
        )
    output_fps = float(video_probe.get("fps", 0.0))
    if output_fps + 1e-6 < min_fps:
        reasons.append(f"decoded output fps {output_fps:.3f} < {min_fps:.3f}")

    flag_names = (
        "end_to_end_average_20fps_passed",
        "codec_realtime_20fps_passed",
        "filter_stream_realtime_20fps_passed",
        "realtime_20fps_passed",
    )
    flags: dict[str, bool] = {}
    for name in flag_names:
        value = report_value(report, name)
        if not isinstance(value, bool):
            reasons.append(f"{name} is missing or not boolean")
            continue
        flags[name] = value
        if not value:
            reasons.append(f"{name}=false")

    observed_fps = finite_float(
        report_value(
            report,
            "end_to_end_observed_fps",
            "stream_observed_fps",
            "codec_engine_fps",
            "processing_fps",
        )
    )
    processing_fps = finite_float(
        report_value(report, "codec_engine_fps", "processing_fps")
    )
    if observed_fps is None:
        reasons.append("report contains no finite observed processing fps")
    elif observed_fps + 1e-6 < min_fps:
        reasons.append(f"observed processing fps {observed_fps:.3f} < {min_fps:.3f}")
    if processing_fps is None:
        reasons.append("report contains no finite codec engine fps")
    elif processing_fps + 1e-6 < min_fps:
        reasons.append(f"codec engine fps {processing_fps:.3f} < {min_fps:.3f}")

    latency_p95 = finite_float(
        report_value(
            report,
            "codec_latency_p95_milliseconds",
            "latency_p95_ms",
        )
    )
    maximum_latency = 1000.0 / min_fps
    if latency_p95 is None:
        reasons.append("report contains no finite codec p95 latency")
    elif latency_p95 > maximum_latency + 1e-6:
        reasons.append(
            f"codec p95 latency {latency_p95:.3f}ms > {maximum_latency:.3f}ms"
        )

    def required_count(label: str, *names: str) -> int:
        raw_value = report_value(report, *names)
        parsed = finite_float(raw_value)
        if parsed is None or parsed < 0 or not float(parsed).is_integer():
            reasons.append(f"{label} is missing or invalid")
            return 0
        return int(parsed)

    processed_frames = required_count("processed_frames", "processed_frames")
    output_frame_count = required_count("output_frame_count", "output_frame_count")
    frames = required_count("filter frames", "frames")
    submitted_frames = required_count("submitted_frames", "submitted_frames")
    emitted_frames = required_count("emitted_frames", "emitted_frames")
    frame_count_preserved = report_value(report, "frame_count_preserved")
    if frame_count_preserved is not True:
        reasons.append("frame_count_preserved is missing or false")
    for label, count in (
        ("processed_frames", processed_frames),
        ("output_frame_count", output_frame_count),
        ("filter frames", frames),
        ("submitted_frames", submitted_frames),
        ("emitted_frames", emitted_frames),
    ):
        if count != declared_frames:
            reasons.append(
                f"{label} {count} != decoded output frames {declared_frames}"
            )
    denominator = max(1, frames)
    fallback_frames = required_count(
        "fallback_frames", "codec_fallback_frames", "fallback_frames"
    )
    intentional_repeat_frames = required_count(
        "intentional_repeat_frames",
        "codec_intentional_repeat_frames",
        "intentional_repeat_frames",
    )
    codec_errors = required_count(
        "codec_errors", "codec_processing_errors", "codec_errors"
    )
    watchdog_recoveries = required_count(
        "watchdog_recoveries",
        "codec_watchdog_recoveries",
        "watchdog_recoveries",
    )
    backpressure_drops = required_count("backpressure_drops", "backpressure_drops")
    poll_queue_drops = required_count("poll_queue_drops", "poll_queue_drops")
    reliability_flag = report_value(
        report, "codec_reliability_passed", "reliability_passed"
    )
    if fallback_frames > 0:
        reasons.append(f"non-intentional fallback frames={fallback_frames}")
    if codec_errors > 0:
        reasons.append(f"codec processing errors={codec_errors}")
    if watchdog_recoveries > 0:
        reasons.append(f"watchdog recoveries={watchdog_recoveries}")
    if backpressure_drops > 0:
        reasons.append(f"backpressure drops={backpressure_drops}")
    if poll_queue_drops > 0:
        reasons.append(f"poll queue drops={poll_queue_drops}")
    if reliability_flag is not True:
        reasons.append("reliability_passed is missing or false")
    hardware_encoder = report_value(
        report, "codec_hardware_encoder", "hardware_encoder"
    )
    hardware_decoder = report_value(
        report, "codec_hardware_decoder", "hardware_decoder"
    )
    if hardware_encoder is not True:
        reasons.append("hardware_encoder is missing or false")
    if hardware_decoder is not True:
        reasons.append("hardware_decoder is missing or false")
    fallback_ratio = fallback_frames / denominator
    error_ratio = codec_errors / denominator
    recovery_ratio = watchdog_recoveries / denominator
    reliability_penalty = (
        0.55 * min(1.0, fallback_ratio / 0.10)
        + 0.30 * min(1.0, error_ratio / 0.05)
        + 0.15 * min(1.0, recovery_ratio / 0.05)
    )
    return {
        "hard_gate_passed": not reasons,
        "gate_reasons": reasons,
        "minimum_fps": round(min_fps, 6),
        "output_fps": round(output_fps, 6),
        "observed_processing_fps": None
        if observed_fps is None
        else round(observed_fps, 6),
        "codec_engine_fps": None
        if processing_fps is None
        else round(processing_fps, 6),
        "latency_p95_ms": None
        if latency_p95 is None
        else round(latency_p95, 6),
        "reported_gate_flags": flags,
        "required_resolution": [required_width, required_height],
        "minimum_frames": minimum_frames,
        "output_width": output_width,
        "output_height": output_height,
        "decoded_output_frames": declared_frames,
        "frame_count_preserved": frame_count_preserved is True,
        "processed_frames": processed_frames,
        "output_frame_count": output_frame_count,
        "submitted_frames": submitted_frames,
        "emitted_frames": emitted_frames,
        "reported_frames": frames,
        "fallback_frames": fallback_frames,
        "intentional_repeat_frames": intentional_repeat_frames,
        "codec_errors": codec_errors,
        "watchdog_recoveries": watchdog_recoveries,
        "backpressure_drops": backpressure_drops,
        "poll_queue_drops": poll_queue_drops,
        "fallback_ratio": round(fallback_ratio, 8),
        "codec_error_ratio": round(error_ratio, 8),
        "watchdog_recovery_ratio": round(recovery_ratio, 8),
        "reliability_penalty": round(reliability_penalty, 8),
        "hardware_encoder": hardware_encoder,
        "hardware_decoder": hardware_decoder,
    }


def score_visual_metrics(metrics: dict[str, float]) -> dict[str, float]:
    effect_presence = float(
        np.mean(
            [
                min(1.0, metrics["mae_rgb"] / 0.12),
                min(1.0, metrics["changed_ratio"] / 0.45),
                min(1.0, metrics["luminance_difference"] / 0.12),
                min(1.0, metrics["chroma_difference"] / 0.10),
                min(1.0, metrics["edge_difference"] / 0.12),
            ]
        )
    )
    temporal_presence = 0.65 * min(
        1.0, metrics["temporal_residual_activity"] / 0.08
    ) + 0.35 * min(1.0, metrics["temporal_activity_delta"] / 0.08)
    freeze_excess = max(
        0.0,
        metrics["candidate_repeated_pair_ratio"]
        - metrics["control_repeated_pair_ratio"],
    )
    return {
        "effect_presence": round(effect_presence, 8),
        "temporal_presence": round(temporal_presence, 8),
        "freeze_excess": round(freeze_excess, 8),
    }


def pairwise_distances(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for left_index, left in enumerate(records):
        if "fingerprint" not in left:
            continue
        for right in records[left_index + 1 :]:
            if "fingerprint" not in right:
                continue
            pairs.append(
                {
                    "left": left["label"],
                    "right": right["label"],
                    "distance": fingerprint_distance(
                        left["fingerprint"], right["fingerprint"]
                    ),
                }
            )
    pairs.sort(key=lambda item: (item["distance"], item["left"], item["right"]))
    return pairs


def rank_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible = [
        record
        for record in records
        if record.get("performance", {}).get("hard_gate_passed")
        and "fingerprint" in record
    ]
    failed = [record for record in records if record not in eligible]

    for record in records:
        visual_scores = record.get("visual_scores", {})
        performance = record.get("performance", {})
        presence = float(visual_scores.get("effect_presence", 0.0))
        temporal = float(visual_scores.get("temporal_presence", 0.0))
        freeze = float(visual_scores.get("freeze_excess", 1.0))
        penalty = float(performance.get("reliability_penalty", 1.0))
        base = 0.65 * presence + 0.35 * temporal - 0.50 * penalty - 0.15 * freeze
        record["scores"] = {
            "technical_base": round(max(0.0, min(1.0, base)), 8),
            "selection_diversity": None,
            "ranking_score": 0.0,
        }

    selected: list[dict[str, Any]] = []
    remaining = list(eligible)
    while remaining:
        best: dict[str, Any] | None = None
        best_key: tuple[float, float, str] | None = None
        for record in remaining:
            base = record["scores"]["technical_base"]
            if not selected:
                diversity = 0.5 if len(eligible) == 1 else 1.0
                score = base
            else:
                diversity = min(
                    fingerprint_distance(
                        record["fingerprint"], chosen["fingerprint"]
                    )
                    for chosen in selected
                )
                score = 0.65 * base + 0.35 * min(1.0, diversity / 0.35)
            key = (score, base, str(record["label"]))
            if best_key is None or key > best_key:
                best = record
                best_key = key
                record["scores"]["selection_diversity"] = round(diversity, 8)
                record["scores"]["ranking_score"] = round(score, 8)
        assert best is not None
        selected.append(best)
        remaining.remove(best)

    failed.sort(
        key=lambda record: (
            -float(record["scores"]["technical_base"]),
            str(record["label"]),
        )
    )
    ranking = selected + failed
    for index, record in enumerate(ranking, 1):
        record["rank"] = index
    return ranking


def discover_report(video: Path) -> Path:
    candidates = [video.with_suffix(video.suffix + ".json"), video.with_suffix(".json")]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def build_candidate_specs(args: argparse.Namespace) -> list[CandidateSpec]:
    if not args.candidate:
        raise EvaluationError("at least one --candidate is required")
    if args.report and len(args.report) != len(args.candidate):
        raise EvaluationError("--report count must match --candidate count")
    if args.label and len(args.label) != len(args.candidate):
        raise EvaluationError("--label count must match --candidate count")
    specs: list[CandidateSpec] = []
    seen_labels: set[str] = set()
    for index, raw_video in enumerate(args.candidate):
        video = raw_video.expanduser().resolve()
        report = (
            args.report[index].expanduser().resolve()
            if args.report
            else discover_report(video).resolve()
        )
        label = args.label[index] if args.label else video.stem
        if not label or "|" in label or label in seen_labels:
            raise EvaluationError(f"candidate label is invalid or duplicated: {label!r}")
        seen_labels.add(label)
        specs.append(CandidateSpec(video=video, report=report, label=label))
    return specs


def evaluate_candidates(args: argparse.Namespace) -> dict[str, Any]:
    if args.control is None:
        raise EvaluationError("--control is required")
    if args.output_json is None or args.output_md is None:
        raise EvaluationError("--output-json and --output-md are required")
    if not math.isfinite(args.sample_fps) or args.sample_fps <= 0.0:
        raise EvaluationError("--sample-fps must be finite and positive")
    if args.max_frames < 2:
        raise EvaluationError("--max-frames must be at least 2")
    if args.analysis_width < 16 or args.analysis_height < 16:
        raise EvaluationError("analysis dimensions must both be at least 16")
    if not math.isfinite(args.min_fps) or args.min_fps <= 0.0:
        raise EvaluationError("--min-fps must be finite and positive")
    if args.required_width <= 0 or args.required_height <= 0:
        raise EvaluationError("required output dimensions must be positive")
    if args.minimum_frames < 2:
        raise EvaluationError("--minimum-frames must be at least 2")
    if not 0.0 <= args.changed_threshold <= 1.0:
        raise EvaluationError("--changed-threshold must be in [0, 1]")

    ffmpeg = executable_path(args.ffmpeg)
    ffprobe = executable_path(args.ffprobe)
    control = args.control.expanduser().resolve()
    specs = build_candidate_specs(args)
    control_probe = probe_video(ffprobe, control)
    control_frames = decode_sampled_frames(
        ffmpeg,
        control,
        sample_fps=args.sample_fps,
        max_frames=args.max_frames,
        width=args.analysis_width,
        height=args.analysis_height,
    )

    records: list[dict[str, Any]] = []
    for spec in specs:
        record: dict[str, Any] = {
            "label": spec.label,
            "video": str(spec.video),
            "video_sha256": None,
            "report": str(spec.report),
            "report_sha256": None,
            "status": "ERROR",
            "errors": [],
        }
        try:
            record["video_sha256"] = sha256_file(spec.video)
            candidate_probe = probe_video(ffprobe, spec.video)
            candidate_frames = decode_sampled_frames(
                ffmpeg,
                spec.video,
                sample_fps=args.sample_fps,
                max_frames=args.max_frames,
                width=args.analysis_width,
                height=args.analysis_height,
            )
            metrics, fingerprint = analyze_frame_pairs(
                control_frames, candidate_frames, args.changed_threshold
            )
            report = load_candidate_report(spec.report)
            reported_output = report_value(report, "output")
            if isinstance(reported_output, str) and reported_output:
                report_output_path = Path(reported_output).expanduser().resolve()
                if report_output_path != spec.video:
                    raise EvaluationError(
                        "candidate/report output mismatch: "
                        f"{spec.video} != {report_output_path}"
                    )
            record["report_sha256"] = sha256_file(spec.report)
            performance = evaluate_performance_gate(
                report,
                candidate_probe,
                args.min_fps,
                args.required_width,
                args.required_height,
                args.minimum_frames,
            )
            record.update(
                {
                    "status": "ELIGIBLE"
                    if performance["hard_gate_passed"]
                    else "INELIGIBLE",
                    "effect": report_value(
                        report, "codec_effect", "effect_family", "preset"
                    ),
                    "controls": {
                        "amount": finite_float(
                            report_value(report, "codec_amount", "amount")
                        ),
                        "rate": finite_float(
                            report_value(report, "codec_rate", "rate")
                        ),
                        "feedback": finite_float(
                            report_value(report, "codec_feedback", "feedback")
                        ),
                    },
                    "probe": candidate_probe,
                    "metrics": metrics,
                    "fingerprint": fingerprint,
                    "visual_scores": score_visual_metrics(metrics),
                    "performance": performance,
                }
            )
        except (EvaluationError, OSError, ValueError) as error:
            record["errors"].append(str(error))
            record["performance"] = {
                "hard_gate_passed": False,
                "gate_reasons": [str(error)],
                "reliability_penalty": 1.0,
            }
        records.append(record)

    pairs = pairwise_distances(records)
    ranking = rank_records(records)
    eligible_count = sum(
        bool(record.get("performance", {}).get("hard_gate_passed"))
        for record in ranking
    )
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "purpose": "technical codec-glitch QA and diversity ranking",
            "minimum_fps_hard_gate": round(args.min_fps, 6),
            "maximum_p95_latency_ms": round(1000.0 / args.min_fps, 6),
            "minimum_output_size": [args.required_width, args.required_height],
            "minimum_output_frames": args.minimum_frames,
            "sample_fps": round(args.sample_fps, 6),
            "max_sampled_frames": args.max_frames,
            "analysis_size": [args.analysis_width, args.analysis_height],
            "changed_threshold": round(args.changed_threshold, 8),
            "ranking": "hard-gate then greedy max-min fingerprint diversity",
            "fingerprint_schema": FINGERPRINT_SCHEMA,
            "fingerprint_group_weights": FINGERPRINT_WEIGHTS,
        },
        "control": {
            "video": str(control),
            "video_sha256": sha256_file(control),
            "probe": control_probe,
            "sampled_frames": int(len(control_frames)),
        },
        "summary": {
            "candidate_count": len(ranking),
            "eligible_count": eligible_count,
            "ineligible_count": len(ranking) - eligible_count,
            "pairwise_distance_count": len(pairs),
        },
        "ranking": ranking,
        "pairwise_distances": pairs,
    }


def _markdown_escape(value: Any) -> str:
    return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")


def _metric(record: dict[str, Any], name: str) -> float:
    return float(record.get("metrics", {}).get(name, 0.0))


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    policy = payload["policy"]
    lines = [
        "# Codec glitch video ranking",
        "",
        f"- Candidates: {summary['candidate_count']}",
        f"- Eligible at {policy['minimum_fps_hard_gate']:.1f} fps: {summary['eligible_count']}",
        f"- Ineligible/error: {summary['ineligible_count']}",
        f"- Control: `{payload['control']['video']}`",
        "",
        "The realtime requirement is a hard gate. Ranking scores are only used "
        "to order eligible candidates; they cannot compensate for a failed fps "
        "or latency gate.",
        "",
        "| Rank | Candidate | Effect | Gate | Score | Diversity | MAE | Changed | "
        "Luma | Chroma | Edge | Temporal | FPS | Intentional | Fallback | Errors | Recovery |",
        "|---:|---|---|:---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for record in payload["ranking"]:
        performance = record.get("performance", {})
        scores = record.get("scores", {})
        diversity = scores.get("selection_diversity")
        fps = performance.get("observed_processing_fps")
        lines.append(
            "| "
            + " | ".join(
                [
                    str(record.get("rank", "—")),
                    _markdown_escape(record.get("label")),
                    _markdown_escape(record.get("effect")),
                    "PASS" if performance.get("hard_gate_passed") else "FAIL",
                    f"{float(scores.get('ranking_score', 0.0)):.3f}",
                    "—" if diversity is None else f"{float(diversity):.3f}",
                    f"{_metric(record, 'mae_8bit'):.2f}",
                    f"{_metric(record, 'changed_ratio'):.3f}",
                    f"{_metric(record, 'luminance_difference'):.3f}",
                    f"{_metric(record, 'chroma_difference'):.3f}",
                    f"{_metric(record, 'edge_difference'):.3f}",
                    f"{_metric(record, 'temporal_residual_activity'):.3f}",
                    "—" if fps is None else f"{float(fps):.2f}",
                    str(performance.get("intentional_repeat_frames", 0)),
                    str(performance.get("fallback_frames", 0)),
                    str(performance.get("codec_errors", 0)),
                    str(performance.get("watchdog_recoveries", 0)),
                ]
            )
            + " |"
        )

    failures = [
        record
        for record in payload["ranking"]
        if not record.get("performance", {}).get("hard_gate_passed")
    ]
    if failures:
        lines.extend(["", "## Hard-gate failures", ""])
        for record in failures:
            reasons = record.get("performance", {}).get("gate_reasons", [])
            if record.get("errors"):
                reasons = list(reasons) + list(record["errors"])
            lines.append(
                f"- **{_markdown_escape(record['label'])}**: "
                + "; ".join(_markdown_escape(reason) for reason in reasons)
            )

    pairs = payload.get("pairwise_distances", [])
    if pairs:
        lines.extend(["", "## Nearest candidate pairs", ""])
        for pair in pairs[: min(20, len(pairs))]:
            lines.append(
                f"- `{_markdown_escape(pair['left'])}` ↔ "
                f"`{_markdown_escape(pair['right'])}`: {pair['distance']:.4f}"
            )
    lines.append("")
    return "\n".join(lines)


def atomic_write_text(path: Path, text: str) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_selftest() -> int:
    height, width, frames = 24, 32, 8
    control = np.zeros((frames, height, width, 3), dtype=np.uint8)
    for index in range(frames):
        control[index, :, :, 0] = np.arange(width, dtype=np.uint8)[None, :] * 7
        control[index, :, :, 1] = index * 12
        control[index, :, :, 2] = np.arange(height, dtype=np.uint8)[:, None] * 9
    color = control.copy()
    color[..., 0] = np.clip(color[..., 0].astype(np.int16) + 48, 0, 255)
    block = np.roll(control, shift=7, axis=2)
    block[::2, : height // 2] = np.roll(
        block[::2, : height // 2], shift=5, axis=2
    )
    color_metrics, color_fingerprint = analyze_frame_pairs(control, color)
    block_metrics, block_fingerprint = analyze_frame_pairs(control, block)
    if color_metrics["mae_rgb"] <= 0.0 or color_metrics["changed_ratio"] <= 0.0:
        raise AssertionError("selftest failed to detect color corruption")
    if block_metrics["temporal_residual_activity"] <= 0.0:
        raise AssertionError("selftest failed to detect temporal corruption")
    if fingerprint_distance(color_fingerprint, block_fingerprint) <= 0.01:
        raise AssertionError("selftest fingerprints did not separate effects")

    report = {
        "processing_mode": "codec_glitch",
        "output_fps": 30.0,
        "processed_frames": 120,
        "output_frame_count": 120,
        "frame_count_preserved": True,
        "end_to_end_observed_fps": 24.0,
        "end_to_end_average_20fps_passed": True,
        "codec_realtime_20fps_passed": True,
        "filter_stream_realtime_20fps_passed": True,
        "codec_latency_p95_milliseconds": 40.0,
        "codec_fallback_frames": 3,
        "codec_intentional_repeat_frames": 0,
        "codec_processing_errors": 1,
        "codec_watchdog_recoveries": 1,
        "codec_reliability_passed": False,
        "codec_hardware_encoder": True,
        "codec_hardware_decoder": True,
        "filter": {
            "frames": 120,
            "submitted_frames": 120,
            "emitted_frames": 120,
            "processing_fps": 24.0,
            "realtime_20fps_passed": True,
            "fallback_frames": 3,
            "intentional_repeat_frames": 0,
            "codec_errors": 1,
            "watchdog_recoveries": 1,
            "backpressure_drops": 0,
            "poll_queue_drops": 0,
            "reliability_passed": False,
            "hardware_encoder": True,
            "hardware_decoder": True,
        },
    }
    video_probe = {
        "fps": 30.0,
        "width": 960,
        "height": 540,
        "declared_frames": 120,
    }
    performance = evaluate_performance_gate(report, video_probe, 20.0)
    if performance["hard_gate_passed"] or performance["reliability_penalty"] <= 0.0:
        raise AssertionError("selftest accepted unreliable output")
    report["codec_fallback_frames"] = 0
    report["codec_processing_errors"] = 0
    report["codec_watchdog_recoveries"] = 0
    report["codec_reliability_passed"] = True
    report["filter"].update(
        fallback_frames=0,
        codec_errors=0,
        watchdog_recoveries=0,
        reliability_passed=True,
    )
    if not evaluate_performance_gate(report, video_probe, 20.0)[
        "hard_gate_passed"
    ]:
        raise AssertionError("selftest rejected reliable output")
    report["end_to_end_average_20fps_passed"] = False
    if evaluate_performance_gate(report, video_probe, 20.0)[
        "hard_gate_passed"
    ]:
        raise AssertionError("selftest hard gate accepted a failed report")
    print("PASS codec glitch video evaluator selftest")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.selftest:
        return run_selftest()
    payload = evaluate_candidates(args)
    atomic_write_text(
        args.output_json,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
    )
    atomic_write_text(args.output_md, render_markdown(payload))
    print(
        f"candidates={payload['summary']['candidate_count']} "
        f"eligible={payload['summary']['eligible_count']} "
        f"json={args.output_json.expanduser().resolve()} "
        f"markdown={args.output_md.expanduser().resolve()}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EvaluationError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
