#!/usr/bin/env python3
"""Direct MPEG-2 motion-vector and quantized-DCT bitstream editing."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import subprocess

from native_syntax_glitch import (
    EFFECTS,
    FEATURE_FOR_EFFECT,
    IMPLEMENTATION_LEVEL,
    mutate_json_file,
)
from process_offline_packet_glitch import (
    frame_count,
    preview_encoder_options,
    require_tool,
    result_json,
    run_isolated,
    safe_probe,
    sha256,
)


CODECS = ("mpeg2", "h264", "hevc")
SUPPORTED_CODEC = "mpeg2"
FFGLITCH_VERSION = "0.10.2"
FFGLITCH_DOWNLOAD = "https://ffglitch.org/download/"


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


def source_contract(ffprobe: str, source: Path) -> dict:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,avg_frame_rate:"
            "format=format_name,duration",
            "-of",
            "json",
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe could not inspect input: {result.stderr}")
    return json.loads(result.stdout)


def validate_preserved_source(probe: dict) -> None:
    streams = probe.get("streams", [])
    format_names = set(probe.get("format", {}).get("format_name", "").split(","))
    if not streams or streams[0].get("codec_name") != "mpeg2video":
        raise RuntimeError(
            "--source-mode preserve requires an MPEG-2 video stream"
        )
    if "avi" not in format_names:
        raise RuntimeError(
            "--source-mode preserve requires an AVI container supported by FFglitch"
        )


def normalized_encode_command(
    ffmpeg: str,
    source: Path,
    destination: Path,
    *,
    width: int,
    height: int,
    fps: int,
    max_frames: int,
    threads: int,
) -> list[str]:
    gop = max(8, fps)
    return [
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
        f"scale={width}:{height}:flags=lanczos,fps={fps},format=yuv420p",
        "-frames:v",
        str(max_frames),
        "-threads",
        str(threads),
        "-c:v",
        "mpeg2video",
        "-q:v",
        "6",
        "-g",
        str(gop),
        "-bf",
        "0",
        "-f",
        "avi",
        str(destination),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Directly mutate entropy-coded MPEG-2 motion vectors or quantized "
            "DCT coefficients through FFglitch transplication."
        )
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--effect", choices=EFFECTS, required=True)
    parser.add_argument("--codec", choices=CODECS, default=SUPPORTED_CODEC)
    parser.add_argument(
        "--source-mode",
        choices=("normalize", "preserve"),
        default="normalize",
        help=(
            "normalize encodes an FFglitch-compatible MPEG-2/AVI source; "
            "preserve edits an existing MPEG-2/AVI input without pre-encoding"
        ),
    )
    parser.add_argument("--amount", type=float, default=0.65)
    parser.add_argument(
        "--seed", type=lambda value: int(value, 0), default=0x474C4943
    )
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--max-frames", type=int, default=180)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--maximum-file-mib", type=int, default=4096)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument(
        "--ffedit", default=os.environ.get("GLIC_FFEDIT", "ffedit")
    )
    args = parser.parse_args()
    args.input = args.input.expanduser().resolve()
    args.output = args.output.expanduser().resolve()
    if not args.input.is_file():
        parser.error(f"input does not exist: {args.input}")
    if args.codec != SUPPORTED_CODEC:
        parser.error(
            "direct motion-vector/coefficient mutation currently supports "
            "MPEG-2 only; H.264/HEVC entropy syntax is fail-closed"
        )
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
    ffedit = require_tool(args.ffedit)
    work = (
        args.work_dir.expanduser().resolve()
        if args.work_dir
        else args.output.with_suffix(args.output.suffix + ".native-syntax-stages")
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

    version_result = run_isolated(
        [ffedit, "-version"],
        log=work / "00-ffedit-version.log",
        timeout_seconds=min(args.timeout, 30),
        maximum_file_bytes=maximum_file_bytes,
    )
    version_text = version_result.log.read_text(errors="replace")
    if version_result.return_code != 0 or "ffedit version ffglitch-" not in version_text:
        raise RuntimeError(
            f"--ffedit is not a working FFglitch ffedit binary; see {version_result.log}"
        )

    source_probe_before = source_contract(ffprobe, args.input)
    encoded = work / "source-mpeg2.avi"
    encode_result = None
    if args.source_mode == "preserve":
        validate_preserved_source(source_probe_before)
        shutil.copy2(args.input, encoded)
    else:
        encode_result = run_isolated(
            normalized_encode_command(
                ffmpeg,
                args.input,
                encoded,
                width=args.width,
                height=args.height,
                fps=args.fps,
                max_frames=args.max_frames,
                threads=args.threads,
            ),
            log=work / "01-normalize-mpeg2.log",
            timeout_seconds=args.timeout,
            maximum_file_bytes=maximum_file_bytes,
        )
        if encode_result.return_code != 0 or not encoded.is_file():
            raise RuntimeError(
                f"MPEG-2 normalization failed; see {encode_result.log}"
            )

    capabilities_result = run_isolated(
        [ffedit, "-hide_banner", "-i", str(encoded)],
        log=work / "02-ffedit-capabilities.log",
        timeout_seconds=args.timeout,
        maximum_file_bytes=maximum_file_bytes,
    )
    capabilities_text = capabilities_result.log.read_text(errors="replace")
    feature = FEATURE_FOR_EFFECT[args.effect]
    if capabilities_result.return_code != 0 or f"[{feature}" not in capabilities_text:
        raise RuntimeError(
            f"FFglitch does not expose {feature} for this source; "
            f"see {capabilities_result.log}"
        )

    exported_json = work / f"source-{feature}.json"
    export_result = run_isolated(
        [
            ffedit,
            "-hide_banner",
            "-y",
            "-i",
            str(encoded),
            "-f",
            feature,
            "-e",
            str(exported_json),
        ],
        log=work / "03-export-syntax.log",
        timeout_seconds=args.timeout,
        maximum_file_bytes=maximum_file_bytes,
    )
    if export_result.return_code != 0 or not exported_json.is_file():
        raise RuntimeError(
            f"FFglitch syntax export failed; see {export_result.log}"
        )

    mutated_json = work / f"mutated-{args.effect}-{feature}.json"
    mutation_evidence = mutate_json_file(
        exported_json,
        mutated_json,
        args.effect,
        args.amount,
        args.seed,
    )
    if mutation_evidence["changed_values"] < 1:
        raise RuntimeError(
            "effect selected no syntax values; increase --amount or use a "
            "source with inter prediction / non-zero AC coefficients"
        )

    damaged = work / f"damaged-{args.effect}-mpeg2.avi"
    apply_result = run_isolated(
        [
            ffedit,
            "-hide_banner",
            "-y",
            "-i",
            str(encoded),
            "-f",
            feature,
            "-a",
            str(mutated_json),
            "-o",
            str(damaged),
        ],
        log=work / "04-apply-syntax.log",
        timeout_seconds=args.timeout,
        maximum_file_bytes=maximum_file_bytes,
    )
    if apply_result.return_code != 0 or not damaged.is_file():
        raise RuntimeError(
            f"FFglitch syntax transplication failed; see {apply_result.log}"
        )
    source_digest = sha256(encoded)
    damaged_digest = sha256(damaged)
    if source_digest == damaged_digest:
        raise RuntimeError("transplication did not change the compressed stream")

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
        log=work / "05-salvage-decode.log",
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
            log=work / "06-preview.log",
            timeout_seconds=args.timeout,
            maximum_file_bytes=maximum_file_bytes,
        )

    output_probe = safe_probe(ffprobe, args.output)
    streams = output_probe.get("streams", [])
    qualified = bool(
        mutation_evidence["changed_values"] > 0
        and source_digest != damaged_digest
        and salvaged_frames >= 2
        and args.output.is_file()
        and preview_result is not None
        and preview_result.return_code == 0
    )
    report = {
        "schema": "glic-native-compressed-syntax-glitch-v1",
        "execution_class": "offline_external_transplication",
        "realtime_certified": False,
        "effect": args.effect,
        "codec": args.codec,
        "feature": feature,
        "implementation_level": IMPLEMENTATION_LEVEL[args.effect],
        "input": str(args.input),
        "output": str(args.output),
        "source_mode": args.source_mode,
        "amount": args.amount,
        "seed": args.seed,
        "target_width": args.width,
        "target_height": args.height,
        "target_fps": args.fps,
        "output_fps": round(
            parse_rate(streams[0].get("avg_frame_rate")) if streams else 0.0,
            6,
        ),
        "source_frames": source_frames,
        "salvaged_frames": salvaged_frames,
        "decode_survival_ratio": round(survival, 6),
        "qualified_preview": qualified,
        "compressed_domain_edit": True,
        "decoded_pixels_modified_before_transplication": False,
        "may_produce_invalid_bitstream": False,
        "h264_hevc_direct_support": "not_implemented_fail_closed",
        "ffglitch": {
            "required_version": FFGLITCH_VERSION,
            "binary": ffedit,
            "download": FFGLITCH_DOWNLOAD,
            "version_log": str(version_result.log),
        },
        "mutation_evidence": mutation_evidence,
        "source_bitstream": {
            "path": str(encoded),
            "bytes": encoded.stat().st_size,
            "sha256": source_digest,
            "probe": source_probe,
        },
        "exported_syntax": {
            "path": str(exported_json),
            "bytes": exported_json.stat().st_size,
            "sha256": sha256(exported_json),
        },
        "mutated_syntax": {
            "path": str(mutated_json),
            "bytes": mutated_json.stat().st_size,
            "sha256": sha256(mutated_json),
        },
        "damaged_bitstream": {
            "path": str(damaged),
            "bytes": damaged.stat().st_size,
            "sha256": damaged_digest,
            "probe": damaged_probe,
        },
        "processes": {
            "ffedit_version": result_json(version_result),
            "normalize_encode": (
                result_json(encode_result) if encode_result else None
            ),
            "ffedit_capabilities": result_json(capabilities_result),
            "syntax_export": result_json(export_result),
            "syntax_apply": result_json(apply_result),
            "salvage_decode": result_json(decode_result),
            "preview": result_json(preview_result) if preview_result else None,
        },
        "input_probe": source_probe_before,
        "output_probe": output_probe,
    }
    report_path.write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"effect={args.effect} feature={feature} "
        f"changed={mutation_evidence['changed_values']} "
        f"survival={survival:.3f} frames={salvaged_frames}/{source_frames} "
        f"qualified={qualified} report={report_path}"
    )
    return 0 if qualified else 4


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"error: {error}")
        raise SystemExit(1)
