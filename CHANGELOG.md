# Changelog

All notable user-visible changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
semantic versioning for tagged releases.

## [Unreleased]

### Added

- A machine-readable public release policy that explicitly retains the visual
  preset audit gallery while enforcing total and per-file size limits in the
  strict publication gate.

- A bilingual AI integration contract and machine-readable integration
  manifest defining category routing, public APIs, runtime resources,
  frameworks, threading, realtime gates, fail-closed rules, and completion
  checks for downstream application agents.
- Automated consistency checks for the integration manifest and distribution
  of that contract through CMake installs, resource copies, the webcam bundle,
  and the macOS SDK.

- An adopted 19-entry production preset bank from `selected-presets.json`,
  with a versioned allocation-free C API for ordered enumeration, lookup, and
  exact Original, Spatial Metal, or Codec control application in host apps.
- Distribution of the selected preset JSON through installs, the macOS SDK
  resource bundle, the CMake resource-copy helper, and the webcam app bundle.

- A macOS-only Codec Glitch lane using the VideoToolbox hardware H.264
  encoder/decoder, Metal-compatible pixel buffers, and a Metal-backed
  post/composite path.
- Twelve stateful codec effects: QP pump, bitrate crush, slice dropout, slice
  transplant, P-frame loss, IDR starvation, payload XOR, reference timewarp,
  codec feedback, generation cascade, resolution hop, and chroma codec echo.
- A versioned asynchronous Codec Glitch C API for `CVPixelBufferRef` input,
  bounded polling, runtime controls, reset/flush, and recovery/performance
  statistics.
- Raw-BGRA Codec Glitch filtering and `process_video.py --processing-mode
  codec_glitch` with effect controls and JSON hardware, latency, fallback,
  intentional-repeat, codec-error, watchdog, and reliability metrics.
- C++ `intentionalRepeat` and C ABI `intentional_repeat_frame` output flags,
  separating designed temporal holds from non-intentional fallback.
- Headless codec-video evaluation with dry/wet, luminance, chroma, edge, and
  temporal metrics plus reliability-gated max-min diversity ranking.
- A reproducible 50-entry Codec Glitch candidate bank generator covering all
  twelve effects, with parameter/seed variation, strict ranking, and a
  checkbox-based HTML review page.
- A balanced mixed 50-pattern selection and review generator: fourteen
  non-codec spatial Metal families, eighteen original-style presets, and
  eighteen stateful Codec Glitch variants ranked with one dry/wet fingerprint.
- Last-good-frame fallback and forced-IDR watchdog recovery for unexpected
  codec failure, independently of intentional P-frame/IDR holds.
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

- Public CI now supports both current and older Xcode Metal compilers, installs
  its native FFmpeg test dependency, and validates the documented non-macOS
  VideoToolbox-unavailable contract in external consumers.
- The original CPU and Metal worker pools now use the Xcode 15-compatible
  `std::thread`; macOS retains the strict Processing leaf/RNG trace oracle
  while other hosts run the remaining portable CPU reference coverage.
- Hardware VideoToolbox/webcam tests are explicitly labelled for physical-Mac
  release runs and excluded only from virtual hosted CI.
- GitHub Actions use the current Node 24-based official action releases.

- The human embedding guide now provides a production 19-preset quick start,
  complete category-routing rules, bundle resource resolution, corrected
  framework requirements, and explicit separation from the 144-preset browser.

- The webcam preview now exposes only the adopted presets: fourteen Original
  Visual presets, four Spatial Metal presets, and one Codec Glitch preset.

- `slice_dropout` and `slice_transplant` now use safe Metal-backed post-decode
  horizontal-row history composites instead of removing or transplanting
  compressed VCL slices.
- `payload_xor` now clean-decodes before Metal-backed posterization, RGB
  rewiring, and displaced macroblock-like tiles; `reference_timewarp` selects
  from a configurable history of four to twelve decoded pixel buffers. No Codec
  Glitch effect directly modifies compressed H.264 VCL bytes.
- Preparation validates hardware operation with the normal encoder; specialized
  QP, cascade, and downscale encoders are lazy, and each decoder is created from
  its first encoded sample.
- VideoToolbox `RealTime` and low-latency rate control remain enabled by
  default. The default average bitrate is 4,000,000 bps, and the
  `min(averageBitRate, width * height * fps / 4)` floor prevents aggressive
  rate changes from dropping encoded frames without exceeding host settings.
- New encoder/decoder sessions use 500/300 ms warm-up deadlines before the
  sustained 100/45 ms limits. A failure before the first successful decode
  emits retained full-size input with `non_intentional_fallback_frame` set.
- Callback and polling delivery are both bounded. The ABI-compatible
  `poll_queue_drops` statistic now combines drops from either output path, and
  `codec_errors` covers encode, sample-extraction, decode, and timeout errors.
- QP pump, bitrate crush, and later cascade generations use stronger quality
  pressure, while resolution hop adds pixelation during full-size recovery.
- Realtime Codec Glitch gates now require at least 960x540, at least 120
  frames, preserved frame count, hardware encode/decode, 20 fps with p95 at or
  below 50 ms, and zero non-intentional fallback, codec errors, watchdog
  recoveries, backpressure drops, or output-queue drops. Designed temporal
  holds remain eligible and are reported separately.
- The webcam preview can switch between the 37-preset Original Visual lane and
  all twelve Codec Glitch effects, with live amount and codec-history reset
  controls.
- Preset search now balances fourteen mechanisms across upstream-base,
  light/strong upstream mutation, archive mutation, and random lanes.
- Review selection balances mechanism, artifact scale, orientation, source
  origin, and source-preset reuse while enforcing minimum perceptual distance.
- Ranking tools now require the external visual-liveliness runner through
  `VISUAL_LIVELINESS_RUNNER`; no maintainer-specific home-directory fallback is
  used.
- Application bundles and installed documentation now include license notices.

### Known limitations

- Codec Glitch requires macOS hardware H.264 support. Its 960x540 30 fps target
  and 20 fps hard floor are per-machine certification gates, not universal
  guarantees, and exact codec/post-composite artifacts can vary with
  VideoToolbox.
- Metal and webcam features require macOS and full Xcode at build time.
- `original_visual` supports 37 audited presets; the all-144 path is a separate
  visual approximation.
- Windows remains unverified by the current CI matrix.
