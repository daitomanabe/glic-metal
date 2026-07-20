# GLIC preset effect gallery

This directory contains rendered outputs for all 144 presets in `presets/`, generated from `daito-testimage.png`.

- `index.html`: browser gallery for quick visual review.
- `contact-sheet.png`: single-image overview of all preset outputs.
- `manifest.tsv`: preset-to-output mapping and render status.
- `images/`: decoded PNG results.
- `glic/`: intermediate `.glic` files.
- `logs/`: encode/decode logs with repository paths normalized to `{repo_path}`.

Generation command shape:

```bash
./build/glic encode daito-testimage.png output/preset-gallery/glic/<preset>.glic --preset <preset> --presets-dir presets
./build/glic decode output/preset-gallery/glic/<preset>.glic output/preset-gallery/images/<preset>.png
```
