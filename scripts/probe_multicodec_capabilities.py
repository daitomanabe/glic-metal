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
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    ffmpeg = executable(args.ffmpeg)
    encoders = output([ffmpeg, "-hide_banner", "-encoders"]) if ffmpeg else ""
    decoders = output([ffmpeg, "-hide_banner", "-decoders"]) if ffmpeg else ""
    native = executable(args.native_filter)
    avmenc = executable(args.avmenc)
    avmdec = executable(args.avmdec)
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
