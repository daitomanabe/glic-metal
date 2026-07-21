#include "realtime_metal.hpp"

#include "realtime_internal.hpp"

#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

#include <algorithm>
#include <cstring>
#include <memory>
#include <span>
#include <string>

#ifndef GLIC_METALLIB_PATH
#define GLIC_METALLIB_PATH "glic_realtime.metallib"
#endif

namespace glic {
namespace {

struct alignas(16) FrameUniform {
  uint32_t frameIndex = 0;
  uint32_t reserved0 = 0;
  uint32_t reserved1 = 0;
  uint32_t reserved2 = 0;
};

std::string errorString(NSError *error) {
  if (error == nil)
    return "Unknown Metal error";
  return std::string(error.localizedDescription.UTF8String
                         ?: "Unknown Metal error");
}

NSArray<NSString *> *
metalLibraryCandidates(const std::string &explicitLibraryPath) {
  NSMutableArray<NSString *> *candidates = [NSMutableArray array];
  if (!explicitLibraryPath.empty())
    [candidates addObject:
                    [NSString stringWithUTF8String:explicitLibraryPath.c_str()]];
  NSString *environmentPath =
      NSProcessInfo.processInfo.environment[@"GLIC_METALLIB_PATH"];
  if (environmentPath.length > 0)
    [candidates addObject:environmentPath];

  NSString *bundlePath = [NSBundle.mainBundle pathForResource:@"glic_realtime"
                                                       ofType:@"metallib"];
  if (bundlePath.length > 0)
    [candidates addObject:bundlePath];

  NSString *executablePath =
      NSProcessInfo.processInfo.arguments.firstObject.stringByStandardizingPath;
  NSString *executableDirectory =
      executablePath.stringByDeletingLastPathComponent;
  if (executableDirectory.length > 0) {
    [candidates addObject:[executableDirectory stringByAppendingPathComponent:
                                                   @"glic_realtime.metallib"]];
    [candidates addObject:[[executableDirectory
                              stringByAppendingPathComponent:
                                  @"../lib/glic/glic_realtime.metallib"]
                              stringByStandardizingPath]];
  }
  [candidates addObject:[NSString stringWithUTF8String:GLIC_METALLIB_PATH]];
  return candidates;
}

class MetalRealtimeBackend final : public RealtimeBackend {
public:
  explicit MetalRealtimeBackend(RealtimeBackendCreateOptions createOptions)
      : createOptions_(std::move(createOptions)) {}

  bool initialize(std::string &error) {
    device_ = createOptions_.metalDevice != nullptr
                  ? (__bridge id<MTLDevice>)createOptions_.metalDevice
                  : MTLCreateSystemDefaultDevice();
    if (device_ == nil) {
      error = "No Metal device is available";
      return false;
    }

    queue_ = [device_ newCommandQueue];
    if (queue_ == nil) {
      error = "Failed to create Metal command queue";
      return false;
    }

    NSError *libraryError = nil;
    NSString *loadedPath = nil;
    for (NSString *candidate in
         metalLibraryCandidates(createOptions_.metalLibraryPath)) {
      if (![NSFileManager.defaultManager fileExistsAtPath:candidate])
        continue;
      library_ = [device_ newLibraryWithURL:[NSURL fileURLWithPath:candidate]
                                      error:&libraryError];
      if (library_ != nil) {
        loadedPath = candidate;
        break;
      }
    }
    if (library_ == nil) {
      error =
          "Failed to load glic_realtime.metallib: " + errorString(libraryError);
      return false;
    }
    libraryPath_ = loadedPath;

    id<MTLFunction> function = [library_ newFunctionWithName:@"glicRealtime"];
    if (function == nil) {
      error = "Metal library does not contain glicRealtime";
      return false;
    }

    NSError *pipelineError = nil;
    pipeline_ = [device_ newComputePipelineStateWithFunction:function
                                                       error:&pipelineError];
    if (pipeline_ == nil) {
      error = "Failed to create glicRealtime pipeline: " +
              errorString(pipelineError);
      return false;
    }

    error.clear();
    return true;
  }

  bool prepare(const RealtimePrepareOptions &options,
               std::string &error) override {
    if (options.width <= 0 || options.height <= 0) {
      error = "Realtime dimensions must be positive";
      return false;
    }

    options_ = options;
    presetUniform_ = realtime::makeMetalPresetUniform(options);

    if (presetBuffer_ == nil) {
      presetBuffer_ =
          [device_ newBufferWithLength:sizeof(presetUniform_)
                               options:MTLResourceStorageModeShared];
      for (auto &buffer : frameBuffers_) {
        buffer = [device_ newBufferWithLength:sizeof(FrameUniform)
                                      options:MTLResourceStorageModeShared];
      }
    }
    if (presetBuffer_ == nil || frameBuffers_[0] == nil ||
        frameBuffers_[1] == nil || frameBuffers_[2] == nil) {
      error = "Failed to allocate persistent Metal uniform buffers";
      return false;
    }
    std::memcpy(presetBuffer_.contents, &presetUniform_,
                sizeof(presetUniform_));

    if (inputTexture_ == nil ||
        inputTexture_.width != static_cast<NSUInteger>(options.width) ||
        inputTexture_.height != static_cast<NSUInteger>(options.height)) {
      MTLTextureDescriptor *descriptor = [MTLTextureDescriptor
          texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                                       width:static_cast<NSUInteger>(
                                                 options.width)
                                      height:static_cast<NSUInteger>(
                                                 options.height)
                                   mipmapped:NO];
      descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
      descriptor.storageMode = MTLStorageModeShared;
      inputTexture_ = [device_ newTextureWithDescriptor:descriptor];
      outputTexture_ = [device_ newTextureWithDescriptor:descriptor];
    }

    if (inputTexture_ == nil || outputTexture_ == nil) {
      error = "Failed to allocate persistent Metal frame textures";
      return false;
    }

    prepared_ = true;
    error.clear();
    return true;
  }

  bool process(std::span<const Color> input, std::span<Color> output,
               uint64_t frameIndex, std::string &error) override {
    if (!prepared_) {
      error = "Metal realtime backend is not prepared";
      return false;
    }

    const size_t pixelCount = static_cast<size_t>(options_.width) *
                              static_cast<size_t>(options_.height);
    if (input.size() != pixelCount || output.size() != pixelCount) {
      error =
          "Realtime input/output span size does not match prepared dimensions";
      return false;
    }

    @autoreleasepool {
      MTLRegion region =
          MTLRegionMake2D(0, 0, static_cast<NSUInteger>(options_.width),
                          static_cast<NSUInteger>(options_.height));
      [inputTexture_ replaceRegion:region
                       mipmapLevel:0
                         withBytes:input.data()
                       bytesPerRow:static_cast<NSUInteger>(options_.width *
                                                           sizeof(Color))];

      if (!processTextureObjects(inputTexture_, outputTexture_, frameIndex,
                                 error))
        return false;

      [outputTexture_
             getBytes:output.data()
          bytesPerRow:static_cast<NSUInteger>(options_.width * sizeof(Color))
           fromRegion:region
          mipmapLevel:0];
    }
    return true;
  }

  bool processTextures(void *inputTexture, void *outputTexture,
                       uint64_t frameIndex, std::string &error) override {
    if (!prepared_) {
      error = "Metal realtime backend is not prepared";
      return false;
    }
    if (inputTexture == nullptr || outputTexture == nullptr) {
      error = "Metal input/output texture is null";
      return false;
    }

    id<MTLTexture> input = (__bridge id<MTLTexture>)inputTexture;
    id<MTLTexture> output = (__bridge id<MTLTexture>)outputTexture;
    return processTextureObjects(input, output, frameIndex, error);
  }

  bool encodeTextures(void *commandBuffer, void *inputTexture,
                      void *outputTexture, uint64_t frameIndex,
                      std::string &error) override {
    if (!prepared_) {
      error = "Metal realtime backend is not prepared";
      return false;
    }
    if (commandBuffer == nullptr || inputTexture == nullptr ||
        outputTexture == nullptr) {
      error = "Metal command buffer and textures must be non-null";
      return false;
    }

    id<MTLCommandBuffer> command = (__bridge id<MTLCommandBuffer>)commandBuffer;
    id<MTLTexture> input = (__bridge id<MTLTexture>)inputTexture;
    id<MTLTexture> output = (__bridge id<MTLTexture>)outputTexture;
    return encodeTextureObjects(command, input, output, frameIndex, error);
  }

  const char *name() const noexcept override { return "metal"; }
  bool isHardwareAccelerated() const noexcept override { return true; }
  RealtimeFrameStats lastFrameStats() const noexcept override {
    return lastStats_;
  }

private:
  bool processTextureObjects(id<MTLTexture> input, id<MTLTexture> output,
                             uint64_t frameIndex, std::string &error) {
    if (input.device != device_ || output.device != device_) {
      error = "Metal textures must belong to the configured Metal device";
      return false;
    }
    if (input.pixelFormat != MTLPixelFormatBGRA8Unorm ||
        output.pixelFormat != MTLPixelFormatBGRA8Unorm) {
      error = "Metal texture interop requires BGRA8Unorm textures";
      return false;
    }
    if (input.width != static_cast<NSUInteger>(options_.width) ||
        input.height != static_cast<NSUInteger>(options_.height) ||
        output.width != static_cast<NSUInteger>(options_.width) ||
        output.height != static_cast<NSUInteger>(options_.height)) {
      error = "Metal texture dimensions do not match prepared dimensions";
      return false;
    }

    @autoreleasepool {
      id<MTLCommandBuffer> commandBuffer = [queue_ commandBuffer];
      if (commandBuffer == nil ||
          !encodeTextureObjects(commandBuffer, input, output, frameIndex,
                                error))
        return false;
      [commandBuffer commit];
      [commandBuffer waitUntilCompleted];

      if (commandBuffer.status == MTLCommandBufferStatusError) {
        error = "Metal command failed: " + errorString(commandBuffer.error);
        return false;
      }

      lastStats_.frameIndex = frameIndex;
      if (commandBuffer.GPUEndTime >= commandBuffer.GPUStartTime) {
        lastStats_.gpuMilliseconds =
            (commandBuffer.GPUEndTime - commandBuffer.GPUStartTime) * 1000.0;
      } else {
        lastStats_.gpuMilliseconds = 0.0;
      }
    }

    error.clear();
    return true;
  }

  bool encodeTextureObjects(id<MTLCommandBuffer> commandBuffer,
                            id<MTLTexture> input, id<MTLTexture> output,
                            uint64_t frameIndex, std::string &error) {
    if (commandBuffer.device != device_ || input.device != device_ ||
        output.device != device_) {
      error = "Metal command buffer and textures must use the configured "
              "Metal device";
      return false;
    }
    if (input.pixelFormat != MTLPixelFormatBGRA8Unorm ||
        output.pixelFormat != MTLPixelFormatBGRA8Unorm) {
      error = "Metal texture interop requires BGRA8Unorm textures";
      return false;
    }
    if (input.width != static_cast<NSUInteger>(options_.width) ||
        input.height != static_cast<NSUInteger>(options_.height) ||
        output.width != static_cast<NSUInteger>(options_.width) ||
        output.height != static_cast<NSUInteger>(options_.height)) {
      error = "Metal texture dimensions do not match prepared dimensions";
      return false;
    }

    const size_t ringIndex =
        static_cast<size_t>(frameIndex % frameBuffers_.size());
    FrameUniform frameUniform{.frameIndex = static_cast<uint32_t>(frameIndex)};
    std::memcpy(frameBuffers_[ringIndex].contents, &frameUniform,
                sizeof(frameUniform));

    id<MTLComputeCommandEncoder> encoder =
        [commandBuffer computeCommandEncoder];
    if (encoder == nil) {
      error = "Failed to create Metal compute encoder";
      return false;
    }
    [encoder setComputePipelineState:pipeline_];
    [encoder setTexture:input atIndex:0];
    [encoder setTexture:output atIndex:1];
    [encoder setBuffer:presetBuffer_ offset:0 atIndex:0];
    [encoder setBuffer:frameBuffers_[ringIndex] offset:0 atIndex:1];

    const NSUInteger width =
        std::min<NSUInteger>(16, pipeline_.threadExecutionWidth);
    const NSUInteger height = std::max<NSUInteger>(
        1, std::min<NSUInteger>(16, pipeline_.maxTotalThreadsPerThreadgroup /
                                        width));
    [encoder dispatchThreads:MTLSizeMake(
                                 static_cast<NSUInteger>(options_.width),
                                 static_cast<NSUInteger>(options_.height), 1)
        threadsPerThreadgroup:MTLSizeMake(width, height, 1)];
    [encoder endEncoding];
    error.clear();
    return true;
  }

  id<MTLDevice> device_ = nil;
  id<MTLCommandQueue> queue_ = nil;
  id<MTLLibrary> library_ = nil;
  NSString *libraryPath_ = nil;
  id<MTLComputePipelineState> pipeline_ = nil;
  id<MTLBuffer> presetBuffer_ = nil;
  std::array<id<MTLBuffer>, 3> frameBuffers_{nil, nil, nil};
  id<MTLTexture> inputTexture_ = nil;
  id<MTLTexture> outputTexture_ = nil;

  RealtimePrepareOptions options_{};
  RealtimeBackendCreateOptions createOptions_{};
  realtime::MetalPresetUniform presetUniform_{};
  RealtimeFrameStats lastStats_{};
  bool prepared_ = false;
};

} // namespace

std::unique_ptr<RealtimeBackend>
createMetalRealtimeBackend(std::string &error) {
  return createMetalRealtimeBackend(RealtimeBackendCreateOptions{}, error);
}

std::unique_ptr<RealtimeBackend>
createMetalRealtimeBackend(const RealtimeBackendCreateOptions &options,
                           std::string &error) {
  auto backend = std::make_unique<MetalRealtimeBackend>(options);
  if (!backend->initialize(error))
    return nullptr;
  return backend;
}

} // namespace glic
