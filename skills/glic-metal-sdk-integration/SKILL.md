---
name: glic-metal-sdk-integration
description: Integrate, upgrade, inspect, or validate the GLIC Metal SDK in macOS video applications. Use for Swift, Objective-C, Objective-C++, C, C++, CMake, JUCE, or openFrameworks hosts that need the adopted glitch preset menu, Original or Spatial Metal processing, VideoToolbox Codec Glitch, offline codec and compressed-syntax workflows, SDK packaging, resource troubleshooting, or realtime performance verification.
---

# GLIC Metal SDK Integration

Use the public GLIC Metal contract without copying internal implementation into
the host. Keep realtime image processing, asynchronous VideoToolbox processing,
and offline damaged-bitstream workflows as separate lanes.

## Start from the matching SDK contract

1. Locate the source checkout, generated `GlicMetalSDK`, or installed CMake
   prefix.
2. Run the bundled inspector before editing the host:

   ```bash
   python3 <skill-dir>/scripts/inspect_glic_metal_sdk.py <sdk-or-source-root> \
     --lane all --strict
   ```

   Select `image`, `codec`, or `offline` instead of `all` when the requested
   scope is narrower. Omit `--strict` during source development when the
   distributable has not been built yet.
3. Read `AI_INTEGRATION.md` and `integration-manifest.json` from the same SDK
   revision. In a source checkout, use `docs/AI_INTEGRATION.md` and
   `resources/integration-manifest.json`.
4. Read [references/integration-map.md](references/integration-map.md) to
   choose the lane and distribution style. Read only the lane-specific
   canonical document named there.
5. Inspect the host's build system, capture pixel format, render queue,
   resource model, deployment target, and existing frame ownership before
   changing it.

Never combine headers, catalogs, tools, or documentation from different SDK
versions.

## Choose the product surface explicitly

Default to the adopted production menu unless the user requests an experimental
browser:

- Production menu: 19 stable presets, enumerated with
  `glic_glitch_preset_count()` and `glic_glitch_preset_get()`.
- Compatibility browser: 144 image presets from
  `glic_metal_enumerate_presets()`.
- Experimental Codec browser: 36 canonical effects from the bundled manifest
  and public codec API.
- Gallery catalog: 147 algorithms with three example variants; treat it as
  discovery data, not realtime certification.

Persist full stable preset names. Do not duplicate preset values in host code.
Unknown names and category mismatches must fail closed while preserving the
last working configuration.

## Integrate the selected lane

### Original and Spatial

- Own one `glic_metal_context` per independent stream.
- Route both categories through `glic_glitch_preset_apply_metal()`.
- Call `glic_metal_prepare()` on a control or background serial queue.
- Process Original through BGRA/RGBA CPU buffers.
- Process Spatial through CPU buffers or `BGRA8Unorm` Metal textures.
- Use at most three encoded Metal frames in flight per context.
- Let the host own command-buffer commit, synchronization, and texture
  lifetime.

Do not route Original through the texture API and do not silently project an
unsupported Original preset to compatibility mode.

### Codec Glitch

- Own a separate `glic_codec_glitch_context`.
- Choose H.264, HEVC, or ProRes 422 and the pixel path before `prepare`.
- Prefer `NV12_METAL` when fast-path performance is required; fail closed if it
  cannot be created. Use `AUTO` only when a disclosed BGRA fallback is
  acceptable.
- Submit `CVPixelBufferRef` nonblockingly. Treat `BACKPRESSURE` as an input
  drop and `NO_FRAME_AVAILABLE` as normal asynchronous state.
- Release every successfully polled output exactly once with
  `glic_codec_glitch_pixel_buffer_release()`.
- Use a host generation ID to reject late output after lane or stream changes.
- Flush on shutdown, then destroy the codec and image contexts.

Never claim that a realtime Codec effect mutates compressed payload bytes.
Preserve the result of
`glic_codec_glitch_effect_implementation_level()` in UI, logs, or reports.

### Offline codec, packet, and syntax work

- Launch the packaged `Tools/` entrypoint as a child process.
- Validate the requested name against the same-version catalog before launch.
- Treat exit status plus the JSON report as the completion contract.
- Keep damaged-bitstream decode outside the host process and every realtime
  callback.
- Preserve `implementation_level`, source-reencode, bitstream-modification, and
  output-kind fields honestly.
- Reject unverified x264, x265, FFmpeg-hook, or FFglitch binaries.

Do not substitute AV1 for AV2, present reconstruction proxies as native syntax
hooks, or label offline workflows realtime.

## Package for the host

Prefer these paths in order:

1. Xcode/macOS: `GlicMetal.xcframework` plus
   `GlicMetalResources.bundle`.
2. CMake: `GlicMetal::GlicMetal` plus
   `glic_metal_copy_resources()`.
3. Manual static linking only when the first two cannot be used.

Include only `<glic_metal/*.h>`. Never include `src/` headers. Resolve
`Presets` and `glic_realtime.metallib` from the application bundle instead of
embedding a developer-machine path.

## Validate before handoff

Read [references/verification.md](references/verification.md) and complete the
applicable gates. At minimum:

- build and launch the actual host;
- enumerate exactly 19 production presets with category counts 14 / 4 / 1;
- process a real frame from every integrated lane;
- exercise switching, backpressure, empty polling, shutdown, and resource
  failure;
- measure the whole host at 960×540 or higher, 20 fps or higher, and p95
  latency at or below 50 ms;
- require hardware encode/decode and all five fast-path evidence flags for a
  required Codec NV12/Metal path;
- report non-intentional fallback, codec errors, watchdog recovery,
  backpressure, and queue drops.

Do not reuse library-only benchmark numbers as a host guarantee.

## Report the integration

Return:

- SDK revision or checksum source;
- selected lane and distribution method;
- host files and resources changed;
- preset/effect counts exposed;
- build, real-frame, and performance evidence;
- implementation-level labels and known fallbacks;
- any unimplemented or intentionally offline behavior.

