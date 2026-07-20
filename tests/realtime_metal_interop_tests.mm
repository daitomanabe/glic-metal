#include "preset_loader.hpp"
#include "realtime.hpp"

#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

#include <cstdint>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>

namespace {

std::vector<glic::Color> makeFixture(int width, int height) {
  std::vector<glic::Color> pixels(static_cast<size_t>(width) *
                                  static_cast<size_t>(height));
  for (int y = 0; y < height; ++y) {
    for (int x = 0; x < width; ++x) {
      pixels[static_cast<size_t>(y) * static_cast<size_t>(width) +
             static_cast<size_t>(x)] =
          glic::makeColor(static_cast<uint8_t>((x * 9 + y) & 0xff),
                          static_cast<uint8_t>((x + y * 7) & 0xff),
                          static_cast<uint8_t>((x * 3 + y * 5) & 0xff));
    }
  }
  return pixels;
}

} // namespace

int main() {
  @autoreleasepool {
    constexpr int width = 96;
    constexpr int height = 64;
    std::string error;
    auto backend =
        glic::createRealtimeBackend(glic::RealtimeBackendKind::METAL, error);
    if (!backend) {
      std::cerr << "Metal backend creation failed: " << error << '\n';
      return 1;
    }

    glic::CodecConfig config;
    if (!glic::PresetLoader::loadPresetByName(GLIC_TEST_PRESETS_DIR,
                                              "colour_glow", config))
      return 1;
    if (!backend->prepare(
            {.width = width, .height = height, .config = config, .seed = 777},
            error)) {
      std::cerr << "Metal prepare failed: " << error << '\n';
      return 1;
    }

    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    id<MTLCommandQueue> queue = [device newCommandQueue];
    MTLTextureDescriptor *descriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                                     width:width
                                    height:height
                                 mipmapped:NO];
    descriptor.storageMode = MTLStorageModeShared;
    descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> inputTexture = [device newTextureWithDescriptor:descriptor];
    id<MTLTexture> synchronousTexture =
        [device newTextureWithDescriptor:descriptor];
    id<MTLTexture> asynchronousTexture =
        [device newTextureWithDescriptor:descriptor];
    if (queue == nil || inputTexture == nil || synchronousTexture == nil ||
        asynchronousTexture == nil)
      return 1;

    const auto input = makeFixture(width, height);
    const MTLRegion region = MTLRegionMake2D(0, 0, width, height);
    [inputTexture replaceRegion:region
                    mipmapLevel:0
                      withBytes:input.data()
                    bytesPerRow:width * sizeof(glic::Color)];

    if (!backend->processTextures((__bridge void *)inputTexture,
                                  (__bridge void *)synchronousTexture, 9,
                                  error)) {
      std::cerr << "Synchronous texture processing failed: " << error << '\n';
      return 1;
    }

    id<MTLCommandBuffer> commandBuffer = [queue commandBuffer];
    if (!backend->encodeTextures(
            (__bridge void *)commandBuffer, (__bridge void *)inputTexture,
            (__bridge void *)asynchronousTexture, 9, error)) {
      std::cerr << "Asynchronous texture encoding failed: " << error << '\n';
      return 1;
    }
    [commandBuffer commit];
    [commandBuffer waitUntilCompleted];
    if (commandBuffer.status == MTLCommandBufferStatusError) {
      std::cerr << "Asynchronous command buffer failed\n";
      return 1;
    }

    std::vector<glic::Color> synchronous(input.size());
    std::vector<glic::Color> asynchronous(input.size());
    [synchronousTexture getBytes:synchronous.data()
                     bytesPerRow:width * sizeof(glic::Color)
                      fromRegion:region
                     mipmapLevel:0];
    [asynchronousTexture getBytes:asynchronous.data()
                      bytesPerRow:width * sizeof(glic::Color)
                       fromRegion:region
                      mipmapLevel:0];
    if (synchronous != asynchronous) {
      std::cerr
          << "Synchronous and asynchronous Metal texture outputs differ\n";
      return 1;
    }
    if (synchronous == input) {
      std::cerr << "Metal texture interop produced an unchanged frame\n";
      return 1;
    }

    std::cout << "PASS metal texture interop\n";
    return 0;
  }
}
