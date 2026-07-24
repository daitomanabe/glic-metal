#!/usr/bin/env python3
"""Install the pinned official FFglitch binary without vendoring it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile


VERSION = "0.10.2"
DOWNLOAD_PAGE = "https://ffglitch.org/download/"
RELEASES = {
    ("Darwin", "arm64"): {
        "url": (
            "https://ffglitch.org/pub/bin/macos-aarch64/"
            "ffglitch-0.10.2-macos-aarch64.zip"
        ),
        "sha256": (
            "25a5a7d51217919db5a093b211fd8a1e27845284efb0a6f616b8f371f6638eef"
        ),
        "directory": "ffglitch-0.10.2-macos-aarch64",
    }
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_extract(archive: Path, destination: Path) -> None:
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            try:
                target.relative_to(root)
            except ValueError as error:
                raise RuntimeError(
                    f"archive member escapes destination: {member.filename}"
                ) from error
        bundle.extractall(destination)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download and verify the pinned official FFglitch reference tools."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".cache") / "ffglitch" / VERSION,
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--print-ffedit", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    release = RELEASES.get((platform.system(), platform.machine()))
    if release is None:
        supported = ", ".join(
            f"{system}/{machine}" for system, machine in sorted(RELEASES)
        )
        raise RuntimeError(
            "no checksum-pinned FFglitch binary is registered for "
            f"{platform.system()}/{platform.machine()}; supported: {supported}. "
            f"Install ffedit manually from {DOWNLOAD_PAGE}"
        )
    output = args.output_dir.expanduser().resolve()
    ffedit = output / "bin" / "ffedit"
    if ffedit.is_file() and os.access(ffedit, os.X_OK) and not args.force:
        print(
            ffedit
            if args.print_ffedit
            else f"FFglitch already installed: {output}"
        )
        return 0

    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="glic-ffglitch-", dir=parent
    ) as temporary_name:
        temporary = Path(temporary_name)
        archive = temporary / "ffglitch.zip"
        request = urllib.request.Request(
            release["url"], headers={"User-Agent": "glic-metal-reference-installer"}
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            with archive.open("wb") as destination:
                shutil.copyfileobj(response, destination)
        actual = sha256(archive)
        if actual != release["sha256"]:
            raise RuntimeError(
                f"FFglitch checksum mismatch: expected {release['sha256']}, "
                f"received {actual}"
            )
        unpacked = temporary / "unpacked"
        unpacked.mkdir()
        safe_extract(archive, unpacked)
        source = unpacked / release["directory"]
        for executable in ("ffedit", "ffgac", "fflive", "qjs"):
            candidate = source / executable
            if not candidate.is_file():
                raise RuntimeError(
                    f"official FFglitch archive is missing {executable}"
                )
        staged = temporary / "install"
        bin_dir = staged / "bin"
        bin_dir.mkdir(parents=True)
        for executable in ("ffedit", "ffgac", "fflive", "qjs"):
            shutil.copy2(source / executable, bin_dir / executable)
            (bin_dir / executable).chmod(0o755)
        shutil.copy2(source / "readme.txt", staged / "README.ffglitch.txt")
        (staged / "install.json").write_text(
            json.dumps(
                {
                    "schema": "glic-ffglitch-reference-install-v1",
                    "version": VERSION,
                    "source": release["url"],
                    "archive_sha256": actual,
                    "license": "GPL-2.0-or-later",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if output.exists():
            if not args.force:
                raise RuntimeError(f"output already exists: {output}")
            shutil.rmtree(output)
        shutil.move(staged, output)

    print(ffedit if args.print_ffedit else f"FFglitch installed: {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, urllib.error.URLError) as error:
        print(f"error: {error}")
        raise SystemExit(1)
