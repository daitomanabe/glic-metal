#!/usr/bin/env python3
"""Render and validate the complete realtime VideoToolbox fast-path matrix.

The runner is deterministic, resumable, and does not call an AI service. Each
matrix cell retains an MP4 preview, filter statistics, and stderr log. A final
JSON/Markdown summary applies fail-closed reliability and realtime gates.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from typing import Any, Sequence


SCHEMA = "glic-videotoolbox-fast-path-validation-v2"
EFFECTS = (
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
    "plane_time_split",
    "reference_atlas",
    "flow_lattice",
    "scan_order_fold",
    "regional_gop_clock",
    "entropy_feedback",
    "rolling_time_shutter",
    "asymmetric_plane_codec",
)
CODECS = ("h264", "hevc", "prores_422")
RESOLUTIONS = ("960x540", "1920x1080")
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_FILTER_BIN = (
    SCRIPT_DIRECTORY / "glic_codec_glitch_filter"
    if (SCRIPT_DIRECTORY / "glic_codec_glitch_filter").is_file()
    else SCRIPT_DIRECTORY.parent / "build" / "glic_codec_glitch_filter"
)


class ValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MatrixCell:
    effect: str
    codec: str
    resolution: str

    @property
    def width(self) -> int:
        return int(self.resolution.split("x", 1)[0])

    @property
    def height(self) -> int:
        return int(self.resolution.split("x", 1)[1])

    @property
    def stem(self) -> str:
        return f"{self.effect}__{self.codec}__{self.resolution}"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render and validate all realtime VideoToolbox fast paths."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("--filter-bin", type=Path, default=DEFAULT_FILTER_BIN)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--minimum-fps", type=float, default=20.0)
    parser.add_argument("--maximum-p95-ms", type=float, default=50.0)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--effect", action="append", choices=EFFECTS)
    parser.add_argument("--codec", action="append", choices=CODECS)
    parser.add_argument("--resolution", action="append", choices=RESOLUTIONS)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def executable(value: str | Path) -> str:
    candidate = Path(value).expanduser()
    if candidate.parent != Path(".") or candidate.is_absolute():
        if not candidate.is_file():
            raise ValidationError(f"executable was not found: {candidate}")
        return str(candidate.resolve())
    resolved = shutil.which(str(value))
    if resolved is None:
        raise ValidationError(f"executable was not found on PATH: {value}")
    return resolved


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"invalid JSON {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValidationError(f"JSON root is not an object: {path}")
    return payload


def completed_cell(video: Path, stats: Path, expected_frames: int) -> bool:
    if not video.is_file() or video.stat().st_size <= 0 or not stats.is_file():
        return False
    try:
        report = load_json(stats)
    except ValidationError:
        return False
    return (
        report.get("frames") == expected_frames
        and report.get("input_pixel_format") == "nv12_420v"
        and report.get("direct_420v_input") is True
        and report.get("pixel_path") == "nv12_metal"
        and report.get("fused_metal_effects") is True
        and report.get("asynchronous_metal_delivery") is True
        and report.get("ordered_delivery") is True
        and report.get("frame_count_preserved") is True
    )


def normalize_filter_report(stats_path: Path, expected_frames: int, fps: int) -> None:
    """Add process-video compatibility fields without hiding filter evidence."""
    report = load_json(stats_path)
    reported_frames = int(report.get("frames", 0))
    preserved = reported_frames == expected_frames
    kernel_20 = report.get("kernel_realtime_20fps_passed") is True
    stream_20 = report.get("realtime_20fps_passed") is True
    report.update(
        {
            "processed_frames": reported_frames,
            "output_frame_count": reported_frames,
            "frame_count_preserved": preserved,
            "output_fps": float(fps),
            "end_to_end_observed_fps": report.get("stream_observed_fps", 0.0),
            "end_to_end_average_20fps_passed": stream_20,
            "codec_realtime_20fps_passed": kernel_20,
            "filter_stream_realtime_20fps_passed": stream_20,
            "codec_latency_p95_milliseconds": report.get("latency_p95_ms", 0.0),
            "codec_fallback_frames": report.get("fallback_frames", 0),
            "codec_intentional_repeat_frames": report.get(
                "intentional_repeat_frames", 0
            ),
            "codec_processing_errors": report.get("codec_errors", 0),
            "codec_watchdog_recoveries": report.get("watchdog_recoveries", 0),
        }
    )
    temporary = stats_path.with_suffix(stats_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(stats_path)


def render_control(
    ffmpeg: str, input_path: Path, output_path: Path, width: int, height: int,
    fps: int, frames: int, force: bool
) -> None:
    if output_path.is_file() and output_path.stat().st_size > 0 and not force:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(input_path), "-an",
        "-vf", f"fps={fps},scale={width}:{height}:flags=lanczos",
        "-frames:v", str(frames), "-c:v", "libx264", "-preset", "fast",
        "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(output_path),
    ]
    subprocess.run(command, check=True)


def render_cell(
    cell: MatrixCell,
    *,
    input_path: Path,
    filter_bin: str,
    ffmpeg: str,
    output_dir: Path,
    frames: int,
    fps: int,
    force: bool,
) -> tuple[MatrixCell, Path, Path, bool]:
    video_path = output_dir / "videos" / f"{cell.stem}.mp4"
    stats_path = output_dir / "reports" / f"{cell.stem}.json"
    log_path = output_dir / "logs" / f"{cell.stem}.log"
    if not force and video_path.is_file() and stats_path.is_file():
        normalize_filter_report(stats_path, frames, fps)
        if completed_cell(video_path, stats_path, frames):
            return cell, video_path, stats_path, True
    video_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    decode_command = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-i", str(input_path), "-an",
        "-vf", f"fps={fps},scale={cell.width}:{cell.height}:flags=lanczos",
        "-frames:v", str(frames), "-f", "rawvideo", "-pix_fmt", "nv12", "-",
    ]
    filter_command = [
        filter_bin,
        "--width", str(cell.width), "--height", str(cell.height),
        "--fps", str(fps), "--codec", cell.codec,
        "--input-pixel-format", "nv12", "--pixel-path", "nv12",
        "--effect", cell.effect,
        "--amount", "0.78", "--rate", "0.64", "--feedback", "0.72",
        "--seed", "0x474c4943434f4445", "--stats-json", str(stats_path),
    ]
    encode_command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "bgra",
        "-s", cell.resolution, "-r", str(fps), "-i", "-",
        "-frames:v", str(frames), "-an", "-c:v", "libx264",
        "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(video_path),
    ]
    with log_path.open("wb") as log:
        decoder = subprocess.Popen(decode_command, stdout=subprocess.PIPE, stderr=log)
        assert decoder.stdout is not None
        glitch = subprocess.Popen(
            filter_command, stdin=decoder.stdout, stdout=subprocess.PIPE, stderr=log
        )
        decoder.stdout.close()
        assert glitch.stdout is not None
        encoder = subprocess.Popen(encode_command, stdin=glitch.stdout, stderr=log)
        glitch.stdout.close()
        encode_status = encoder.wait()
        glitch_status = glitch.wait()
        decode_status = decoder.wait()
    if decode_status != 0 or glitch_status != 0 or encode_status != 0:
        raise ValidationError(
            f"{cell.stem} failed: decode={decode_status} "
            f"filter={glitch_status} encode={encode_status}; see {log_path}"
        )
    normalize_filter_report(stats_path, frames, fps)
    if not completed_cell(video_path, stats_path, frames):
        raise ValidationError(f"{cell.stem} did not produce complete evidence")
    return cell, video_path, stats_path, False


def probe_video(ffprobe: str, path: Path) -> dict[str, Any]:
    command = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,nb_frames",
        "-of", "json", str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    if not streams:
        raise ValidationError(f"no video stream in {path}")
    return streams[0]


def summarize(
    cells: Sequence[MatrixCell],
    *,
    output_dir: Path,
    ffprobe: str,
    frames: int,
    minimum_fps: float,
    maximum_p95_ms: float,
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for cell in cells:
        video = output_dir / "videos" / f"{cell.stem}.mp4"
        stats_path = output_dir / "reports" / f"{cell.stem}.json"
        stats = load_json(stats_path)
        probe = probe_video(ffprobe, video)
        reasons: list[str] = []
        if stats.get("frames") != frames:
            reasons.append("frame_count")
        if not stats.get("reliability_passed"):
            reasons.append("reliability")
        if float(stats.get("processing_fps", 0.0)) < minimum_fps:
            reasons.append("processing_fps")
        if float(stats.get("latency_p95_ms", 1e9)) > maximum_p95_ms:
            reasons.append("p95_latency")
        if int(stats.get("gpu_timeouts", 0)) != 0:
            reasons.append("gpu_timeout")
        if stats.get("input_pixel_format") != "nv12_420v":
            reasons.append("input_pixel_format")
        if stats.get("direct_420v_input") is not True:
            reasons.append("direct_420v_input")
        for key in (
            "nv12_metal_fast_path",
            "metal_texture_cache",
            "fused_metal_effects",
            "asynchronous_metal_delivery",
            "ordered_delivery",
        ):
            if stats.get(key) is not True:
                reasons.append(key)
        if (
            int(probe.get("width", 0)) != cell.width
            or int(probe.get("height", 0)) != cell.height
        ):
            reasons.append("output_dimensions")
        runs.append(
            {
                "effect": cell.effect,
                "codec": cell.codec,
                "resolution": cell.resolution,
                "input_pixel_format": stats.get("input_pixel_format"),
                "direct_420v_input": stats.get("direct_420v_input"),
                "video": str(video),
                "stats": str(stats_path),
                "processing_fps": stats.get("processing_fps"),
                "latency_p95_ms": stats.get("latency_p95_ms"),
                "fallback_frames": stats.get("fallback_frames"),
                "gpu_timeouts": stats.get("gpu_timeouts"),
                "passed": not reasons,
                "failure_reasons": reasons,
            }
        )
    passed = sum(1 for run in runs if run["passed"])
    return {
        "schema": SCHEMA,
        "matrix": {
            "effects": len({cell.effect for cell in cells}),
            "codecs": len({cell.codec for cell in cells}),
            "resolutions": len({cell.resolution for cell in cells}),
            "runs": len(cells),
        },
        "gates": {
            "frames": frames,
            "minimum_processing_fps": minimum_fps,
            "maximum_p95_ms": maximum_p95_ms,
            "reliability_required": True,
            "gpu_timeouts_allowed": 0,
            "input_pixel_format": "nv12_420v",
            "direct_420v_input_required": True,
        },
        "passed_runs": passed,
        "failed_runs": len(runs) - passed,
        "passed": passed == len(runs),
        "runs": runs,
    }


def write_summary(output_dir: Path, summary: dict[str, Any]) -> None:
    json_path = output_dir / "summary.json"
    markdown_path = output_dir / "summary.md"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    lines = [
        "# VideoToolbox Fast Path Validation",
        "",
        f"- Runs: {summary['matrix']['runs']}",
        f"- Passed: {summary['passed_runs']}",
        f"- Failed: {summary['failed_runs']}",
        f"- Overall: {'PASS' if summary['passed'] else 'FAIL'}",
        "",
        "| Effect | Codec | Resolution | FPS | p95 ms | Result |",
        "|---|---|---:|---:|---:|---|",
    ]
    for run in summary["runs"]:
        result = "PASS" if run["passed"] else ", ".join(run["failure_reasons"])
        lines.append(
            f"| `{run['effect']}` | `{run['codec']}` | {run['resolution']} | "
            f"{float(run['processing_fps']):.2f} | "
            f"{float(run['latency_p95_ms']):.2f} | {result} |"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    options = parse_args(argv)
    if options.frames < 2 or options.fps <= 0 or options.jobs <= 0:
        raise ValidationError("frames, fps, and jobs must be positive")
    input_path = options.input.expanduser().resolve()
    if not input_path.is_file():
        raise ValidationError(f"input video was not found: {input_path}")
    filter_bin = executable(options.filter_bin)
    ffmpeg = executable(options.ffmpeg)
    ffprobe = executable(options.ffprobe)
    output_dir = options.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    effects = tuple(options.effect or EFFECTS)
    codecs = tuple(options.codec or CODECS)
    resolutions = tuple(options.resolution or RESOLUTIONS)
    cells = [
        MatrixCell(effect, codec, resolution)
        for effect in effects
        for codec in codecs
        for resolution in resolutions
    ]
    for resolution in resolutions:
        width, height = (int(value) for value in resolution.split("x", 1))
        render_control(
            ffmpeg, input_path, output_dir / "controls" / f"{resolution}.mp4",
            width, height, options.fps, options.frames, options.force
        )

    print_lock = threading.Lock()
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=options.jobs) as executor:
        futures = [
            executor.submit(
                render_cell, cell, input_path=input_path,
                filter_bin=filter_bin, ffmpeg=ffmpeg, output_dir=output_dir,
                frames=options.frames, fps=options.fps, force=options.force
            )
            for cell in cells
        ]
        for future in as_completed(futures):
            try:
                cell, _, _, cached = future.result()
                with print_lock:
                    print(f"{'CACHED' if cached else 'PASS'} {cell.stem}", flush=True)
            except Exception as error:
                failures.append(str(error))
                with print_lock:
                    print(f"FAIL {error}", file=sys.stderr, flush=True)
    if failures:
        raise ValidationError(f"{len(failures)} render(s) failed")
    summary = summarize(
        cells, output_dir=output_dir, ffprobe=ffprobe, frames=options.frames,
        minimum_fps=options.minimum_fps,
        maximum_p95_ms=options.maximum_p95_ms
    )
    write_summary(output_dir, summary)
    print(
        f"{'PASS' if summary['passed'] else 'FAIL'} "
        f"{summary['passed_runs']}/{summary['matrix']['runs']} runs"
    )
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValidationError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
