#!/usr/bin/env python3
"""Build the pinned external x265 CLI with GLIC late-entropy hooks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


X265_REPOSITORY = "https://github.com/Multicorewareinc/x265.git"
X265_COMMIT = "e444744c03978c1fb4e037168967020cf2648427"
SCHEMA = "glic-x265-entropy-hook-build-v1"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command: list[str], *, cwd: Path | None = None) -> None:
    print("+", subprocess.list2cmdline(command))
    subprocess.run(command, cwd=cwd, check=True)


def find_assets(script: Path) -> tuple[Path, Path]:
    candidates = (
        script.parent.parent / "patches",
        script.parent,
    )
    for directory in candidates:
        patch = directory / "x265-4.2-glic-entropy-hooks.patch"
        header = directory / "x265_glic_entropy_hook.h"
        if patch.is_file() and header.is_file():
            return patch, header
    raise RuntimeError(
        "x265 entropy-hook patch assets are missing beside the tool or "
        "in the repository patches directory"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Clone pinned official x265 4.2 source, apply the GLIC "
            "late-entropy MVD/coefficient hook, and build a separate GPL CLI."
        )
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(
            os.environ.get(
                "GLIC_X265_HOOK_CACHE",
                Path(__file__).resolve().parents[1]
                / ".cache"
                / "x265-glic-entropy",
            )
        ),
    )
    parser.add_argument("--source", type=Path)
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 12))
    parser.add_argument("--print-x265", action="store_true")
    return parser.parse_args()


def valid_existing(binary: Path, marker: Path, patch: Path, header: Path) -> bool:
    if not binary.is_file() or not marker.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        data.get("schema") == SCHEMA
        and data.get("x265_commit") == X265_COMMIT
        and data.get("patch_sha256") == sha256(patch)
        and data.get("header_sha256") == sha256(header)
        and data.get("binary_sha256") == sha256(binary)
    )


def main() -> int:
    args = parse_args()
    if args.jobs < 1:
        raise RuntimeError("--jobs must be positive")
    script = Path(__file__).resolve()
    patch, header = find_assets(script)
    cache = args.cache_dir.expanduser().resolve()
    source = (
        args.source.expanduser().resolve()
        if args.source
        else cache / "source"
    )
    build = cache / "build"
    binary = cache / "bin" / "x265-glic-entropy"
    marker = binary.with_suffix(binary.suffix + ".glic-hook.json")

    if valid_existing(binary, marker, patch, header):
        print(binary if args.print_x265 else f"x265 GLIC entropy hook: {binary}")
        return 0

    cache.mkdir(parents=True, exist_ok=True)
    if args.source:
        if not (source / ".git").exists():
            raise RuntimeError("--source must be an x265 git checkout")
    elif not source.exists():
        run(
            [
                "git",
                "clone",
                "--filter=blob:none",
                X265_REPOSITORY,
                str(source),
            ]
        )
    if not (source / ".git").exists():
        raise RuntimeError(f"x265 source checkout is invalid: {source}")

    run(["git", "fetch", "origin", X265_COMMIT], cwd=source)
    run(["git", "checkout", "--detach", X265_COMMIT], cwd=source)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status:
        raise RuntimeError(
            "x265 source checkout is not clean; use a separate --cache-dir"
        )
    shutil.copy2(header, source / "source" / "encoder" / header.name)
    run(["git", "apply", "--unidiff-zero", "--check", str(patch)], cwd=source)
    run(["git", "apply", "--unidiff-zero", str(patch)], cwd=source)

    run(
        [
            "cmake",
            "-S",
            str(source / "source"),
            "-B",
            str(build),
            "-DCMAKE_BUILD_TYPE=Release",
            "-DENABLE_SHARED=OFF",
            "-DENABLE_CLI=ON",
            "-DENABLE_ASSEMBLY=ON",
        ]
    )
    run(["cmake", "--build", str(build), "--parallel", str(args.jobs)])
    built = build / "x265"
    if not built.is_file():
        raise RuntimeError(f"x265 build did not create {built}")
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
    if "HEVC encoder version 4.2" not in version_text:
        raise RuntimeError("built x265 does not report the pinned 4.2 version")

    marker.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "implementation_level": (
                    "native_hevc_x265_late_entropy_mvd_and_quantized_"
                    "coefficient_injection"
                ),
                "x265_repository": X265_REPOSITORY,
                "x265_commit": X265_COMMIT,
                "x265_license": "GPL-2.0-or-later",
                "patch_sha256": sha256(patch),
                "header_sha256": sha256(header),
                "binary": str(binary),
                "binary_sha256": sha256(binary),
                "required_runtime_constraints": {
                    "frame_threads": 1,
                    "wpp": False,
                    "pools": "none",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(binary if args.print_x265 else f"x265 GLIC entropy hook: {binary}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"error: {error}")
        raise SystemExit(1)
