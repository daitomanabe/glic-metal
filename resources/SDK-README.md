# GLIC Metal macOS SDK

Contents:

- `GlicMetal.xcframework` — static C ABI library and public headers;
- `GlicMetalResources.bundle` — Metal kernels, presets, and license notices;
- `SHA256SUMS` — checksums for the packaged files.

Add the XCFramework and resource bundle to the Xcode application target. Link
`libc++.tbd`, Foundation.framework, and Metal.framework. Swift can
`import GlicMetal`; Objective-C/C hosts can include
`<glic_metal/glic_metal.h>` or `<glic_metal/glic_metal_metal.h>` from the host.

Resolve the runtime files from `GlicMetalResources.bundle` and pass their paths
through `glic_metal_config.preset_directory` and
`glic_metal_config.metal_library_path` before calling `glic_metal_prepare()`.

See the source repository's `docs/EMBEDDING.md` for lifecycle, threading,
pixel-format, Swift, and zero-copy Metal examples.
