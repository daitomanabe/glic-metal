#!/usr/bin/env python3
"""Isolated encoded-packet glitching with tolerant salvage decode."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import resource
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

CODECS = ("h264", "hevc", "av1", "vp9", "prores")
EFFECTS = (
    "packet_bit_rot",
    "gop_amputation",
    "packet_dropout_score",
    "timestamp_fracture",
    "nal_obu_surgery",
    "header_hallucination",
    "packet_transplant",
    "vp9_superframe_shuffle",
)
SUPPORT = {
    "packet_bit_rot": set(CODECS),
    "gop_amputation": {"h264", "hevc", "av1", "vp9"},
    "packet_dropout_score": set(CODECS),
    "timestamp_fracture": set(CODECS),
    "nal_obu_surgery": {"h264", "hevc", "av1", "vp9"},
    "header_hallucination": {"h264", "hevc", "av1", "vp9"},
    "packet_transplant": set(CODECS),
    "vp9_superframe_shuffle": {"vp9"},
}


@dataclass
class ProcessResult:
    command: list[str]
    return_code: int
    elapsed_seconds: float
    timed_out: bool
    log: Path


def require_tool(name: str) -> str:
    candidate = Path(name).expanduser()
    if candidate.parent != Path(".") or "/" in name:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        raise RuntimeError(f"Required executable was not found: {candidate}")
    resolved = shutil.which(name)
    if resolved is None:
        raise RuntimeError(f"Required executable was not found: {name}")
    return resolved


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _limit_child(cpu_seconds: int, maximum_file_bytes: int) -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(
        resource.RLIMIT_FSIZE, (maximum_file_bytes, maximum_file_bytes)
    )
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))


def run_isolated(
    command: list[str],
    *,
    log: Path,
    timeout_seconds: int,
    maximum_file_bytes: int,
) -> ProcessResult:
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timed_out = False
    with log.open("w") as output:
        output.write("+ " + shlex.join(command) + "\n")
        output.flush()
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=lambda: _limit_child(
                # RLIMIT_CPU is accumulated CPU time, not wall time. Codec
                # processes can legitimately consume several CPU-seconds per
                # elapsed second when row/tile workers are enabled. The wall
                # timeout below remains the authoritative runaway guard.
                max(
                    2,
                    (timeout_seconds + 5)
                    * min(max(os.cpu_count() or 1, 1), 16),
                ),
                maximum_file_bytes,
            ),
        )
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            return_code = process.wait()
            output.write(
                f"\nprocess exceeded {timeout_seconds}s and was terminated\n"
            )
    return ProcessResult(
        command=command,
        return_code=return_code,
        elapsed_seconds=time.monotonic() - started,
        timed_out=timed_out,
        log=log,
    )


def run_json(command: list[str]) -> dict:
    result = subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=30
    )
    return json.loads(result.stdout)


def probe_video(ffprobe: str, path: Path) -> dict:
    return run_json(
        [
            ffprobe,
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,codec_tag_string,width,height,avg_frame_rate,"
            "nb_frames,nb_read_frames,pix_fmt:format=duration",
            "-of",
            "json",
            str(path),
        ]
    )


def safe_probe(ffprobe: str, path: Path) -> dict:
    if not path.is_file() or path.stat().st_size == 0:
        return {}
    try:
        return probe_video(ffprobe, path)
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return {}


def frame_count(probe: dict) -> int:
    streams = probe.get("streams", [])
    if not streams:
        return 0
    for key in ("nb_read_frames", "nb_frames"):
        try:
            count = int(streams[0].get(key, 0))
        except (TypeError, ValueError):
            continue
        if count > 0:
            return count
    return 0


def parse_rate(value: object) -> float:
    try:
        text = str(value)
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            result = float(numerator) / float(denominator)
        else:
            result = float(text)
    except (ValueError, ZeroDivisionError):
        return 0.0
    return result if math.isfinite(result) and result > 0 else 0.0


def encoder_options(codec: str, fps: int, threads: int) -> list[str]:
    gop = max(8, fps)
    if codec == "h264":
        return [
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-g",
            str(gop),
            "-keyint_min",
            str(gop),
            "-sc_threshold",
            "0",
            "-bf",
            "2",
        ]
    if codec == "hevc":
        return [
            "-c:v",
            "libx265",
            "-preset",
            "fast",
            "-crf",
            "24",
            "-x265-params",
            f"log-level=error:keyint={gop}:min-keyint={gop}:scenecut=0:bframes=2",
        ]
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
            "34",
            "-b:v",
            "0",
            "-g",
            str(gop),
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
            "36",
            "-b:v",
            "0",
            "-g",
            str(gop),
        ]
    if codec == "prores":
        return ["-c:v", "prores_ks", "-profile:v", "2"]
    raise RuntimeError(f"Unsupported codec: {codec}")


def effect_bsf(effect: str, codec: str, amount: float, seed: int) -> str:
    if codec not in SUPPORT[effect]:
        raise RuntimeError(f"{effect} does not support codec {codec}")
    if effect == "packet_bit_rot":
        interval = max(12, round(360 - amount * 320))
        phase = seed % interval
        return (
            "noise=amount="
            f"'if(key,-1,if(eq(mod(n,{interval}),{phase}),1,-1))'"
        )
    if effect == "gop_amputation":
        return "noise=drop='key*gt(n,0)'"
    if effect == "packet_dropout_score":
        period = max(4, round(14 - amount * 9))
        phase = seed % period
        return f"noise=drop='not(key)*eq(mod(n,{period}),{phase})'"
    if effect == "timestamp_fracture":
        period = max(5, round(11 - amount * 5))
        offset = max(1, round(1 + amount * 4))
        first = seed % period
        second = (first + period // 2) % period
        return (
            "setts=pts="
            f"'PTS+if(eq(mod(N,{period}),{first}),{offset}*DURATION,"
            f"if(eq(mod(N,{period}),{second}),-{offset}*DURATION,0))'"
        )
    if effect == "nal_obu_surgery":
        if codec == "h264":
            return "filter_units=remove_types=1"
        if codec == "hevc":
            return "filter_units=remove_types=0|1"
        return "filter_units=discard=nonref:discard_flags=keep_non_vcl"
    if effect == "header_hallucination":
        if codec == "h264":
            return (
                "h264_metadata=sample_aspect_ratio=4/3:"
                "video_full_range_flag=1:colour_primaries=9:"
                "transfer_characteristics=16:matrix_coefficients=9"
            )
        if codec == "hevc":
            return (
                "hevc_metadata=sample_aspect_ratio=4/3:"
                "video_full_range_flag=1:colour_primaries=9:"
                "transfer_characteristics=16:matrix_coefficients=9"
            )
        if codec == "av1":
            return (
                "av1_metadata=color_primaries=9:"
                "transfer_characteristics=16:matrix_coefficients=9:"
                "color_range=pc"
            )
        return "vp9_metadata=color_space=bt2020:color_range=pc"
    if effect == "packet_transplant":
        return ""
    if effect == "vp9_superframe_shuffle":
        offset = max(1, round(1 + amount * 3))
        return (
            "vp9_superframe_split,"
            "setts=pts="
            f"'PTS+if(eq(mod(N,4),1),{offset}*DURATION,"
            f"if(eq(mod(N,4),3),-{offset}*DURATION,0))'"
        )
    raise RuntimeError(f"Unknown effect: {effect}")


def preview_encoder_options(ffmpeg: str) -> list[str]:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    encoders = result.stdout + result.stderr
    if "h264_videotoolbox" in encoders:
        return ["-c:v", "h264_videotoolbox", "-b:v", "16M"]
    if "libx264" in encoders:
        return ["-c:v", "libx264", "-preset", "fast", "-crf", "18"]
    raise RuntimeError(
        "No H.264 preview encoder is available (h264_videotoolbox or libx264)"
    )


def result_json(result: ProcessResult) -> dict:
    return {
        "command": result.command,
        "return_code": result.return_code,
        "elapsed_seconds": round(result.elapsed_seconds, 6),
        "timed_out": result.timed_out,
        "log": str(result.log),
    }


def count_decoder_diagnostics(path: Path) -> int:
    if not path.is_file():
        return 0
    terms = ("error", "invalid", "corrupt", "missing", "no frame", "failed")
    return sum(
        any(term in line.lower() for term in terms)
        for line in path.read_text(errors="replace").splitlines()
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create intentionally damaged codec packets in an isolated "
            "subprocess, salvage decodable frames, and write a full report."
        )
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--codec", choices=CODECS, required=True)
    parser.add_argument("--effect", choices=EFFECTS, required=True)
    parser.add_argument(
        "--donor",
        type=Path,
        help="Second video required by packet_transplant.",
    )
    parser.add_argument("--amount", type=float, default=0.65)
    parser.add_argument("--seed", type=lambda value: int(value, 0), default=0x474C4943)
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--max-frames", type=int, default=180)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--maximum-file-mib", type=int, default=2048)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()
    args.input = args.input.expanduser().resolve()
    args.output = args.output.expanduser().resolve()
    if not args.input.is_file():
        parser.error(f"input does not exist: {args.input}")
    if args.codec not in SUPPORT[args.effect]:
        parser.error(
            f"{args.effect} supports: {', '.join(sorted(SUPPORT[args.effect]))}"
        )
    if args.effect == "packet_transplant":
        if args.donor is None:
            parser.error("--donor is required for packet_transplant")
        args.donor = args.donor.expanduser().resolve()
        if not args.donor.is_file():
            parser.error(f"donor does not exist: {args.donor}")
    if not 0.0 <= args.amount <= 1.0:
        parser.error("--amount must be between 0 and 1")
    if args.width < 2 or args.height < 2 or args.width % 2 or args.height % 2:
        parser.error("--width and --height must be positive even values")
    if min(args.fps, args.max_frames, args.threads, args.timeout) < 1:
        parser.error("--fps, --max-frames, --threads, and --timeout must be positive")
    if args.maximum_file_mib < 16:
        parser.error("--maximum-file-mib must be at least 16")
    return args


def main() -> int:
    args = parse_args()
    ffmpeg = require_tool(args.ffmpeg)
    ffprobe = require_tool(args.ffprobe)
    report = (
        args.report.expanduser().resolve()
        if args.report
        else args.output.with_suffix(args.output.suffix + ".json")
    )
    work = (
        args.work_dir.expanduser().resolve()
        if args.work_dir
        else args.output.with_suffix(args.output.suffix + ".packet-stages")
    )
    work.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    maximum_file_bytes = args.maximum_file_mib * 1024 * 1024

    encoded = work / f"source-{args.codec}.mkv"
    damaged = work / f"damaged-{args.effect}-{args.codec}.mkv"
    salvaged = work / f"salvaged-{args.effect}-{args.codec}.mkv"
    encode_command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(args.input),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        f"scale={args.width}:{args.height}:flags=lanczos,"
        f"fps={args.fps},format=yuv420p",
        "-frames:v",
        str(args.max_frames),
        *encoder_options(args.codec, args.fps, args.threads),
        str(encoded),
    ]
    encode_result = run_isolated(
        encode_command,
        log=work / "01-encode.log",
        timeout_seconds=args.timeout,
        maximum_file_bytes=maximum_file_bytes,
    )
    if encode_result.return_code != 0 or not encoded.is_file():
        raise RuntimeError(f"source encode failed; see {encode_result.log}")

    bsf = effect_bsf(args.effect, args.codec, args.amount, args.seed)
    transplant_results: list[ProcessResult] = []
    donor_encoded: Path | None = None
    if args.effect == "packet_transplant":
        donor_encoded = work / f"donor-{args.codec}.mkv"
        donor_command = encode_command.copy()
        donor_command[donor_command.index(str(args.input))] = str(args.donor)
        donor_command[-1] = str(donor_encoded)
        donor_result = run_isolated(
            donor_command,
            log=work / "02a-donor-encode.log",
            timeout_seconds=args.timeout,
            maximum_file_bytes=maximum_file_bytes,
        )
        transplant_results.append(donor_result)
        if donor_result.return_code != 0 or not donor_encoded.is_file():
            raise RuntimeError(f"donor encode failed; see {donor_result.log}")

        duration = args.max_frames / args.fps
        head_seconds = duration * 0.38
        donor_seconds = duration * (0.18 + args.amount * 0.24)
        tail_start = min(duration * 0.82, head_seconds + donor_seconds)
        segments = (
            (encoded, 0.0, head_seconds, work / "transplant-source-head.mkv"),
            (
                donor_encoded,
                head_seconds,
                donor_seconds,
                work / "transplant-donor-mid.mkv",
            ),
            (
                encoded,
                tail_start,
                max(0.1, duration - tail_start),
                work / "transplant-source-tail.mkv",
            ),
        )
        for index, (source, start, length, destination) in enumerate(segments):
            segment_command = [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-y",
                "-ss",
                f"{start:.6f}",
                "-i",
                str(source),
                "-t",
                f"{length:.6f}",
                "-map",
                "0:v:0",
                "-an",
                "-c:v",
                "copy",
                str(destination),
            ]
            segment_result = run_isolated(
                segment_command,
                log=work / f"02b-segment-{index}.log",
                timeout_seconds=args.timeout,
                maximum_file_bytes=maximum_file_bytes,
            )
            transplant_results.append(segment_result)
            if segment_result.return_code != 0 or not destination.is_file():
                raise RuntimeError(
                    f"packet transplant segment failed; see {segment_result.log}"
                )
        concat_list = work / "packet-transplant.ffconcat"
        concat_list.write_text(
            "ffconcat version 1.0\n"
            + "\n".join(f"file {segment[3]}" for segment in segments)
            + "\n"
        )
        mutate_command = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
            str(damaged),
        ]
    else:
        mutate_command = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            str(encoded),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
            "-bsf:v",
            bsf,
            str(damaged),
        ]
    mutate_result = run_isolated(
        mutate_command,
        log=work / "02-mutate.log",
        timeout_seconds=args.timeout,
        maximum_file_bytes=maximum_file_bytes,
    )

    if args.effect in {"timestamp_fracture", "vp9_superframe_shuffle"}:
        # Materialize the damaged packet timestamps as repeated/dropped frames
        # before resetting the preview to a stable CFR timeline.
        salvage_filter = (
            f"fps={args.fps},setpts=N/({args.fps}*TB),format=yuv420p"
        )
    else:
        salvage_filter = (
            f"setpts=N/({args.fps}*TB),fps={args.fps},format=yuv420p"
        )
    decode_command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-err_detect",
        "ignore_err",
        "-fflags",
        "+discardcorrupt+genpts",
        "-threads",
        "1",
        "-i",
        str(damaged),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        salvage_filter,
        "-c:v",
        "ffv1",
        str(salvaged),
    ]
    decode_result = run_isolated(
        decode_command,
        log=work / "03-salvage-decode.log",
        timeout_seconds=args.timeout,
        maximum_file_bytes=maximum_file_bytes,
    )

    source_probe = safe_probe(ffprobe, encoded)
    damaged_probe = safe_probe(ffprobe, damaged)
    salvage_probe = safe_probe(ffprobe, salvaged)
    source_frames = frame_count(source_probe)
    salvaged_frames = frame_count(salvage_probe)
    survival = (
        min(1.0, salvaged_frames / source_frames) if source_frames > 0 else 0.0
    )
    preview_result: ProcessResult | None = None
    if salvaged_frames > 0:
        preview_options = preview_encoder_options(ffmpeg)
        preview_command = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(salvaged),
            "-i",
            str(args.input),
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
            *preview_options,
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(args.output),
        ]
        preview_result = run_isolated(
            preview_command,
            log=work / "04-preview.log",
            timeout_seconds=args.timeout,
            maximum_file_bytes=maximum_file_bytes,
        )

    output_probe = safe_probe(ffprobe, args.output)
    output_streams = output_probe.get("streams", [])
    output_fps = (
        parse_rate(output_streams[0].get("avg_frame_rate"))
        if output_streams
        else 0.0
    )
    report_data = {
        "schema": "glic-offline-packet-glitch-v1",
        "execution_class": "offline",
        "realtime_certified": False,
        "input": str(args.input),
        "donor": str(args.donor) if args.donor else None,
        "output": str(args.output),
        "codec": args.codec,
        "effect": args.effect,
        "amount": args.amount,
        "seed": args.seed,
        "width": args.width,
        "height": args.height,
        "target_fps": args.fps,
        "source_frames": source_frames,
        "salvaged_frames": salvaged_frames,
        "decode_survival_ratio": round(survival, 6),
        "decoder_diagnostics": count_decoder_diagnostics(decode_result.log),
        "may_produce_invalid_bitstream": args.effect
        in {
            "packet_bit_rot",
            "gop_amputation",
            "nal_obu_surgery",
            "packet_transplant",
        },
        "lookahead_frames": 4
        if args.effect in {"timestamp_fracture", "vp9_superframe_shuffle"}
        else 0,
        "qualified_preview": bool(
            salvaged_frames >= 2
            and args.output.is_file()
            and preview_result is not None
            and preview_result.return_code == 0
        ),
        "isolation": {
            "subprocess": True,
            "timeout_seconds": args.timeout,
            "maximum_file_mib": args.maximum_file_mib,
            "decoder_threads": 1,
        },
        "bitstream_filter": bsf,
        "source_bitstream": {
            "path": str(encoded),
            "bytes": encoded.stat().st_size,
            "sha256": sha256(encoded),
            "probe": source_probe,
        },
        "donor_bitstream": {
            "path": str(donor_encoded),
            "bytes": donor_encoded.stat().st_size,
            "sha256": sha256(donor_encoded),
            "probe": safe_probe(ffprobe, donor_encoded),
        }
        if donor_encoded is not None
        else None,
        "damaged_bitstream": {
            "path": str(damaged),
            "bytes": damaged.stat().st_size if damaged.is_file() else 0,
            "sha256": sha256(damaged) if damaged.is_file() else None,
            "probe": damaged_probe,
        },
        "processes": {
            "encode": result_json(encode_result),
            "mutate": result_json(mutate_result),
            "transplant_prepare": [
                result_json(result) for result in transplant_results
            ],
            "salvage_decode": result_json(decode_result),
            "preview": result_json(preview_result) if preview_result else None,
        },
        "output_fps": round(output_fps, 6),
        "output_probe": output_probe,
    }
    report.write_text(json.dumps(report_data, indent=2) + "\n")
    print(
        f"effect={args.effect} codec={args.codec} "
        f"survival={survival:.3f} frames={salvaged_frames}/{source_frames} "
        f"qualified={report_data['qualified_preview']} report={report}"
    )
    return 0 if report_data["qualified_preview"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
