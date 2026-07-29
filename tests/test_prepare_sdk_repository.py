#!/usr/bin/env python3

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PACKAGER = ROOT / "scripts/prepare_sdk_repository.py"
VERSION = "0.1.0"
REVISION = "1" * 40


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, text: str = "fixture\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def create_sdk(root: Path) -> None:
    write(root / "AI_INTEGRATION.md")
    write(root / "Documentation/EMBEDDING.md")
    write(root / "GlicMetal.xcframework/Info.plist")
    write(root / "GlicMetal.xcframework/macos-arm64/libglic_metal.a")
    resources = root / "GlicMetalResources.bundle/Contents/Resources"
    write(resources / "Presets/vv01")
    write(resources / "glic_realtime.metallib")
    write(resources / "integration-manifest.json", "{}\n")
    write(resources / "LICENSE", "Copyright (c) 2026 Daito Manabe\n")
    write(resources / "THIRD_PARTY_NOTICES.md")
    write(root / "README.md")
    write(root / "Skills/glic-metal-sdk-integration/SKILL.md")
    write(root / "Tools/process_video.py")
    release = {
        "schema": "glic-metal-sdk-release-v1",
        "sdk_version": VERSION,
        "source_repository": "https://github.com/daitomanabe/glic-metal",
        "source_revision": REVISION,
        "source_dirty": False,
        "build_number": 1,
        "platform": "macOS",
        "architectures": ["arm64"],
        "minimum_macos": "13.0",
        "abi_versions": {
            "image": 1,
            "codec": 1,
            "selected_presets": 1,
        },
        "production_presets": {
            "count": 28,
            "category_counts": {
                "original": 14,
                "spatial": 8,
                "codec": 6,
            },
        },
    }
    release_text = json.dumps(release, indent=2, sort_keys=True) + "\n"
    write(root / "RELEASE-MANIFEST.json", release_text)
    write(resources / "RELEASE-MANIFEST.json", release_text)
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            rows.append(f"{sha256(path)}  {path.relative_to(root).as_posix()}")
    write(root / "SHA256SUMS", "\n".join(rows))


def run_packager(sdk: Path, output: Path) -> dict:
    result = subprocess.run(
        [
            sys.executable,
            str(PACKAGER),
            "--sdk-root",
            str(sdk),
            "--output",
            str(output),
            "--version",
            VERSION,
            "--source-root",
            str(ROOT),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        temporary_root = Path(temporary)
        sdk = temporary_root / "GlicMetalSDK"
        first = temporary_root / "first"
        second = temporary_root / "second"
        create_sdk(sdk)
        first_report = run_packager(sdk, first)
        second_report = run_packager(sdk, second)

        assert first_report["sdk_version"] == VERSION
        assert first_report["source_revision"] == REVISION
        assert first_report["architectures"] == ["arm64"]
        assert first_report["swiftpm_checksum"] == second_report[
            "swiftpm_checksum"
        ]
        assert (first / "Package.swift").is_file()
        assert (first / "AI_INTEGRATION.md").is_file()
        assert (
            first
            / "Sources/GlicMetalResources/Resources/RELEASE-MANIFEST.json"
        ).is_file()
        assert (
            first
            / "Skills/glic-metal-sdk-integration/SKILL.md"
        ).is_file()
        assert (
            first / "dist/GlicMetalSDK-0.1.0-macos-arm64.zip"
        ).is_file()
        assert sha256(first / "dist/GlicMetal.xcframework.zip") == sha256(
            second / "dist/GlicMetal.xcframework.zip"
        )
        package = (first / "Package.swift").read_text(encoding="utf-8")
        assert first_report["swiftpm_checksum"] in package
        release = json.loads(
            (first / "RELEASE-MANIFEST.json").read_text(encoding="utf-8")
        )
        assert release["release_tag"] == "v0.1.0"
        assert release["production_presets"]["count"] == 28

    print("PASS SDK repository packager")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
