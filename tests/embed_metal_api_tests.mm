#include <glic_metal/glic_metal_metal.h>

#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

#include <cstdint>
#include <cstring>
#include <iostream>
#include <vector>

#ifndef GLIC_TEST_PRESETS_DIR
#define GLIC_TEST_PRESETS_DIR "presets"
#endif

#ifndef GLIC_TEST_METALLIB
#define GLIC_TEST_METALLIB "glic_realtime.metallib"
#endif

int main() {
  @autoreleasepool {
    constexpr int width = 96;
    constexpr int height = 64;
    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    id<MTLCommandQueue> queue = [device newCommandQueue];
    if (device == nil || queue == nil)
      return 1;

    glic_metal_context *context = nullptr;
    if (glic_metal_context_create(&context) != GLIC_METAL_OK)
      return 1;
    glic_metal_config config;
    glic_metal_config_init(&config);
    config.width = width;
    config.height = height;
    config.preset_directory = GLIC_TEST_PRESETS_DIR;
    config.preset_name = "colour_glow";
    config.backend = GLIC_METAL_BACKEND_METAL;
    config.mode = GLIC_METAL_MODE_COMPAT_REALTIME;
    config.metal_device = (__bridge void *)device;
    config.metal_library_path = GLIC_TEST_METALLIB;
    if (glic_metal_prepare(context, &config) != GLIC_METAL_OK) {
      std::cerr << glic_metal_get_last_error(context) << '\n';
      return 1;
    }

    MTLTextureDescriptor *descriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                                     width:width
                                    height:height
                                 mipmapped:NO];
    descriptor.storageMode = MTLStorageModeShared;
    descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> input = [device newTextureWithDescriptor:descriptor];
    id<MTLTexture> synchronous = [device newTextureWithDescriptor:descriptor];
    id<MTLTexture> asynchronous = [device newTextureWithDescriptor:descriptor];
    if (input == nil || synchronous == nil || asynchronous == nil)
      return 1;

    std::vector<uint32_t> source(static_cast<size_t>(width) * height);
    for (int y = 0; y < height; ++y) {
      for (int x = 0; x < width; ++x) {
        const uint8_t red = static_cast<uint8_t>((x * 9 + y) & 255);
        const uint8_t green = static_cast<uint8_t>((x + y * 7) & 255);
        const uint8_t blue = static_cast<uint8_t>((x * 3 + y * 5) & 255);
        source[static_cast<size_t>(y) * width + x] =
            0xff000000u | (static_cast<uint32_t>(red) << 16u) |
            (static_cast<uint32_t>(green) << 8u) | blue;
      }
    }
    const MTLRegion region = MTLRegionMake2D(0, 0, width, height);
    [input replaceRegion:region
              mipmapLevel:0
                withBytes:source.data()
              bytesPerRow:width * sizeof(uint32_t)];

    if (glic_metal_process_texture_objects(context, input, synchronous, 7) !=
        GLIC_METAL_OK) {
      std::cerr << glic_metal_get_last_error(context) << '\n';
      return 1;
    }
    id<MTLCommandBuffer> commandBuffer = [queue commandBuffer];
    if (glic_metal_encode_texture_objects(context, commandBuffer, input,
                                          asynchronous, 7) != GLIC_METAL_OK) {
      std::cerr << glic_metal_get_last_error(context) << '\n';
      return 1;
    }
    [commandBuffer commit];
    [commandBuffer waitUntilCompleted];
    if (commandBuffer.status == MTLCommandBufferStatusError)
      return 1;

    std::vector<uint32_t> synchronousPixels(source.size());
    std::vector<uint32_t> asynchronousPixels(source.size());
    [synchronous getBytes:synchronousPixels.data()
               bytesPerRow:width * sizeof(uint32_t)
                fromRegion:region
               mipmapLevel:0];
    [asynchronous getBytes:asynchronousPixels.data()
                bytesPerRow:width * sizeof(uint32_t)
                 fromRegion:region
                mipmapLevel:0];
    if (synchronousPixels != asynchronousPixels ||
        synchronousPixels == source)
      return 1;

    MTLTextureDescriptor *wrongDescriptor = [descriptor copy];
    wrongDescriptor.pixelFormat = MTLPixelFormatRGBA8Unorm;
    id<MTLTexture> wrongFormat =
        [device newTextureWithDescriptor:wrongDescriptor];
    if (glic_metal_process_texture_objects(context, wrongFormat, synchronous,
                                           8) !=
        GLIC_METAL_PROCESSING_FAILED)
      return 1;
    if (std::strstr(glic_metal_get_last_error(context), "BGRA8Unorm") ==
        nullptr)
      return 1;

    glic_metal_context_destroy(context);
    std::cout << "PASS embedded Metal C API\n";
    return 0;
  }
}
