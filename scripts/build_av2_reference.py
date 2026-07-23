#!/usr/bin/env python3
"""Build the pinned AOMedia AV2 v1.0.0 reference encoder and decoder."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

AVM_REPOSITORY = "https://github.com/AOMediaCodec/avm.git"
AVM_TAG = "v1.0.0"
AVM_COMMIT = "966a7d7cd6fcf60360caf5dc413b2aeeb65e144d"


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Build the official AVM AV2 v1.0.0 reference tools."
    )
    parser.add_argument(
        "--source-dir", type=Path, default=root / ".cache" / "avm-v1.0.0"
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
                AVM_TAG,
                "--depth",
                "1",
                AVM_REPOSITORY,
                str(source),
            ]
        )
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if revision != AVM_COMMIT:
        raise RuntimeError(
            f"AVM {AVM_TAG} resolved to {revision}, expected {AVM_COMMIT}"
        )

    run(
        [
            "cmake",
            "-S",
            str(source),
            "-B",
            str(build),
            "-DENABLE_DOCS=OFF",
            "-DENABLE_TESTS=OFF",
            "-DENABLE_EXAMPLES=ON",
            "-DCONFIG_AV2=1",
        ]
    )
    run(
        [
            "cmake",
            "--build",
            str(build),
            "--target",
            "avmenc",
            "avmdec",
            "--parallel",
            str(max(1, args.jobs)),
        ]
    )
    result = {
        "schema": "glic-av2-reference-build-v1",
        "repository": AVM_REPOSITORY,
        "tag": AVM_TAG,
        "commit": revision,
        "avmenc": str(build / "avmenc"),
        "avmdec": str(build / "avmdec"),
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
