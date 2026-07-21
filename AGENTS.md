# Repository guidance

## Role

- Repository: `glic-metal`
- Upstream: `GlitchCodec/GLIC`
- Public overview: `README.md` and `website/`
- This directory is the canonical Git root.

## Working rules

- Keep nested repositories as submodules; never commit nested `.git`
  directories.
- Preserve the distinction between the file codec, `original_visual`, and
  `compat_realtime` in code and documentation.
- Preserve bilingual public documentation when changing user-facing behavior.
- Keep generated videos, camera captures, search runs, and build trees out of
  Git. `output/preset-gallery/` is the only intentional tracked-output area.
- Never commit credentials, environment files, personal input media, or
  machine-specific absolute paths.

## Important paths

- `FILE-STRUCTURE.md` maps the normalized public layout.
- `docs/BUILDING.md` is the reproducible build entry point.
- `docs/ORIGINAL_PRESET_REALTIME.md` defines compatibility claims.
- `docs/PUBLIC_RELEASE.md` contains the publication gate.
- `presets.upstream.sha256` pins the upstream preset corpus.

## Validation

Before committing a public-facing change, run:

```bash
python3 scripts/check_public_release.py --source .
cmake --build build --parallel
ctest --test-dir build --output-on-failure
```

Metal and webcam changes require macOS validation. Prefer small, repository-
local commits, and update `CHANGELOG.md` for user-visible behavior.
