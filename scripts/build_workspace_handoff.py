#!/usr/bin/env python3
"""Build a verified GLIC Metal workspace transfer package."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SUBMODULE = ROOT / "external" / "stb"

DATA_GROUPS = (
    {
        "name": "test-materials",
        "paths": ("assets", "test-videos"),
        "role": "required test input and accumulated real-video validation",
        "required_for_full_handoff": True,
    },
    {
        "name": "gallery-evidence",
        "paths": (
            "output/glitch-algorithm-gallery",
            "output/glitch-algorithm-gallery-smoke",
            "output/glitch-algorithm-gallery-smoke-codecs",
            "output/glitch-gallery-structured-fix",
        ),
        "role": "441-video gallery, thumbnails, task states, QA, and smoke evidence",
        "required_for_full_handoff": True,
    },
    {
        "name": "search-evidence",
        "paths": ("search-runs",),
        "role": "preset search archives, rankings, and remote-search evidence",
        "required_for_full_handoff": True,
    },
    {
        "name": "codec-toolchains-cache",
        "paths": (".cache",),
        "role": "pinned FFglitch, AVM, VVenC, x264/x265/FFmpeg hook sources and builds",
        "required_for_full_handoff": True,
    },
    {
        "name": "local-plans",
        "paths": ("plans-local",),
        "role": "ignored implementation planning context",
        "required_for_full_handoff": True,
    },
)


def parse_args() -> argparse.Namespace:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output_dir",
        nargs="?",
        type=Path,
        default=Path.home() / "Desktop" / f"glic-metal-handoff-{timestamp}",
    )
    parser.add_argument(
        "--include-large-data",
        action="store_true",
        help="Archive ignored test, gallery, search, and codec-cache data",
    )
    parser.add_argument(
        "--skip-sdk",
        action="store_true",
        help="Do not build the distributable GlicMetalSDK",
    )
    return parser.parse_args()


def command(
    arguments: list[str],
    *,
    cwd: Path = ROOT,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


def output(arguments: list[str], *, cwd: Path = ROOT) -> str:
    result = command(arguments, cwd=cwd, check=False)
    text = (result.stdout or result.stderr).strip()
    return text


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def archive_pipeline(
    producer: list[str],
    destination: Path,
    *,
    cwd: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    zstd = shutil.which("zstd")
    if zstd is None:
        raise RuntimeError("zstd is required")
    print(f"ARCHIVE {destination.name}", flush=True)
    with destination.open("wb") as encoded:
        source = subprocess.Popen(
            producer,
            cwd=cwd,
            stdout=subprocess.PIPE,
            env={**dict(os.environ), "COPYFILE_DISABLE": "1"},
        )
        assert source.stdout is not None
        compressor = subprocess.Popen(
            [zstd, "-T0", "-3", "-q", "-c"],
            stdin=source.stdout,
            stdout=encoded,
        )
        source.stdout.close()
        compressor_status = compressor.wait()
        producer_status = source.wait()
    if producer_status != 0 or compressor_status != 0:
        raise RuntimeError(
            f"archive pipeline failed: producer={producer_status}, "
            f"zstd={compressor_status}"
        )
    print(
        f"DONE {destination.name} {destination.stat().st_size} bytes",
        flush=True,
    )


def git_source_archive(repository: Path, destination: Path, prefix: str) -> None:
    archive_pipeline(
        [
            "git",
            "archive",
            "--format=tar",
            f"--prefix={prefix}",
            "HEAD",
        ],
        destination,
        cwd=repository,
    )


def data_archive(paths: tuple[str, ...], destination: Path) -> None:
    existing = [path for path in paths if (ROOT / path).exists()]
    if not existing:
        print(f"SKIP {destination.name}: no source paths", flush=True)
        return
    archive_pipeline(["tar", "-cf", "-", *existing], destination, cwd=ROOT)


def directory_stats(path: Path) -> dict[str, int]:
    files = [candidate for candidate in path.rglob("*") if candidate.is_file()]
    return {
        "file_count": len(files),
        "bytes": sum(candidate.stat().st_size for candidate in files),
    }


def file_record(path: Path, package: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(package).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
    }


def environment_report() -> dict[str, str]:
    developer_dir = "/Applications/Xcode.app/Contents/Developer"
    return {
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "macos": output(["sw_vers"]),
        "xcode": output(
            [
                "env",
                f"DEVELOPER_DIR={developer_dir}",
                "xcodebuild",
                "-version",
            ]
        ),
        "metal": output(
            [
                "env",
                f"DEVELOPER_DIR={developer_dir}",
                "xcrun",
                "-f",
                "metal",
            ]
        ),
        "cmake": output(["cmake", "--version"]).splitlines()[0],
        "clang": output(["clang", "--version"]).splitlines()[0],
        "python": output(["python3", "--version"]),
        "ffmpeg": output(["ffmpeg", "-version"]).splitlines()[0],
        "zstd": output(["zstd", "--version"]).splitlines()[0],
    }


def main() -> int:
    args = parse_args()
    package = args.output_dir.expanduser().resolve()
    if package.exists() and any(package.iterdir()):
        raise RuntimeError(f"output directory is not empty: {package}")
    package.mkdir(parents=True, exist_ok=True)

    tracked_status = output(
        ["git", "status", "--porcelain", "--untracked-files=no"]
    )
    if tracked_status:
        raise RuntimeError(
            "tracked worktree changes must be committed before packaging:\n"
            + tracked_status
        )
    if output(["git", "status", "--porcelain"], cwd=SUBMODULE):
        raise RuntimeError("external/stb submodule is dirty")

    print("COPY handoff documents", flush=True)
    shutil.copy2(ROOT / "docs" / "CODEX_HANDOFF.md", package / "HANDOFF.md")
    summary = ROOT / "agent-conversation-summary.md"
    if summary.is_file():
        shutil.copy2(summary, package / summary.name)

    git_dir = package / "git"
    git_dir.mkdir()
    print("BUNDLE repository history", flush=True)
    command(
        [
            "git",
            "bundle",
            "create",
            str(git_dir / "glic-metal.git.bundle"),
            "--all",
        ]
    )
    command(
        [
            "git",
            "bundle",
            "create",
            str(git_dir / "stb.git.bundle"),
            "--all",
        ],
        cwd=SUBMODULE,
    )

    source_dir = package / "source"
    git_source_archive(
        ROOT, source_dir / "glic-metal-source.tar.zst", "glic-metal/"
    )
    git_source_archive(
        SUBMODULE,
        source_dir / "stb-source.tar.zst",
        "glic-metal/external/stb/",
    )

    sdk_stats: dict[str, int] | None = None
    if not args.skip_sdk:
        sdk_dir = package / "sdk" / "GlicMetalSDK"
        sdk_dir.parent.mkdir()
        print("BUILD distributable SDK", flush=True)
        subprocess.run(
            [str(ROOT / "scripts" / "build_macos_sdk.sh"), str(sdk_dir)],
            cwd=ROOT,
            check=True,
        )
        inspector = (
            ROOT
            / "skills"
            / "glic-metal-sdk-integration"
            / "scripts"
            / "inspect_glic_metal_sdk.py"
        )
        command(
            [
                sys.executable,
                str(inspector),
                str(sdk_dir),
                "--lane",
                "all",
                "--strict",
            ]
        )
        command(
            ["shasum", "-a", "256", "-c", "SHA256SUMS"], cwd=sdk_dir
        )
        sdk_stats = directory_stats(sdk_dir)

    archives: list[dict[str, Any]] = []
    if args.include_large_data:
        data_dir = package / "data"
        for group in DATA_GROUPS:
            destination = data_dir / f"{group['name']}.tar.zst"
            data_archive(group["paths"], destination)
            if destination.is_file():
                archives.append(
                    {
                        **file_record(destination, package),
                        "role": group["role"],
                        "restore_to": "repository_root",
                        "required_for_full_handoff": (
                            group["required_for_full_handoff"]
                        ),
                        "source_paths": list(group["paths"]),
                    }
                )

    selected = json.loads(
        (ROOT / "resources" / "selected-presets.json").read_text()
    )
    gallery = json.loads(
        (
            ROOT
            / "output"
            / "glitch-algorithm-gallery"
            / "site"
            / "manifest.json"
        ).read_text()
    )
    manifest = {
        "schema": "glic-metal-workspace-handoff-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "branch": output(["git", "branch", "--show-current"]),
            "head": output(["git", "rev-parse", "HEAD"]),
            "describe": output(["git", "describe", "--always", "--dirty"]),
            "remote_fetch": output(["git", "remote", "get-url", "origin"]),
            "remote_push": output(["git", "remote", "get-url", "--push", "origin"]),
            "working_tree_clean": True,
            "submodule": {
                "path": "external/stb",
                "head": output(["git", "rev-parse", "HEAD"], cwd=SUBMODULE),
            },
        },
        "product": {
            "gallery_algorithms": gallery["algorithm_count"],
            "gallery_variants": gallery["rendered_video_count"],
            "gallery_status_counts": gallery["status_counts"],
            "gallery_curation": gallery.get("curation"),
            "realtime_preset_count": selected["count"],
            "realtime_category_counts": selected["category_counts"],
            "offline_adopted_count": selected["source"][
                "offline_adopted_count"
            ],
        },
        "environment": environment_report(),
        "distribution_sdk": sdk_stats,
        "large_data_included": args.include_large_data,
        "data_archives": archives,
        "excluded": {
            "build_directories": (
                "machine-specific CMake caches and absolute paths; rebuild"
            ),
            "raw_agent_sessions": (
                "privacy and credential boundary; sanitized summary included"
            ),
            "projects_site_runtime": (
                "gallery site is contained in gallery-evidence archive"
            ),
        },
    }
    manifest_path = package / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    checksum_lines = []
    for path in sorted(
        candidate
        for candidate in package.rglob("*")
        if candidate.is_file() and candidate.name != "SHA256SUMS"
    ):
        checksum_lines.append(
            f"{sha256(path)}  {path.relative_to(package).as_posix()}"
        )
    (package / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8"
    )
    print(json.dumps({"package": str(package), **directory_stats(package)}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
