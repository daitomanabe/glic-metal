# Changelog

All notable user-visible changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
semantic versioning for tagged releases.

## [Unreleased]

### Added

- Five realtime Metal/CPU glitch families: tile shuffle, vertical tear,
  diagonal slip, scanline weave, and quad mirror.
- Search seeding from the complete 144-preset upstream GLIC value corpus, with
  source-preset and mapping-fidelity provenance in ranking and review exports.
- Deterministic selection of moderately complex generated patterns that stay
  visually distant from a prior ranked image corpus.
- Checkbox-based preset review with persistent browser state and checked-only
  JSON/CSV adoption exports.
- Native macOS webcam preview with runtime preset and quality-mode selection.
- Strict, Fast Match, and fail-closed Auto 20fps original-style Metal lanes.
- 144-preset compatibility corpus and 37-preset higher-fidelity original lane.
- Realtime performance certification, visual analysis, diversity ranking, and
  unattended search tooling.
- Public release layout check, CI workflow, licensing, contribution, and
  security documentation.
- Versioned C embedding API with preallocated BGRA/RGBA frame processing,
  host-device Metal texture interop, an installable CMake package, and external
  consumer tests.

### Changed

- Preset search now balances fourteen mechanisms across upstream-base,
  light/strong upstream mutation, archive mutation, and random lanes.
- Review selection balances mechanism, artifact scale, orientation, source
  origin, and source-preset reuse while enforcing minimum perceptual distance.
- Ranking tools now require the external visual-liveliness runner through
  `VISUAL_LIVELINESS_RUNNER`; no maintainer-specific home-directory fallback is
  used.
- Application bundles and installed documentation now include license notices.

### Known limitations

- Metal and webcam features require macOS and full Xcode at build time.
- `original_visual` supports 37 audited presets; the all-144 path is a separate
  visual approximation.
- Windows remains unverified by the current CI matrix.
