#!/usr/bin/env python3
"""Build pinned FFmpeg with decoder-side HEVC MVD/coefficient hooks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


FFMPEG_REPOSITORY = "https://github.com/FFmpeg/FFmpeg.git"
FFMPEG_COMMIT = "894da5ca7d742e4429ffb2af534fcda0103ef593"
SCHEMA = "glic-ffmpeg-hevc-decoder-hook-build-v1"
ASSET_NAMES = (
    "ffmpeg-8.0.1-hevc-glic-decoder-hooks.patch",
    "ffmpeg_hevc_glic_decoder_hook.h",
    "ffmpeg_hevc_glic_decoder_hook.c",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command: list[str], *, cwd: Path | None = None) -> None:
    print("+", subprocess.list2cmdline(command))
    subprocess.run(command, cwd=cwd, check=True)


def find_assets(script: Path) -> tuple[Path, Path, Path]:
    for directory in (script.parent.parent / "patches", script.parent):
        assets = tuple(directory / name for name in ASSET_NAMES)
        if all(path.is_file() for path in assets):
            return assets
    raise RuntimeError(
        "FFmpeg HEVC decoder-hook assets are missing beside the tool or "
        "in the repository patches directory"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Clone pinned official FFmpeg, apply the GLIC decoder-side HEVC "
            "MVD/coefficient hook, and build a separate LGPL CLI."
        )
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(
            os.environ.get(
                "GLIC_FFMPEG_HEVC_HOOK_CACHE",
                Path(__file__).resolve().parents[1]
                / ".cache"
                / "ffmpeg-glic-hevc-decoder",
            )
        ),
    )
    parser.add_argument("--source", type=Path)
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 12))
    parser.add_argument("--print-ffmpeg", action="store_true")
    return parser.parse_args()


def valid_existing(
    binary: Path,
    marker: Path,
    patch: Path,
    header: Path,
    implementation: Path,
) -> bool:
    if not binary.is_file() or not marker.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        data.get("schema") == SCHEMA
        and data.get("ffmpeg_commit") == FFMPEG_COMMIT
        and data.get("patch_sha256") == sha256(patch)
        and data.get("header_sha256") == sha256(header)
        and data.get("implementation_sha256") == sha256(implementation)
        and data.get("binary_sha256") == sha256(binary)
    )


def main() -> int:
    args = parse_args()
    if args.jobs < 1:
        raise RuntimeError("--jobs must be positive")
    patch, header, implementation = find_assets(Path(__file__).resolve())
    cache = args.cache_dir.expanduser().resolve()
    source = (
        args.source.expanduser().resolve()
        if args.source
        else cache / "source"
    )
    binary = cache / "bin" / "ffmpeg-glic-hevc-decoder"
    marker = Path(str(binary) + ".glic-hook.json")

    if valid_existing(binary, marker, patch, header, implementation):
        print(
            binary
            if args.print_ffmpeg
            else f"FFmpeg GLIC HEVC decoder hook: {binary}"
        )
        return 0

    cache.mkdir(parents=True, exist_ok=True)
    if args.source:
        if not (source / ".git").exists():
            raise RuntimeError("--source must be an FFmpeg git checkout")
    elif not source.exists():
        run(
            [
                "git",
                "clone",
                "--filter=blob:none",
                FFMPEG_REPOSITORY,
                str(source),
            ]
        )
    if not (source / ".git").exists():
        raise RuntimeError(f"FFmpeg source checkout is invalid: {source}")

    run(["git", "fetch", "origin", FFMPEG_COMMIT], cwd=source)
    run(["git", "checkout", "--detach", FFMPEG_COMMIT], cwd=source)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status:
        raise RuntimeError(
            "FFmpeg source checkout is not clean; use a separate --cache-dir"
        )

    hook_directory = source / "libavcodec" / "hevc"
    shutil.copy2(header, hook_directory / header.name)
    shutil.copy2(
        implementation, hook_directory / "glic_decoder_hook.c"
    )
    run(
        ["git", "apply", "--unidiff-zero", "--check", str(patch)],
        cwd=source,
    )
    run(["git", "apply", "--unidiff-zero", str(patch)], cwd=source)
    run(
        [
            str(source / "configure"),
            "--disable-everything",
            "--disable-autodetect",
            "--disable-doc",
            "--disable-debug",
            "--disable-network",
            "--enable-static",
            "--disable-shared",
            "--enable-ffmpeg",
            "--enable-protocol=file",
            "--enable-demuxer=hevc",
            "--enable-parser=hevc",
            "--enable-decoder=hevc",
            "--enable-encoder=rawvideo",
            "--enable-muxer=rawvideo",
        ],
        cwd=source,
    )
    run(["make", f"-j{args.jobs}", "ffmpeg"], cwd=source)
    built = source / "ffmpeg"
    if not built.is_file():
        raise RuntimeError(f"FFmpeg build did not create {built}")
    binary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, binary)
    binary.chmod(binary.stat().st_mode | 0o111)
    version = subprocess.run(
        [str(binary), "-version"],
        check=True,
        capture_output=True,
        text=True,
    )
    version_text = version.stdout + version.stderr
    if "ffmpeg version" not in version_text or "8.0.1" not in version_text:
        raise RuntimeError("built FFmpeg does not report pinned version 8.0.1")

    marker.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "implementation_level": (
                    "native_hevc_ffmpeg_decoder_parsed_cabac_mvd_and_"
                    "quantized_coefficient_injection"
                ),
                "ffmpeg_repository": FFMPEG_REPOSITORY,
                "ffmpeg_commit": FFMPEG_COMMIT,
                "ffmpeg_version": "8.0.1",
                "ffmpeg_license": "LGPL-2.1-or-later",
                "patch_sha256": sha256(patch),
                "header_sha256": sha256(header),
                "implementation_sha256": sha256(implementation),
                "binary": str(binary),
                "binary_sha256": sha256(binary),
                "supported_input": "annex_b_hevc_main_8bit_420",
                "supported_output": "raw_yuv420p_reconstruction",
                "emits_mutated_hevc_bitstream": False,
                "required_runtime_constraints": {
                    "decoder_threads": 1,
                    "hardware_acceleration": False,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        binary
        if args.print_ffmpeg
        else f"FFmpeg GLIC HEVC decoder hook: {binary}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"error: {error}")
        raise SystemExit(1)
