# GLIC Metal integration verification

Use the gates that match the selected lane. Record commands, exit status, and
the evidence artifact instead of reporting only that the integration works.

## Inspect the distribution

```bash
python3 <skill-dir>/scripts/inspect_glic_metal_sdk.py <root> \
  --lane all --strict --json
```

For a generated SDK, also verify:

```bash
(cd <GlicMetalSDK> && shasum -a 256 -c SHA256SUMS)
```

Require `RELEASE-MANIFEST.json` at the SDK root and in the resource bundle.
The two copies must match, name a full source Git SHA, and report
`source_dirty=false`.

Do not integrate when the contract, headers, resources, or tools are from
different versions.

## Verify the source checkout

Run after public API, catalog, packaging, or documentation changes:

```bash
python3 scripts/check_public_release.py --source .
cmake --build build --parallel
ctest --test-dir build --output-on-failure
```

Build a disposable SDK path and inspect it when packaging changed. The builder
refuses to overwrite:

```bash
scripts/build_macos_sdk.sh "$(mktemp -d)/GlicMetalSDK"
```

## Verify the host build and resources

- Build the real application target, not only a standalone sample.
- Confirm the linked SDK revision and public ABI.
- Confirm all Apple frameworks are linked.
- Resolve `Presets` and `glic_realtime.metallib` from the installed app bundle.
- Remove or rename one required resource and confirm a visible fail-closed
  diagnostic.
- Confirm no source contains a developer-machine absolute resource path.

## Verify the production menu

- Enumerate 28 items from the C API.
- Confirm category counts are 14 Original, 8 Spatial, and 6 Codec.
- Find every stored full name through `glic_glitch_preset_find()`.
- Reject an unknown name without replacing the active preset.
- Confirm an explicit experimental browser cannot overwrite the production
  bank or its ordering.

## Verify Original and Spatial

- Process a changing real-video frame with one preset from each integrated
  category.
- Confirm Original uses a CPU buffer.
- When using the Spatial texture path, confirm `BGRA8Unorm`, host-owned command
  buffer commit, and no more than three in-flight frames.
- Switch presets outside the frame callback.
- Exercise padded row bytes and the host's actual resolution changes.
- Confirm steady-state processing does not introduce host-side per-frame heap
  allocation.

## Verify Codec Glitch

- Test the selected H.264, HEVC, or ProRes 422 configuration.
- Test the host's actual 420v or 32BGRA input.
- Confirm submit never blocks the capture callback.
- Exercise `BACKPRESSURE` and `NO_FRAME_AVAILABLE`.
- Release every successful output once and only once.
- Reject stale output with a generation ID after switching.
- Flush and destroy cleanly at shutdown.
- Record `glic_codec_glitch_effect_implementation_level()`.
- Distinguish intentional repeat from non-intentional fallback.

When `NV12_METAL` is required, require all five evidence flags. When `AUTO` is
allowed, report whether fallback occurred instead of calling it fast path.

Use the packaged actual-video validation when available:

```bash
python3 Tools/validate_videotoolbox_fast_path.py input.mov \
  --output-dir validation/videotoolbox-fast-path
```

## Verify realtime acceptance

Measure inside the final host with its capture, display, analysis, and other
work enabled:

- width at least 960;
- height at least 540;
- processing and delivered stream at least 20 fps;
- p95 frame latency at most 50 ms;
- at least 120 frames in the measured interval.

Codec acceptance also requires:

- hardware encoder and decoder;
- preserved required frame behavior;
- zero non-intentional fallback;
- zero codec errors;
- zero watchdog recoveries;
- zero backpressure and output-queue drops for certification;
- zero GPU timeouts for the required fast path.

Treat library benchmark results as diagnostic context, not host certification.

## Verify offline workflows

- Validate effect and codec support before launching.
- Launch in a child process.
- Capture stdout, stderr, exit code, and JSON report.
- Confirm declared `execution_class`, `realtime_certified`, and
  `implementation_level`.
- Keep damaged decoding out of the host process.
- Check sidecar and binary SHA for external syntax tools.
- Preserve explicit source-reencode and bitstream-modification claims.
- Evaluate visual difference and diversity on actual video before presenting a
  preset as useful.

## Handoff evidence

Report:

1. SDK root and revision or checksum verification.
2. Distribution and lanes integrated.
3. Menu and effect counts.
4. Real-video test asset characteristics.
5. Build and test commands with status.
6. Resolution, fps, p95 latency, hardware state, and fast-path flags.
7. Reliability counters and ownership checks.
8. Offline implementation levels and output-kind limitations.
