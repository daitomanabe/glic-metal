# GLIC Metal macOS SDK

Contents:

- `GlicMetal.xcframework` — static C ABI library and public headers;
- `GlicMetalResources.bundle` — Metal kernels, presets, and license notices;
- `AI_INTEGRATION.md` — first-read contract for coding agents;
- `Documentation/` — self-contained integration and codec-lab documentation;
- `Tools/` — realtime VideoToolbox video wrapper plus offline codec, packet,
  evaluation, and search entrypoints;
- `Skills/glic-metal-sdk-integration/` — installable Codex skill for choosing,
  integrating, inspecting, and validating the SDK;
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
Agents with Codex Skills support can install or reference
`Skills/glic-metal-sdk-integration` and invoke
`$glic-metal-sdk-integration`. Its inspector accepts this SDK root, the source
checkout, or a CMake install prefix.
`offline-codec-effects.json` and `codec-lab-effects.json` describe the separate
offline packet/syntax/analysis workflows and the realtime Crossbreed subset.
`glitch-gallery-presets.json` provides three deterministic, effect-aware
parameter recipes for all 147 gallery algorithms. It is a portable preset
catalog, not a realtime certification claim. See
`Documentation/GLITCH_ALGORITHM_GALLERY.md`.
The XCFramework realtime ABI does not decode damaged bitstreams. Install
`Tools/requirements.txt`, run `Tools/process_offline_packet_glitch.py` in an
isolated process, and follow `Documentation/OFFLINE_PACKET_GLITCH.md`.
`Tools/process_codec_lab.py` and `Tools/evolutionary_codec_search.py` provide
the separate syntax/analysis workflows. Their exit status and JSON report are
the completion contract; do not call them from a capture or render callback.
`Tools/process_native_syntax_glitch.py` provides true MPEG-2 MV/qDCT/qscale
and MPEG-4 Part 2 MV transplication through a separately installed FFglitch
`ffedit`. It provides four MVD and four quantized-coefficient effects for
H.264 CABAC/CAVLC through pinned x264, and for HEVC through pinned x265 4.2.
`Tools/build_x264_glitch_reference.py` and
`Tools/build_x265_glitch_reference.py` build and verify that external GPL
CLI pair; stock x265 remains an analysis-load motion fallback. These encoder
lanes re-encode their normalized source.
`Tools/build_ffmpeg_hevc_glitch_reference.py` builds a separate pinned
LGPL FFmpeg decoder hook. With `--codec hevc --source-mode preserve`, it reads
an existing HEVC stream without source re-encoding or bitstream modification
and changes parsed MVD/coefficient values. Its output is a mutated decoder
reconstruction, never a mutated HEVC bitstream.
`Tools/evaluate_native_syntax_glitches.py` renders and
diversity-ranks all supported variants using actual-video metrics.
`Tools/install_ffglitch_reference.py` installs the checksum-pinned Apple
Silicon reference build into a cache; FFglitch is not bundled with this SDK.
None of the generated x264/x265/FFmpeg hook binaries are bundled with this SDK;
the builders, patches, hook source, and verification contracts are bundled.
Structured NAL/OBU, transport, and metadata workflows have dedicated Tools
entrypoints. See `Documentation/GLITCH_EXPANSION.md` for the complete catalog,
implementation-level labels, and actual-video validation.
The asynchronous hardware-codec lane is exposed separately through
`<glic_metal/codec_glitch.h>` and accepts opaque `CVPixelBufferRef` values.
Set `glic_codec_glitch_config.codec` to H.264, HEVC, or ProRes 422 before
prepare. Its 36 effects use codec-quality control, intentional encoded-frame holds, and
safe codec-decoded history/post composites. All compressed sample bytes reach the
decoder unchanged. `payload_xor` is a Metal-backed digital-damage composite,
and `reference_timewarp` selects from a configurable history of four to twelve
decoded pixel buffers;
neither mutates or reuses a compressed payload.
Query `glic_codec_glitch_effect_implementation_level()` instead of inferring
native compressed-field access from an effect name.
`Tools/process_video.py --processing-mode codec_glitch` is the packaged
actual-video wrapper. It defaults to FFmpeg NV12 raw output, copies that data
directly into IOSurface-backed 420v buffers, and requests the NV12/Metal path.
Use `--codec-input-pixel-format bgra` only for compatibility comparison.

Set `glic_codec_glitch_config.pixel_path` before prepare. `AUTO` prefers the
NV12/IOSurface/Metal path and permits BGRA compatibility fallback;
`NV12_METAL` requires the fast path and fails closed; `BGRA_COMPATIBILITY`
selects the previous path. Full-size video-range NV12 (`420v`) input reaches
the encoder without BGRA staging. 32BGRA input is converted by Metal. Decoder
planes are mapped through `CVMetalTextureCache`, one fused Metal dispatch
creates the stable 32BGRA output, and completion is asynchronous but delivered
in accepted-submission order. With `AUTO`, inspect the five path flags in
`glic_codec_glitch_stats`; prepare success alone is not fast-path evidence.
Read `Documentation/VIDEOTOOLBOX_FAST_PATH.md` before integration.

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
`Tools/validate_videotoolbox_fast_path.py` reproduces the 36-effect ×
three-codec × two-resolution matrix and requires direct 420v input in every
cell. `Tools/evaluate_codec_glitch_videos.py` uses its normalized reports for
actual-video difference and diversity ranking.

Resolve the runtime files from `GlicMetalResources.bundle` and pass their paths
through `glic_metal_config.preset_directory` and
`glic_metal_config.metal_library_path` before calling `glic_metal_prepare()`.

Start with `Documentation/DOWNSTREAM_QUICKSTART.md`. See
`Documentation/CODEC_GLITCH.md` for effect and safety semantics, and
`Documentation/EMBEDDING.md` for lifecycle, threading, pixel-format, Swift,
and zero-copy Metal examples.
