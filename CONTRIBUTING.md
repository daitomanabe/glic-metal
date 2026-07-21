# Contributing to GLIC Metal

Thank you for helping improve the project. Contributions should preserve the
distinction between upstream-compatible behavior and deliberate realtime visual
approximations.

## Before opening a change

1. Clone with submodules: `git clone --recurse-submodules <repository-url>`.
2. Follow [docs/BUILDING.md](docs/BUILDING.md) for dependencies and build steps.
3. Keep generated runs in the ignored `search-runs/`, `test-videos/`, or
   `output/` locations. Do not commit machine-specific paths or credentials.
4. If behavior comes from upstream GLIC, cite the upstream file and audited
   revision in the code or accompanying documentation.

## Validation

Run the release layout check and the complete test suite before submitting:

```bash
python3 scripts/check_public_release.py --source .
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON
cmake --build build --parallel
ctest --test-dir build --output-on-failure
```

Metal or webcam changes must also be tested on macOS with full Xcode. Include
the machine model, OS version, resolution, warm-up/measured frame counts, mean
frame time, p95 frame time, and any visual-fidelity gate in the change report.
The realtime acceptance floor is 960x540 at 20 fps; stricter 30 fps reports
should remain labelled as such.

## Change scope

- Keep `original_visual`, `compat_realtime`, and file-codec compatibility claims
  separate.
- Add tests for preset parsing, RNG order, or numerical behavior when those
  semantics change.
- Do not replace upstream preset files without updating and verifying
  `presets.upstream.sha256`.
- Update `CHANGELOG.md` for user-visible behavior.
- Add new third-party code or data to `THIRD_PARTY_NOTICES.md`.

By contributing, you agree that your contribution is licensed under the MIT
License in [LICENSE](LICENSE).
