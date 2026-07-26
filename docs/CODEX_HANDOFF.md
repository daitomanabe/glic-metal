# GLIC Metal workspace handoff

This is the first-read document for continuing GLIC Metal on another Apple
Silicon Mac with Codex. It covers the source repository, local-only test and
evaluation data, expensive codec toolchains, the generated SDK, and the
verification boundary.

## Current product boundary

GLIC Metal has three realtime lanes and several deliberately separate offline
workflows:

- Original: 14 gallery-adopted original-style Metal presets.
- Spatial: 8 gallery-adopted, allocation-free Metal effects.
- Codec: 6 gallery-adopted VideoToolbox presets using H.264, HEVC, or
  ProRes 422.
- Offline: packet, codec-generation, structured-bitstream, transport,
  metadata, and native compressed-syntax workflows.

The gallery contains 147 algorithms and 441 rendered variants. Its canonical
curation has 53 adopted, 41 rejected, and 347 pending variants. Only the 28
adopted variants carrying `realtime_certified=true` enter the SDK production
menu. The 25 adopted offline variants remain visible in the gallery and must
not be presented as realtime SDK presets.

The machine-readable sources of truth are:

- `resources/integration-manifest.json`
- `resources/selected-presets.json`
- `resources/glic-metal-gallery-review.json`
- `resources/glic-metal-adopted-presets.json`

## What Codex must read first

On the destination machine, ask Codex to read these files in this order:

1. `AGENTS.md`
2. `docs/CODEX_HANDOFF.md`
3. `skills/glic-metal-sdk-integration/SKILL.md`
4. `docs/AI_INTEGRATION.md`
5. `resources/integration-manifest.json`
6. `docs/GLITCH_ALGORITHM_GALLERY.md`
7. `agent-conversation-summary.md`, if restored from the handoff package

The automatically generated conversation summary is historical context only.
Repository source, manifests, live CLI output, and current tests override any
stale or over-broad statement in that summary.

Suggested first Codex request:

> Read AGENTS.md and docs/CODEX_HANDOFF.md completely. Inspect the current Git
> branch and worktree before editing. Run the GLIC Metal SDK inspector in
> strict mode, verify the curation drift check, then report the current
> realtime/offline boundaries and any missing restored data.

## Transfer package layout

`scripts/build_workspace_handoff.py` creates:

```text
glic-metal-handoff-YYYYMMDD-HHMMSS/
├── HANDOFF.md
├── agent-conversation-summary.md
├── manifest.json
├── SHA256SUMS
├── git/
│   ├── glic-metal.git.bundle
│   └── stb.git.bundle
├── source/
│   ├── glic-metal-source.tar.zst
│   └── stb-source.tar.zst
├── sdk/
│   └── GlicMetalSDK/
└── data/
    ├── test-materials.tar.zst
    ├── gallery-evidence.tar.zst
    ├── search-evidence.tar.zst
    ├── codec-toolchains-cache.tar.zst
    └── local-plans.tar.zst
```

The Git bundles preserve all local branches, tags, submodule history, and
commits that may not yet be on GitHub. The source archives are a convenient
checkout-independent fallback. `GlicMetalSDK` is the inspected downstream
distribution. The data archives preserve ignored files that Git cannot
restore.

Build directories are intentionally excluded. CMake caches contain absolute
paths and SDK/toolchain discovery results from the source machine; restoring
them on another Mac is less reliable than rebuilding. The generated SDK,
source, test material, evidence, and expensive pinned dependency cache are
included instead.

Raw `.codex` and `.claude` session files are also excluded. They may contain
unrelated conversations or credentials. The package contains a sanitized
project summary.

## Restore using the Git bundles

Verify the package before extracting:

```bash
cd /path/to/glic-metal-handoff-YYYYMMDD-HHMMSS
shasum -a 256 -c SHA256SUMS
git bundle list-heads git/glic-metal.git.bundle
git bundle list-heads git/stb.git.bundle
```

Clone the exact repository state:

```bash
mkdir -p ~/development/sandbox
cd ~/development/sandbox
git clone /path/to/handoff/git/glic-metal.git.bundle glic-metal
cd glic-metal
git switch codex/videotoolbox-fast-path
git bundle verify /path/to/handoff/git/glic-metal.git.bundle
git remote set-url origin https://github.com/daitomanabe/glic-metal.git
git remote set-url --push origin git@github.com:daitomanabe/glic-metal.git
```

If network access is available, restore the submodule normally:

```bash
git submodule update --init --recursive
```

For an offline restore, clone the bundled submodule:

```bash
git clone /path/to/handoff/git/stb.git.bundle external/stb
git -C external/stb checkout 31c1ad37456438565541f4919958214b6e762fb4
git -C external/stb bundle verify /path/to/handoff/git/stb.git.bundle
```

Restore ignored data from inside the checkout:

```bash
tar --zstd -xf /path/to/handoff/data/test-materials.tar.zst
tar --zstd -xf /path/to/handoff/data/gallery-evidence.tar.zst
tar --zstd -xf /path/to/handoff/data/search-evidence.tar.zst
tar --zstd -xf /path/to/handoff/data/codec-toolchains-cache.tar.zst
tar --zstd -xf /path/to/handoff/data/local-plans.tar.zst
cp /path/to/handoff/agent-conversation-summary.md .
```

If `tar --zstd` is unavailable:

```bash
zstd -dc /path/to/archive.tar.zst | tar -xf -
```

## Destination prerequisites

The validated source machine is Apple Silicon. Install:

- Full Xcode, including the Metal command-line toolchain.
- CMake, FFmpeg/FFprobe, Python 3, Zstandard, Git, and a C/C++ compiler.
- Python QA dependencies from `requirements-qa.txt` when running image/video
  analysis.

The project CMake configuration explicitly locates full Xcode under
`/Applications/Xcode.app/Contents/Developer`; changing the global
`xcode-select` value is not required.

Do not reuse benchmark numbers as a guarantee for another Mac. Rerun
performance and VideoToolbox checks on the destination hardware.

## Rebuild and verify

From the restored repository:

```bash
python3 scripts/import_gallery_curation.py --check

cmake -S . -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_TESTING=ON
cmake --build build --parallel
ctest --test-dir build --output-on-failure

python3 skills/glic-metal-sdk-integration/scripts/inspect_glic_metal_sdk.py \
  . --lane all --strict
python3 scripts/check_public_release.py --source .
```

Build a fresh SDK for the destination machine:

```bash
./scripts/build_macos_sdk.sh /tmp/GlicMetalSDK
python3 skills/glic-metal-sdk-integration/scripts/inspect_glic_metal_sdk.py \
  /tmp/GlicMetalSDK --lane all --strict
(cd /tmp/GlicMetalSDK && shasum -a 256 -c SHA256SUMS)
```

Run the webcam preview:

```bash
cmake --build build --target glic_webcam_preview --parallel
open "build/GLIC Webcam Preview.app"
```

Rebuild the local gallery site without rerendering the 441 processor outputs:

```bash
python3 scripts/build_glitch_algorithm_gallery.py --build-site-only
python3 server.py 8000
```

Then open `http://127.0.0.1:8000/output/glitch-algorithm-gallery/site/`.
The default decision filter must show 53 adopted variants; `SHOW ALL` must
show 441.

## Acceptance checklist

- `git status --short` is understood before editing.
- The expected branch and commit from `manifest.json` are checked out.
- The stb submodule is at the recorded commit.
- `SHA256SUMS` passes before using transferred artifacts.
- The curation check reports 53 adopted, 28 realtime SDK, 25 offline-only.
- The production menu enumerates exactly 14 Original, 8 Spatial, and 6 Codec
  presets.
- CTest passes.
- The generated SDK strict inspector and its internal checksums pass.
- A real frame is processed by every integrated lane.
- Realtime is remeasured at 960×540 or higher and at least 20fps, with p95 at
  or below 50ms.
- Required Codec fast paths report hardware encode/decode and the documented
  Metal evidence flags.
- The one known gallery warning,
  `offline_packet:av1:nal_obu_surgery::saturation` (`mostly_frozen`), remains
  a warning and is not silently relabeled PASS.
