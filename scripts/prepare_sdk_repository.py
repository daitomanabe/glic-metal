#!/usr/bin/env python3
"""Create the versioned glic-metal-sdk repository payload and release assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MANAGED_PATHS = (
    "AI_INTEGRATION.md",
    "CHANGELOG.md",
    "Documentation",
    "LICENSE",
    "Package.swift",
    "README.md",
    "RELEASE-MANIFEST.json",
    "Skills",
    "Sources",
    "THIRD_PARTY_NOTICES.md",
    "dist",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--repository",
        default="https://github.com/daitomanabe/glic-metal-sdk",
    )
    parser.add_argument("--source-root", type=Path, default=ROOT)
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_checksums(sdk_root: Path) -> None:
    checksum_path = sdk_root / "SHA256SUMS"
    if not checksum_path.is_file():
        raise ValueError(f"missing SDK checksums: {checksum_path}")
    checked = 0
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, separator, relative = line.partition("  ")
        if not separator or not SHA256_RE.fullmatch(expected):
            raise ValueError(f"invalid checksum row: {line}")
        candidate = (sdk_root / relative).resolve()
        try:
            candidate.relative_to(sdk_root.resolve())
        except ValueError as error:
            raise ValueError(f"checksum path escapes SDK root: {relative}") from error
        if not candidate.is_file():
            raise ValueError(f"checksummed SDK file is missing: {relative}")
        actual = sha256(candidate)
        if actual != expected:
            raise ValueError(
                f"SDK checksum mismatch for {relative}: {actual} != {expected}"
            )
        checked += 1
    if checked == 0:
        raise ValueError("SDK checksum file is empty")


def validate_sdk(sdk_root: Path, version: str) -> dict[str, object]:
    required = (
        "AI_INTEGRATION.md",
        "Documentation",
        "GlicMetal.xcframework",
        "GlicMetalResources.bundle/Contents/Resources",
        "README.md",
        "RELEASE-MANIFEST.json",
        "SHA256SUMS",
        "Skills/glic-metal-sdk-integration/SKILL.md",
        "Tools",
    )
    missing = [relative for relative in required if not (sdk_root / relative).exists()]
    if missing:
        raise ValueError(f"SDK is incomplete: {', '.join(missing)}")
    validate_checksums(sdk_root)
    release = json.loads(
        (sdk_root / "RELEASE-MANIFEST.json").read_text(encoding="utf-8")
    )
    if release.get("schema") != "glic-metal-sdk-release-v1":
        raise ValueError("SDK release manifest has an unsupported schema")
    if release.get("sdk_version") != version:
        raise ValueError(
            "SDK version mismatch: "
            f"{release.get('sdk_version')} != {version}"
        )
    revision = str(release.get("source_revision", ""))
    if not FULL_SHA_RE.fullmatch(revision):
        raise ValueError("SDK release manifest requires a full source Git SHA")
    if release.get("source_dirty") is not False:
        raise ValueError("refusing to publish an SDK built from dirty source")
    return release


def reset_managed_output(output: Path) -> None:
    resolved = output.resolve()
    if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
        raise ValueError(f"unsafe SDK repository output: {output}")
    output.mkdir(parents=True, exist_ok=True)
    for relative in MANAGED_PATHS:
        target = output / relative
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)


def copy_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, symlinks=True)


def zip_directory(source: Path, archive: Path, root_name: str) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as output:
        for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_dir():
                continue
            relative = path.relative_to(source)
            archive_name = (Path(root_name) / relative).as_posix()
            info = zipfile.ZipInfo(archive_name, (1980, 1, 1, 0, 0, 0))
            mode = path.lstat().st_mode
            info.create_system = 3
            info.external_attr = (mode & 0xFFFF) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            if path.is_symlink():
                info.external_attr = (
                    stat.S_IFLNK | stat.S_IMODE(mode)
                ) << 16
                payload = os.readlink(path).encode("utf-8")
            else:
                payload = path.read_bytes()
            output.writestr(info, payload)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def package_swift(
    *,
    repository: str,
    version: str,
    checksum: str,
    minimum_macos: str,
) -> str:
    major = int(minimum_macos.split(".", 1)[0])
    artifact_url = (
        f"{repository}/releases/download/v{version}/"
        "GlicMetal.xcframework.zip"
    )
    return f"""// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "GlicMetalSDK",
    platforms: [
        .macOS(.v{major})
    ],
    products: [
        .library(
            name: "GlicMetal",
            targets: ["GlicMetal", "GlicMetalResources"]
        )
    ],
    targets: [
        .binaryTarget(
            name: "GlicMetal",
            url: "{artifact_url}",
            checksum: "{checksum}"
        ),
        .target(
            name: "GlicMetalResources",
            resources: [
                .copy("Resources")
            ],
            linkerSettings: [
                .linkedLibrary("c++"),
                .linkedFramework("Foundation"),
                .linkedFramework("Metal"),
                .linkedFramework("CoreImage"),
                .linkedFramework("CoreGraphics"),
                .linkedFramework("CoreMedia"),
                .linkedFramework("CoreVideo"),
                .linkedFramework("VideoToolbox")
            ]
        )
    ]
)
"""


def resource_helper() -> str:
    return """import Foundation

public enum GlicMetalSDKResources {
    public static let bundle = Bundle.module

    public static var rootURL: URL {
        guard let url = bundle.resourceURL?.appendingPathComponent(
            "Resources",
            isDirectory: true
        ) else {
            preconditionFailure("GlicMetal SDK resource bundle is unavailable")
        }
        return url
    }

    public static var presetsURL: URL {
        rootURL.appendingPathComponent("Presets", isDirectory: true)
    }

    public static var metalLibraryURL: URL {
        rootURL.appendingPathComponent("glic_realtime.metallib")
    }

    public static var integrationManifestURL: URL {
        rootURL.appendingPathComponent("integration-manifest.json")
    }

    public static var releaseManifestURL: URL {
        rootURL.appendingPathComponent("RELEASE-MANIFEST.json")
    }
}
"""


def readme(
    *,
    version: str,
    repository: str,
    source_repository: str,
    source_revision: str,
    architectures: list[str],
    minimum_macos: str,
) -> str:
    architecture_text = ", ".join(architectures)
    return f"""# GLIC Metal SDK

[日本語](#日本語) | [English](#english)

Version `{version}` · macOS {minimum_macos}+ · {architecture_text}

This repository is the generated distribution surface for
[glic-metal]({source_repository}). Do not edit generated SDK contracts,
resources, or binaries independently of the source repository.

## 日本語

GLIC Metalを他のmacOSアプリへ組み込むためのversion固定SDKです。
開発・テスト・ギャラリーの正本は`glic-metal`に残し、このリポジトリは
検証済み配布物、Swift Package、組み込み資料だけを提供します。

### Swift Package Manager

XcodeのPackage Dependenciesへ次を追加します。

```text
{repository}
```

product `GlicMetal`を選択し、Swiftでは次の2 moduleを使用します。

```swift
import GlicMetal
import GlicMetalResources

let presets = GlicMetalSDKResources.presetsURL.path
let metallib = GlicMetalSDKResources.metalLibraryURL.path
```

採用済みリアルタイムpresetは28件です。内訳はOriginal 14、Spatial 8、
Codec 6です。offline codec／packet／syntax処理はSwift Packageの
リアルタイムcallbackへ混在させず、Releaseの完全SDKに含まれる`Tools/`を
別processとして使用してください。

Xcodeへ手動組み込みする場合はReleaseから
`GlicMetalSDK-{version}-macos-{architecture_text.replace(', ', '-')}.zip`
を取得し、`GlicMetal.xcframework`と`GlicMetalResources.bundle`を追加します。
最初に`AI_INTEGRATION.md`を読み、次に`Documentation/EMBEDDING.md`を参照します。

## English

This is the versioned distribution repository for integrating GLIC Metal into
other macOS applications. The source implementation, tests, gallery, and SDK
builder remain in [glic-metal]({source_repository}).

Add `{repository}` as a Swift package and select the `GlicMetal` product.
Import `GlicMetal` for the C ABI and `GlicMetalResources` for version-matched
preset, metallib, integration-manifest, and release-manifest URLs.

The production menu contains 28 realtime-certified presets: 14 Original,
eight Spatial, and six Codec. Keep offline codec, packet, transport, metadata,
and compressed-syntax tools outside capture and render callbacks. They are
available in the complete SDK release asset.

Source revision: [`{source_revision}`]({source_repository}/commit/{source_revision})

## Integrity

Every release publishes:

- `GlicMetal.xcframework.zip` for SwiftPM;
- the complete versioned SDK archive;
- `SHA256SUMS`;
- `RELEASE-MANIFEST.json` with source revision, ABI, platform, and preset counts.

Run the bundled inspector and checksum verification before integration.
"""


def main() -> int:
    args = parse_args()
    sdk_root = args.sdk_root.expanduser().resolve()
    output = args.output.expanduser().resolve()
    source_root = args.source_root.expanduser().resolve()
    if not VERSION_RE.fullmatch(args.version):
        raise ValueError(f"invalid semantic version: {args.version}")
    if not sdk_root.is_dir():
        raise ValueError(f"SDK root does not exist: {sdk_root}")
    for protected in (sdk_root, source_root):
        if output == protected or protected.is_relative_to(output):
            raise ValueError(
                f"SDK repository output would contain protected input: {output}"
            )
    release = validate_sdk(sdk_root, args.version)
    reset_managed_output(output)

    distribution = output / "dist"
    xcframework_archive = distribution / "GlicMetal.xcframework.zip"
    architecture_slug = "-".join(release["architectures"])
    complete_name = (
        f"GlicMetalSDK-{args.version}-macos-{architecture_slug}.zip"
    )
    complete_archive = distribution / complete_name
    zip_directory(
        sdk_root / "GlicMetal.xcframework",
        xcframework_archive,
        "GlicMetal.xcframework",
    )
    zip_directory(sdk_root, complete_archive, "GlicMetalSDK")
    swiftpm_checksum = sha256(xcframework_archive)

    copy_tree(sdk_root / "Documentation", output / "Documentation")
    copy_tree(sdk_root / "Skills", output / "Skills")
    shutil.copy2(sdk_root / "AI_INTEGRATION.md", output / "AI_INTEGRATION.md")
    resources = (
        sdk_root / "GlicMetalResources.bundle/Contents/Resources"
    )
    copy_tree(
        resources,
        output / "Sources/GlicMetalResources/Resources",
    )
    write_text(
        output / "Sources/GlicMetalResources/GlicMetalSDKResources.swift",
        resource_helper(),
    )
    shutil.copy2(resources / "LICENSE", output / "LICENSE")
    shutil.copy2(
        resources / "THIRD_PARTY_NOTICES.md",
        output / "THIRD_PARTY_NOTICES.md",
    )

    minimum_macos = str(release["minimum_macos"])
    write_text(
        output / "Package.swift",
        package_swift(
            repository=args.repository,
            version=args.version,
            checksum=swiftpm_checksum,
            minimum_macos=minimum_macos,
        ),
    )
    write_text(
        output / "README.md",
        readme(
            version=args.version,
            repository=args.repository,
            source_repository=str(release["source_repository"]),
            source_revision=str(release["source_revision"]),
            architectures=list(release["architectures"]),
            minimum_macos=minimum_macos,
        ),
    )
    write_text(
        output / "CHANGELOG.md",
        f"""# Changelog

## [{args.version}] - 2026-07-29

- First versioned GLIC Metal SDK distribution.
- 28 realtime-certified presets: 14 Original, 8 Spatial, and 6 Codec.
- SwiftPM binary target, version-matched resources, complete SDK archive,
  integration contracts, offline tools, Codex skill, and checksums.
""",
    )
    write_text(
        output / ".gitignore",
        """.DS_Store
.build/
.swiftpm/
dist/
""",
    )

    assets = {}
    for asset in (xcframework_archive, complete_archive):
        assets[asset.name] = {
            "sha256": sha256(asset),
            "size_bytes": asset.stat().st_size,
        }
    release["distribution_repository"] = args.repository
    release["release_tag"] = f"v{args.version}"
    release["swift_package"] = {
        "product": "GlicMetal",
        "binary_target": "GlicMetal",
        "resource_helper_module": "GlicMetalResources",
        "artifact": xcframework_archive.name,
        "checksum": swiftpm_checksum,
    }
    release["release_assets"] = assets
    write_text(
        output / "RELEASE-MANIFEST.json",
        json.dumps(release, indent=2, sort_keys=True),
    )
    checksum_lines = [
        f"{metadata['sha256']}  {name}"
        for name, metadata in sorted(assets.items())
    ]
    write_text(distribution / "SHA256SUMS", "\n".join(checksum_lines))

    summary = {
        "repository_root": str(output),
        "sdk_version": args.version,
        "source_revision": release["source_revision"],
        "architectures": release["architectures"],
        "swiftpm_checksum": swiftpm_checksum,
        "release_assets": assets,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
