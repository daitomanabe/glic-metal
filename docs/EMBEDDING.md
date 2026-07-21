# Embedding GLIC Metal

GLIC Metal exposes a versioned C ABI so a host does not need to depend on the
project's internal C++ classes. The same API can be called from C, C++,
Objective-C, Objective-C++, Swift through a bridging header, JUCE, openFrameworks,
or another native video application.

The public surface is:

- `include/glic_metal/glic_metal.h` — portable C API;
- `include/glic_metal/glic_metal_metal.h` — typed Objective-C Metal helpers;
- `GlicMetal::GlicMetal` — CMake target;
- `glic_realtime.metallib` — Metal kernels to copy into the host bundle;
- `presets/` — runtime preset data.

Internal headers under `src/` are not part of the stable API.

## Choose a processing path

| Mode | Presets | Input | Main use |
|---|---:|---|---|
| `GLIC_METAL_MODE_ORIGINAL` + Strict | 37 audited | BGRA/RGBA CPU buffer | Closest original-style result |
| `GLIC_METAL_MODE_ORIGINAL` + Fast Match | 37 algorithm-supported | BGRA/RGBA CPU buffer | Faster approximate CDF 9/7 |
| `GLIC_METAL_MODE_COMPAT_REALTIME` | all 144 | CPU buffer or Metal texture | Maximum variety and easiest GPU composition |

The original-style lane still performs input conversion and segmentation on
the CPU before Metal reconstruction, so its public integration path is a
preallocated CPU frame buffer. Compatibility mode can operate directly on the
host's `BGRA8Unorm` textures and append work to the host command buffer.

Fast Match availability in this library is the algorithm-support boundary, not
the preview application's quality allowlist. Hosts that need the same visual
gate as the bundled preview can read `config/fast-match-allowlist.json` or apply
their own measured preset policy before selecting Fast Match.

## Add as a CMake subdirectory

When GLIC Metal is not the top-level project, standalone tools and installation
rules default to off. A host only builds the library and shaders:

```cmake
set(GLIC_BUILD_STANDALONE OFF CACHE BOOL "" FORCE)
set(GLIC_INSTALL OFF CACHE BOOL "" FORCE)
add_subdirectory(path/to/glic-metal)

target_link_libraries(MyApp PRIVATE GlicMetal::GlicMetal)
```

On macOS, full Xcode is required to compile the Metal kernels. Attach the
runtime resources to a bundle target with the supplied helper:

```cmake
glic_metal_copy_resources(
  TARGET MyApp
  DESTINATION "$<TARGET_BUNDLE_CONTENT_DIR:MyApp>/Resources")
```

It copies `${GLIC_METAL_METALLIB}` as `glic_realtime.metallib` and copies
`${GLIC_METAL_PRESETS_DIR}` into a `Presets` resource folder. The same function
is available from the installed CMake package.

## Use an installed CMake package

Build and install once:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
cmake --install build --prefix dist
```

Then consume it from another project:

```cmake
find_package(GlicMetal 1 CONFIG REQUIRED)
target_link_libraries(MyApp PRIVATE GlicMetal::GlicMetal)

# Useful runtime resources exported by GlicMetalConfig.cmake:
message(STATUS "Presets: ${GLIC_METAL_PRESETS_DIR}")
if(APPLE)
  message(STATUS "Metal library: ${GLIC_METAL_METALLIB}")
endif()
```

Configure the host with `-DCMAKE_PREFIX_PATH=/absolute/path/to/dist` when the
prefix is outside CMake's normal search path.

## Minimal C API

```c
#include <glic_metal/glic_metal.h>

glic_metal_context *engine = NULL;
glic_metal_context_create(&engine);

glic_metal_config config;
glic_metal_config_init(&config);
config.width = 960;
config.height = 540;
config.preset_directory = preset_directory;
config.preset_name = "vv02";
config.backend = GLIC_METAL_BACKEND_AUTO;
config.mode = GLIC_METAL_MODE_ORIGINAL;
config.fidelity = GLIC_METAL_FIDELITY_STRICT;

if (glic_metal_prepare(engine, &config) != GLIC_METAL_OK) {
  log_error(glic_metal_get_last_error(engine));
}

glic_metal_process_frame(engine,
                         input_bgra, input_bytes_per_row,
                         output_bgra, output_bytes_per_row,
                         GLIC_METAL_PIXEL_FORMAT_BGRA8,
                         frame_index);

glic_metal_context_destroy(engine);
```

`BGRA8` matches `kCVPixelFormatType_32BGRA`. `RGBA8` is also supported. Row
padding is accepted, and input/output may point to the same buffer. This path
copies pixels into preallocated engine storage but performs no heap allocation
during successful steady-state frame processing.

`glic_metal_prepare()` performs preset loading, allocation, pipeline creation,
and worker startup. Run it on a control/background queue, never inside a render
callback. If a new preset fails to prepare, the previous engine stays active.

## Zero-copy Metal integration

Include the typed helper in Objective-C or Objective-C++:

```objc
#include <glic_metal/glic_metal_metal.h>

glic_metal_config config;
glic_metal_config_init(&config);
config.width = width;
config.height = height;
config.preset_directory = presetDirectory.fileSystemRepresentation;
config.preset_name = "colour_glow";
config.backend = GLIC_METAL_BACKEND_METAL;
config.mode = GLIC_METAL_MODE_COMPAT_REALTIME;
config.metal_device = (__bridge void *)device;
config.metal_library_path = metalLibraryPath.fileSystemRepresentation;
glic_metal_prepare(engine, &config);

// input/output must be BGRA8Unorm textures created by `device`.
glic_metal_encode_texture_objects(engine, commandBuffer,
                                  inputTexture, outputTexture,
                                  frameIndex);
// The host commits the command buffer after adding its remaining work.
```

The asynchronous call only encodes work. The host owns command-buffer commit,
completion, synchronization, and texture lifetime. Keep at most three encoded
frames in flight for one context because uniforms use a three-slot ring.
`glic_metal_process_texture_objects()` is the synchronous alternative.

## Swift

Expose the headers through a bridging header:

```objc
#include <glic_metal/glic_metal.h>
#include <glic_metal/glic_metal_metal.h>
```

The installed `module.modulemap` also defines module `GlicMetal`. Pass an
existing `MTLDevice` without ownership transfer:

```swift
config.metal_device = Unmanaged.passUnretained(device).toOpaque()
```

Keep the Swift strings backing `preset_directory`, `preset_name`, and
`metal_library_path` alive only through `glic_metal_prepare()`; the engine
copies their values during preparation.

## Preset menus and switching

Use `glic_metal_enumerate_presets()` to populate a host menu. It calls the
provided callback once per preset in sorted order. Switch by changing
`preset_name` and calling `glic_metal_prepare()` off the render queue.

The 37-preset original-mode support boundary remains fail-closed. An
unsupported original preset returns `GLIC_METAL_UNSUPPORTED`; it is never
silently projected to compatibility mode.

## Ownership and threading

- One context owns its workers, buffers, Metal pipeline, and current preset.
- Context functions are not concurrently thread-safe. Process one context from
  one serial render queue.
- Use one context per independently processed stream.
- The host retains ownership of all frame buffers, Metal objects, and strings.
- Destroying a context stops its workers and releases its internal Metal
  objects. Passing `NULL` to destroy is safe.
- Use `glic_metal_get_last_stats()` for frame timing and
  `glic_metal_get_last_error()` immediately after a failed call.

The ABI version is `GLIC_METAL_ABI_VERSION`. Always initialize public structs
with their matching init function so `struct_size` and `abi_version` are set.

## Xcode resource checklist

For an Xcode application that links `libglic_metal.a` directly:

1. Add `include/` to Header Search Paths.
2. Link `libglic_metal.a`, `libc++.tbd`, `Foundation.framework`, and
   `Metal.framework`.
3. Copy `glic_realtime.metallib` into the application Resources phase.
4. Copy the required preset files into a `Presets` resource directory.
5. Pass the bundle resource paths in `glic_metal_config`.
6. If camera input is used, add the host application's camera usage string;
   the library itself does not request camera permission.

Alternatively, build a drop-in XCFramework and resource bundle:

```bash
scripts/build_macos_sdk.sh build/GlicMetalSDK
```

The command refuses to overwrite an existing output. Set
`GLIC_SDK_ARCHITECTURES='arm64;x86_64'` for a universal macOS library when the
installed Xcode supports both architectures.

See `examples/embed_c.c`, `tests/embed_metal_api_tests.mm`, and
`tests/consumer/` for complete buildable integrations.
