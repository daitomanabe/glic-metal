#!/usr/bin/env python3
"""Isolated offline transport-level glitch workflows with retained evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

from process_offline_packet_glitch import (
    frame_count,
    preview_encoder_options,
    require_tool,
    result_json,
    run_isolated,
    safe_probe,
    sha256,
)
from transport_glitch import (
    decode_rtp_capture,
    depacketize_h264_rtp,
    encode_rtp_capture,
    mutate_hls_playlist,
    mutate_mpegts_continuity,
    mutate_rtp_sequence_jitter,
    packetize_h264_rtp,
)


EFFECTS = (
    "mpegts_continuity_fracture",
    "rtp_sequence_jitter",
    "hls_segment_boundary_splice",
)
IMPLEMENTATION_LEVEL = {
    "mpegts_continuity_fracture": "native_mpegts_188_byte_continuity_counter_rewrite",
    "rtp_sequence_jitter": "rfc6184_h264_offline_packet_model_not_network_capture",
    "hls_segment_boundary_splice": "native_hls_mpegts_segment_reorder_with_discontinuity",
}


def encode_common(args: argparse.Namespace, ffmpeg: str, work: Path) -> tuple[Path, object]:
    if args.effect == "mpegts_continuity_fracture":
        destination = work / "source.ts"
        tail = ["-f", "mpegts", str(destination)]
    elif args.effect == "rtp_sequence_jitter":
        destination = work / "source.h264"
        tail = ["-f", "h264", str(destination)]
    else:
        destination = work / "source.m3u8"
        tail = [
            "-f",
            "hls",
            "-hls_time",
            "0.5",
            "-hls_list_size",
            "0",
            "-hls_segment_filename",
            str(work / "segment-%03d.ts"),
            str(destination),
        ]
    command = [
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
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "25",
        "-g",
        str(max(2, args.fps // 2)),
        "-keyint_min",
        str(max(2, args.fps // 2)),
        "-sc_threshold",
        "0",
        *tail,
    ]
    result = run_isolated(
        command,
        log=work / "01-encode.log",
        timeout_seconds=args.timeout,
        maximum_file_bytes=args.maximum_file_mib * 1024 * 1024,
    )
    if result.return_code != 0 or not destination.is_file():
        raise RuntimeError(f"transport source encode failed; see {result.log}")
    return destination, result


def mutate_transport(
    args: argparse.Namespace, source: Path, work: Path
) -> tuple[Path, dict, list[Path]]:
    evidence_paths: list[Path] = [source]
    if args.effect == "mpegts_continuity_fracture":
        destination = work / "damaged.ts"
        mutated, evidence = mutate_mpegts_continuity(
            source.read_bytes(), args.amount, args.seed
        )
        destination.write_bytes(mutated)
    elif args.effect == "rtp_sequence_jitter":
        source_packets = packetize_h264_rtp(source.read_bytes())
        source_capture = work / "source.rtpbin"
        source_capture.write_bytes(encode_rtp_capture(source_packets))
        damaged_packets, evidence = mutate_rtp_sequence_jitter(
            source_packets, args.amount, args.seed
        )
        damaged_capture = work / "damaged.rtpbin"
        damaged_capture.write_bytes(encode_rtp_capture(damaged_packets))
        decoded_packets = decode_rtp_capture(damaged_capture.read_bytes())
        damaged_h264, depacketized = depacketize_h264_rtp(decoded_packets)
        destination = work / "damaged.h264"
        destination.write_bytes(damaged_h264)
        evidence["depacketization"] = depacketized
        evidence_paths.extend([source_capture, damaged_capture])
    else:
        destination = work / "damaged.m3u8"
        mutated, evidence = mutate_hls_playlist(
            source.read_text(), args.amount, args.seed
        )
        destination.write_text(mutated)
        evidence_paths.extend(sorted(work.glob("segment-*.ts")))
    evidence_paths.append(destination)
    return destination, evidence, evidence_paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--effect", choices=EFFECTS, required=True)
    parser.add_argument("--amount", type=float, default=0.62)
    parser.add_argument("--seed", type=lambda value: int(value, 0), default=0x474C4943)
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--max-frames", type=int, default=180)
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
    if not 0.0 <= args.amount <= 1.0:
        parser.error("--amount must be in [0, 1]")
    if args.width < 2 or args.height < 2 or args.width % 2 or args.height % 2:
        parser.error("--width and --height must be positive even values")
    if min(args.fps, args.max_frames, args.timeout, args.maximum_file_mib) < 1:
        parser.error("fps, max-frames, timeout, and maximum-file-mib must be positive")
    return args


def main() -> int:
    args = parse_args()
    ffmpeg = require_tool(args.ffmpeg)
    ffprobe = require_tool(args.ffprobe)
    work = (
        args.work_dir.expanduser().resolve()
        if args.work_dir
        else args.output.with_suffix(args.output.suffix + ".transport-stages")
    )
    report_path = (
        args.report.expanduser().resolve()
        if args.report
        else args.output.with_suffix(args.output.suffix + ".json")
    )
    work.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    source, encode_result = encode_common(args, ffmpeg, work)
    source_probe = safe_probe(ffprobe, source)
    source_frames = frame_count(source_probe)
    damaged, mutation_evidence, evidence_paths = mutate_transport(
        args, source, work
    )

    salvaged = work / "salvaged.ffv1.mkv"
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
    ]
    if args.effect == "hls_segment_boundary_splice":
        decode_command.extend(
            ["-protocol_whitelist", "file,crypto,data"]
        )
    decode_command.extend(
        [
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
        ]
    )
    decode_result = run_isolated(
        decode_command,
        log=work / "02-salvage.log",
        timeout_seconds=args.timeout,
        maximum_file_bytes=args.maximum_file_mib * 1024 * 1024,
    )
    salvage_probe = safe_probe(ffprobe, salvaged)
    salvaged_frames = frame_count(salvage_probe)
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
            log=work / "03-preview.log",
            timeout_seconds=args.timeout,
            maximum_file_bytes=args.maximum_file_mib * 1024 * 1024,
        )
    output_probe = safe_probe(ffprobe, args.output)
    qualified = bool(
        args.output.is_file()
        and salvaged_frames >= 2
        and preview_result is not None
        and preview_result.return_code == 0
    )
    report = {
        "schema": "glic-transport-glitch-v1",
        "execution_class": "offline_isolated_process",
        "realtime_certified": False,
        "effect": args.effect,
        "implementation_level": IMPLEMENTATION_LEVEL[args.effect],
        "input": str(args.input),
        "output": str(args.output),
        "amount": args.amount,
        "seed": args.seed,
        "qualified_preview": qualified,
        "source_frames": source_frames,
        "salvaged_frames": salvaged_frames,
        "decode_survival_ratio": round(
            min(1.0, salvaged_frames / source_frames), 6
        )
        if source_frames
        else 0.0,
        "mutation_evidence": mutation_evidence,
        "artifacts": [
            {
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in evidence_paths
            if path.is_file()
        ],
        "processes": {
            "encode": result_json(encode_result),
            "salvage_decode": result_json(decode_result),
            "preview": result_json(preview_result) if preview_result else None,
        },
        "output_probe": output_probe,
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"effect={args.effect} frames={salvaged_frames} "
        f"qualified={qualified} report={report_path}"
    )
    return 0 if qualified else 4


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"error: {error}")
        raise SystemExit(1)
