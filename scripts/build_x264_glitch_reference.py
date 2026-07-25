#!/usr/bin/env python3
"""Build the pinned external x264 CLI with GLIC late-entropy hooks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


X264_REPOSITORY = "https://code.videolan.org/videolan/x264.git"
X264_COMMIT = "0480cb05fa188d37ae87e8f4fd8f1aea3711f7ee"
SCHEMA = "glic-x264-entropy-hook-build-v1"
ASSET_NAMES = (
    "x264-0480cb0-glic-entropy-hooks.patch",
    "x264_glic_entropy_hook.h",
    "x264_glic_entropy_hook.c",
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
        "x264 entropy-hook assets are missing beside the tool or "
        "in the repository patches directory"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Clone pinned official x264 source, apply the GLIC CABAC/CAVLC "
            "MVD/coefficient hook, and build a separate GPL CLI."
        )
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(
            os.environ.get(
                "GLIC_X264_HOOK_CACHE",
                Path(__file__).resolve().parents[1]
                / ".cache"
                / "x264-glic-entropy",
            )
        ),
    )
    parser.add_argument("--source", type=Path)
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 12))
    parser.add_argument("--print-x264", action="store_true")
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
        and data.get("x264_commit") == X264_COMMIT
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
    binary = cache / "bin" / "x264-glic-entropy"
    marker = Path(str(binary) + ".glic-hook.json")

    if valid_existing(binary, marker, patch, header, implementation):
        print(binary if args.print_x264 else f"x264 GLIC entropy hook: {binary}")
        return 0

    cache.mkdir(parents=True, exist_ok=True)
    if args.source:
        if not (source / ".git").exists():
            raise RuntimeError("--source must be an x264 git checkout")
    elif not source.exists():
        run(
            [
                "git",
                "clone",
                "--filter=blob:none",
                X264_REPOSITORY,
                str(source),
            ]
        )
    if not (source / ".git").exists():
        raise RuntimeError(f"x264 source checkout is invalid: {source}")

    run(["git", "fetch", "origin", X264_COMMIT], cwd=source)
    run(["git", "checkout", "--detach", X264_COMMIT], cwd=source)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status:
        raise RuntimeError(
            "x264 source checkout is not clean; use a separate --cache-dir"
        )

    shutil.copy2(header, source / "encoder" / header.name)
    shutil.copy2(implementation, source / "encoder" / "glic_hook.c")
    run(["git", "apply", "--unidiff-zero", "--check", str(patch)], cwd=source)
    run(["git", "apply", "--unidiff-zero", str(patch)], cwd=source)
    configure = [
        str(source / "configure"),
        "--enable-static",
        "--disable-opencl",
        "--bit-depth=8",
        "--chroma-format=420",
        "--disable-lavf",
        "--disable-swscale",
        "--disable-avs",
        "--disable-gpac",
    ]
    run(configure, cwd=source)
    run(["make", f"-j{args.jobs}"], cwd=source)
    built = source / "x264"
    if not built.is_file():
        raise RuntimeError(f"x264 build did not create {built}")
    binary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, binary)
    binary.chmod(binary.stat().st_mode | 0o111)
    version = subprocess.run(
        [str(binary), "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    version_text = version.stdout + version.stderr
    if "0480cb0" not in version_text or "GPL version 2 or later" not in version_text:
        raise RuntimeError("built x264 does not report the pinned GPL version")

    marker.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "implementation_level": (
                    "native_h264_x264_cabac_cavlc_late_entropy_"
                    "mvd_and_quantized_coefficient_injection"
                ),
                "x264_repository": X264_REPOSITORY,
                "x264_commit": X264_COMMIT,
                "x264_license": "GPL-2.0-or-later",
                "patch_sha256": sha256(patch),
                "header_sha256": sha256(header),
                "implementation_sha256": sha256(implementation),
                "binary": str(binary),
                "binary_sha256": sha256(binary),
                "supported_entropy_modes": ["cabac", "cavlc"],
                "required_runtime_constraints": {
                    "threads": 1,
                    "sliced_threads": False,
                    "bit_depth": 8,
                    "chroma_format": "420",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(binary if args.print_x264 else f"x264 GLIC entropy hook: {binary}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"error: {error}")
        raise SystemExit(1)
