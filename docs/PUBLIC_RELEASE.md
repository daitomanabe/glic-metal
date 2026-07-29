# Public release checklist

This document separates source-readiness from actions that publish files or
create external services. Running the checks below does not push a repository
or upload a binary.

## Source release gate

- [x] Root MIT license and third-party notices are present.
- [x] Upstream GLIC revision and 144-preset SHA-256 manifest are documented.
- [x] Build, contribution, security, and changelog documents are present.
- [x] Build/search/test outputs and local input videos are ignored.
- [x] Maintainer-specific absolute paths are removed from tracked scripts/docs.
- [x] A reproducible public-layout check is available.
- [x] The CI workflow defines CPU validation on Linux and CPU/Metal validation
      on macOS.
- [x] Keep the generated preset gallery in Git as the static visual audit trail.
      `config/public-release-policy.json` caps it at 300MiB total and 10MiB per
      file; `.gitattributes` marks the generated binary content explicitly.
- [x] Use `https://github.com/daitomanabe/glic-metal` as the canonical public
      repository on branch `main`; no placeholder repository URLs remain.
- [x] Enable private vulnerability reporting in the public host settings.

## Release candidate verification

```bash
python3 scripts/check_public_release.py --source . --strict
cmake -S . -B build-release -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON
cmake --build build-release --parallel
ctest --test-dir build-release --output-on-failure
cmake --install build-release --prefix dist
```

On macOS, inspect the bundle notices and signature:

```bash
test -f "build-release/GLIC Webcam Preview.app/Contents/Resources/LICENSE"
test -f "build-release/GLIC Webcam Preview.app/Contents/Resources/THIRD_PARTY_NOTICES.md"
codesign --verify --deep --strict "build-release/GLIC Webcam Preview.app"
```

## Publication boundary

Before making the repository public, inspect the staged tree, confirm the
gallery decision, create a tagged release from a clean commit, and publish
checksums for downloadable application bundles. Never upload `test-videos/`,
`search-runs/`, local build directories, or camera captures.

## SDK distribution release

`glic-metal` remains the source of truth. The separate
`daitomanabe/glic-metal-sdk` repository contains only generated, versioned
distribution files. Do not edit its copied contracts, resources, or binaries
independently.

From a clean tagged source commit:

```bash
release_root="$(mktemp -d)"
GLIC_SDK_VERSION=0.1.0 \
GLIC_SDK_DEPLOYMENT_TARGET=13.0 \
GLIC_SDK_ARCHITECTURES=arm64 \
GLIC_SDK_REQUIRE_CLEAN=1 \
GLIC_SDK_BUILD_DIR="${release_root}/build" \
scripts/build_macos_sdk.sh "${release_root}/GlicMetalSDK"

python3 skills/glic-metal-sdk-integration/scripts/inspect_glic_metal_sdk.py \
  "${release_root}/GlicMetalSDK" --lane all --strict
(cd "${release_root}/GlicMetalSDK" && shasum -a 256 -c SHA256SUMS)

python3 scripts/prepare_sdk_repository.py \
  --sdk-root "${release_root}/GlicMetalSDK" \
  --output /path/to/glic-metal-sdk \
  --version 0.1.0
```

The SDK repository tag and source repository tag must use the same semantic
version. Verify `RELEASE-MANIFEST.json.source_revision`, the SwiftPM checksum,
the complete SDK checksum, a fresh remote Swift package consumer, and the
28-preset 14/8/6 production menu before publishing the release.
