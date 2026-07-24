#!/usr/bin/env python3
"""Check that the source tree is safe and complete enough for public review."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path


REQUIRED_PATHS = (
    ".gitattributes",
    ".gitignore",
    ".gitmodules",
    ".github/workflows/ci.yml",
    "CHANGELOG.md",
    "CMakeLists.txt",
    "CONTRIBUTING.md",
    "config/public-release-policy.json",
    "FILE-STRUCTURE.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "docs/BUILDING.md",
    "docs/AI_INTEGRATION.md",
    "docs/CODEC_GLITCH.md",
    "docs/CODEC_LAB.md",
    "docs/DOWNSTREAM_QUICKSTART.md",
    "docs/EMBEDDING.md",
    "docs/MULTICODEC_GLITCH.md",
    "docs/OFFLINE_PACKET_GLITCH.md",
    "docs/ORIGINAL_PRESET_REALTIME.md",
    "docs/PUBLIC_RELEASE.md",
    "external/stb/LICENSE",
    "external/stb/stb_image.h",
    "include/glic_metal/glic_metal.h",
    "include/glic_metal/codec_glitch.h",
    "include/glic_metal/glic_metal_metal.h",
    "include/glic_metal/glitch_presets.h",
    "include/module.modulemap",
    "cmake/GlicMetalConfig.cmake.in",
    "cmake/GlicMetalResources.cmake",
    "examples/embed_c.c",
    "presets.upstream.sha256",
    "scripts/check_public_release.py",
    "scripts/build_macos_sdk.sh",
    "scripts/evaluate_offline_packet_glitches.py",
    "scripts/evolutionary_codec_search.py",
    "scripts/process_codec_lab.py",
    "scripts/process_structured_codec_glitch.py",
    "scripts/structured_bitstream.py",
    "scripts/process_transport_glitch.py",
    "scripts/transport_glitch.py",
    "scripts/process_metadata_glitch.py",
    "scripts/process_multicodec_glitch.py",
    "scripts/process_offline_packet_glitch.py",
    "scripts/probe_multicodec_capabilities.py",
    "scripts/select_novel_moderate_presets.py",
    "resources/SDK-README.md",
    "resources/integration-manifest.json",
    "resources/codec-lab-effects.json",
    "resources/offline-codec-effects.json",
    "resources/selected-presets.json",
    "requirements-qa.txt",
    "src/glic_metal_c.cpp",
    "tests/consumer/CMakeLists.txt",
    "tests/consumer/main.c",
    "tests/embed_c_api_tests.c",
    "tests/embed_metal_api_tests.mm",
    "tests/test_integration_manifest.py",
    "tests/test_codec_lab.py",
    "tests/test_structured_bitstream.py",
    "tests/test_structured_codec_glitch.py",
    "tests/test_transport_glitch.py",
    "tests/test_metadata_glitch.py",
    "tests/test_offline_packet_glitch.py",
    "tests/test_select_novel_moderate_presets.py",
    "tests/run_installed_consumer.cmake",
    "tests/run_subdirectory_consumer.cmake",
    "tests/subdirectory_consumer/CMakeLists.txt",
    "website/index.html",
)

FORBIDDEN_TRACKED_PARTS = {
    ".cache",
    ".claude",
    ".idea",
    ".next",
    ".vscode",
    "__pycache__",
    "build",
    "search-runs",
    "test-videos",
}

BINARY_SUFFIXES = {
    ".a",
    ".dylib",
    ".glic",
    ".icns",
    ".jpg",
    ".jpeg",
    ".o",
    ".png",
    ".so",
}

UNIX_PRIVATE_PATH = rb"/(?:" + rb"Users|home" + rb")/[A-Za-z0-9._-]+/"
WINDOWS_PRIVATE_PATH = rb"[A-Za-z]:\\" + rb"Users\\"
ABSOLUTE_PRIVATE_PATH = re.compile(rb"(?:" + UNIX_PRIVATE_PATH + rb"|" + WINDOWS_PRIVATE_PATH + rb")")
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


class LocalLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.targets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"a", "img", "script", "link"}:
            return
        attributes = dict(attrs)
        target = attributes.get("href") or attributes.get("src")
        if target:
            self.targets.append(target)


def git_paths(source: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(source), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def verify_manifest(source: Path, errors: list[str]) -> int:
    manifest = source / "presets.upstream.sha256"
    expected_count = None
    entries: list[tuple[str, str]] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.startswith("# count="):
            expected_count = int(line.removeprefix("# count="))
        elif line and not line.startswith("#"):
            digest, name = line.split(maxsplit=1)
            entries.append((digest, name.strip()))

    if expected_count != len(entries):
        errors.append(
            f"preset manifest count mismatch: header={expected_count}, entries={len(entries)}"
        )

    for digest, name in entries:
        preset = source / "presets" / name
        if not preset.is_file():
            errors.append(f"preset missing: presets/{name}")
            continue
        actual = hashlib.sha256(preset.read_bytes()).hexdigest()
        if actual != digest:
            errors.append(f"preset checksum mismatch: presets/{name}")
    return len(entries)


def verify_markdown_links(source: Path, errors: list[str]) -> int:
    documents = [
        source / "README.md",
        source / "CONTRIBUTING.md",
        source / "FILE-STRUCTURE.md",
        source / "THIRD_PARTY_NOTICES.md",
        source / "docs" / "BUILDING.md",
        source / "docs" / "AI_INTEGRATION.md",
        source / "docs" / "EMBEDDING.md",
        source / "docs" / "PUBLIC_RELEASE.md",
    ]
    checked = 0
    for document in documents:
        text = document.read_text(encoding="utf-8")
        for target in MARKDOWN_LINK.findall(text):
            target = target.strip().split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            checked += 1
            resolved = (document.parent / target).resolve()
            try:
                resolved.relative_to(source.resolve())
            except ValueError:
                errors.append(f"documentation link escapes source tree: {document.name} -> {target}")
                continue
            if not resolved.exists():
                errors.append(f"broken documentation link: {document.relative_to(source)} -> {target}")
    return checked


def gallery_size(source: Path) -> tuple[int, int]:
    gallery = source / "output" / "preset-gallery"
    files = [path for path in gallery.rglob("*") if path.is_file()]
    return len(files), sum(path.stat().st_size for path in files)


def verify_gallery_policy(
    source: Path, gallery_bytes: int, errors: list[str], warnings: list[str]
) -> None:
    policy_path = source / "config" / "public-release-policy.json"
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        if policy.get("schema") != "glic-metal-public-release-policy-v1":
            errors.append("public release policy has an unsupported schema")
            return
        gallery_policy = policy["preset_gallery"]
        if gallery_policy.get("distribution") != "keep-in-git":
            warnings.append(
                "committed preset gallery has no keep-in-git release decision"
            )
            return
        maximum_total = int(gallery_policy["maximum_total_mib"]) * 1024 * 1024
        maximum_single = (
            int(gallery_policy["maximum_single_file_mib"]) * 1024 * 1024
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError) as exc:
        errors.append(f"invalid public release policy: {exc}")
        return

    if gallery_bytes > maximum_total:
        warnings.append(
            "committed preset gallery exceeds its documented total-size policy"
        )
    gallery = source / "output" / "preset-gallery"
    oversized = [
        path.relative_to(source).as_posix()
        for path in gallery.rglob("*")
        if path.is_file() and path.stat().st_size > maximum_single
    ]
    if oversized:
        warnings.append(
            "committed preset gallery exceeds its single-file policy: "
            + ", ".join(oversized[:5])
        )


def verify_html_links(source: Path, errors: list[str]) -> int:
    document = source / "website" / "index.html"
    parser = LocalLinkParser()
    parser.feed(document.read_text(encoding="utf-8"))
    checked = 0
    for target in parser.targets:
        path_target = target.split("#", 1)[0]
        if not path_target or "://" in path_target or path_target.startswith("data:"):
            continue
        checked += 1
        resolved = (document.parent / path_target).resolve()
        try:
            resolved.relative_to(source.resolve())
        except ValueError:
            errors.append(f"website link escapes source tree: {target}")
            continue
        if not resolved.exists():
            errors.append(f"broken website link: website/index.html -> {target}")
    return checked


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat release-size warnings as failures.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output.")
    args = parser.parse_args()
    source = args.source.resolve()

    errors: list[str] = []
    warnings: list[str] = []

    for relative in REQUIRED_PATHS:
        if not (source / relative).exists():
            errors.append(f"required path missing: {relative}")

    try:
        tracked = git_paths(source)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        errors.append(f"could not inspect tracked files: {exc}")
        tracked = []

    for relative in tracked:
        path = Path(relative)
        if path.name == ".DS_Store" or path.name == ".env" or path.name.startswith(".env."):
            errors.append(f"private/generated file is tracked: {relative}")
        if any(part in FORBIDDEN_TRACKED_PARTS for part in path.parts):
            errors.append(f"private/generated directory is tracked: {relative}")
        if any(part.startswith("build-") or part.startswith("cmake-build-") for part in path.parts):
            errors.append(f"build directory is tracked: {relative}")
        if path.parts and path.parts[0] == "output" and path.parts[:2] != ("output", "preset-gallery"):
            errors.append(f"non-gallery output is tracked: {relative}")

    scan_paths = {source / relative for relative in tracked}
    scan_paths.update(source / relative for relative in REQUIRED_PATHS)
    for path in sorted(scan_paths):
        if not path.is_file() or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        if path.stat().st_size > 2 * 1024 * 1024:
            continue
        data = path.read_bytes()
        if b"\0" not in data and ABSOLUTE_PRIVATE_PATH.search(data):
            errors.append(f"private absolute path found: {path.relative_to(source)}")

    preset_count = verify_manifest(source, errors) if (source / "presets.upstream.sha256").is_file() else 0
    link_count = verify_markdown_links(source, errors)
    html_link_count = verify_html_links(source, errors)
    gallery_files, gallery_bytes = gallery_size(source)
    verify_gallery_policy(source, gallery_bytes, errors, warnings)

    result = {
        "ok": not errors and (not args.strict or not warnings),
        "source": str(source),
        "tracked_files": len(tracked),
        "preset_manifest_entries": preset_count,
        "documentation_links_checked": link_count,
        "website_links_checked": html_link_count,
        "gallery_files": gallery_files,
        "gallery_bytes": gallery_bytes,
        "errors": errors,
        "warnings": warnings,
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        status = "PASS" if result["ok"] else "FAIL"
        print(f"public-release-check: {status}")
        print(f"tracked files: {len(tracked)}")
        print(f"preset manifest entries: {preset_count}")
        print(f"documentation links checked: {link_count}")
        print(f"website links checked: {html_link_count}")
        print(f"preset gallery: {gallery_files} files, {gallery_bytes / (1024 * 1024):.1f} MiB")
        for warning in warnings:
            print(f"warning: {warning}")
        for error in errors:
            print(f"error: {error}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
