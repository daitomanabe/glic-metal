#!/usr/bin/env python3
"""Decode video, apply a persistent GLIC realtime backend, and restore audio."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


CODEC_GLITCH_EFFECTS = (
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process a video through an explicit GLIC realtime mode."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--preset", default="default")
    parser.add_argument(
        "--preset-semantics",
        choices=("legacy", "original"),
        default=None,
        help="compat_realtime preset decoding; original_visual always uses original semantics.",
    )
    parser.add_argument(
        "--processing-mode",
        choices=("compat_realtime", "original_visual", "codec_glitch"),
        default="compat_realtime",
        help=(
            "Select the Metal/CPU approximation, fail-closed original-style "
            "core, or stateful VideoToolbox codec glitch lane."
        ),
    )
    recipe_mode = parser.add_mutually_exclusive_group()
    recipe_mode.add_argument(
        "--canonical",
        help="Exact v1/v2 search recipe; overrides preset, strength, and effect controls.",
    )
    recipe_mode.add_argument(
        "--passthrough",
        action="store_true",
        help="Copy BGRA frames unchanged to create an A/B codec baseline.",
    )
    parser.add_argument("--presets-dir", type=Path)
    parser.add_argument(
        "--backend",
        choices=("auto", "cpu", "metal"),
        default=None,
        help=(
            "Defaults to metal; explicit auto selects Metal on macOS and CPU "
            "elsewhere for original_visual."
        ),
    )
    parser.add_argument("--width", type=int, help="Output/filter width in pixels.")
    parser.add_argument("--height", type=int, help="Output/filter height in pixels.")
    parser.add_argument("--fps", type=float, help="Output/filter frame rate.")
    parser.add_argument(
        "--strength",
        type=float,
        default=1.0,
        help="Glitch intensity from 0 (off) to 2 (maximum).",
    )
    parser.add_argument(
        "--effect-family",
        choices=(
            "legacy_block",
            "line_tear",
            "channel_shear",
            "analog_sync",
            "mirror_fold",
            "edge_echo",
            "bitplane_dither",
            "wave_warp",
            "poster_solar",
            "tile_shuffle",
            "vertical_tear",
            "diagonal_slip",
            "scanline_weave",
            "quad_mirror",
        ),
        default="legacy_block",
        help="Realtime glitch mechanism (default: legacy_block).",
    )
    parser.add_argument("--effect-amount", type=float, default=0.7)
    parser.add_argument("--effect-scale", type=float, default=0.5)
    parser.add_argument("--effect-rate", type=float, default=0.5)
    parser.add_argument(
        "--codec-effect",
        choices=CODEC_GLITCH_EFFECTS,
        default="bitrate_crush",
        help="Stateful VideoToolbox encode/decode glitch used by codec_glitch mode.",
    )
    parser.add_argument(
        "--codec-format",
        choices=("h264", "hevc", "prores_422"),
        default="h264",
        help="Native VideoToolbox codec used by codec_glitch mode.",
    )
    parser.add_argument(
        "--codec-amount",
        type=float,
        default=0.55,
        help="Codec quality, temporal hold, or post-decode glitch amount from 0 to 1.",
    )
    parser.add_argument(
        "--codec-rate",
        type=float,
        default=0.35,
        help="Codec glitch temporal modulation rate from 0 to 1.",
    )
    parser.add_argument(
        "--codec-feedback",
        type=float,
        default=0.60,
        help="Decoded history contribution from 0 to 1.",
    )
    parser.add_argument(
        "--codec-generations",
        type=int,
        choices=(2, 3),
        default=3,
        help="Encode/decode generations for generation_cascade.",
    )
    parser.add_argument(
        "--seed",
        type=lambda value: int(value, 0),
        default=0x474C4943,
        help="Pattern seed as decimal or 0x-prefixed integer.",
    )
    parser.add_argument("--filter-bin", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"Required executable was not found: {name}")
    return path


def run_json(command: list[str]) -> dict:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def parse_frame_rate(value: object) -> float:
    """Parse ffprobe's decimal or rational frame-rate representation."""
    try:
        text = str(value)
        if "/" in text:
            numerator_text, denominator_text = text.split("/", 1)
            denominator = float(denominator_text)
            if denominator == 0.0:
                return 0.0
            rate = float(numerator_text) / denominator
        else:
            rate = float(text)
    except (TypeError, ValueError):
        return 0.0
    return rate if math.isfinite(rate) and rate > 0.0 else 0.0


def parse_frame_count(value: object) -> int | None:
    try:
        count = int(str(value))
    except (TypeError, ValueError):
        return None
    return count if 0 < count <= 2_000_000 else None


def first_valid_duration(*values: object) -> float | None:
    """Return the first finite positive ffprobe duration candidate."""
    for value in values:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and seconds > 0.0:
            return seconds
    return None


def estimate_frame_capacity(duration: object, frame_rate: float) -> int | None:
    seconds = first_valid_duration(duration)
    if seconds is None:
        return None
    if frame_rate <= 0.0:
        return None
    # Container timestamps can end a fraction before the final decoded frame.
    # Two slots of slack retain zero-growth behavior without claiming an exact
    # frame count when ffprobe reports nb_frames=N/A.
    return parse_frame_count(math.ceil(seconds * frame_rate) + 2)


def select_frame_capacity(
    nb_frames: object,
    duration: object,
    target_fps: float,
    frame_rate_overridden: bool,
) -> int | None:
    exact_count = None if frame_rate_overridden else parse_frame_count(nb_frames)
    estimated_count = estimate_frame_capacity(duration, target_fps)
    if exact_count is None:
        return estimated_count
    if estimated_count is None:
        return exact_count
    return max(exact_count, estimated_count)


def passes_end_to_end_average_30fps(
    *,
    width: int,
    height: int,
    frames: int,
    elapsed_seconds: float,
    target_fps: float,
    output_fps: float,
) -> bool:
    observed_fps = frames / elapsed_seconds if elapsed_seconds > 0.0 else 0.0
    return (
        width >= 960
        and height >= 540
        and frames >= 120
        and target_fps >= 30.0
        and output_fps >= 30.0
        and observed_fps >= 30.0
    )


def passes_end_to_end_average_20fps(
    *,
    width: int,
    height: int,
    frames: int,
    elapsed_seconds: float,
    target_fps: float,
    output_fps: float,
) -> bool:
    observed_fps = frames / elapsed_seconds if elapsed_seconds > 0.0 else 0.0
    return (
        width >= 960
        and height >= 540
        and frames >= 120
        and target_fps >= 20.0
        and output_fps >= 20.0
        and observed_fps >= 20.0
    )


def probe_video(ffprobe: str, path: Path) -> dict:
    return run_json(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate,r_frame_rate,codec_name,pix_fmt,nb_frames,duration",
            "-show_entries",
            "format=duration,size,bit_rate",
            "-of",
            "json",
            str(path),
        ]
    )


def count_video_frames(ffprobe: str, path: Path) -> int | None:
    """Count decoded video frames when container metadata is unavailable."""
    payload = run_json(
        [
            ffprobe,
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "json",
            str(path),
        ]
    )
    streams = payload.get("streams", [])
    if not streams:
        return None
    return parse_frame_count(streams[0].get("nb_read_frames"))


def filter_binary_name(processing_mode: str) -> str:
    names = {
        "compat_realtime": "glic_realtime_filter",
        "original_visual": "glic_original_visual_filter",
        "codec_glitch": "glic_codec_glitch_filter",
    }
    try:
        return names[processing_mode]
    except KeyError as error:
        raise RuntimeError(
            f"Unsupported processing mode: {processing_mode}"
        ) from error


def select_filter_binary(
    root: Path, requested: Path | None, processing_mode: str
) -> Path:
    binary_name = filter_binary_name(processing_mode)
    candidates: list[Path] = []
    if requested is not None:
        candidates.append(requested)
    candidates.extend(
        [
            root / "build" / binary_name,
            Path(__file__).resolve().parent / binary_name,
        ]
    )
    installed = shutil.which(binary_name)
    if installed is not None:
        candidates.append(Path(installed))
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    raise RuntimeError(f"{binary_name} is not built; run cmake --build build")


def select_presets_directory(root: Path, requested: Path | None) -> Path:
    candidates = (
        [requested.expanduser().resolve()]
        if requested is not None
        else [root / "presets", root / "share" / "glic-metal" / "presets"]
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise RuntimeError("Preset directory was not found; pass --presets-dir")


def select_encoder(ffmpeg: str) -> tuple[str, list[str]]:
    encoders = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if "h264_videotoolbox" in encoders:
        return "h264_videotoolbox", [
            "-c:v",
            "h264_videotoolbox",
            "-b:v",
            "20M",
        ]
    if "libx264" in encoders:
        return "libx264", ["-c:v", "libx264", "-preset", "fast", "-crf", "18"]
    raise RuntimeError("No supported H.264 encoder was found")


def log_tail(path: Path, lines: int = 30) -> str:
    if not path.exists():
        return ""
    return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])


def resolve_backend(
    processing_mode: str, requested: str | None, platform: str = sys.platform
) -> str:
    if processing_mode == "codec_glitch":
        return "videotoolbox"
    if processing_mode == "original_visual":
        if requested == "auto":
            return "metal" if platform == "darwin" else "cpu"
        return requested or "metal"
    return requested or "metal"


def codec_glitch_filter_options(
    *,
    codec_format: str = "h264",
    codec_effect: str,
    codec_amount: float,
    codec_rate: float,
    codec_feedback: float,
    codec_generations: int = 3,
    seed: int,
    frame_rate: float,
) -> list[str]:
    """Build the stable CLI boundary to the VideoToolbox stream filter."""
    if codec_effect not in CODEC_GLITCH_EFFECTS:
        raise RuntimeError(f"Unsupported codec glitch effect: {codec_effect}")
    codec_frames_per_second = max(1, int(round(frame_rate)))
    return [
        "--fps",
        str(codec_frames_per_second),
        "--codec",
        codec_format,
        "--effect",
        codec_effect,
        "--amount",
        str(codec_amount),
        "--rate",
        str(codec_rate),
        "--feedback",
        str(codec_feedback),
        "--generations",
        str(codec_generations),
        "--seed",
        str(seed),
    ]


def codec_glitch_report_fields(
    args: argparse.Namespace, filter_report: dict
) -> dict:
    """Promote codec provenance and recovery metrics into the video report."""
    if args.processing_mode != "codec_glitch":
        return {}
    statistics = filter_report.get("statistics", {})
    if not isinstance(statistics, dict):
        statistics = {}

    def metric(name: str, default: object = None) -> object:
        return filter_report.get(name, statistics.get(name, default))

    return {
        "codec_format": filter_report.get("codec", args.codec_format),
        "codec_backend": filter_report.get("codec_backend", "videotoolbox"),
        "codec_effect": filter_report.get(
            "effect_family", filter_report.get("codec_effect", args.codec_effect)
        ),
        "codec_amount": filter_report.get(
            "amount", filter_report.get("codec_amount", args.codec_amount)
        ),
        "codec_rate": filter_report.get(
            "rate", filter_report.get("codec_rate", args.codec_rate)
        ),
        "codec_feedback": filter_report.get(
            "feedback", filter_report.get("codec_feedback", args.codec_feedback)
        ),
        "codec_hardware_encoder": metric("hardware_encoder"),
        "codec_hardware_decoder": metric("hardware_decoder"),
        "codec_base_frame_qp_supported": metric("base_frame_qp_supported"),
        "codec_encoded_frames": metric("encoded_frames"),
        "codec_decoded_frames": metric("decoded_frames"),
        "codec_intentional_packet_drops": metric(
            "intentional_packet_drops", 0
        ),
        "codec_processing_errors": metric("codec_errors", 0),
        "codec_watchdog_recoveries": metric("watchdog_recoveries", 0),
        "codec_average_latency_milliseconds": metric(
            "average_latency_milliseconds"
        ),
        "codec_latency_p50_milliseconds": metric("latency_p50_ms"),
        "codec_latency_p95_milliseconds": metric("latency_p95_ms"),
        "codec_fallback_frames": metric("fallback_frames", 0),
        "codec_intentional_repeat_frames": metric(
            "intentional_repeat_frames", 0
        ),
        "codec_reliability_passed": metric("reliability_passed"),
        "codec_realtime_20fps_passed": metric("realtime_20fps_passed"),
        "codec_realtime_30fps_passed": metric("realtime_30fps_passed"),
        "codec_engine_fps": metric("processing_fps"),
    }


def main() -> int:
    args = parse_args()
    if (args.width is None) != (args.height is None):
        raise RuntimeError("--width and --height must be provided together")
    if args.width is not None and (args.width <= 0 or args.height <= 0):
        raise RuntimeError("--width and --height must be positive")
    if args.fps is not None and (not math.isfinite(args.fps) or args.fps <= 0):
        raise RuntimeError("--fps must be finite and positive")
    if args.processing_mode == "original_visual":
        if args.passthrough or args.canonical:
            raise RuntimeError(
                "original_visual accepts named presets only; canonical and passthrough are unavailable"
            )
        if args.preset_semantics not in (None, "original"):
            raise RuntimeError("original_visual requires original preset semantics")
        preset_semantics = "original"
        backend_requested = resolve_backend(args.processing_mode, args.backend)
    elif args.processing_mode == "codec_glitch":
        if args.passthrough or args.canonical:
            raise RuntimeError(
                "codec_glitch accepts --codec-effect controls only; "
                "canonical and passthrough are unavailable"
            )
        if args.preset_semantics is not None:
            raise RuntimeError("codec_glitch does not use GLIC preset semantics")
        if args.backend == "cpu":
            raise RuntimeError(
                "codec_glitch requires the VideoToolbox hardware codec lane"
            )
        preset_semantics = "not-applicable"
        backend_requested = resolve_backend(args.processing_mode, args.backend)
    else:
        preset_semantics = args.preset_semantics or "legacy"
        backend_requested = resolve_backend(args.processing_mode, args.backend)
    if not math.isfinite(args.strength) or not 0.0 <= args.strength <= 2.0:
        raise RuntimeError("--strength must be between 0 and 2")
    for name, value in (
        ("--effect-amount", args.effect_amount),
        ("--effect-scale", args.effect_scale),
        ("--effect-rate", args.effect_rate),
    ):
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise RuntimeError(f"{name} must be between 0 and 1")
    for name, value in (
        ("--codec-amount", args.codec_amount),
        ("--codec-rate", args.codec_rate),
        ("--codec-feedback", args.codec_feedback),
    ):
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise RuntimeError(f"{name} must be between 0 and 1")
    if not 0 <= args.seed <= 0xFFFFFFFF:
        raise RuntimeError("--seed must fit an unsigned 32-bit integer")
    root = Path(__file__).resolve().parent.parent
    input_path = args.input.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    report_path = (
        args.report.expanduser().resolve()
        if args.report is not None
        else output_path.with_suffix(output_path.suffix + ".json")
    )
    presets_dir = (
        None
        if args.passthrough
        or args.canonical
        or args.processing_mode == "codec_glitch"
        else select_presets_directory(root, args.presets_dir)
    )

    if not input_path.is_file():
        raise RuntimeError(f"Input video does not exist: {input_path}")
    if output_path.exists() and not args.overwrite:
        raise RuntimeError(f"Output already exists; pass --overwrite: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg = require_tool("ffmpeg")
    ffprobe = require_tool("ffprobe")
    filter_binary = select_filter_binary(
        root, args.filter_bin, args.processing_mode
    )
    encoder_name, encoder_args = select_encoder(ffmpeg)
    input_probe = probe_video(ffprobe, input_path)
    streams = input_probe.get("streams", [])
    if not streams:
        raise RuntimeError(f"No video stream found: {input_path}")
    stream = streams[0]
    source_width = int(stream["width"])
    source_height = int(stream["height"])
    source_frame_rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    source_fps = parse_frame_rate(source_frame_rate)
    if source_fps <= 0.0:
        raise RuntimeError("Input frame rate could not be determined")
    width = args.width or source_width
    height = args.height or source_height
    target_fps = args.fps if args.fps is not None else source_fps
    frame_rate = f"{target_fps:g}"
    source_duration = first_valid_duration(
        stream.get("duration"), input_probe.get("format", {}).get("duration")
    )
    expected_frames = select_frame_capacity(
        stream.get("nb_frames"),
        source_duration,
        target_fps,
        args.fps is not None,
    )

    if args.processing_mode == "original_visual":
        assert presets_dir is not None
        preflight = subprocess.run(
            [
                str(filter_binary),
                "--width",
                str(width),
                "--height",
                str(height),
                "--target-fps",
                frame_rate,
                "--backend",
                backend_requested,
                "--preset",
                args.preset,
                "--presets-dir",
                str(presets_dir),
                "--check",
            ],
            capture_output=True,
            text=True,
        )
        if preflight.returncode != 0:
            raise RuntimeError(
                "original_visual preset preflight failed:\n"
                + preflight.stderr.strip()
            )

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="glic-metal-video-") as temp_text:
        temp_dir = Path(temp_text)
        silent_video = temp_dir / "processed-silent.mp4"
        filter_stats = temp_dir / "filter-stats.json"
        decode_log = temp_dir / "decode.log"
        filter_log = temp_dir / "filter.log"
        encode_log = temp_dir / "encode.log"

        decode_command = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-noautorotate",
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-an",
        ]
        video_filters: list[str] = []
        if width != source_width or height != source_height:
            video_filters.append(f"scale={width}:{height}:flags=lanczos")
        if args.fps is not None:
            video_filters.append(f"fps={frame_rate}")
        if video_filters:
            decode_command.extend(["-vf", ",".join(video_filters)])
        decode_command.extend(["-f", "rawvideo", "-pix_fmt", "bgra", "pipe:1"])
        filter_command = [
            str(filter_binary),
            "--width",
            str(width),
            "--height",
            str(height),
        ]
        if args.processing_mode == "original_visual":
            assert presets_dir is not None
            if expected_frames is not None:
                filter_command.extend(
                    ["--expected-frames", str(expected_frames)]
                )
            filter_command.extend(
                [
                    "--target-fps",
                    frame_rate,
                    "--backend",
                    backend_requested,
                    "--preset",
                    args.preset,
                    "--presets-dir",
                    str(presets_dir),
                ]
            )
        elif args.processing_mode == "codec_glitch":
            filter_command.extend(
                codec_glitch_filter_options(
                    codec_format=args.codec_format,
                    codec_effect=args.codec_effect,
                    codec_amount=args.codec_amount,
                    codec_rate=args.codec_rate,
                    codec_feedback=args.codec_feedback,
                    codec_generations=args.codec_generations,
                    seed=args.seed,
                    frame_rate=target_fps,
                )
            )
        elif args.passthrough:
            filter_command.append("--passthrough")
        elif args.canonical:
            filter_command.extend(
                [
                    "--canonical",
                    args.canonical,
                    "--backend",
                    backend_requested,
                    "--seed",
                    str(args.seed),
                ]
            )
        else:
            assert presets_dir is not None
            filter_command.extend(
                [
                    "--preset",
                    args.preset,
                    "--presets-dir",
                    str(presets_dir),
                    "--preset-semantics",
                    preset_semantics,
                    "--backend",
                    backend_requested,
                    "--strength",
                    str(args.strength),
                    "--effect-family",
                    args.effect_family,
                    "--effect-amount",
                    str(args.effect_amount),
                    "--effect-scale",
                    str(args.effect_scale),
                    "--effect-rate",
                    str(args.effect_rate),
                    "--seed",
                    str(args.seed),
                ]
            )
        filter_command.extend(["--stats-json", str(filter_stats)])
        encode_command = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgra",
            "-video_size",
            f"{width}x{height}",
            "-framerate",
            frame_rate,
            "-i",
            "pipe:0",
            "-an",
            *encoder_args,
            "-pix_fmt",
            "yuv420p",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
            "-movflags",
            "+faststart",
            str(silent_video),
        ]

        with (
            decode_log.open("wb") as decode_errors,
            filter_log.open("wb") as filter_errors,
            encode_log.open("wb") as encode_errors,
        ):
            decoder = subprocess.Popen(
                decode_command, stdout=subprocess.PIPE, stderr=decode_errors
            )
            assert decoder.stdout is not None
            realtime_filter = subprocess.Popen(
                filter_command,
                stdin=decoder.stdout,
                stdout=subprocess.PIPE,
                stderr=filter_errors,
            )
            decoder.stdout.close()
            assert realtime_filter.stdout is not None
            encoder = subprocess.Popen(
                encode_command, stdin=realtime_filter.stdout, stderr=encode_errors
            )
            realtime_filter.stdout.close()

            encoder_code = encoder.wait()
            filter_code = realtime_filter.wait()
            decoder_code = decoder.wait()

        if decoder_code != 0 or filter_code != 0 or encoder_code != 0:
            raise RuntimeError(
                "Video pipeline failed\n"
                f"decoder rc={decoder_code}:\n{log_tail(decode_log)}\n"
                f"filter rc={filter_code}:\n{log_tail(filter_log)}\n"
                f"encoder rc={encoder_code}:\n{log_tail(encode_log)}"
            )

        mux_command = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(silent_video),
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
            "-map_metadata",
            "1",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        subprocess.run(mux_command, check=True)
        filter_report = json.loads(filter_stats.read_text())

    output_probe = probe_video(ffprobe, output_path)
    output_streams = output_probe.get("streams", [])
    output_frame_rate = (
        output_streams[0].get("avg_frame_rate") if output_streams else None
    )
    output_fps = parse_frame_rate(output_frame_rate)
    elapsed = time.monotonic() - started
    duration = source_duration or 0.0
    processed_frames = int(filter_report.get("frames", 0))
    output_frame_count: int | None = None
    if output_streams:
        output_frame_count = parse_frame_count(output_streams[0].get("nb_frames"))
    if output_frame_count is None:
        output_frame_count = count_video_frames(ffprobe, output_path)
    frame_count_preserved = (
        output_frame_count == processed_frames
        if output_frame_count is not None and processed_frames > 0
        else None
    )
    if args.processing_mode == "codec_glitch" and frame_count_preserved is not True:
        raise RuntimeError(
            "codec_glitch output frame count is unavailable or changed during encode/mux: "
            f"filter={processed_frames}, output={output_frame_count}"
        )
    end_to_end_observed_fps = processed_frames / elapsed if elapsed else 0.0
    report = {
        "schema": "glic-video-process-v1",
        "input": str(input_path),
        "output": str(output_path),
        "preset": filter_report.get("preset", "passthrough"),
        "preset_semantics": filter_report.get(
            "preset_semantics", preset_semantics
        ),
        "processing_mode": filter_report.get(
            "processing_mode", "compat_realtime"
        ),
        "preset_mapping_fidelity": filter_report.get(
            "preset_mapping_fidelity", "not-applicable"
        ),
        "preset_mapping_reasons": filter_report.get(
            "preset_mapping_reasons", []
        ),
        "recipe_source": filter_report.get(
            "recipe_source",
            "codec_glitch_controls"
            if args.processing_mode == "codec_glitch"
            else "passthrough",
        ),
        "canonical": args.canonical,
        "canonical_version": filter_report.get("canonical_version"),
        "fidelity_claim": filter_report.get("fidelity_claim"),
        "processing_pixel_exact": filter_report.get("processing_pixel_exact"),
        "unsupported_policy": filter_report.get("unsupported_policy"),
        "known_deviations": filter_report.get("known_deviations", []),
        "strength": filter_report.get("strength"),
        "effect_family": filter_report.get("effect_family"),
        "effect_amount": filter_report.get(
            "effect_amount", filter_report.get("amount")
        ),
        "effect_scale": filter_report.get("effect_scale"),
        "effect_rate": filter_report.get(
            "effect_rate", filter_report.get("rate")
        ),
        "seed": filter_report.get("seed"),
        "backend_requested": "passthrough" if args.passthrough else backend_requested,
        "target_width": width,
        "target_height": height,
        "target_frame_rate": frame_rate,
        "target_fps": round(target_fps, 6),
        "output_fps": round(output_fps, 6),
        "processed_frames": processed_frames,
        "output_frame_count": output_frame_count,
        "frame_count_preserved": frame_count_preserved,
        "encoder": encoder_name,
        "elapsed_seconds": round(elapsed, 3),
        "source_duration_seconds": duration,
        "end_to_end_realtime_factor": round(duration / elapsed, 3)
        if elapsed
        else 0.0,
        "end_to_end_observed_fps": round(end_to_end_observed_fps, 3),
        "end_to_end_average_30fps_passed": passes_end_to_end_average_30fps(
            width=width,
            height=height,
            frames=processed_frames,
            elapsed_seconds=elapsed,
            target_fps=target_fps,
            output_fps=output_fps,
        ),
        "end_to_end_average_20fps_passed": passes_end_to_end_average_20fps(
            width=width,
            height=height,
            frames=processed_frames,
            elapsed_seconds=elapsed,
            target_fps=target_fps,
            output_fps=output_fps,
        ),
        "filter_stream_realtime_20fps_passed": filter_report.get(
            "realtime_20fps_passed"
        ),
        "filter_kernel_realtime_20fps_passed": filter_report.get(
            "kernel_realtime_20fps_passed"
        ),
        "filter_stream_realtime_30fps_passed": filter_report.get(
            "realtime_30fps_passed"
        ),
        "filter_kernel_realtime_30fps_passed": filter_report.get(
            "kernel_realtime_30fps_passed"
        ),
        "filter": filter_report,
        "input_probe": input_probe,
        "output_probe": output_probe,
        **codec_glitch_report_fields(args, filter_report),
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"output={output_path} frames={filter_report['frames']} "
        f"kernel_fps={filter_report['processing_fps']:.3f} "
        f"stream_fps={filter_report.get('stream_observed_fps', filter_report['processing_fps']):.3f} "
        f"elapsed={elapsed:.3f}s report={report_path}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
