# GLIC Metal integration map

Use this map after inspecting the SDK. The bundled
`AI_INTEGRATION.md` and `integration-manifest.json` remain authoritative.

## Distribution layouts

| Layout | Contract | Library | Resources | Tools |
|---|---|---|---|---|
| Source checkout | `docs/AI_INTEGRATION.md` | CMake build target | `presets/`, generated metallib, `resources/` | `scripts/` |
| Generated SDK | `AI_INTEGRATION.md` | `GlicMetal.xcframework` | `GlicMetalResources.bundle` | `Tools/` |
| CMake install | `share/doc/glic-metal/AI_INTEGRATION.md` | `GlicMetal::GlicMetal` | `share/glic-metal/` | `${GLIC_METAL_TOOLS_DIR}` |

Prefer the generated SDK for Xcode and Swift hosts. Prefer the exported CMake
target for CMake, JUCE, and openFrameworks projects.

## Realtime lane selection

| Requirement | Lane | Input | Execution | Context |
|---|---|---|---|---|
| Closest original-style reconstruction | Original | BGRA/RGBA CPU buffer | synchronous | `glic_metal_context` |
| Allocation-free spatial GPU composition | Spatial | CPU buffer or `BGRA8Unorm` texture | synchronous | `glic_metal_context` |
| Stateful hardware codec history | Codec | 420v or 32BGRA `CVPixelBufferRef` | asynchronous | `glic_codec_glitch_context` |

Use separate image and Codec contexts when a host exposes all categories.
Prepare and switch them outside capture and render callbacks.

### Image call sequence

1. Create `glic_metal_context`.
2. Initialize `glic_metal_config`.
3. Set width, height, `Presets`, and metallib paths.
4. Apply the full preset name with `glic_glitch_preset_apply_metal()`.
5. Prepare on a control/background serial queue.
6. Process from one serial render queue.
7. Destroy the context after processing has stopped.

Original uses `glic_metal_process_frame()`. Spatial may instead encode into the
host's uncommitted command buffer with
`glic_metal_encode_texture_objects()`.

### Codec call sequence

1. Create `glic_codec_glitch_context`.
2. Initialize `glic_codec_glitch_config`.
3. Set dimensions, fps, codec, hardware requirements, and pixel path.
4. Prepare outside the frame callback.
5. Initialize and apply controls.
6. Submit without blocking.
7. Poll the latest output and release successful ownership exactly once.
8. Read stats and implementation level.
9. Flush and destroy during shutdown.

Required NV12/Metal evidence flags:

- `nv12_metal_fast_path`
- `metal_texture_cache`
- `fused_metal_effects`
- `asynchronous_metal_delivery`
- `ordered_delivery`

`AUTO` permits a BGRA compatibility fallback. `NV12_METAL` must fail closed.

## Menu selection

Use the adopted bank by default:

| Category | Count | Apply function |
|---|---:|---|
| Original | 14 | `glic_glitch_preset_apply_metal()` |
| Spatial | 8 | `glic_glitch_preset_apply_metal()` |
| Codec | 6 | `glic_glitch_preset_apply_codec_config()` |

Enumerate it with `glic_glitch_preset_count()` and
`glic_glitch_preset_get()`. Look up persisted names with
`glic_glitch_preset_find()`.

Use `glic_metal_enumerate_presets()` only for an explicitly requested
144-preset compatibility browser. Read the 36 Codec effect names from the
same-version manifest or public enum; never infer future names.

The gallery review currently adopts 53 rendered variants. Twenty-five are
honestly retained as offline-only gallery selections; the production menu is
the 28-item intersection with `realtime_certified=true`.

## Offline workflow routing

| Need | Packaged entrypoint | Catalog or contract |
|---|---|---|
| AV1, AV2, VP9, VVC, Theora, Dirac codec cycles | `Tools/process_multicodec_glitch.py` | `codec-lab-effects.json` |
| Damaged packet salvage | `Tools/process_offline_packet_glitch.py` | `offline-codec-effects.json` |
| Reconstruction and analysis | `Tools/process_codec_lab.py` | `codec-lab-effects.json` |
| Native compressed syntax | `Tools/process_native_syntax_glitch.py` | native syntax manifest section |
| Structured NAL/OBU | `Tools/process_structured_codec_glitch.py` | `codec-lab-effects.json` |
| Transport simulation | `Tools/process_transport_glitch.py` | `codec-lab-effects.json` |
| Metadata mutation | `Tools/process_metadata_glitch.py` | `codec-lab-effects.json` |
| Automated search | `Tools/evolutionary_codec_search.py` | JSON report and implementation level |

Invoke all of these out of process. Preserve persistent stage bitstreams and
reports when the tool produces them.

## Canonical detail documents

- Lifecycle, C/Swift/CMake examples: `Documentation/EMBEDDING.md`
- VideoToolbox pixel paths: `Documentation/VIDEOTOOLBOX_FAST_PATH.md`
- Realtime Codec effects: `Documentation/CODEC_GLITCH.md`
- Multi-codec workflows: `Documentation/MULTICODEC_GLITCH.md`
- Packet isolation: `Documentation/OFFLINE_PACKET_GLITCH.md`
- Syntax and search: `Documentation/CODEC_LAB.md`
- Native compressed syntax: `Documentation/NATIVE_SYNTAX_GLITCH.md`
- Full expansion catalog: `Documentation/GLITCH_EXPANSION.md`

In a source checkout, replace `Documentation/` with `docs/`.

## macOS link set

Link `libc++.tbd` and:

- Foundation
- Metal
- CoreImage
- CoreGraphics
- CoreMedia
- CoreVideo
- VideoToolbox
