#!/usr/bin/env python3
"""Report actual local encode/decode glitch capabilities without guessing."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


def executable(value: str) -> str | None:
    candidate = Path(value).expanduser()
    if candidate.parent != Path(".") or "/" in value:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        return None
    return shutil.which(value)


def output(command: list[str]) -> str:
    return subprocess.run(
        command, check=True, capture_output=True, text=True
    ).stdout


def native_probe(binary: str | None, codec: str) -> dict:
    if binary is None:
        return {
            "available": False,
            "reason": "glic_codec_glitch_filter not found",
        }
    result = subprocess.run(
        [binary, "--codec", codec, "--check"],
        capture_output=True,
        text=True,
    )
    return {
        "available": result.returncode == 0,
        "command": [binary, "--codec", codec, "--check"],
        "return_code": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument(
        "--native-filter",
        default=str(root / "build" / "glic_codec_glitch_filter"),
    )
    parser.add_argument(
        "--avmenc",
        default=str(root / ".cache" / "avm-v1.0.0" / "build" / "avmenc"),
    )
    parser.add_argument(
        "--avmdec",
        default=str(root / ".cache" / "avm-v1.0.0" / "build" / "avmdec"),
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
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    ffmpeg = executable(args.ffmpeg)
    encoders = output([ffmpeg, "-hide_banner", "-encoders"]) if ffmpeg else ""
    decoders = output([ffmpeg, "-hide_banner", "-decoders"]) if ffmpeg else ""
    bitstream_filters = output([ffmpeg, "-hide_banner", "-bsfs"]) if ffmpeg else ""
    native = executable(args.native_filter)
    avmenc = executable(args.avmenc)
    avmdec = executable(args.avmdec)
    vvencapp = executable(args.vvencapp)
    catalog_path = root / "resources" / "offline-codec-effects.json"
    offline_catalog = json.loads(catalog_path.read_text())
    offline_codec_available = {
        "h264": "libx264" in encoders and " h264 " in decoders,
        "hevc": "libx265" in encoders and " hevc " in decoders,
        "av1": "libaom-av1" in encoders
        and ("libdav1d" in decoders or " av1 " in decoders),
        "vp9": "libvpx-vp9" in encoders and "libvpx-vp9" in decoders,
        "prores": "prores_ks" in encoders and " prores " in decoders,
    }
    required_filters = {
        "packet_bit_rot": {"noise"},
        "gop_amputation": {"noise"},
        "packet_dropout_score": {"noise"},
        "timestamp_fracture": {"setts"},
        "nal_obu_surgery": {"filter_units"},
        "header_hallucination": {
            "h264_metadata",
            "hevc_metadata",
            "av1_metadata",
            "vp9_metadata",
        },
        "packet_transplant": set(),
        "vp9_superframe_shuffle": {"vp9_superframe_split", "setts"},
    }
    available_filters = {
        name
        for names in required_filters.values()
        for name in names
        if name in bitstream_filters
    }
    offline_effects = {}
    for effect in offline_catalog["offline_effects"]:
        name = effect["name"]
        supported_codecs = effect["codecs"]
        ready_codecs = [
            codec
            for codec in supported_codecs
            if offline_codec_available.get(codec, False)
        ]
        missing_filters = sorted(required_filters[name] - available_filters)
        offline_effects[name] = {
            "available": bool(ready_codecs) and not missing_filters,
            "supported_codecs": supported_codecs,
            "available_codecs": ready_codecs if not missing_filters else [],
            "required_bitstream_filters": sorted(required_filters[name]),
            "missing_bitstream_filters": missing_filters,
        }
    result = {
        "schema": "glic-multicodec-capabilities-v1",
        "codecs": {
            "av1": {
                "available": "libaom-av1" in encoders
                and "libdav1d" in decoders,
                "backend": "FFmpeg libaom-av1 + libdav1d",
                "realtime_claim": False,
            },
            "av2": {
                "available": avmenc is not None and avmdec is not None,
                "backend": "AOMedia AVM v1.0.0 reference",
                "realtime_claim": False,
                "avmenc": avmenc,
                "avmdec": avmdec,
            },
            "hevc": {
                **native_probe(native, "hevc"),
                "backend": "VideoToolbox",
                "realtime_claim": "measured_per_report",
            },
            "vp9": {
                "available": "libvpx-vp9" in encoders
                and "libvpx-vp9" in decoders,
                "backend": "FFmpeg libvpx-vp9",
                "realtime_claim": False,
            },
            "prores": {
                **native_probe(native, "prores_422"),
                "backend": "VideoToolbox",
                "realtime_claim": "measured_per_report",
            },
            "vvc": {
                "available": vvencapp is not None and " vvc " in decoders,
                "backend": "Fraunhofer VVenC v1.14.0 + FFmpeg vvc decoder",
                "realtime_claim": False,
                "vvencapp": vvencapp,
            },
            "theora": {
                "available": "libtheora" in encoders and " theora " in decoders,
                "backend": "FFmpeg libtheora/theora",
                "realtime_claim": False,
            },
            "dirac": {
                "available": " vc2 " in encoders and " dirac " in decoders,
                "backend": "FFmpeg VC-2/Dirac",
                "realtime_claim": False,
            },
        },
        "offline_packet_lab": {
            "available": any(
                effect["available"] for effect in offline_effects.values()
            ),
            "realtime_claim": False,
            "catalog": str(catalog_path),
            "ffmpeg": ffmpeg,
            "codec_backends": offline_codec_available,
            "bitstream_filters": sorted(available_filters),
            "effects": offline_effects,
        },
    }
    serialized = json.dumps(result, indent=2) + "\n"
    if args.output:
        destination = args.output.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(serialized)
    print(serialized, end="")
    return (
        0
        if all(entry["available"] for entry in result["codecs"].values())
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
