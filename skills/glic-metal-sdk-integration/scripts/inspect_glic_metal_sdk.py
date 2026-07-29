#!/usr/bin/env python3
"""Inspect a GLIC Metal source tree, generated SDK, or CMake install prefix."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


LANES = ("all", "image", "codec", "offline")
PUBLIC_HEADERS = (
    "glic_metal/glic_metal.h",
    "glic_metal/glic_metal_metal.h",
    "glic_metal/codec_glitch.h",
    "glic_metal/glitch_presets.h",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="source, SDK, bundle, or install root")
    parser.add_argument("--lane", choices=LANES, default="all")
    parser.add_argument("--strict", action="store_true", help="fail on missing artifacts")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    return parser.parse_args()


def manifest_candidates(root: Path) -> list[tuple[str, Path, Path]]:
    return [
        ("source", root / "resources/integration-manifest.json", root),
        (
            "generated_sdk",
            root
            / "GlicMetalResources.bundle/Contents/Resources/integration-manifest.json",
            root,
        ),
        (
            "resource_bundle",
            root / "Contents/Resources/integration-manifest.json",
            root,
        ),
        (
            "cmake_install",
            root / "share/glic-metal/integration-manifest.json",
            root,
        ),
    ]


def locate_manifest(root: Path) -> tuple[str, Path, Path]:
    for layout, candidate, layout_root in manifest_candidates(root):
        if candidate.is_file():
            return layout, candidate, layout_root
    raise FileNotFoundError(
        "integration-manifest.json was not found in a supported GLIC Metal layout"
    )


def find_header(root: Path, layout: str, header: str) -> Path | None:
    direct = [
        root / "include" / header,
        root / header,
    ]
    for candidate in direct:
        if candidate.is_file():
            return candidate
    if layout == "generated_sdk":
        matches = sorted(
            (root / "GlicMetal.xcframework").glob(f"**/Headers/{header}")
        )
        if matches:
            return matches[0]
    return None


def check_path(
    result: dict[str, Any],
    label: str,
    path: Path,
    *,
    required: bool = True,
) -> bool:
    exists = path.exists()
    result["artifacts"][label] = {
        "path": str(path),
        "exists": exists,
    }
    if required and not exists:
        result["errors"].append(f"missing {label}: {path}")
    return exists


def inspect(root: Path, lane: str) -> dict[str, Any]:
    root = root.expanduser().resolve()
    layout, manifest_path, layout_root = locate_manifest(root)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result: dict[str, Any] = {
        "ok": True,
        "layout": layout,
        "root": str(layout_root),
        "manifest": str(manifest_path),
        "schema": manifest.get("schema"),
        "lane": lane,
        "library": manifest.get("library", {}),
        "selected_preset_bank": manifest.get("selected_preset_bank", {}),
        "realtime_acceptance": manifest.get("realtime_acceptance", {}),
        "artifacts": {},
        "headers": {},
        "errors": [],
        "warnings": [],
        "verification": manifest.get("verification", []),
    }

    for header in manifest.get("public_headers", PUBLIC_HEADERS):
        found = find_header(layout_root, layout, header)
        result["headers"][header] = str(found) if found else None
        if found is None:
            result["errors"].append(f"missing public header: {header}")

    wants_image = lane in ("all", "image")
    wants_codec = lane in ("all", "codec")
    wants_offline = lane in ("all", "offline")

    if layout == "source":
        check_path(result, "CMakeLists.txt", layout_root / "CMakeLists.txt")
        check_path(
            result,
            "AI integration contract",
            layout_root / "docs/AI_INTEGRATION.md",
        )
        check_path(
            result,
            "SDK builder",
            layout_root / "scripts/build_macos_sdk.sh",
        )
        if wants_image:
            check_path(result, "preset sources", layout_root / "presets")
            result["warnings"].append(
                "source layout requires a build before a consumable metallib exists"
            )
        if wants_codec:
            check_path(
                result,
                "Codec public header",
                layout_root / "include/glic_metal/codec_glitch.h",
            )
        if wants_offline:
            check_path(result, "offline tools", layout_root / "scripts")
            check_path(
                result,
                "offline packet catalog",
                layout_root / "resources/offline-codec-effects.json",
            )
            check_path(
                result,
                "codec lab catalog",
                layout_root / "resources/codec-lab-effects.json",
            )

    elif layout == "generated_sdk":
        resources = layout_root / "GlicMetalResources.bundle/Contents/Resources"
        release_manifest_path = layout_root / "RELEASE-MANIFEST.json"
        check_path(
            result, "GlicMetal.xcframework", layout_root / "GlicMetal.xcframework"
        )
        check_path(
            result, "AI integration contract", layout_root / "AI_INTEGRATION.md"
        )
        check_path(result, "Documentation", layout_root / "Documentation")
        check_path(result, "SHA256SUMS", layout_root / "SHA256SUMS")
        if check_path(result, "release manifest", release_manifest_path):
            try:
                release_manifest = json.loads(
                    release_manifest_path.read_text(encoding="utf-8")
                )
                result["release"] = release_manifest
                if release_manifest.get("schema") != "glic-metal-sdk-release-v1":
                    result["errors"].append(
                        "release manifest has an unsupported schema"
                    )
                revision = str(release_manifest.get("source_revision", ""))
                if len(revision) != 40 or any(
                    character not in "0123456789abcdef" for character in revision
                ):
                    result["errors"].append(
                        "release manifest source_revision must be a full Git SHA"
                    )
                if release_manifest.get("source_dirty") is not False:
                    result["errors"].append(
                        "release manifest reports a dirty source worktree"
                    )
                bundled_release_manifest = (
                    resources / "RELEASE-MANIFEST.json"
                )
                if not check_path(
                    result,
                    "bundled release manifest",
                    bundled_release_manifest,
                ):
                    pass
                elif (
                    bundled_release_manifest.read_bytes()
                    != release_manifest_path.read_bytes()
                ):
                    result["errors"].append(
                        "root and bundled release manifests differ"
                    )
            except json.JSONDecodeError:
                result["errors"].append("release manifest is not valid JSON")
        check_path(
            result,
            "agent skill",
            layout_root / "Skills/glic-metal-sdk-integration/SKILL.md",
        )
        if wants_image:
            check_path(result, "Presets", resources / "Presets")
            check_path(
                result, "glic_realtime.metallib", resources / "glic_realtime.metallib"
            )
        if wants_codec:
            check_path(
                result, "glic_realtime.metallib", resources / "glic_realtime.metallib"
            )
        if wants_offline:
            check_path(result, "Tools", layout_root / "Tools")
            check_path(
                result,
                "offline packet catalog",
                resources / "offline-codec-effects.json",
            )
            check_path(
                result, "codec lab catalog", resources / "codec-lab-effects.json"
            )

    elif layout == "resource_bundle":
        resources = layout_root / "Contents/Resources"
        result["warnings"].append(
            "only a resource bundle was supplied; inspect the parent SDK for headers"
        )
        if wants_image or wants_codec:
            check_path(
                result, "glic_realtime.metallib", resources / "glic_realtime.metallib"
            )
        if wants_image:
            check_path(result, "Presets", resources / "Presets")
        if wants_offline:
            check_path(
                result,
                "offline packet catalog",
                resources / "offline-codec-effects.json",
            )

    elif layout == "cmake_install":
        share = layout_root / "share/glic-metal"
        check_path(
            result,
            "AI integration contract",
            layout_root / "share/doc/glic-metal/AI_INTEGRATION.md",
        )
        if wants_image:
            check_path(result, "Presets", share / "presets")
            check_path(
                result,
                "glic_realtime.metallib",
                layout_root / "lib/glic/glic_realtime.metallib",
            )
        if wants_offline:
            check_path(result, "installed tools", layout_root / "bin")
            check_path(
                result,
                "offline packet catalog",
                share / "offline-codec-effects.json",
            )
            check_path(
                result, "codec lab catalog", share / "codec-lab-effects.json"
            )

    result["ok"] = not result["errors"]
    return result


def print_human(result: dict[str, Any]) -> None:
    print(f"layout: {result['layout']}")
    print(f"root: {result['root']}")
    print(f"manifest: {result['manifest']}")
    library = result["library"]
    print(
        "library: "
        f"{library.get('name', 'unknown')} "
        f"(CMake {library.get('cmake_target', 'unknown')}, "
        f"Swift {library.get('swift_module', 'unknown')})"
    )
    bank = result["selected_preset_bank"]
    print(
        "production presets: "
        f"{bank.get('count', 'unknown')} "
        f"{bank.get('category_counts', {})}"
    )
    print(f"lane: {result['lane']}")
    for error in result["errors"]:
        print(f"ERROR: {error}")
    for warning in result["warnings"]:
        print(f"WARN: {warning}")
    print("status:", "PASS" if result["ok"] else "FAIL")


def main() -> int:
    args = parse_args()
    try:
        result = inspect(args.root, args.lane)
    except (FileNotFoundError, json.JSONDecodeError) as error:
        result = {
            "ok": False,
            "root": str(args.root.expanduser().resolve()),
            "lane": args.lane,
            "errors": [str(error)],
        }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        if "layout" in result:
            print_human(result)
        else:
            print(f"ERROR: {result['errors'][0]}")
    return 1 if args.strict and not result["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
