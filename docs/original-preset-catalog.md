# Original GLIC preset realtime audit

`scripts/build_original_preset_catalog.py` audits the 144 upstream-named preset
parameter files on the GLIC Metal realtime path. It produces a deterministic,
token-free JSON/CSV/HTML catalog and a farthest-first shortlist of visibly
dissimilar dry/wet morphologies.

## Claim boundary

The report keeps these facts separate:

1. `preset_files = sha256_verified_pinned_upstream_corpus` only after the
   complete provenance gate passes
2. `preset_parameters = per_row_mapping_fidelity_required`
3. `render_path = glic_metal_realtime_visual_approximation`
4. `codec_pixel_fidelity = not_claimed`
5. `original_processing_codec_performance = not_measured`

Passing this audit means that the Metal approximation of that named preset was
measured at 960x540 with at least 10 warm-up frames and 120 measured frames,
and both mean and p95 synchronous wall time were at most 33.333 ms. It does not
mean that the original Processing encode/decode codec is pixel-identical or
runs at 30 fps.

## Inputs

- A `glic-realtime-benchmark-v1` JSON covering all 144 presets. Use the same
  960x540 dry PNG with `--all-presets --backend metal --frames 120 --warmup 10
  --require-fps 30 --preset-semantics original`.
- One representative 960x540 wet PNG per preset.
- The exact 960x540 dry reference used to render the wet PNGs.
- `presets.upstream.sha256` plus the local `presets/` directory. Every file
  hash and the benchmark/preview name sets must match the pinned 144-file
  upstream corpus before the catalog is publishable.
- Optionally, a JSON array emitted by `visual-liveliness`, with each row's
  `name` exactly equal to its preset name.

The preview directory convention is `<exact preset name>.png`, including spaces
and punctuation. A manifest can be used instead:

```json
{
  "schema": "glic-original-preset-preview-manifest-v1",
  "previews": [
    {"preset": "colour_waves", "path": "previews/colour_waves.png"},
    {"preset": "cute blocks", "path": "previews/cute blocks.png"}
  ]
}
```

Manifest paths are resolved relative to the manifest file. A compact mapping
such as `{"colour_waves": "previews/colour_waves.png"}` is also accepted.

## Run

```bash
python3 scripts/build_original_preset_catalog.py \
  --benchmark audit/realtime-all-presets-960x540.json \
  --previews-dir audit/previews \
  --dry audit/dry-960x540.png \
  --liveliness-json audit/liveliness.json \
  --select 12 \
  --output-dir audit/catalog
```

Use `--preview-manifest audit/previews.json` instead of `--previews-dir` when
filenames cannot preserve the exact preset names. The default expected corpus
size is 144; `--expected-count` exists only for fixtures and deliberately
smaller audits. Such fixture reports remain `publishable=false` unless their
content is the pinned upstream corpus.

The process exits `0` only when the corpus is complete and at least one preset
is eligible. It still writes reports before returning `3` for an incomplete or
empty catalog. Malformed source JSON returns `2` and publishes no new report.

Outputs:

- `ranking.json`: complete evidence, feature vectors, gate reasons, fidelity
  labels, and deterministic ranking.
- `ranking.csv`: flat operator-friendly audit table.
- `index.html`: thumbnail shortlist plus the complete timing audit.

## Metric families

Metrics are not collapsed into an aesthetic score:

- `realtime_performance` is the mandatory fail-closed gate.
- `preset_mapping_fidelity` is a separate compatibility gate. Only
  `exact-compatible` rows enter the original-compatible ranking.
  `approximated` rows receive their own ranking and counts. `unsupported`,
  missing, legacy, or malformed mappings are never promoted to either tier,
  even if their projected shader configuration is fast.
- `dry_wet_pixel_difference` measures effect presence (MAE, changed ratios,
  luma correlation, and a global luma SSIM proxy).
- `visual_integrity` rejects previews that do not reach the repository's
  VISIBLE dry/wet floor (RGB MAE 10, 25% of pixels changed, Gaussian-window
  luma SSIM
  at most 0.95) or clip more than 15% of output channels at the highlight
  ceiling. This is a technical gate, not an aesthetic score.
- `dry_wet_perceptual_morphology` drives greedy diversity selection using
  residual grids, residual hashes, artifact scale, orientation, and a smaller
  color/layout component.
- The requested selection count is a maximum. A candidate is not selected
  when its minimum morphology distance from the current shortlist is below
  `0.10`, so near-duplicates never fill a quota.
- `visual-liveliness` is optional technical presence/shape evidence. It is
  attached verbatim after strict finite-field validation and has zero ranking
  weight.
- Aesthetic quality is not scored.

The compatible and approximated tiers are ranked independently. The first
preset in each tier has the strongest technical effect-presence score. Every
following preset maximizes its minimum morphology distance from the already
selected set; presence is only a 10% tie-break component. This keeps
near-duplicates late even when many presets use similar block scales, without
allowing an approximation to be counted as original-compatible.
