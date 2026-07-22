# Repository structure

This is the public source-tree map. Generated and machine-local content is kept
out of the source layout except for the intentionally committed preset gallery.

```text
glic-metal/
├── apps/                     macOS webcam preview entry point
├── benchmarks/               realtime and original-style benchmarks
├── config/                   reviewed runtime allowlists
├── docs/                     build, integration, fidelity, codec, catalog, and release documents
├── examples/                 small preset/code examples
├── external/stb/             pinned image-I/O Git submodule
├── include/glic_metal/       stable image, Metal, and codec-glitch C APIs
├── cmake/                    installed CMake package configuration
├── output/preset-gallery/    committed generated comparison gallery
├── presets/                  144 SHA-256-pinned upstream presets
├── resources/                macOS metadata and machine-readable integration contract
├── scripts/                  video processing, visual analysis, search, and QA tools
├── src/                      C++20, Objective-C++, and Metal implementation
├── tests/                    API consumer, C++, Objective-C++, and Python tests
├── tools/                    image/codec filtering, search, and certification CLIs
├── website/                  static public project introduction
├── CMakeLists.txt            build, test, install, and bundle rules
├── README.md                 bilingual project and operator guide
├── LICENSE                   project and upstream MIT license
└── THIRD_PARTY_NOTICES.md    bundled dependency and derivation notices
```

## Tracked data profile

- Source and tooling is separated across `src/`, `scripts/`, `tests/`, and
  `tools/`; public layout checks validate the required embedding and codec
  entry points without relying on a stale file count.
- Compatibility data: 144 preset files plus `presets.upstream.sha256`.
- Preset gallery: 436 files, approximately 275MB; 144 `.glic` files account for
  approximately 223MB and 145 PNG files for approximately 47MB.
- The gallery is marked generated in `.gitattributes`; its retention in Git is
  an explicit release decision recorded in `docs/PUBLIC_RELEASE.md`.

## Local-only directories

The following are ignored and must not be included in a public source archive:

- `build/`, `build-*/`, `cmake-build-*/`
- `test-videos/`
- `search-runs/`
- non-gallery `output/`
- Python caches, editor metadata, logs, and environment files

Run `python3 scripts/check_public_release.py --source .` after reorganizing the
tree. It verifies required release files, tracked-path hygiene, absolute path
leaks, the preset manifest, and documentation links.
