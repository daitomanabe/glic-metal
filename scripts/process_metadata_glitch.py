#!/usr/bin/env python3
"""Offline temporal color/HDR metadata modulation with native codec fields."""

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


EFFECTS = ("color_vui_oscillator", "hdr_metadata_pulse")
CODECS = ("h264", "hevc")
SUPPORT = {
    "color_vui_oscillator": {"h264", "hevc"},
    "hdr_metadata_pulse": {"hevc"},
}
IMPLEMENTATION_LEVEL = {
    "color_vui_oscillator": (
        "native_h264_hevc_vui_rewrite_plus_metadata_aware_preview"
    ),
    "hdr_metadata_pulse": (
        "native_hevc_vui_and_mastering_display_sei_temporal_reencode"
    ),
}
DISPLAY_NORMALIZATION_FILTER = (
    "zscale=primaries=bt709:transfer=bt709:matrix=bt709,"
    "eq=contrast=0.82:brightness=-0.04:saturation=0.92,"
    "colorlevels=romax=0.88:gomax=0.88:bomax=0.88,"
    "format=yuv420p"
)


def metadata_profiles(effect: str) -> list[dict[str, object]]:
    if effect == "color_vui_oscillator":
        return [
            {
                "name": "bt709_limited",
                "primaries": 1,
                "transfer": 1,
                "matrix": 1,
                "full_range": 0,
            },
            {
                "name": "bt2020_pq_limited",
                "primaries": 9,
                "transfer": 16,
                "matrix": 9,
                "full_range": 0,
            },
            {
                "name": "smpte170m_full",
                "primaries": 6,
                "transfer": 6,
                "matrix": 6,
                "full_range": 1,
            },
        ]
    if effect == "hdr_metadata_pulse":
        return [
            {
                "name": "bt709_sdr",
                "primaries": 1,
                "transfer": 1,
                "matrix": 1,
                "full_range": 0,
                "mastering_display": None,
                "max_cll": None,
            },
            {
                "name": "bt2020_pq_1000nit",
                "primaries": 9,
                "transfer": 16,
                "matrix": 9,
                "full_range": 0,
                "mastering_display": (
                    "G(13250,34500)B(7500,3000)R(34000,16000)"
                    "WP(15635,16450)L(10000000,1)"
                ),
                "max_cll": "1000,400",
            },
            {
                "name": "bt2020_pq_4000nit",
                "primaries": 9,
                "transfer": 16,
                "matrix": 9,
                "full_range": 0,
                "mastering_display": (
                    "G(13250,34500)B(7500,3000)R(34000,16000)"
                    "WP(15635,16450)L(40000000,1)"
                ),
                "max_cll": "4000,1000",
            },
        ]
    raise ValueError(f"unknown metadata effect: {effect}")


def segment_encoder(
    codec: str,
    profile: dict[str, object],
    fps: int,
    frames_per_segment: int,
) -> list[str]:
    keyint = max(2, min(fps, frames_per_segment))
    primaries = int(profile["primaries"])
    transfer = int(profile["transfer"])
    matrix = int(profile["matrix"])
    full_range = int(profile["full_range"])
    if codec == "h264":
        bsf = (
            f"h264_metadata=colour_primaries={primaries}:"
            f"transfer_characteristics={transfer}:"
            f"matrix_coefficients={matrix}:"
            f"video_full_range_flag={full_range}"
        )
        return [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-x264-params",
            f"repeat-headers=1:keyint={keyint}:min-keyint={keyint}:scenecut=0",
            "-bsf:v",
            bsf,
            "-f",
            "h264",
        ]
    if codec != "hevc":
        raise ValueError(f"unsupported metadata codec: {codec}")
    x265 = [
        "repeat-headers=1",
        f"keyint={keyint}",
        f"min-keyint={keyint}",
        "scenecut=0",
    ]
    if profile.get("mastering_display"):
        x265.extend(
            [
                f"colorprim={primaries}",
                f"transfer={transfer}",
                f"colormatrix={matrix}",
                f"master-display={profile['mastering_display']}",
                f"max-cll={profile['max_cll']}",
                "hdr10=1",
                "hdr10-opt=1",
            ]
        )
    bsf = (
        f"hevc_metadata=colour_primaries={primaries}:"
        f"transfer_characteristics={transfer}:"
        f"matrix_coefficients={matrix}:"
        f"video_full_range_flag={full_range}"
    )
    return [
        "-c:v",
        "libx265",
        "-preset",
        "fast",
        "-crf",
        "25",
        "-x265-params",
        ":".join(x265),
        "-bsf:v",
        bsf,
        "-f",
        "hevc",
    ]


def probe_frame_metadata(ffprobe: str, path: Path) -> dict:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=color_primaries,color_transfer,color_space,color_range,side_data_list",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        payload = json.loads(result.stdout) if result.stdout else {}
    except json.JSONDecodeError:
        payload = {}
    frames = payload.get("frames", [])
    signatures: dict[str, int] = {}
    mastering_frames = 0
    for frame in frames:
        signature = "/".join(
            str(frame.get(key, "unknown"))
            for key in (
                "color_primaries",
                "color_transfer",
                "color_space",
                "color_range",
            )
        )
        signatures[signature] = signatures.get(signature, 0) + 1
        if any(
            side.get("side_data_type") == "Mastering display metadata"
            for side in frame.get("side_data_list", [])
        ):
            mastering_frames += 1
    return {
        "return_code": result.returncode,
        "frame_count": len(frames),
        "color_signatures": signatures,
        "mastering_display_frames": mastering_frames,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--effect", choices=EFFECTS, required=True)
    parser.add_argument("--codec", choices=CODECS, default="hevc")
    parser.add_argument("--amount", type=float, default=0.7)
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--max-frames", type=int, default=180)
    parser.add_argument("--segments", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--maximum-file-mib", type=int, default=4096)
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
    if not 0.0 <= args.amount <= 1.0:
        parser.error("--amount must be in [0, 1]")
    if args.width < 2 or args.height < 2 or args.width % 2 or args.height % 2:
        parser.error("--width and --height must be positive even values")
    if min(
        args.fps,
        args.max_frames,
        args.segments,
        args.timeout,
        args.maximum_file_mib,
    ) < 1:
        parser.error("frame, segment, timeout, and size controls must be positive")
    return args


def main() -> int:
    args = parse_args()
    ffmpeg = require_tool(args.ffmpeg)
    ffprobe = require_tool(args.ffprobe)
    work = (
        args.work_dir.expanduser().resolve()
        if args.work_dir
        else args.output.with_suffix(args.output.suffix + ".metadata-stages")
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
    profiles = metadata_profiles(args.effect)
    frames_per_segment = max(2, args.max_frames // args.segments)
    extension = ".h264" if args.codec == "h264" else ".hevc"
    segments: list[Path] = []
    processes: list[dict] = []
    profile_evidence: list[dict] = []
    for segment_index in range(args.segments):
        start = segment_index * frames_per_segment
        stop = min(args.max_frames - 1, start + frames_per_segment - 1)
        if start > stop:
            break
        profile = profiles[segment_index % len(profiles)]
        segment = work / f"segment-{segment_index:03d}-{profile['name']}{extension}"
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
            (
                f"select='between(n,{start},{stop})',"
                f"setpts=N/({args.fps}*TB),"
                f"scale={args.width}:{args.height}:flags=lanczos,"
                f"fps={args.fps},format="
                f"{'yuv420p10le' if args.effect == 'hdr_metadata_pulse' else 'yuv420p'}"
            ),
            "-frames:v",
            str(stop - start + 1),
            *segment_encoder(
                args.codec, profile, args.fps, frames_per_segment
            ),
            str(segment),
        ]
        process = run_isolated(
            command,
            log=work / f"segment-{segment_index:03d}.log",
            timeout_seconds=args.timeout,
            maximum_file_bytes=maximum_file_bytes,
        )
        processes.append(result_json(process))
        if process.return_code != 0 or not segment.is_file():
            raise RuntimeError(
                f"metadata segment {segment_index} failed; see {process.log}"
            )
        segments.append(segment)
        profile_evidence.append(
            {
                "segment": segment_index,
                "start_frame": start,
                "end_frame": stop,
                "profile": profile,
                "path": str(segment),
                "bytes": segment.stat().st_size,
                "sha256": sha256(segment),
                "frame_metadata": probe_frame_metadata(ffprobe, segment),
            }
        )
    if len(segments) < 2:
        raise RuntimeError("metadata modulation produced fewer than two segments")
    joined = work / f"temporal-metadata{extension}"
    with joined.open("wb") as destination:
        for segment in segments:
            destination.write(segment.read_bytes())

    rendered = work / "metadata-aware.ffv1.mkv"
    decode = run_isolated(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            str(joined),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            DISPLAY_NORMALIZATION_FILTER,
            "-c:v",
            "ffv1",
            str(rendered),
        ],
        log=work / "decode-metadata-aware.log",
        timeout_seconds=args.timeout,
        maximum_file_bytes=maximum_file_bytes,
    )
    if decode.return_code != 0 or not rendered.is_file():
        raise RuntimeError(f"metadata-aware decode failed; see {decode.log}")
    processes.append(result_json(decode))
    preview = run_isolated(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(rendered),
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
        log=work / "preview.log",
        timeout_seconds=args.timeout,
        maximum_file_bytes=maximum_file_bytes,
    )
    processes.append(result_json(preview))
    output_probe = safe_probe(ffprobe, args.output)
    output_frames = frame_count(output_probe)
    qualified = bool(
        preview.return_code == 0 and args.output.is_file() and output_frames >= 2
    )
    report = {
        "schema": "glic-metadata-glitch-v1",
        "execution_class": "offline",
        "realtime_certified": False,
        "effect": args.effect,
        "codec": args.codec,
        "implementation_level": IMPLEMENTATION_LEVEL[args.effect],
        "preview_display_normalization": DISPLAY_NORMALIZATION_FILTER,
        "input": str(args.input),
        "output": str(args.output),
        "amount": args.amount,
        "qualified_preview": qualified,
        "output_frames": output_frames,
        "temporal_bitstream": {
            "path": str(joined),
            "bytes": joined.stat().st_size,
            "sha256": sha256(joined),
            "frame_metadata": probe_frame_metadata(ffprobe, joined),
        },
        "segments": profile_evidence,
        "processes": processes,
        "output_probe": output_probe,
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"effect={args.effect} codec={args.codec} frames={output_frames} "
        f"qualified={qualified} report={report_path}"
    )
    return 0 if qualified else 4


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"error: {error}")
        raise SystemExit(1)
