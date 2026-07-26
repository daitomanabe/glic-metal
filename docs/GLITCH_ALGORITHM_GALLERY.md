# Glitch algorithm gallery

`scripts/build_glitch_algorithm_gallery.py` creates the public comparison
gallery used at `https://projects.daito.ws/glic-metal-gallery/`.

The catalog definition is deliberately narrower than “every possible codec and
parameter permutation” and broader than the adopted 19-preset bank:

- every executable named effect is included once;
- the 14 adopted original-style GLIC recipes are included as named programs;
- codec-generation effects are expanded across all eight codec
  implementations because AV1, AV2, HEVC, VP9, ProRes, VVC, Theora, and Dirac
  use materially different encoders;
- duplicate spatial/codec recipes in `selected-presets.json` are represented by
  their canonical effect entry.

This produces 147 algorithm entries and three deterministic variants per entry,
or 441 videos. The three variants are not a generic low/medium/high slider.
Effect-name-aware profiles choose decode-safe damage, motion-field, color-plane,
history, quantization, or source-mixing ranges, then apply a deterministic
per-algorithm offset. The public presets also declare an explicit 62%, 82%,
and 100% dry/wet mix. This preserves the real algorithm output while keeping
the three gallery looks distinct when an original codec recipe quantizes
different parameter values to the same internal state.

## Generate the preset catalog

```bash
python3 scripts/build_glitch_algorithm_gallery.py --catalog-only
python3 tests/test_glitch_gallery.py
```

The machine-readable result is
`resources/glitch-gallery-presets.json`.

## Render

```bash
python3 scripts/build_glitch_algorithm_gallery.py \
  /Users/daitomacm5/development/sandbox/glic-metal/assets/test-video.mp4 \
  --workers 2 \
  --resume
```

The renderer is resumable by input SHA-256, Git revision, algorithm definition,
and render contract. It writes processor reports and logs beneath
`output/glitch-algorithm-gallery/work/`, then creates the publishable static
bundle beneath `output/glitch-algorithm-gallery/site/`.

Each public clip is normalized to exactly five seconds / 120 frames at 480×270,
24 fps, H.264 `yuv420p`, muted, and fast-start enabled. Short damaged streams
hold their final decoded frame instead of shortening the gallery card. WebP
posters are selected by a deterministic
source-difference plus spatial-complexity score. Technical QA records decoded
frame count, source difference, changed-pixel ratio, motion, frozen-frame
ratio, luminance, and entropy. A weak or nearly static result is labeled
`WARN`; a failed processor or undecodable result is never replaced with a
passthrough clip.

To change only the public delivery mix or duration contract without rerunning
the expensive codec processors, reuse each task's retained `raw.mp4`:

```bash
python3 scripts/build_glitch_algorithm_gallery.py \
  /Users/daitomacm5/development/sandbox/glic-metal/assets/test-video.mp4 \
  --rebuild-web-only \
  --workers 8
```

The official AVM AV2 paths (including `av2_optical_flow_wound`) and VVenC
reference implementation process the complete five-second source at 2 fps
(10 coded frames) and 12 fps (60 coded frames), respectively, to keep the
exhaustive gallery finite; the web delivery is normalized back to 24 fps.
This is recorded in the catalog and is not presented as a realtime performance
claim. AV2 gallery variants hold the generation count at one and vary amount,
rate, and feedback; this keeps the official AVM path for all three looks
without multiplying the multi-minute reference encode depth. AVM v1.0.0 only
exposes the Good Quality usage profile, so the reduced temporal sampling is
explicit instead of being mislabeled as a realtime AV2 implementation.

The pinned FFglitch, AVM, and VVenC dependencies must exist in `.cache/`.
Build missing AV2 or VVC tools with `scripts/build_av2_reference.py` and
`scripts/build_vvc_reference.py`.
