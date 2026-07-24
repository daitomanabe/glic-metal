#!/usr/bin/env python3
"""Build the pinned official Fraunhofer VVenC encoder locally."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess


VVENC_REPOSITORY = "https://github.com/fraunhoferhhi/vvenc.git"
VVENC_TAG = "v1.14.0"
VVENC_COMMIT = "9428ea8636ae7f443ecde89999d16b2dfc421524"


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True)


def locate_vvencapp(source: Path) -> Path:
    candidates = [
        source / "bin" / "release-static" / "vvencapp",
        source / "build" / "bin" / "release-static" / "vvencapp",
        source / "build" / "bin" / "vvencapp",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    matches = sorted(source.glob("**/vvencapp"))
    for match in matches:
        if match.is_file():
            return match
    raise RuntimeError("VVenC build produced no vvencapp executable")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-dir", type=Path, default=root / ".cache" / "vvenc-v1.14.0"
    )
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()
    source = args.source_dir.expanduser().resolve()
    build = source / "build"
    source.parent.mkdir(parents=True, exist_ok=True)
    if not (source / ".git").is_dir():
        run(
            [
                "git",
                "clone",
                "--branch",
                VVENC_TAG,
                "--depth",
                "1",
                VVENC_REPOSITORY,
                str(source),
            ]
        )
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if revision != VVENC_COMMIT:
        raise RuntimeError(
            f"VVenC {VVENC_TAG} resolved to {revision}, expected {VVENC_COMMIT}"
        )
    run(
        [
            "cmake",
            "-S",
            str(source),
            "-B",
            str(build),
            "-DCMAKE_BUILD_TYPE=Release",
            "-DVVENC_ENABLE_TESTS=OFF",
        ]
    )
    run(
        [
            "cmake",
            "--build",
            str(build),
            "--target",
            "vvencapp",
            "--parallel",
            str(max(1, args.jobs)),
        ]
    )
    vvencapp = locate_vvencapp(source)
    result = {
        "schema": "glic-vvc-reference-build-v1",
        "repository": VVENC_REPOSITORY,
        "tag": VVENC_TAG,
        "commit": revision,
        "vvencapp": str(vvencapp),
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
