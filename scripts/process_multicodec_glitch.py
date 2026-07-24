#!/usr/bin/env python3
"""Actual encode/decode glitch loops for native and research video codecs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

CODECS = (
    "av1",
    "av2",
    "hevc",
    "vp9",
    "prores",
    "vvc",
    "theora",
    "dirac",
)
SOFTWARE_EFFECTS = (
    "generation_cascade",
    "temporal_echo",
    "chroma_drift",
    "residual_noise",
)
NATIVE_EFFECT_MAP = {
    "generation_cascade": "generation_cascade",
    "temporal_echo": "temporal_polyphony",
    "chroma_drift": "chroma_codec_echo",
    "residual_noise": "codec_grain_synth",
}
AVM_VERSION = "1.0.0"
AVM_COMMIT = "966a7d7cd6fcf60360caf5dc413b2aeeb65e144d"
VVENC_VERSION = "1.14.0"
VVENC_COMMIT = "9428ea8636ae7f443ecde89999d16b2dfc421524"


def require_tool(value: str) -> str:
    candidate = Path(value).expanduser()
    if candidate.parent != Path(".") or "/" in value:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        raise RuntimeError(f"Required executable was not found: {candidate}")
    resolved = shutil.which(value)
    if resolved is None:
        raise RuntimeError(f"Required executable was not found: {value}")
    return resolved


def run(command: list[str], *, quiet: bool = False) -> float:
    if not quiet:
        print("+", " ".join(command), flush=True)
    started = time.monotonic()
    subprocess.run(command, check=True)
    return time.monotonic() - started


def run_json(command: list[str]) -> dict:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_video(ffprobe: str, path: Path, *, count_frames: bool = False) -> dict:
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
    ]
    if count_frames:
        command.append("-count_frames")
    command.extend(
        [
            "-show_entries",
            "stream=codec_name,codec_tag_string,width,height,avg_frame_rate,"
            "nb_frames,nb_read_frames,pix_fmt:format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    return run_json(command)


def parse_rate(value: object) -> float:
    text = str(value or "0")
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            result = float(numerator) / float(denominator)
        else:
            result = float(text)
    except (ValueError, ZeroDivisionError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def frame_count(probe: dict) -> int:
    streams = probe.get("streams", [])
    if not streams:
        return 0
    stream = streams[0]
    for key in ("nb_read_frames", "nb_frames"):
        try:
            value = int(stream.get(key, 0))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def effect_filter(effect: str, amount: float, generation: int) -> str:
    if effect == "generation_cascade":
        return "null"
    if effect == "temporal_echo":
        far = 0.15 + amount * 0.70
        near = 0.35 + amount * 0.55
        return f"tmix=frames=3:weights=1 {near:.3f} {far:.3f}"
    if effect == "chroma_drift":
        shift = max(1, round((4 + generation * 3) * amount))
        return f"chromashift=cbh={shift}:crv={-shift}"
    if effect == "residual_noise":
        strength = max(1, round(4 + amount * 28))
        return f"noise=alls={strength}:allf=t+u"
    raise RuntimeError(f"Unknown software codec effect: {effect}")


def quality_for(codec: str, amount: float, generation: int) -> int:
    damage = min(1.0, amount * (0.62 + generation * 0.18))
    if codec == "av2":
        return round(36 + damage * 180)
    if codec == "theora":
        return round(8 - damage * 7)
    if codec == "dirac":
        # VC-2 may legally encode but collapse every slice to neutral grey
        # below its practical slice budget. Keep enough headroom for repeated
        # generations while still allowing visible wavelet loss.
        return max(10_000_000, round(15_000_000 - damage * 13_000_000))
    return round(20 + damage * 38)


def ffmpeg_encoder(codec: str, quality: int, fps: int, threads: int) -> list[str]:
    if codec == "av1":
        return [
            "-c:v",
            "libaom-av1",
            "-cpu-used",
            "8",
            "-row-mt",
            "1",
            "-threads",
            str(threads),
            "-crf",
            str(min(63, quality)),
            "-b:v",
            "0",
            "-g",
            str(fps * 2),
        ]
    if codec == "vp9":
        return [
            "-c:v",
            "libvpx-vp9",
            "-deadline",
            "realtime",
            "-cpu-used",
            "8",
            "-row-mt",
            "1",
            "-threads",
            str(threads),
            "-crf",
            str(min(63, quality)),
            "-b:v",
            "0",
            "-g",
            str(fps * 2),
        ]
    if codec == "theora":
        return [
            "-c:v",
            "libtheora",
            "-q:v",
            str(max(0, min(10, quality))),
            "-g",
            str(fps * 2),
        ]
    if codec == "dirac":
        return [
            "-c:v",
            "vc2",
            "-b:v",
            str(max(250_000, quality)),
            "-g",
            "1",
        ]
    raise RuntimeError(f"FFmpeg encoder is not defined for {codec}")


def ffmpeg_container(codec: str) -> tuple[str, str]:
    if codec == "av1":
        return ".webm", "libdav1d"
    if codec == "vp9":
        return ".webm", "libvpx-vp9"
    if codec == "theora":
        return ".ogv", "theora"
    if codec == "dirac":
        return ".mkv", "dirac"
    raise RuntimeError(f"FFmpeg container is not defined for {codec}")


def codec_working_dimensions(codec: str, width: int, height: int) -> tuple[int, int]:
    if codec == "dirac":
        # FFmpeg's VC-2 encoder uses 32x16 slices by default. Partial slices at
        # the right/bottom edge can decode as a flat neutral frame, so pad the
        # lossless working raster and scale the public preview back to the
        # requested dimensions.
        return ((width + 31) // 32 * 32, (height + 15) // 16 * 16)
    return width, height


def process_native(args: argparse.Namespace, root: Path, report: Path) -> dict:
    process_video = root / "scripts" / "process_video.py"
    native_codec = "hevc" if args.codec == "hevc" else "prores_422"
    native_effect = NATIVE_EFFECT_MAP[args.effect]
    command = [
        sys.executable,
        str(process_video),
        str(args.input),
        str(args.output),
        "--processing-mode",
        "codec_glitch",
        "--codec-format",
        native_codec,
        "--codec-effect",
        native_effect,
        "--codec-amount",
        str(args.amount),
        "--codec-rate",
        str(args.rate),
        "--codec-feedback",
        str(args.feedback),
        "--codec-generations",
        str(max(2, args.generations)),
        "--width",
        str(args.width),
        "--height",
        str(args.height),
        "--fps",
        str(args.fps),
        "--report",
        str(report),
        "--overwrite",
    ]
    if args.max_frames > 0:
        raise RuntimeError("--max-frames is only available for AV1/AV2/VP9")
    run(command)
    result = json.loads(report.read_text())
    result["schema"] = "glic-multicodec-glitch-v1"
    result["requested_codec"] = args.codec
    result["codec_generation_count"] = (
        max(2, args.generations) if args.effect == "generation_cascade" else 1
    )
    result["codec_toolchain"] = {
        "backend": "VideoToolbox",
        "encode_decode_codec": native_codec,
        "preview_codec": result.get("encoder"),
    }
    report.write_text(json.dumps(result, indent=2) + "\n")
    return result


def normalize_input(
    ffmpeg: str,
    source: Path,
    destination: Path,
    width: int,
    height: int,
    fps: int,
    max_frames: int,
) -> float:
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-an",
        "-vf",
        f"scale={width}:{height}:flags=lanczos,fps={fps},format=yuv420p",
    ]
    if max_frames > 0:
        command.extend(["-frames:v", str(max_frames)])
    command.extend(["-c:v", "ffv1", str(destination)])
    return run(command)


def process_ffmpeg_codec(
    args: argparse.Namespace,
    ffmpeg: str,
    ffprobe: str,
    work: Path,
) -> tuple[Path, list[dict]]:
    current = work / "normalized.mkv"
    working_width, working_height = codec_working_dimensions(
        args.codec, args.width, args.height
    )
    normalize_input(
        ffmpeg,
        args.input,
        current,
        working_width,
        working_height,
        args.fps,
        args.max_frames,
    )
    stages: list[dict] = []
    for generation in range(1, args.generations + 1):
        extension, decoder = ffmpeg_container(args.codec)
        bitstream = work / (
            f"{args.codec}-generation-{generation:02d}{extension}"
        )
        decoded = work / f"{args.codec}-decoded-{generation:02d}.mkv"
        quality = quality_for(args.codec, args.amount, generation)
        encode_command = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(current),
            "-an",
            "-pix_fmt",
            "yuv420p",
            *ffmpeg_encoder(args.codec, quality, args.fps, args.threads),
            str(bitstream),
        ]
        encode_seconds = run(encode_command)
        decode_command = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-c:v",
            decoder,
            "-i",
            str(bitstream),
            "-an",
            "-vf",
            effect_filter(args.effect, args.amount, generation),
            "-c:v",
            "ffv1",
            str(decoded),
        ]
        decode_seconds = run(decode_command)
        stream_probe = probe_video(ffprobe, bitstream, count_frames=True)
        stages.append(
            {
                "generation": generation,
                "bitstream": str(bitstream),
                "bitstream_bytes": bitstream.stat().st_size,
                "bitstream_sha256": sha256(bitstream),
                "quality": quality,
                "encoder": ffmpeg_encoder(
                    args.codec, quality, args.fps, args.threads
                )[1],
                "decoder": decoder,
                "encode_seconds": round(encode_seconds, 6),
                "decode_seconds": round(decode_seconds, 6),
                "probe": stream_probe,
            }
        )
        current = decoded
    return current, stages


def process_vvc(
    args: argparse.Namespace,
    ffmpeg: str,
    ffprobe: str,
    vvencapp: str,
    work: Path,
) -> tuple[Path, list[dict]]:
    current = work / "normalized.mkv"
    normalize_input(
        ffmpeg,
        args.input,
        current,
        args.width,
        args.height,
        args.fps,
        args.max_frames,
    )
    version = subprocess.run(
        [vvencapp, "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    version_text = (version.stdout + version.stderr).strip()
    stages: list[dict] = []
    for generation in range(1, args.generations + 1):
        y4m_input = work / f"vvc-input-{generation:02d}.y4m"
        bitstream = work / f"vvc-generation-{generation:02d}.266"
        decoded = work / f"vvc-decoded-{generation:02d}.mkv"
        run(
            [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(current),
                "-an",
                "-vf",
                effect_filter(args.effect, args.amount, generation),
                "-pix_fmt",
                "yuv420p",
                "-f",
                "yuv4mpegpipe",
                str(y4m_input),
            ]
        )
        quality = min(63, quality_for("vvc", args.amount, generation))
        encode_command = [
            vvencapp,
            "--preset",
            "faster",
            "-i",
            str(y4m_input),
            "--fps",
            f"{args.fps}/1",
            "-q",
            str(quality),
            "-t",
            str(args.threads),
            "--sdr",
            "sdr_709",
            "-o",
            str(bitstream),
        ]
        encode_seconds = run(encode_command)
        decode_seconds = run(
            [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-c:v",
                "vvc",
                "-i",
                str(bitstream),
                "-an",
                "-c:v",
                "ffv1",
                str(decoded),
            ]
        )
        stages.append(
            {
                "generation": generation,
                "bitstream": str(bitstream),
                "bitstream_bytes": bitstream.stat().st_size,
                "bitstream_sha256": sha256(bitstream),
                "quality_qp": quality,
                "encoder": "Fraunhofer VVenC vvencapp",
                "decoder": "FFmpeg vvc",
                "vvenc_version": VVENC_VERSION,
                "vvenc_commit": VVENC_COMMIT,
                "vvenc_version_output": version_text,
                "encode_seconds": round(encode_seconds, 6),
                "decode_seconds": round(decode_seconds, 6),
                "probe": probe_video(ffprobe, bitstream, count_frames=True),
            }
        )
        current = decoded
    return current, stages


def process_av2(
    args: argparse.Namespace,
    ffmpeg: str,
    ffprobe: str,
    avmenc: str,
    avmdec: str,
    work: Path,
) -> tuple[Path, list[dict]]:
    current = work / "normalized.mkv"
    normalize_input(
        ffmpeg,
        args.input,
        current,
        args.width,
        args.height,
        args.fps,
        args.max_frames,
    )
    stages: list[dict] = []
    for generation in range(1, args.generations + 1):
        y4m_input = work / f"av2-input-{generation:02d}.y4m"
        bitstream = work / f"av2-generation-{generation:02d}.ivf"
        decoded = work / f"av2-decoded-{generation:02d}.y4m"
        to_y4m = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(current),
            "-an",
            "-vf",
            effect_filter(args.effect, args.amount, generation),
            "-pix_fmt",
            "yuv420p",
            "-f",
            "yuv4mpegpipe",
            str(y4m_input),
        ]
        run(to_y4m)
        quality = quality_for("av2", args.amount, generation)
        encode_command = [
            avmenc,
            "--codec=av2",
            "--ivf",
            "--good",
            "--cpu-used=9",
            f"--threads={args.threads}",
            "--end-usage=q",
            f"--qp={quality}",
            "--passes=1",
            "--quiet",
            "-o",
            str(bitstream),
            str(y4m_input),
        ]
        encode_seconds = run(encode_command)
        decode_command = [
            avmdec,
            "--codec=av2",
            f"--threads={args.threads}",
            "-o",
            str(decoded),
            str(bitstream),
        ]
        decode_seconds = run(decode_command)
        header = bitstream.read_bytes()[:12]
        if len(header) < 12 or header[:4] != b"DKIF":
            raise RuntimeError("AV2 encoder did not produce a valid IVF header")
        stages.append(
            {
                "generation": generation,
                "bitstream": str(bitstream),
                "bitstream_bytes": bitstream.stat().st_size,
                "bitstream_sha256": sha256(bitstream),
                "ivf_fourcc": header[8:12].decode("ascii", errors="replace"),
                "quality_qp": quality,
                "encoder": "avmenc",
                "decoder": "avmdec",
                "avm_version": AVM_VERSION,
                "avm_commit": AVM_COMMIT,
                "encode_seconds": round(encode_seconds, 6),
                "decode_seconds": round(decode_seconds, 6),
                "decoded_probe": probe_video(ffprobe, decoded, count_frames=True),
            }
        )
        current = decoded
    return current, stages


def create_preview(
    args: argparse.Namespace, ffmpeg: str, current: Path
) -> tuple[str, float]:
    encoders = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if "h264_videotoolbox" in encoders:
        preview_codec = "h264_videotoolbox"
        video_options = ["-c:v", preview_codec, "-b:v", "16M"]
    else:
        preview_codec = "libx264"
        video_options = ["-c:v", preview_codec, "-crf", "16", "-preset", "fast"]
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(current),
        "-i",
        str(args.input),
        "-map",
        "0:v:0",
        "-map",
        "1:a?",
        "-vf",
        f"scale={args.width}:{args.height}:flags=lanczos",
        *video_options,
        "-c:a",
        "aac",
        "-shortest",
        "-movflags",
        "+faststart",
        str(args.output),
    ]
    return preview_codec, run(command)


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description=(
            "Run real codec encode/decode glitch generations. HEVC and ProRes "
            "use VideoToolbox; AV1/VP9/Theora/Dirac use FFmpeg; AV2 uses AVM; "
            "VVC uses the official Fraunhofer VVenC tool."
        )
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--codec", choices=CODECS, required=True)
    parser.add_argument("--effect", choices=SOFTWARE_EFFECTS, default="generation_cascade")
    parser.add_argument("--generations", type=int, default=2)
    parser.add_argument("--amount", type=float, default=0.65)
    parser.add_argument("--rate", type=float, default=0.45)
    parser.add_argument("--feedback", type=float, default=0.60)
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--max-frames", type=int, default=0)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument(
        "--avmenc", default=str(root / ".cache" / "avm-v1.0.0" / "build" / "avmenc")
    )
    parser.add_argument(
        "--avmdec", default=str(root / ".cache" / "avm-v1.0.0" / "build" / "avmdec")
    )
    parser.add_argument(
        "--vvencapp",
        default=str(
            root
            / ".cache"
            / "vvenc-v1.14.0"
            / "bin"
            / "release-static"
            / "vvencapp"
        ),
    )
    args = parser.parse_args()
    args.input = args.input.expanduser().resolve()
    args.output = args.output.expanduser().resolve()
    if not args.input.is_file():
        parser.error(f"input does not exist: {args.input}")
    if args.generations < 1 or args.generations > 3:
        parser.error("--generations must be between 1 and 3")
    if not 0.0 <= args.amount <= 1.0:
        parser.error("--amount must be between 0 and 1")
    if args.width < 2 or args.height < 2 or args.width % 2 or args.height % 2:
        parser.error("--width and --height must be positive even values")
    if args.fps < 1 or args.threads < 1 or args.max_frames < 0:
        parser.error("--fps/--threads must be positive and --max-frames nonnegative")
    return args


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    report = (
        args.report.expanduser().resolve()
        if args.report
        else args.output.with_suffix(args.output.suffix + ".json")
    )
    work = (
        args.work_dir.expanduser().resolve()
        if args.work_dir
        else args.output.with_suffix(args.output.suffix + ".codec-stages")
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    if args.codec in ("hevc", "prores"):
        result = process_native(args, root, report)
        print(
            f"output={args.output} codec={args.codec} "
            f"backend={result['codec_toolchain']['backend']} report={report}"
        )
        return 0

    ffmpeg = require_tool(args.ffmpeg)
    ffprobe = require_tool(args.ffprobe)
    started = time.monotonic()
    if args.codec == "av2":
        avmenc = require_tool(args.avmenc)
        avmdec = require_tool(args.avmdec)
        current, stages = process_av2(
            args, ffmpeg, ffprobe, avmenc, avmdec, work
        )
        backend = "AOMedia AVM reference v1.0.0"
    elif args.codec == "vvc":
        vvencapp = require_tool(args.vvencapp)
        current, stages = process_vvc(
            args, ffmpeg, ffprobe, vvencapp, work
        )
        backend = "Fraunhofer VVenC reference v1.14.0 + FFmpeg vvc decoder"
    else:
        current, stages = process_ffmpeg_codec(args, ffmpeg, ffprobe, work)
        backend = {
            "av1": "FFmpeg libaom/libdav1d",
            "vp9": "FFmpeg libvpx",
            "theora": "FFmpeg libtheora/theora",
            "dirac": "FFmpeg VC-2/Dirac",
        }[args.codec]
    preview_codec, preview_seconds = create_preview(args, ffmpeg, current)
    output_probe = probe_video(ffprobe, args.output, count_frames=True)
    frames = frame_count(output_probe)
    elapsed = time.monotonic() - started
    output_stream = (output_probe.get("streams") or [{}])[0]
    output_fps = parse_rate(output_stream.get("avg_frame_rate"))
    result = {
        "schema": "glic-multicodec-glitch-v1",
        "input": str(args.input),
        "output": str(args.output),
        "requested_codec": args.codec,
        "codec_backend": backend,
        "effect": args.effect,
        "amount": args.amount,
        "generations": args.generations,
        "width": args.width,
        "height": args.height,
        "target_fps": args.fps,
        "max_frames": args.max_frames,
        "processed_frames": frames,
        "output_fps": round(output_fps, 6),
        "elapsed_seconds": round(elapsed, 6),
        "observed_processing_fps": round(frames / elapsed, 6) if elapsed else 0.0,
        "realtime_20fps_passed": bool(
            frames >= 120 and frames / elapsed >= 20.0 and output_fps >= 20.0
        )
        if elapsed
        else False,
        "realtime_30fps_passed": bool(
            frames >= 120 and frames / elapsed >= 30.0 and output_fps >= 30.0
        )
        if elapsed
        else False,
        "preview_codec": preview_codec,
        "preview_encode_seconds": round(preview_seconds, 6),
        "work_dir": str(work),
        "stages": stages,
        "output_probe": output_probe,
    }
    report.write_text(json.dumps(result, indent=2) + "\n")
    print(
        f"output={args.output} codec={args.codec} frames={frames} "
        f"observed_fps={result['observed_processing_fps']:.3f} report={report}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
