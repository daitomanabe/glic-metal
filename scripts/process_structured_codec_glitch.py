#!/usr/bin/env python3
"""Offline field-aware AV1/H.26x structured bitstream glitch pipeline."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess

from process_offline_packet_glitch import (
    encoder_options,
    frame_count,
    preview_encoder_options,
    require_tool,
    result_json,
    run_isolated,
    safe_probe,
    sha256,
)
from structured_bitstream import (
    join_annexb,
    join_av1_obus,
    mutate_av1_field_syntax,
    mutate_temporal_layers,
    parse_av1_obus,
    split_annexb,
    transplant_annexb_units,
    transplant_av1_units,
)


EFFECTS = (
    "av1_tile_group_surgery",
    "av1_film_grain_seed_surgery",
    "av1_reference_slot_surgery",
    "temporal_layer_dropout",
    "temporal_layer_reorder",
    "cross_stream_unit_transplant",
)
CODECS = ("h264", "hevc", "av1")
SUPPORT = {
    "av1_tile_group_surgery": {"av1"},
    "av1_film_grain_seed_surgery": {"av1"},
    "av1_reference_slot_surgery": {"av1"},
    "temporal_layer_dropout": {"hevc"},
    "temporal_layer_reorder": {"hevc"},
    "cross_stream_unit_transplant": set(CODECS),
}
IMPLEMENTATION_LEVEL = {
    "av1_tile_group_surgery": "av1_trace_aligned_tile_group_obu_dropout",
    "av1_film_grain_seed_surgery": (
        "av1_trace_aligned_grain_seed_field_rewrite"
    ),
    "av1_reference_slot_surgery": (
        "av1_trace_aligned_ref_frame_idx_field_rewrite"
    ),
    "temporal_layer_dropout": "hevc_nuh_temporal_id_unit_dropout",
    "temporal_layer_reorder": "hevc_nuh_temporal_id_unit_reorder",
    "cross_stream_unit_transplant": "codec_frame_unit_transplant",
}


def raw_extension(codec: str) -> str:
    return {"h264": ".h264", "hevc": ".hevc", "av1": ".obu"}[codec]


def raw_muxer(codec: str) -> str:
    return {"h264": "h264", "hevc": "hevc", "av1": "obu"}[codec]


def av1_encoder_options(effect: str, amount: float, fps: int) -> list[str]:
    if effect == "av1_film_grain_seed_surgery":
        level = max(1, round(4 + amount * 36))
        return [
            "-c:v",
            "libsvtav1",
            "-preset",
            "8",
            "-crf",
            "34",
            "-g",
            str(max(8, fps)),
            "-svtav1-params",
            f"film-grain={level}",
        ]
    options = [
        "-c:v",
        "libaom-av1",
        "-cpu-used",
        "8",
        "-row-mt",
        "1",
        "-crf",
        "34",
        "-b:v",
        "0",
        "-g",
        str(8 if effect == "av1_tile_group_surgery" else max(8, fps)),
    ]
    if effect == "av1_tile_group_surgery":
        options.extend(["-tiles", "2x2"])
    return options


def encode_raw(
    ffmpeg: str,
    source: Path,
    destination: Path,
    codec: str,
    effect: str,
    amount: float,
    width: int,
    height: int,
    fps: int,
    max_frames: int,
    threads: int,
    timeout: int,
    maximum_file_bytes: int,
    log: Path,
) -> object:
    options = (
        av1_encoder_options(effect, amount, fps)
        if codec == "av1"
        else encoder_options(codec, fps, threads)
    )
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        f"scale={width}:{height}:flags=lanczos,"
        f"fps={fps},format=yuv420p",
        "-frames:v",
        str(max_frames),
        *options,
        "-f",
        raw_muxer(codec),
        str(destination),
    ]
    result = run_isolated(
        command,
        log=log,
        timeout_seconds=timeout,
        maximum_file_bytes=maximum_file_bytes,
    )
    if result.return_code != 0 or not destination.is_file():
        raise RuntimeError(f"{codec} raw encode failed; see {result.log}")
    return result


def trace_av1(
    ffmpeg: str,
    source: Path,
    log: Path,
    timeout: int,
    maximum_file_bytes: int,
) -> tuple[str, object]:
    result = run_isolated(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "trace",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
            "-bsf:v",
            "trace_headers",
            "-f",
            "null",
            "-",
        ],
        log=log,
        timeout_seconds=timeout,
        maximum_file_bytes=maximum_file_bytes,
    )
    if result.return_code != 0:
        raise RuntimeError(f"AV1 trace_headers failed; see {result.log}")
    return log.read_text(errors="replace"), result


def mutate_stream(
    codec: str,
    effect: str,
    source: Path,
    donor: Path | None,
    trace_text: str | None,
    amount: float,
    seed: int,
) -> tuple[bytes, dict]:
    data = source.read_bytes()
    if effect.startswith("av1_"):
        if trace_text is None:
            raise RuntimeError("AV1 field surgery requires trace_headers output")
        return mutate_av1_field_syntax(
            data, trace_text, effect, amount, seed
        )
    if effect in {"temporal_layer_dropout", "temporal_layer_reorder"}:
        units, evidence = mutate_temporal_layers(
            split_annexb(data), effect, amount, seed
        )
        return join_annexb(units), evidence
    if effect == "cross_stream_unit_transplant":
        if donor is None:
            raise RuntimeError("cross-stream transplant requires a donor")
        if codec == "av1":
            units, evidence = transplant_av1_units(
                parse_av1_obus(data),
                parse_av1_obus(donor.read_bytes()),
                amount,
                seed,
            )
            return join_av1_obus(units), evidence
        units, evidence = transplant_annexb_units(
            split_annexb(data),
            split_annexb(donor.read_bytes()),
            codec,
            amount,
            seed,
        )
        return join_annexb(units), evidence
    raise ValueError(f"unknown effect: {effect}")


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Apply field-aware AV1 or H.26x mutations and salvage a preview."
        )
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--codec", choices=CODECS, required=True)
    parser.add_argument("--effect", choices=EFFECTS, required=True)
    parser.add_argument("--donor", type=Path)
    parser.add_argument("--amount", type=float, default=0.65)
    parser.add_argument(
        "--seed", type=lambda value: int(value, 0), default=0x474C4943
    )
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--max-frames", type=int, default=180)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=180)
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
    if args.effect == "cross_stream_unit_transplant":
        if args.donor is None:
            parser.error("--donor is required for cross_stream_unit_transplant")
        args.donor = args.donor.expanduser().resolve()
        if not args.donor.is_file():
            parser.error(f"donor does not exist: {args.donor}")
    if not 0.0 <= args.amount <= 1.0:
        parser.error("--amount must be between 0 and 1")
    if args.width < 2 or args.height < 2 or args.width % 2 or args.height % 2:
        parser.error("--width and --height must be positive even values")
    if min(args.fps, args.max_frames, args.threads, args.timeout) < 1:
        parser.error("fps, max-frames, threads, and timeout must be positive")
    return args


def main() -> int:
    args = parse_args()
    ffmpeg = require_tool(args.ffmpeg)
    ffprobe = require_tool(args.ffprobe)
    work = (
        args.work_dir.expanduser().resolve()
        if args.work_dir
        else args.output.with_suffix(
            args.output.suffix + ".structured-codec-stages"
        )
    )
    report_path = (
        args.report.expanduser().resolve()
        if args.report
        else args.output.with_suffix(args.output.suffix + ".json")
    )
    work.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    maximum_file_bytes = args.maximum_file_mib * 1024 * 1024
    extension = raw_extension(args.codec)
    encoded = work / f"source-{args.codec}{extension}"
    damaged = work / f"damaged-{args.effect}-{args.codec}{extension}"

    encode_result = encode_raw(
        ffmpeg,
        args.input,
        encoded,
        args.codec,
        args.effect,
        args.amount,
        args.width,
        args.height,
        args.fps,
        args.max_frames,
        args.threads,
        args.timeout,
        maximum_file_bytes,
        work / "01-encode.log",
    )

    donor_encoded: Path | None = None
    donor_result = None
    if args.donor is not None:
        donor_encoded = work / f"donor-{args.codec}{extension}"
        donor_result = encode_raw(
            ffmpeg,
            args.donor,
            donor_encoded,
            args.codec,
            args.effect,
            args.amount,
            args.width,
            args.height,
            args.fps,
            args.max_frames,
            args.threads,
            args.timeout,
            maximum_file_bytes,
            work / "01-donor-encode.log",
        )

    trace_text = None
    trace_result = None
    if args.effect.startswith("av1_"):
        trace_text, trace_result = trace_av1(
            ffmpeg,
            encoded,
            work / "02-trace-headers.log",
            args.timeout,
            maximum_file_bytes,
        )

    mutated, mutation_evidence = mutate_stream(
        args.codec,
        args.effect,
        encoded,
        donor_encoded,
        trace_text,
        args.amount,
        args.seed,
    )
    damaged.write_bytes(mutated)

    salvaged = work / "salvaged.ffv1.mkv"
    decode_result = run_isolated(
        [
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
            f"setpts=N/({args.fps}*TB),fps={args.fps},format=yuv420p",
            "-c:v",
            "ffv1",
            str(salvaged),
        ],
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
        min(1.0, salvaged_frames / source_frames) if source_frames else 0.0
    )
    preview_result = None
    if salvaged_frames >= 2:
        preview_result = run_isolated(
            [
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
                *preview_encoder_options(ffmpeg),
                "-c:a",
                "aac",
                "-shortest",
                "-movflags",
                "+faststart",
                str(args.output),
            ],
            log=work / "04-preview.log",
            timeout_seconds=args.timeout,
            maximum_file_bytes=maximum_file_bytes,
        )

    output_probe = safe_probe(ffprobe, args.output)
    streams = output_probe.get("streams", [])
    qualified = bool(
        salvaged_frames >= 2
        and args.output.is_file()
        and preview_result is not None
        and preview_result.return_code == 0
    )
    report = {
        "schema": "glic-structured-codec-glitch-v1",
        "execution_class": "offline",
        "realtime_certified": False,
        "effect": args.effect,
        "codec": args.codec,
        "implementation_level": IMPLEMENTATION_LEVEL[args.effect],
        "input": str(args.input),
        "donor": str(args.donor) if args.donor else None,
        "output": str(args.output),
        "amount": args.amount,
        "seed": args.seed,
        "width": args.width,
        "height": args.height,
        "target_fps": args.fps,
        "output_fps": round(
            parse_rate(streams[0].get("avg_frame_rate")) if streams else 0.0,
            6,
        ),
        "source_frames": source_frames,
        "salvaged_frames": salvaged_frames,
        "decode_survival_ratio": round(survival, 6),
        "qualified_preview": qualified,
        "may_produce_invalid_bitstream": True,
        "mutation_evidence": mutation_evidence,
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
            "bytes": damaged.stat().st_size,
            "sha256": sha256(damaged),
            "probe": damaged_probe,
        },
        "processes": {
            "encode": result_json(encode_result),
            "donor_encode": result_json(donor_result) if donor_result else None,
            "trace_headers": (
                result_json(trace_result) if trace_result else None
            ),
            "salvage_decode": result_json(decode_result),
            "preview": result_json(preview_result) if preview_result else None,
        },
        "output_probe": output_probe,
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"effect={args.effect} codec={args.codec} "
        f"survival={survival:.3f} frames={salvaged_frames}/{source_frames} "
        f"qualified={qualified} report={report_path}"
    )
    return 0 if qualified else 4


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"error: {error}")
        raise SystemExit(1)
