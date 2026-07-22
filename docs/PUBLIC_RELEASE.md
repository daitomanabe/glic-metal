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
