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
| `original_visual` | Upstream GUI semantics | Original-style segmentation, prediction, quantization and transform reconstruction | No | Yes on CPU or Metal, supported subset only |
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

The higher-fidelity `original_visual` lane has a CPU/JWave-double reference and
a Metal implementation with compensated float-float CDF97 accumulation plus
fp32 matrix storage. Both use the same resolution, warm-up, measured-frame,
mean, and p95 limits. Reports must identify
`cpu-reference` or `metal-original-visual`; timing from one backend is not
evidence for the other. The Metal result for all 144 names still belongs only
to the separately labelled `compat_realtime` visual approximation lane.

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
4. Add a JWave-double-equivalent CPU CDF 9/7 path and a separately
   precision-labelled Metal CDF97 path for the 21 deterministic FWT/WPT
   presets in the largest wavelet family in the corpus.
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
workspaces and 512 x 512 transform workspaces in `prepare()`. Segmentation uses
one Processing-compatible Java 48-bit RNG in the original channel 0 -> 1 -> 2
and TL/TR/BL/BR DFS order. Reconstruction then uses independent channel
workspaces and runs all three channels in parallel. Two persistent workers are
created in `prepare()` and sleep on a condition variable between generations;
threads are not created per frame. The lane does not allocate a prediction or
transform matrix for each quadtree leaf.

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
- the colorspace port and arithmetic sites without explicit golden tests have
  not been byte-for-byte certified against the Processing/JVM implementation;
- the original sketch does not call `randomSeed`; this realtime lane fixes the
  otherwise time-dependent Processing-compatible stream to seed 42 for
  reproducible video and tests;
- non-CDF97 wavelets, random transforms and predictor-search modes are
  unsupported rather than approximated.

Processing/Java `round(float)` and `Planes.toPixels` raw shift/OR packing are
matched explicitly, including negative-half rounding, NaN/infinity saturation,
and unmasked cross-byte spill. Java RNG sequence and skip-ahead state have
golden tests; forced quadtree nodes advance the state without unused image
reads. Host compilation disables multiply/add contraction in the CPU oracle and
Metal segmentation control pass so ARM FMA cannot move a Java float rounding
boundary. A two-frame adaptive-tree golden checks cross-channel RNG progression.

Fixed-predictor reconstruction is checked against the slower C++ reference for
all 14 supported predictors and both clamp modes. The source quantization value
`87`, for example, is tested with the upstream codec step `43.5`; it is not
scaled twice.

## Implemented `original_metal_visual` lane

`OriginalRealtimeMetalLane` keeps the same 37-preset fail-closed support
boundary while moving reconstruction to Metal:

1. six persistent CPU slices convert input pixels into the upstream color
   space;
2. one small CPU control pass builds the sampled quadtree in original channel
   and DFS order using the shared Java 48-bit RNG;
3. after the exact leaf lists are fixed, three independent CPU workers assign
   dependency levels from the fully reconstructed top and left boundaries;
4. Metal dispatches each dependency frontier in order and processes all three
   channels concurrently;
5. each leaf runs residual prediction, quantization, CDF97 FWT/WPT,
   compression, inverse transform, and reconstruction inside one threadgroup;
6. six persistent CPU slices convert the shared output planes back to BGRA.

Fixed-block presets build the exact DFS leaf/frontier schedule once in
`prepare()` and reuse it for every frame, while advancing the otherwise-unused
RNG state by the original draw count in O(log N). Adaptive nodes whose split or
leaf result is forced by block-size bounds likewise advance the RNG without
unused plane sampling and variance work. For sampled adaptive nodes, Welford's
sum is monotonic: once the deviation computed with the final denominator
exceeds the threshold, dead image reads and arithmetic stop while the remaining
Java RNG calls are advanced exactly. An independent full-sampling test oracle
compares ordered leaves and terminal RNG state over multiple thresholds and
consecutive frames. Every `process()` call also checkpoints that stream and
commits it only after the full CPU or Metal frame succeeds; any failure rolls
back, so retry/drop paths cannot perturb later trees. Fixed-block presets and adaptive presets that cannot emit
leaves above 32 px use the dedicated threadgroup-memory pipeline with cached
top/left boundaries, local matrix and scratch storage. Adaptive CDF97 presets
whose declared bounds admit larger leaves use a frame-stable preallocated
global workspace route; fixed mixed-
channel frontiers may bucket independent small and large leaves before their
single frontier barrier.
Dependency barriers fence only the reconstructed plane buffer. Threadgroup size
is selected from the current block size and frame segment density. Per-frame
work uses one command-buffer submission, one completion wait, and no
mapped-buffer copy on Apple unified memory.

Plane, segment, transform, scratch, uniform, dependency-map, and worker storage
is allocated in `prepare()`. Frame processing does not grow those workspaces or
create threads. The current API is a synchronous span/raw-video hybrid; it is
not the zero-copy texture API used by `compat_realtime`.

Metal/Foundation initialization, repeated `prepare()`, and destruction each
run inside an autorelease pool. The video wrapper reserves the larger of
`nb_frames` and its duration/fps estimate, so all timing vectors are allocated
before streaming even when the container under-reports its frame count. The
JSON report records the initial capacity and any unexpected growth event.

Metal does not provide the fp64 arithmetic used by the CPU/JWave port. The
Metal CDF97 kernel therefore splits each JWave coefficient into high and low
fp32 components and uses compensated float-float product accumulation. Inverse
passes iterate only taps matching the destination parity, preserving the exact
ascending accumulation subsequence while removing rejected iterations. The
matrix remains fp32 between passes, so CDF97 is explicitly
`processing_pixel_exact=false`. No-wavelet reconstruction is integer-exact
against the CPU lane. Tests require bit-exact local/global Metal output for
2--32 px FWT/WPT leaves, including 63 x 47 and 65 x 49 edge padding, mixed
channel block sizes, and DC/JPEGLS/DIFF predictors. CPU-double deviation is
bounded through 64 px, and all 37 supported presets are executed rather than
only prepared. Reports expose local/global pipeline dispatches and segment
totals, dependency-frontier plane-buffer barriers, and fixed-schedule reuse so
the accelerated route can be verified. The CPU/Metal comparison binds each
preset to its terminal Java RNG state, ordered-leaf FNV-1a64 hash, and early
skip counters in addition to input/config/preview provenance. The final
manifest refuses both tracked and untracked dirty source and records hashes for
the benchmark binary, video-filter binary, and compiled metallib.

The report accounting invariants are checked with per-frame integer counters
before a frame is accepted:

- GPU dispatches = threadgroup dispatches + global dispatches;
- total segments = threadgroup segments + global segments;
- plane-buffer barriers = dependency frontiers - 1.

### Certified local baseline

#### Historical CPU baseline

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

#### Previous certified Metal baseline (`6e1d1f8`, 2026-07-20)

The previous isolated baseline was measured on `rhizoma30`, a MacBook Pro
`Mac16,5` with Apple M4 Max (16 CPU cores) and 128 GB RAM. At 960 x 540 with
10 warm-up and 120 measured frames, `metal-original-visual` passed all 37
supported presets. Both mean and p95 remained below 33.333 ms:

- normal source image: 37/37; slowest mean 32.554 ms and slowest p95
  33.049 ms (`wtf2`);
- deterministic uniform-noise stress: 37/37; slowest mean 23.922 ms and p95
  24.074 ms (`webp`).

The CPU-reference image comparison has two explicit gates. Integer/no-wavelet
presets must be pixel-exact. CDF97 presets first receive a numeric gate using
RGB MAE, luma SSIM, and aligned-edge correlation; 34/37 passed that gate.
`colour_mess2`, `colour_waves_sharp`, and `colour_waves_sharp2` retain fp32
matrix storage between CDF97 passes. Remaining storage differences can cross
integer-rounding boundaries and propagate through later predictors. They fail
numeric identity but
pass the separate original-style morphology gate based on blurred structure,
aligned spatial-edge correlation, edge-orientation distribution, and
edge-energy ratio. A negative-control test rejects spatially shuffled 32 px
tiles even when their global edge statistics remain similar. Consequently the
Metal claim is algorithmic and morphological fidelity for 37/37, not
CPU-double pixel identity.

An actual 166-frame 960 x 540 / 30 fps `vv02` video measured 150.787 kernel
fps, 132.854 fps including BGRA pipe backpressure, and 107.669 fps including
FFmpeg decode, VideoToolbox encode, and mux. Timing storage started at 168
slots and recorded zero growth events; the Metal counters remained one command
buffer, one CPU/GPU completion wait, and zero mapped-buffer copies per frame.
Against a passthrough encode, the effect gate classified the output `STRONG`
(RGB MAE 70.247, 99.91% of pixels changed by at least 10, luma SSIM 0.1085).
Technical video QA decoded all 166 frames, found zero repeated or frozen pairs,
and passed motion, exposure, color, complexity, and lighting checks.

The complete reports, processed video, passthrough control, difference heatmap,
and 111 CPU/Metal preview PNGs are under
`test-videos/original-visual-metal/remote-m4max-20260720/`.

```bash
./build/glic_original_realtime_bench \
  test-videos/preview/original-presets-v1/dry-960x540.png \
  --all-supported \
  --presets-dir presets \
  --backend cpu \
  --frames 120 --warmup 10 \
  --output-dir test-videos/original-visual-metal/cpu-reference-previews \
  --json test-videos/original-visual-metal/benchmark-cpu-reference-960x540.json

./build/glic_original_realtime_bench \
  test-videos/preview/original-presets-v1/dry-960x540.png \
  --all-supported \
  --presets-dir presets \
  --backend metal \
  --frames 120 --warmup 10 \
  --require-fps 30 \
  --output-dir test-videos/original-visual-metal/previews \
  --json test-videos/original-visual-metal/benchmark-metal-960x540.json

scripts/compare_original_metal_reference.py \
  --cpu-dir test-videos/original-visual-metal/cpu-reference-previews \
  --metal-dir test-videos/original-visual-metal/previews \
  --cpu-benchmark test-videos/original-visual-metal/benchmark-cpu-reference-960x540.json \
  --benchmark test-videos/original-visual-metal/benchmark-metal-960x540.json \
  --output-json /tmp/glic-original-metal-reference.json
```

For a complete repeatable run, including the noise stress case, real-video
throughput, allocation counters, passthrough-relative visible-effect gate, and
technical video QA:

```bash
scripts/run_original_metal_validation.sh \
  --normal-image /absolute/path/to/dry-960x540.png \
  --noise-image /absolute/path/to/noise-960x540.png \
  --video /absolute/path/to/source-960x540-30fps.mkv \
  --output-dir /absolute/path/to/validation-results
```

The comparison refuses missing or mismatched input hashes, resolution, sample
counts, preview frame index, preset sets, or full three-channel preset
configuration hashes. Each benchmark result binds its generated PNG with a
raw-byte FNV-1a64 hash. The comparator reads each PNG once and derives the
provenance check, image pixels, and SHA-256 audit hash from that same byte
sequence, so stale or replaced previews cannot be scored accidentally.
