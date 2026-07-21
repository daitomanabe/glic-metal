# Changelog

All notable user-visible changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
semantic versioning for tagged releases.

## [Unreleased]

### Added

- Native macOS webcam preview with runtime preset and quality-mode selection.
- Strict, Fast Match, and fail-closed Auto 20fps original-style Metal lanes.
- 144-preset compatibility corpus and 37-preset higher-fidelity original lane.
- Realtime performance certification, visual analysis, diversity ranking, and
  unattended search tooling.
- Public release layout check, CI workflow, licensing, contribution, and
  security documentation.

### Changed

- Ranking tools now require the external visual-liveliness runner through
  `VISUAL_LIVELINESS_RUNNER`; no maintainer-specific home-directory fallback is
  used.
- Application bundles and installed documentation now include license notices.

### Known limitations

- Metal and webcam features require macOS and full Xcode at build time.
- `original_visual` supports 37 audited presets; the all-144 path is a separate
  visual approximation.
- Windows remains unverified by the current CI matrix.
