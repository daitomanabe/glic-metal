# GLIC Metal macOS SDK

Contents:

- `GlicMetal.xcframework` — static C ABI library and public headers;
- `GlicMetalResources.bundle` — Metal kernels, presets, and license notices;
- `AI_INTEGRATION.md` — first-read contract for coding agents;
- `Documentation/` — self-contained integration and codec-lab documentation;
- `Tools/` — offline codec, packet, evaluation, and search entrypoints;
- `SHA256SUMS` — checksums for the packaged files.

Add the XCFramework and resource bundle to the Xcode application target. Link
`libc++.tbd`, Foundation.framework, Metal.framework, CoreImage.framework,
CoreGraphics.framework, CoreMedia.framework, CoreVideo.framework, and
VideoToolbox.framework. Swift can
`import GlicMetal`; Objective-C/C hosts can include
`<glic_metal/glic_metal.h>` or `<glic_metal/glic_metal_metal.h>` from the host.
The adopted 19-preset production bank is available through
`<glic_metal/glitch_presets.h>` and as
`GlicMetalResources.bundle/Contents/Resources/selected-presets.json`. Use the
C API to enumerate stable names and apply exact Original, Spatial Metal, or
Codec controls without parsing JSON.
`integration-manifest.json` in the same resource directory is the
machine-readable contract for downstream coding agents. The adjacent
`AI_INTEGRATION.md` is its normative implementation checklist.
`offline-codec-effects.json` and `codec-lab-effects.json` describe the separate
offline packet/syntax/analysis workflows and the realtime Crossbreed subset.
The XCFramework realtime ABI does not decode damaged bitstreams. Install
`Tools/requirements.txt`, run `Tools/process_offline_packet_glitch.py` in an
isolated process, and follow `Documentation/OFFLINE_PACKET_GLITCH.md`.
`Tools/process_codec_lab.py` and `Tools/evolutionary_codec_search.py` provide
the separate syntax/analysis workflows. Their exit status and JSON report are
the completion contract; do not call them from a capture or render callback.
The asynchronous hardware-codec lane is exposed separately through
`<glic_metal/codec_glitch.h>` and accepts opaque `CVPixelBufferRef` values.
Set `glic_codec_glitch_config.codec` to H.264, HEVC, or ProRes 422 before
prepare. Its 28 effects use codec-quality control, intentional encoded-frame holds, and
safe codec-decoded history/post composites. All compressed sample bytes reach the
decoder unchanged. `payload_xor` is a Metal-backed digital-damage composite,
and `reference_timewarp` selects from a configurable history of four to twelve
decoded pixel buffers;
neither mutates or reuses a compressed payload.

Preparation creates pools and validates the normal hardware encoder.
Specialized QP/cascade/downscale encoders and the decoder are created on first
use. VideoToolbox `RealTime` and low-latency rate control are enabled by
default. The default average bitrate is 4,000,000 bps. Dynamic bitrate uses
`min(averageBitRate, width * height * fps / 4)` as its no-drop floor.
New stages use 500/300 ms encode/decode warm-up deadlines and set
`codec_warmup_frame`; sustained work returns to 100/45 ms.

Output repeats expose `repeated_previous_frame` and
`intentional_repeat_frame`, allowing a host to distinguish designed
`pframe_loss`/`idr_starvation` holds from failure fallback. The first failure
before a successful decode can emit the retained full-size input and sets
`non_intentional_fallback_frame`.

The legacy `poll_queue_drops` statistic combines drops from the bounded
callback and poll delivery paths. `codec_errors` includes encode, sample
extraction, decode, and timeout errors. Certification through the raw-video
filter requires at least 960x540, at least 120 frames, preserved frame count,
hardware encode/decode, 20 fps with p95 at or below 50 ms, and zero fallback,
codec errors, watchdog recovery, backpressure, or output-queue drops.

Resolve the runtime files from `GlicMetalResources.bundle` and pass their paths
through `glic_metal_config.preset_directory` and
`glic_metal_config.metal_library_path` before calling `glic_metal_prepare()`.

Start with `Documentation/DOWNSTREAM_QUICKSTART.md`. See
`Documentation/CODEC_GLITCH.md` for effect and safety semantics, and
`Documentation/EMBEDDING.md` for lifecycle, threading, pixel-format, Swift,
and zero-copy Metal examples.
