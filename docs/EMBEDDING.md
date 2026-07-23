# Embedding GLIC Metal

> For AI coding agents, start with [AI_INTEGRATION.md](AI_INTEGRATION.md) and
> `resources/integration-manifest.json`. This document is the human-facing
> integration guide.

GLIC Metal exposes a versioned C ABI so a host does not need to depend on the
project's internal C++ classes. The same API can be called from C, C++,
Objective-C, Objective-C++, Swift through a bridging header, JUCE, openFrameworks,
or another native video application.

The public surface is:

- `include/glic_metal/glic_metal.h` — portable C API;
- `include/glic_metal/glic_metal_metal.h` — typed Objective-C Metal helpers;
- `include/glic_metal/codec_glitch.h` — macOS VideoToolbox codec-glitch C API;
- `include/glic_metal/glitch_presets.h` — adopted cross-lane preset bank;
- `GlicMetal::GlicMetal` — CMake target;
- `glic_realtime.metallib` — Metal kernels to copy into the host bundle;
- `presets/` — runtime preset data;
- `selected-presets.json` — portable data copy of the adopted 19 presets.
- `integration-manifest.json` — machine-readable integration contract.
- `docs/MULTICODEC_GLITCH.md` — native and offline codec capability contract.

Internal headers under `src/` are not part of the stable API.

## Use the adopted preset bank

The shipped bank contains the exact 19 presets selected for production: 14
original-style presets, four allocation-free spatial Metal presets, and one
codec-control preset. The host may prepare that codec lane as H.264, HEVC, or
ProRes 422. Stable names such as `original__vv01`,
`spatial__poster_solar`, and `codec__bitrate_meltdown` can be stored by a host
application. The compiled C API is the authoritative runtime catalog and
preserves the order in `selected-presets.json`; the JSON is an optional
inspection/exchange copy.

```c
#include <glic_metal/glitch_presets.h>

for (uint32_t i = 0; i < glic_glitch_preset_count(); ++i) {
  glic_glitch_preset_descriptor preset;
  glic_glitch_preset_descriptor_init(&preset);
  if (glic_glitch_preset_get(i, &preset) == GLIC_GLITCH_PRESET_OK) {
    add_menu_item(preset.name, preset.category);
  }
}

glic_metal_config image_config;
glic_metal_config_init(&image_config);
image_config.width = 960;
image_config.height = 540;
image_config.preset_directory = preset_directory;
image_config.metal_library_path = metallib_path;
glic_glitch_preset_apply_metal("spatial__poster_solar", &image_config);

glic_codec_glitch_controls codec_controls;
if (glic_glitch_preset_apply_codec("codec__bitrate_meltdown",
                                   &codec_controls) ==
    GLIC_GLITCH_PRESET_OK) {
  glic_codec_glitch_set_controls(codec_context, &codec_controls);
}
```

`glic_glitch_preset_apply_metal()` leaves host-owned resolution, resource
paths, Metal device, and library path untouched. Original presets select
`GLIC_METAL_MODE_ORIGINAL`; spatial presets select
`GLIC_METAL_MODE_COMPAT_REALTIME` and apply their exact family, amount, scale,
rate, and seed. `glic_glitch_preset_apply_codec()` initializes the controls and
applies the exact codec effect, amount, rate, feedback, and seed. Category
mismatches fail closed.

Build the production menu only with `glic_glitch_preset_count()` and
`glic_glitch_preset_get()`. `glic_metal_enumerate_presets()` intentionally
returns the complete 144-preset compatibility corpus and must not be used for
the adopted 19-item menu.

Keep two engine objects when the host supports all three categories:

- one `glic_metal_context` for synchronous Original and Spatial processing;
- one `glic_codec_glitch_context` for asynchronous Codec processing.

On selection, inspect `descriptor.category`. Route Original and Spatial names
to `glic_glitch_preset_apply_metal()` followed by `glic_metal_prepare()`.
Route Codec names to `glic_glitch_preset_apply_codec()` followed by
`glic_codec_glitch_set_controls()`. Prepare or switch outside the frame
callback. Use a host-side generation ID to discard late asynchronous Codec
output after switching lanes.

## Choose a processing path

This table describes the full library capability surface. The adopted
production bank is the 14 / 4 / 1 subset described above.

| Mode | Presets | Input | Main use |
|---|---:|---|---|
| `GLIC_METAL_MODE_ORIGINAL` + Strict | 37 audited | BGRA/RGBA CPU buffer | Closest original-style result |
| `GLIC_METAL_MODE_ORIGINAL` + Fast Match | 37 algorithm-supported | BGRA/RGBA CPU buffer | Faster approximate CDF 9/7 |
| `GLIC_METAL_MODE_COMPAT_REALTIME` | all 144 | CPU buffer or Metal texture | Maximum variety and easiest GPU composition |
| Codec Glitch | 18 codec effects | `CVPixelBufferRef` | Stateful H.264 / HEVC / ProRes encode/decode and codec-history effects |

Select the native codec before `glic_codec_glitch_prepare()`:

```c
glic_codec_glitch_config codec_config;
glic_codec_glitch_config_init(&codec_config);
codec_config.codec = GLIC_CODEC_GLITCH_CODEC_HEVC;
```

AV1, AV2, and VP9 are offline/reference workflows rather than members of this
realtime C ABI. Integrations that need them should invoke
`scripts/process_multicodec_glitch.py` and consume its JSON plus persistent
stage bitstreams. See [MULTICODEC_GLITCH.md](MULTICODEC_GLITCH.md).

The original-style lane still performs input conversion and segmentation on
the CPU before Metal reconstruction, so its public integration path is a
preallocated CPU frame buffer. Compatibility mode can operate directly on the
host's `BGRA8Unorm` textures and append work to the host command buffer.

Fast Match availability in this library is the algorithm-support boundary, not
the preview application's quality allowlist. Hosts that need the same visual
gate as the bundled preview can read `config/fast-match-allowlist.json` or apply
their own measured preset policy before selecting Fast Match.

## Add as a CMake subdirectory

When GLIC Metal is not the top-level project, standalone tools and installation
rules default to off. A host only builds the library and shaders:

```cmake
set(GLIC_BUILD_STANDALONE OFF CACHE BOOL "" FORCE)
set(GLIC_INSTALL OFF CACHE BOOL "" FORCE)
add_subdirectory(path/to/glic-metal)

target_link_libraries(MyApp PRIVATE GlicMetal::GlicMetal)
```

On macOS, full Xcode is required to compile the Metal kernels. Attach the
runtime resources to a bundle target with the supplied helper:

```cmake
glic_metal_copy_resources(
  TARGET MyApp
  DESTINATION "$<TARGET_BUNDLE_CONTENT_DIR:MyApp>/Resources")
```

It copies `${GLIC_METAL_METALLIB}` as `glic_realtime.metallib`, copies
`${GLIC_METAL_PRESETS_DIR}` into a `Presets` resource folder, and copies
`${GLIC_METAL_SELECTED_PRESETS_JSON}` as `selected-presets.json` plus
`${GLIC_METAL_INTEGRATION_MANIFEST}` as `integration-manifest.json`. The same
function is available from the installed CMake package.

## Use an installed CMake package

Build and install once:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
cmake --install build --prefix dist
```

Then consume it from another project:

```cmake
find_package(GlicMetal 1 CONFIG REQUIRED)
target_link_libraries(MyApp PRIVATE GlicMetal::GlicMetal)

# Useful runtime resources exported by GlicMetalConfig.cmake:
message(STATUS "Presets: ${GLIC_METAL_PRESETS_DIR}")
message(STATUS "Selected bank: ${GLIC_METAL_SELECTED_PRESETS_JSON}")
message(STATUS "Agent contract: ${GLIC_METAL_INTEGRATION_MANIFEST}")
if(APPLE)
  message(STATUS "Metal library: ${GLIC_METAL_METALLIB}")
endif()
```

Configure the host with `-DCMAKE_PREFIX_PATH=/absolute/path/to/dist` when the
prefix is outside CMake's normal search path.

## Minimal C API

```c
#include <glic_metal/glic_metal.h>
#include <glic_metal/glitch_presets.h>

glic_metal_context *engine = NULL;
glic_metal_context_create(&engine);

glic_metal_config config;
glic_metal_config_init(&config);
config.width = 960;
config.height = 540;
config.preset_directory = preset_directory;
if (glic_glitch_preset_apply_metal("original__vv01", &config) !=
    GLIC_GLITCH_PRESET_OK) {
  return;
}
config.fidelity = GLIC_METAL_FIDELITY_STRICT;

if (glic_metal_prepare(engine, &config) != GLIC_METAL_OK) {
  log_error(glic_metal_get_last_error(engine));
}

glic_metal_process_frame(engine,
                         input_bgra, input_bytes_per_row,
                         output_bgra, output_bytes_per_row,
                         GLIC_METAL_PIXEL_FORMAT_BGRA8,
                         frame_index);

glic_metal_context_destroy(engine);
```

`BGRA8` matches `kCVPixelFormatType_32BGRA`. `RGBA8` is also supported. Row
padding is accepted, and input/output may point to the same buffer. This path
copies pixels into preallocated engine storage but performs no heap allocation
during successful steady-state frame processing.

`glic_metal_prepare()` performs preset loading, allocation, pipeline creation,
and worker startup. Run it on a control/background queue, never inside a render
callback. If a new preset fails to prepare, the previous engine stays active.

## Codec Glitch C API (macOS only)

### 日本語

Codec Glitchは、上記の同期画像APIとは独立した、VideoToolboxによる非同期H.264
処理です。GLICのファイルcodec、37 presetの`original_visual`、全144 presetを扱う
`compat_realtime`のいずれでもありません。入力は`CVPixelBufferRef`で、18種類の
effect名と安全境界は[CODEC_GLITCH.md](CODEC_GLITCH.md)にあります。
全effectが圧縮H.264のVCL byteを変更しません。`slice_dropout`、
`slice_transplant`、`payload_xor`はMetal-backed CoreImageでclean decode結果へ作用し、
`reference_timewarp`は4〜12 frameへ設定できるdecode済み`CVPixelBuffer`履歴を使います。

`prepare`はqueue、pixel-buffer pool、Metal-backed post path、通常stageのhardware
encoderを作ってbackendを検証するため、capture callbackではなくcontrol/background
queueで一度実行します。特殊なQP/cascade/縮小encoderは最初の利用時、decoderは最初の
encode済みsampleで遅延生成されます。`RealTime`とlow-latency rate controlは既定で有効で、
既定average bitrateは4,000,000 bpsです。動的bitrateの安全floorは
`min(averageBitRate, width * height * fps / 4)`です。
`submit`は非blockで、処理中frame数が上限に達すると
`GLIC_CODEC_GLITCH_BACKPRESSURE`を返します。出力は非同期なので、
`copy_latest_pixel_buffer`の`GLIC_CODEC_GLITCH_NO_FRAME_AVAILABLE`はerrorでは
ありません。

`pframe_loss`／`idr_starvation`による設計上のholdは
`repeated_previous_frame=true`かつ`intentional_repeat_frame=true`です。障害時の
fallback repeatは`intentional_repeat_frame=false`なので区別できます。
最初のdecode前に失敗した場合はretain済みfull-size入力を出し、
`non_intentional_fallback_frame=true`にします。filterの
`reliability_passed`は非意図的fallback、全codec処理error、watchdog recovery、
backpressure drop、output queue dropがすべて0の場合だけtrueになり、意図的repeatは
`intentional_repeat_frames`へ別集計されます。ABI互換の`poll_queue_drops`はcallbackと
pollの両方を合算します。20fps gateはさらに960×540以上、120 frame以上、hardware
encode/decode、frame数維持、実測/stream 20fps以上、p95 50ms以下を要求します。

### English

Codec Glitch is a separate asynchronous VideoToolbox H.264 / HEVC / ProRes path, not the
synchronous image API above. It is distinct from the GLIC file codec,
37-preset `original_visual` lane, and all-144 `compat_realtime` lane. It accepts
`CVPixelBufferRef` input and exposes the eighteen effects documented in
[CODEC_GLITCH.md](CODEC_GLITCH.md). No effect modifies compressed H.264 VCL
bytes. `slice_dropout`, `slice_transplant`, and `payload_xor` use Metal-backed
CoreImage after a clean decode, while `reference_timewarp` uses a bounded
history configured from four to twelve decoded `CVPixelBuffer` objects.

Run `prepare` once on a control or background queue because it creates queues,
pixel-buffer pools, the Metal-backed post path, and the normal-stage hardware
encoder used to validate the backend. Specialized QP/cascade/downscale encoders
are lazy, and the decoder is created from the first encoded sample. `RealTime`
and low-latency rate control are enabled by default, with a dynamic bitrate
floor of `min(averageBitRate, width * height * fps / 4)`. The default average
bitrate is 4,000,000 bps.
`submit` is nonblocking and returns `GLIC_CODEC_GLITCH_BACKPRESSURE` when its
bounded in-flight set is full. Output is asynchronous, so
`GLIC_CODEC_GLITCH_NO_FRAME_AVAILABLE` from the poll function is not an error.

```c
#include <glic_metal/codec_glitch.h>
#include <CoreVideo/CoreVideo.h>

glic_codec_glitch_context *codec = NULL;
if (glic_codec_glitch_context_create(&codec) != GLIC_CODEC_GLITCH_OK) {
  return;
}

glic_codec_glitch_config config;
glic_codec_glitch_config_init(&config);
config.width = 960;
config.height = 540;
config.frames_per_second = 30;
config.average_bit_rate = 4000000;
config.decoded_history_frames = 12; /* Clamped to [4, 12]. */
config.require_hardware_encoder = 1;
config.require_hardware_decoder = 1;

if (glic_codec_glitch_prepare(codec, &config) != GLIC_CODEC_GLITCH_OK) {
  log_error(glic_codec_glitch_get_last_error(codec));
  glic_codec_glitch_context_destroy(codec);
  return;
}

glic_codec_glitch_controls controls;
glic_codec_glitch_controls_init(&controls);
controls.effect = GLIC_CODEC_GLITCH_SLICE_TRANSPLANT;
controls.amount = 0.52f;
controls.rate = 0.34f;
controls.feedback = 0.58f;
controls.seed = UINT64_C(0x474c4943);
glic_codec_glitch_set_controls(codec, &controls);

/* input_pixel_buffer is a CVPixelBufferRef owned by the host. */
glic_codec_glitch_status status = glic_codec_glitch_submit_pixel_buffer(
    codec, (void *)input_pixel_buffer, frame_index,
    presentation_time.value, presentation_time.timescale);
if (status != GLIC_CODEC_GLITCH_OK &&
    status != GLIC_CODEC_GLITCH_BACKPRESSURE) {
  log_error(glic_codec_glitch_get_last_error(codec));
}

glic_codec_glitch_frame output;
glic_codec_glitch_frame_init(&output);
status = glic_codec_glitch_copy_latest_pixel_buffer(codec, &output);
if (status == GLIC_CODEC_GLITCH_OK) {
  CVPixelBufferRef image = (CVPixelBufferRef)output.pixel_buffer;
  if (output.repeated_previous_frame && output.intentional_repeat_frame) {
    /* Expected pframe_loss / idr_starvation hold, not failure fallback. */
  }
  if (output.non_intentional_fallback_frame) {
    /* Reliability failure: this may be last-good or retained initial input. */
  }
  present_pixel_buffer(image);
  glic_codec_glitch_pixel_buffer_release(output.pixel_buffer);
}

glic_codec_glitch_stats stats;
glic_codec_glitch_stats_init(&stats);
glic_codec_glitch_get_stats(codec, &stats);

glic_codec_glitch_flush(codec, 2000);
glic_codec_glitch_context_destroy(codec);
```

The host owns the submitted buffer; the engine retains it only as needed for
the asynchronous encode. A successful poll returns one retained output buffer.
Release exactly that ownership with
`glic_codec_glitch_pixel_buffer_release()`. Initialize a fresh frame struct
before each poll, or clear and release its previous `pixel_buffer` first.

`repeated_previous_frame` reports every repeated last-good frame.
`intentional_repeat_frame` distinguishes the designed `pframe_loss` /
`idr_starvation` hold from an unexpected encode/decode fallback. The C++ frame
uses `repeatedPreviousFrame`, `intentionalRepeat`, `nonIntentionalFallback`,
and `codecWarmupFrame`. The ABI-compatible
`packet_was_modified` and C++ `packetWasModified` fields remain false because
this safe implementation does not mutate compressed VCL bytes. The legacy-named
`intentional_packet_drops` statistic counts intentionally held encoded frames.
`codec_errors` counts all codec-processing failures, including encode, sample
extraction, decode, and operation timeouts. The legacy-named
`poll_queue_drops` combines losses from the bounded callback and polling output
queues.

Before any successful decode exists, a failed frame returns the retained
full-size input with `non_intentional_fallback_frame=true`; later failures can
return last-good output with the same flag. New stages use 500/300 ms
encode/decode warm-up deadlines, then sustained work uses 100/45 ms.

Use `glic_codec_glitch_reset()` when switching streams or when the operator
requests an immediate clean codec history; this explicit operation drains and
rebuilds codec sessions. Unexpected codec failure is handled internally with
last-good output, and the watchdog forces the next IDR through the existing
session without tearing it down. It remains a reliability failure rather than
an intended visual result. The filter's `reliability_passed` field requires
zero non-intentional fallback, codec errors, watchdog recoveries, backpressure
drops, and output-queue drops; intentional repeats are counted separately and
remain eligible. The 20 fps gate additionally requires at least 960×540, at
least 120 frames, hardware encode/decode, preserved output frame count,
processing/stream rates of at least 20 fps, and p95 at or below 50 ms. The API
cannot ingest arbitrary external H.264.

Linking Codec Glitch directly from Xcode additionally requires
`CoreImage.framework`, `CoreGraphics.framework`, `CoreMedia.framework`,
`CoreVideo.framework`, and `VideoToolbox.framework` alongside `Foundation.framework` and
`Metal.framework`.

## Zero-copy Metal integration

Include the typed helper in Objective-C or Objective-C++:

```objc
#include <glic_metal/glic_metal_metal.h>

glic_metal_config config;
glic_metal_config_init(&config);
config.width = width;
config.height = height;
config.preset_directory = presetDirectory.fileSystemRepresentation;
config.preset_name = "colour_glow";
config.backend = GLIC_METAL_BACKEND_METAL;
config.mode = GLIC_METAL_MODE_COMPAT_REALTIME;
config.metal_device = (__bridge void *)device;
config.metal_library_path = metalLibraryPath.fileSystemRepresentation;
glic_metal_prepare(engine, &config);

// input/output must be BGRA8Unorm textures created by `device`.
glic_metal_encode_texture_objects(engine, commandBuffer,
                                  inputTexture, outputTexture,
                                  frameIndex);
// The host commits the command buffer after adding its remaining work.
```

The asynchronous call only encodes work. The host owns command-buffer commit,
completion, synchronization, and texture lifetime. Keep at most three encoded
frames in flight for one context because uniforms use a three-slot ring.
`glic_metal_process_texture_objects()` is the synchronous alternative.

## Swift

Expose the headers through a bridging header:

```objc
#include <glic_metal/codec_glitch.h>
#include <glic_metal/glic_metal.h>
#include <glic_metal/glic_metal_metal.h>
#include <glic_metal/glitch_presets.h>
```

The installed `module.modulemap` also defines module `GlicMetal`. Pass an
existing `MTLDevice` without ownership transfer:

```swift
config.metal_device = Unmanaged.passUnretained(device).toOpaque()
```

Keep the Swift strings backing `preset_directory`, `preset_name`, and
`metal_library_path` alive only through `glic_metal_prepare()`; the engine
copies their values during preparation.

## Preset menus and switching

For the production menu, use `glic_glitch_preset_count()` and
`glic_glitch_preset_get()` to expose exactly the adopted 19 presets in their
reviewed order. Store the full stable name and route by `descriptor.category`.
Switch Original/Spatial by applying the name and calling
`glic_metal_prepare()` off the render queue. Switch Codec by applying the name
to controls and calling `glic_codec_glitch_set_controls()`.

Use `glic_metal_enumerate_presets()` only for an explicit advanced browser of
the complete 144-preset compatibility corpus. It calls the callback once per
preset in sorted order and is not the adopted production menu.

The 37-preset original-mode support boundary remains fail-closed. An
unsupported original preset returns `GLIC_METAL_UNSUPPORTED`; it is never
silently projected to compatibility mode.

## Ownership and threading

- One context owns its workers, buffers, Metal pipeline, and current preset.
- Context functions are not concurrently thread-safe. Process one context from
  one serial render queue.
- Use one context per independently processed stream.
- The host retains ownership of all frame buffers, Metal objects, and strings.
- Destroying a context stops its workers and releases its internal Metal
  objects. Passing `NULL` to destroy is safe.
- Use `glic_metal_get_last_stats()` for frame timing and
  `glic_metal_get_last_error()` immediately after a failed call.

The ABI version is `GLIC_METAL_ABI_VERSION`. Always initialize public structs
with their matching init function so `struct_size` and `abi_version` are set.

## Xcode resource checklist

When using the generated resource bundle, resolve it from the host bundle
instead of hard-coding a build-machine path:

```objc
NSURL *bundleURL = [NSBundle.mainBundle
    URLForResource:@"GlicMetalResources" withExtension:@"bundle"];
NSBundle *glicResources = [NSBundle bundleWithURL:bundleURL];
NSString *presetsPath = [glicResources pathForResource:@"Presets" ofType:nil];
NSString *metallibPath =
    [glicResources pathForResource:@"glic_realtime" ofType:@"metallib"];
```

Fail preparation with a visible diagnostic if a required path is missing.
Never embed a developer-machine absolute path in the host source code.

For an Xcode application that links `libglic_metal.a` directly:

1. Add `include/` to Header Search Paths.
2. Link `libglic_metal.a`, `libc++.tbd`, `Foundation.framework`,
   `Metal.framework`, `CoreImage.framework`, `CoreGraphics.framework`,
   `CoreMedia.framework`, `CoreVideo.framework`, and
   `VideoToolbox.framework`.
3. Copy `glic_realtime.metallib` into the application Resources phase.
4. Copy the required preset files into a `Presets` resource directory.
5. Copy `selected-presets.json` and `integration-manifest.json` when downstream
   developers or agents need the data/contract beside the binary.
6. Pass the bundle resource paths in `glic_metal_config`.
7. If camera input is used, add the host application's camera usage string;
   the library itself does not request camera permission.

Alternatively, build a drop-in XCFramework and resource bundle:

```bash
scripts/build_macos_sdk.sh build/GlicMetalSDK
```

The command refuses to overwrite an existing output. Set
`GLIC_SDK_ARCHITECTURES='arm64;x86_64'` for a universal macOS library when the
installed Xcode supports both architectures.

See `examples/embed_c.c`, `tests/embed_metal_api_tests.mm`, and
`tests/consumer/` for buildable integrations. AI agents must also follow
[AI_INTEGRATION.md](AI_INTEGRATION.md); its completion checklist is the handoff
contract for another application.
