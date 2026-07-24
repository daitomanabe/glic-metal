# Building GLIC Metal

## Requirements

- CMake 3.16 or newer
- A C++20 compiler
- Python 3 for analysis scripts and test adapters
- The `external/stb` Git submodule
- Full Xcode in `/Applications/Xcode.app` for Metal and the webcam app

Python image-analysis workflows additionally use the packages in
`requirements-qa.txt`.

Direct MPEG-2 MV/qDCT/qscale and MPEG-4 Part 2 MV editing additionally requires
the external FFglitch 0.10.2 `ffedit` executable. On Apple Silicon, install the
checksum-pinned official build into the ignored cache:

```bash
python3 scripts/install_ffglitch_reference.py
```

FFglitch is GPL-2.0-or-later and is neither linked into nor bundled with the
MIT-licensed GLIC Metal library. See
[NATIVE_SYNTAX_GLITCH.md](NATIVE_SYNTAX_GLITCH.md).

## Clone and build

```bash
git clone --recurse-submodules <repository-url>
cd glic-metal
python3 -m pip install -r requirements-qa.txt
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON
cmake --build build --parallel
ctest --test-dir build --output-on-failure
```

This unfiltered command includes the `hardware`-labelled webcam and
VideoToolbox codec tests and is the release check on a physical Mac. Hosted CI
uses `ctest --test-dir build --output-on-failure -LE hardware` because its
virtual macOS runner does not provide a reliable hardware decoder.

If the repository was cloned without submodules:

```bash
git submodule update --init --recursive
```

## macOS application

```bash
cmake --build build --target glic_webcam_preview --parallel
open "build/GLIC Webcam Preview.app"
```

The first launch requests camera permission. The generated application is
ad-hoc signed for local testing. Distribution outside the local machine needs
an appropriate Developer ID signature and notarization.

## Install tree

```bash
cmake --install build --prefix dist
```

This installs command-line tools, presets, scripts, the macOS application when
available, the `GlicMetal::GlicMetal` CMake package, public embedding headers,
and the project/third-party license notices. See
[EMBEDDING.md](EMBEDDING.md) for host-application integration.

When included with `add_subdirectory()`, the project only builds the library by
default. Set `GLIC_BUILD_STANDALONE=ON` to also build the CLI, benchmarks,
tests, examples, and webcam application. `GLIC_INSTALL` controls installation
rules independently.

## Optional ranking pipeline

The ranking workflow intentionally does not bundle the visual-liveliness
measurement tool. Point to a compatible runner explicitly:

```bash
export VISUAL_LIVELINESS_RUNNER=/absolute/path/to/visual-liveliness/scripts/run.sh
export GLIC_REALTIME_CERTIFIER="$PWD/build/glic_realtime_certify"
scripts/build_ranked_catalog.sh search-runs/<run-name>
```

Generated videos, search runs, build trees, and local test inputs are ignored
by Git.
