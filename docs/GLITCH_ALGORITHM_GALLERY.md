# Glitch algorithm gallery

`scripts/build_glitch_algorithm_gallery.py` creates the public comparison
gallery used at `https://projects.daito.ws/glic-metal-gallery/`.

The catalog definition is deliberately narrower than “every possible codec and
parameter permutation” and broader than the adopted 28-preset realtime SDK bank:

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

## Review and adopt presets

The public gallery includes a three-state review control on every rendered
variant:

- `採用 / ADOPT` — include the exact algorithm and parameters in the adopted
  export;
- `不採用 / REJECT` — retain an explicit rejection for review provenance;
- `未判定 / PENDING` — leave the variant undecided.

The canonical review adopts 53 variants. Those adopted variants are the
default gallery view; choosing `全て表示 / SHOW ALL` reveals all rejected and
pending candidates. Decisions are saved automatically in browser
`localStorage` under the versioned `glic-metal-gallery-review-v2` envelope.
Family, text, and review-state filters can be combined. Each algorithm also
has bulk controls for its three variants.

When review is complete:

1. Use **採用JSONをコピー** or **採用JSONを保存** for the compact adopted set.
2. Use **全判定をコピー** or **全判定を保存** when rejected and pending entries
   must also be retained.
3. Hand `glic-metal-adopted-presets.json` to the integrating agent. Each item
   contains the stable algorithm ID, family, effect, codec, implementation
   level, realtime claim, exact parameters, media path, and technical QA.

The export schema is `glic-metal-gallery-review-v1`. The committed canonical
files are `resources/glic-metal-gallery-review.json` and
`resources/glic-metal-adopted-presets.json`. Importing them through
`scripts/import_gallery_curation.py` generates the 28-entry realtime SDK bank
from the adopted entries carrying `realtime_certified=true`; the 25
offline-only adopted entries remain gallery selections.

```bash
python3 scripts/import_gallery_curation.py \
  /path/to/glic-metal-gallery-review.json \
  --adopted-json /path/to/glic-metal-adopted-presets.json \
  --write
python3 scripts/import_gallery_curation.py --check
```

## Run a recipe on your own video

Every card shows its recipe key, such as `original:vv01::saturation`, and offers
**レシピをコピー / COPY RECIPE** and **レシピを保存 / DOWNLOAD RECIPE**. The
`glic-metal-gallery-recipe-v1` JSON records the key, algorithm, variant, exact
parameters, and gallery revision.

```bash
python3 scripts/run_gallery_recipe.py input.mov output.mp4 --key 'original:vv01::saturation'
pbpaste | python3 scripts/run_gallery_recipe.py input.mov output.mp4 --recipe -
python3 scripts/run_gallery_recipe.py input.mov output.mp4 --recipe recipe.json \
  --width 960 --height 540 --fps 30
python3 scripts/run_gallery_recipe.py --list --family spatial
```

`scripts/run_gallery_recipe.py` builds the same processor command as the gallery
and then applies the delivery `wet_mix` blend, which the processors do not
perform themselves. The family, effect, and codec always come from
`resources/glitch-gallery-presets.json`; a recipe contributes only its
parameters and is rejected when its schema, variant, or parameter names do not
match. `--print-command` shows the processor command without running it.

- Output defaults to the gallery contract of 480×270 at 24 fps. The look depends
  on resolution, so a larger size keeps the recipe but changes the result.
- Offline families stop at the gallery frame limit unless `--max-frames` raises
  it. AV2 and VVC keep their reduced reference frame rates.
- Donor families derive their second video from the input with the gallery
  filter unless `--donor` provides one.
- Failed runs keep their temporary directory and print its path; `--work-dir`
  keeps intermediates from every run.
- The script needs the CMake `build/` tree, NumPy and OpenCV from
  `requirements-qa.txt`, and the pinned `.cache/` tools for families that use
  them.

Prefer a recipe to a `.glic` file. A `.glic` file is one encoded still image, and
the gallery videos do not pass through that container, so it cannot carry a look
to other footage. Results are also sensitive to the input: re-encoding a source
at near-lossless quality kept the character of `original:vv01::saturation` but
moved its blocks entirely. Reproducing a card exactly requires the gallery's
normalized source.

For application integration, use the SDK preset names of the 28
realtime-certified variants, such as `original__vv01`. See
[`AI_INTEGRATION.md`](AI_INTEGRATION.md).

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
  assets/test-video.mp4 \
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
passthrough clip. The final site also measures all three poster pairs per
algorithm and records their minimum/mean MAE under `variant_diversity`; a
minimum below 1.0 is labeled `WEAK` instead of silently presenting visually
identical variants.

To change only the public delivery mix or duration contract without rerunning
the expensive codec processors, reuse each task's retained `raw.mp4`:

```bash
python3 scripts/build_glitch_algorithm_gallery.py \
  assets/test-video.mp4 \
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
