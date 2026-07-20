# Original GLIC preset realtime compatibility

This project treats upstream preset compatibility and realtime throughput as
separate claims. A preset can run quickly through the realtime approximation
without reproducing the image produced by the original codec.

## Upstream baseline

The audited source is GlitchCodec/GLIC commit
[`460e61b`](https://github.com/GlitchCodec/GLIC/commit/460e61bf9b01f7415cf973b3d655a0ae2c7962a7).
Its `presets/` directory contains 144 Java-serialized ControlP5 maps. The files
in this repository are byte-identical to that upstream directory. The pinned
commit and SHA-256 of every file are recorded in `presets.upstream.sha256`, and
the catalog fails closed unless that complete corpus and its exact name set are
verified.

[`GLIC.pde`](https://github.com/GlitchCodec/GLIC/blob/460e61bf9b01f7415cf973b3d655a0ae2c7962a7/GLIC.pde#L49-L124)
sets the Processing UI to 20 fps, but `draw()` only displays the current
buffer. Encoding and decoding are synchronous still-image operations invoked
by a key or GUI event. The 20 fps value is therefore not an encode/decode
throughput measurement.

## Compatibility levels

| Level | Preset values | Reconstructed image | `.glic` stream | Realtime target |
|---|---|---|---|---|
| `legacy_realtime` | Historical C++ interpretation | One-pass glitch approximation | No | Yes |
| `original_values_realtime` | Upstream GUI semantics | One-pass glitch approximation | No | Yes |
| `original_visual` | Upstream GUI semantics | Original-style segmentation, prediction, quantization and transform reconstruction | No | Yes on CPU, supported subset only |
| `file_codec` | Codec configuration | C++ encode and decode path | Yes | No |

Reports and command output must name the compatibility level. A 30 fps pass at
one level is not evidence for a higher-fidelity level.

## Why the historical loader is not an upstream decoder

The serialized maps hold controller state. `GUI.pde::readValues()` converts
that state before invoking the codec. Important conversions include:

- minimum and maximum block sizes are powers of two;
- transform scale is a power of two;
- quantization and wavelet compression use GUI-specific scaling;
- prediction, clamp, transform type and encoding values are list/radio indices;
- upstream has 68 wavelet entries plus random selection;
- unless `separate_channels` is enabled, channel 0 is copied to all channels.

The original-values API decodes these meanings explicitly and reports fields
that the current C++ enums or realtime shader can only approximate. The legacy
API remains available so existing canonical recipes and search archives do not
silently change identity.

## Performance gates

The `original_values_realtime` approximation gate is:

- Metal backend;
- 960 x 540 pixels;
- at least 10 warm-up frames;
- at least 120 measured frames;
- both mean and p95 processing time at or below 33.333 ms;
- no processing failure or dropped output frame.

The higher-fidelity `original_visual` lane is currently CPU-only. Its gate
uses `backend=cpu-reference` with the same resolution, warm-up, measured-frame,
mean and p95 limits. Therefore the 35/37 normal-input result and 34/37
normal-plus-stress result below prove CPU throughput for the original-style
lane; they do **not** satisfy or imply the Metal-backend requirement. The
Metal result for all 144 names belongs only to the explicitly labelled visual
approximation lane.

Video delivery is checked separately. Decode, colorspace conversion, the
realtime kernel, display/encode and audio remux can make end-to-end throughput
lower than the kernel-only benchmark. Finished videos must also pass decode,
motion/repeated-frame QA and a dry/wet difference gate.

## Measured baseline before the original-values decoder

On the local Apple GPU, the historical `legacy_realtime` path passed all 144
names at 960 x 540 with 10 warm-up and 120 measured frames. The slowest p95 was
3.136 ms. This proves realtime headroom only; it does not prove upstream visual
fidelity.

For comparison, eight named presets run through the existing full C++
encode-to-file then decode path measured 183.452 to 340.949 ms per image,
including process startup and PNG/`.glic` I/O. None reached 30 fps. This is a
cost baseline for the full path, not a bit-exact benchmark of the Processing
implementation.

## Implementation order

1. Decode and test all upstream GUI preset semantics without changing legacy
   recipe behavior.
2. Benchmark and image-analyze the correctly decoded presets through the
   explicitly labelled realtime approximation.
3. Implement `original_visual` first for the 16 no-wavelet, non-search
   prediction presets and fail closed for unsupported modes. This is retained
   below as the historical tier-one baseline.
4. Add the exact CDF 9/7 FWT and WPT paths used by the largest deterministic
   wavelet family in the corpus. This tier adds 21 supported presets.
5. Add the remaining wavelets, random transform and expensive
   REF/ANGLE/SAD/BSAD prediction modes only after separate 30 fps and
   visual-reference certification.

File encoding methods affect the `.glic` representation but not the returned
reconstructed preview after the residual has been rebuilt. They remain outside
the live visual path.

## Implemented `original_visual` CPU lane

`OriginalRealtimeCpuLane` implements a fail-closed visual-fidelity subset. It
performs the following work for every input frame:

1. upstream colorspace conversion and border-color conversion;
2. adaptive sampled quadtree segmentation;
3. one of the fixed upstream predictors `NONE` through `DIFF`;
4. residual subtraction and upstream quantization scaling;
5. either direct reconstruction or exact JWave CDF 9/7 FWT/WPT, magnitude
   compression, coefficient scaling and inverse reconstruction;
6. conversion back to RGB with the source alpha channel.

The implementation allocates its three contiguous planes, maximum-size segment
workspaces and 512 x 512 transform workspaces in `prepare()`. Frame processing
uses independent channel workspaces and runs all three channels in parallel.
Two persistent workers are created in `prepare()` and sleep on a condition
variable between generations; threads are not created per frame. The lane does
not allocate a prediction or transform matrix for each quadtree leaf.

The CDF 9/7 coefficients come from the `CDF97` class in the exact
`code/JWave.jar` bundled by upstream commit `460e61b`; its FWT, WPT and
mean-magnitude compression behavior are reproduced without routing through the
historical C++ wavelet factory. FWT/WPT parity is tested against a slower
allocation-heavy reference for both clamp modes.

The lane rejects a preset when any channel requests a wavelet other than CDF
9/7, a random transform, or a search predictor (`SAD`, `BSAD`, `RANDOM`, `REF`,
or `ANGLE`). It never substitutes a different transform, wavelet or predictor.
The current upstream corpus therefore has 37 algorithmically supported presets:
the historical 16 no-wavelet presets plus 19 CDF97 FWT and two CDF97 WPT
presets.

The historical 16 are:

`0rg4n1c-___`, `0rg4n1c-t1ny4ngl3z`, `0rg4n1c-tr1angl3`,
`0rg4n1c-tr1f0rc3`, `0rg4n1c-tr33`, `0rg4n1c-v1n3z`, `1amblu`,
`bi0g4n1c`, `burn`, `colour_glow`, `default`, `lightblur`, `vv03`, `vv07`,
`vv08`, and `vv10`.

This is an algorithmic-core fidelity claim, not a bit-identical Processing
claim. The deviations are explicit:

- `.glic` header/payload serialization and final entropy encoding are omitted;
- the C++ colorspace and arithmetic port has not been byte-for-byte certified
  against the Processing/JVM implementation;
- Processing's evolving global random sampler is replaced by deterministic,
  independent `mt19937` streams per channel so the channels can run safely in
  parallel;
- non-CDF97 wavelets, random transforms and predictor-search modes are
  unsupported rather than approximated.

Fixed-predictor reconstruction is checked against the slower C++ reference for
all 14 supported predictors and both clamp modes. The source quantization value
`87`, for example, is tested with the upstream codec step `43.5`; it is not
scaled twice.

### Certified local baseline

The original no-wavelet tier certified all 16 presets on the Apple M5 Max. That
result is a historical baseline, not the current supported count.

With exact CDF97 enabled, 35 of 37 supported presets passed three repeated
960 x 540 runs, each using 10 warm-up frames and 120 measured frames. Both mean
and p95 had to remain below 33.333 ms in every run; the published count is the
intersection, not the best run. `vv13` reached a worst 40.755 ms mean / 41.712
ms p95 and `webp` reached 42.776 ms mean / 44.224 ms p95. They remain visually
supported but are excluded by the mandatory realtime gate. Of the 21 CDF97
presets, 19 passed the repeated performance gate and 18 passed performance plus
dry/wet visibility and clipping gates. Across both tiers, 31 presets passed the
combined realtime and visible-output gates.

A separate deterministic 960 x 540 uniform-noise stress frame raised adaptive
segmentation load. It passed 34 of 37 presets: `burn` crossed the p95 budget at
33.627 ms in addition to `vv13` and `webp`. The conservative normal-plus-stress
intersection is therefore 34 realtime presets and 30 realtime-plus-visible
presets. CDF97 itself remained 19 of 21 under stress; `burn` is a no-wavelet
preset. Its real-video QA result remains valid for that tested video, but it is
not part of the adversarial-input guarantee set.

The exact reports and all 37 previews are under
`test-videos/preview/original-presets-v1/original-visual-fidelity/` as
`cdf97-benchmark-run-01.json` through `cdf97-benchmark-run-03.json`,
`cdf97-noise-stress.json`, `cdf97-image-analysis.json`,
`cdf97-robust-analysis.json`, the matching HTML indexes and
`cdf97-previews/`.
`scripts/analyze_original_visual_tier.py` verifies the pinned 144-file corpus,
requires three distinct benchmark reports, takes the repeat intersection,
applies dry/wet visibility and clipping gates, and creates a morphology-diverse
shortlist without filling the quota below distance 0.10. A report is not
publishable unless the requested shortlist count is actually filled. The benchmark report
records resolution, sample counts, required fps and the mean-plus-p95 policy.
Runs below those evidence minima can report `timing_passed`, but cannot report
`performance_passed`.

```bash
sips -z 540 960 test-videos/preview/benchmark-1920x1080.png \
  --out /tmp/glic-original-960x540.png

./build/glic_original_realtime_bench /tmp/glic-original-960x540.png \
  --all-supported \
  --presets-dir presets \
  --require-fps 30 \
  --json /tmp/glic-original-fidelity.json
```
