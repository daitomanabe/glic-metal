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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process a video through the GLIC CPU/Metal realtime backend."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--preset", default="default")
    processing_mode = parser.add_mutually_exclusive_group()
    processing_mode.add_argument(
        "--canonical",
        help="Exact v1/v2 search recipe; overrides preset, strength, and effect controls.",
    )
    processing_mode.add_argument(
        "--passthrough",
        action="store_true",
        help="Copy BGRA frames unchanged to create an A/B codec baseline.",
    )
    parser.add_argument("--presets-dir", type=Path)
    parser.add_argument(
        "--backend", choices=("auto", "cpu", "metal"), default="metal"
    )
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
        ),
        default="legacy_block",
        help="Realtime glitch mechanism (default: legacy_block).",
    )
    parser.add_argument("--effect-amount", type=float, default=0.7)
    parser.add_argument("--effect-scale", type=float, default=0.5)
    parser.add_argument("--effect-rate", type=float, default=0.5)
    parser.add_argument(
        "--seed",
        type=lambda value: int(value, 0),
        default=0x474C4943,
        help="Pattern seed as decimal or 0x-prefixed integer.",
    )
    parser.add_argument("--filter-bin", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"Required executable was not found: {name}")
    return path


def run_json(command: list[str]) -> dict:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


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


def select_filter_binary(root: Path, requested: Path | None) -> Path:
    candidates: list[Path] = []
    if requested is not None:
        candidates.append(requested)
    candidates.extend(
        [
            root / "build" / "glic_realtime_filter",
            Path(__file__).resolve().parent / "glic_realtime_filter",
        ]
    )
    installed = shutil.which("glic_realtime_filter")
    if installed is not None:
        candidates.append(Path(installed))
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    raise RuntimeError("glic_realtime_filter is not built; run cmake --build build")


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


def main() -> int:
    args = parse_args()
    if not math.isfinite(args.strength) or not 0.0 <= args.strength <= 2.0:
        raise RuntimeError("--strength must be between 0 and 2")
    for name, value in (
        ("--effect-amount", args.effect_amount),
        ("--effect-scale", args.effect_scale),
        ("--effect-rate", args.effect_rate),
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
        if args.passthrough or args.canonical
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
    filter_binary = select_filter_binary(root, args.filter_bin)
    encoder_name, encoder_args = select_encoder(ffmpeg)
    input_probe = probe_video(ffprobe, input_path)
    streams = input_probe.get("streams", [])
    if not streams:
        raise RuntimeError(f"No video stream found: {input_path}")
    stream = streams[0]
    width = int(stream["width"])
    height = int(stream["height"])
    frame_rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    if not frame_rate or frame_rate == "0/0":
        raise RuntimeError("Input frame rate could not be determined")

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
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgra",
            "pipe:1",
        ]
        filter_command = [
            str(filter_binary),
            "--width",
            str(width),
            "--height",
            str(height),
        ]
        if args.passthrough:
            filter_command.append("--passthrough")
        elif args.canonical:
            filter_command.extend(
                [
                    "--canonical",
                    args.canonical,
                    "--backend",
                    args.backend,
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
                    "--backend",
                    args.backend,
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
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        subprocess.run(mux_command, check=True)
        filter_report = json.loads(filter_stats.read_text())

    output_probe = probe_video(ffprobe, output_path)
    elapsed = time.monotonic() - started
    duration = float(
        stream.get("duration") or input_probe.get("format", {}).get("duration", 0.0)
    )
    report = {
        "schema": "glic-video-process-v1",
        "input": str(input_path),
        "output": str(output_path),
        "preset": filter_report.get("preset", "passthrough"),
        "recipe_source": filter_report.get("recipe_source", "passthrough"),
        "canonical": args.canonical,
        "canonical_version": filter_report.get("canonical_version"),
        "strength": filter_report.get("strength", 0.0),
        "effect_family": filter_report.get("effect_family", "passthrough"),
        "effect_amount": filter_report.get("effect_amount", 0.0),
        "effect_scale": filter_report.get("effect_scale", 0.0),
        "effect_rate": filter_report.get("effect_rate", 0.0),
        "seed": filter_report.get("seed", 0),
        "backend_requested": "passthrough" if args.passthrough else args.backend,
        "encoder": encoder_name,
        "elapsed_seconds": round(elapsed, 3),
        "source_duration_seconds": duration,
        "end_to_end_realtime_factor": round(duration / elapsed, 3)
        if elapsed
        else 0.0,
        "filter": filter_report,
        "input_probe": input_probe,
        "output_probe": output_probe,
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"output={output_path} frames={filter_report['frames']} "
        f"processing_fps={filter_report['processing_fps']:.3f} "
        f"elapsed={elapsed:.3f}s report={report_path}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
