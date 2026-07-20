#include "original_realtime_metal.hpp"

#include "colorspaces.hpp"

#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstring>
#include <exception>
#include <limits>
#include <mutex>
#include <random>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <vector>

#ifndef GLIC_METALLIB_PATH
#define GLIC_METALLIB_PATH "glic_realtime.metallib"
#endif

namespace glic {
namespace {

using Clock = std::chrono::steady_clock;

double milliseconds(Clock::time_point start, Clock::time_point stop) {
  return std::chrono::duration<double, std::milli>(stop - start).count();
}

bool isPowerOfTwo(int value) { return value > 0 && (value & (value - 1)) == 0; }

int nextPowerOfTwo(int value) {
  int result = 1;
  while (result < value && result <= (1 << 29))
    result <<= 1;
  return result;
}

std::string metalErrorString(NSError *error) {
  if (error == nil)
    return "Unknown Metal error";
  return std::string(error.localizedDescription.UTF8String
                         ?: "Unknown Metal error");
}

NSArray<NSString *> *metalLibraryCandidates() {
  NSMutableArray<NSString *> *candidates = [NSMutableArray array];
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

struct alignas(16) MetalChannelUniform {
  int32_t predictionMethod = 0;
  uint32_t quantizationValue = 0;
  uint32_t clampMethod = 0;
  uint32_t originalWaveletId = 0;
  uint32_t transformType = 0;
  int32_t transformScale = 1;
  float transformCompress = 0.0f;
  float compressionThreshold = 0.0f;
};
static_assert(sizeof(MetalChannelUniform) == 32);

struct alignas(16) MetalPresetUniform {
  uint32_t width = 0;
  uint32_t height = 0;
  uint32_t rootSize = 0;
  uint32_t colorSpace = 0;
  int32_t reference0 = 0;
  int32_t reference1 = 0;
  int32_t reference2 = 0;
  uint32_t reserved = 0;
  std::array<MetalChannelUniform, 3> channels{};
};
static_assert(sizeof(MetalPresetUniform) == 128);

struct alignas(16) MetalSegmentDescriptor {
  uint32_t x = 0;
  uint32_t y = 0;
  uint32_t size = 0;
  uint32_t channel = 0;
};
static_assert(sizeof(MetalSegmentDescriptor) == 16);

class MetalOriginalRealtimeLane final : public OriginalRealtimeMetalLane {
public:
  ~MetalOriginalRealtimeLane() override {
    stopWorkers();
    @autoreleasepool {
      uniformBuffer_ = nil;
      segmentBuffer_ = nil;
      scratchBuffer_ = nil;
      matrixBuffer_ = nil;
      planeBuffer_ = nil;
      pipeline_ = nil;
      library_ = nil;
      queue_ = nil;
      device_ = nil;
    }
  }

  bool initialize(std::string &error) {
    device_ = MTLCreateSystemDefaultDevice();
    if (device_ == nil) {
      error = "No Metal device is available";
      return false;
    }
    queue_ = [device_ newCommandQueue];
    if (queue_ == nil) {
      error = "Failed to create original-style Metal command queue";
      return false;
    }

    NSError *libraryError = nil;
    for (NSString *candidate in metalLibraryCandidates()) {
      if (![NSFileManager.defaultManager fileExistsAtPath:candidate])
        continue;
      library_ = [device_ newLibraryWithURL:[NSURL fileURLWithPath:candidate]
                                      error:&libraryError];
      if (library_ != nil)
        break;
    }
    if (library_ == nil) {
      error = "Failed to load original-style Metal library: " +
              metalErrorString(libraryError);
      return false;
    }

    id<MTLFunction> function =
        [library_ newFunctionWithName:@"glicOriginalSegments"];
    if (function == nil) {
      error = "Metal library does not contain glicOriginalSegments";
      return false;
    }
    NSError *pipelineError = nil;
    pipeline_ = [device_ newComputePipelineStateWithFunction:function
                                                       error:&pipelineError];
    if (pipeline_ == nil) {
      error = "Failed to create glicOriginalSegments pipeline: " +
              metalErrorString(pipelineError);
      return false;
    }
    pipelineThreadLimit_ =
        std::min<NSUInteger>(256, pipeline_.maxTotalThreadsPerThreadgroup);
    // The reduction assumes a power-of-two thread count.
    NSUInteger powerOfTwo = 1;
    while ((powerOfTwo << 1u) <= pipelineThreadLimit_)
      powerOfTwo <<= 1u;
    pipelineThreadLimit_ = powerOfTwo;
    if (pipelineThreadLimit_ < 32) {
      error = "Metal pipeline exposes fewer than 32 threads per threadgroup";
      return false;
    }
    error.clear();
    return true;
  }

  bool prepare(int width, int height, const OriginalPresetConfig &config,
               std::string &error) override {
    @autoreleasepool {
      stopWorkers();
      prepared_ = false;
      error.clear();
      if (width <= 0 || height <= 0) {
        error = "original-style Metal lane requires positive dimensions";
        return false;
      }
      const auto support = evaluateOriginalRealtimeSupport(config);
      if (!support.supported) {
        std::ostringstream message;
        message << "unsupported original-style Metal preset";
        for (std::size_t index = 0; index < support.reasons.size(); ++index)
          message << (index == 0 ? ": " : "; ") << support.reasons[index];
        error = message.str();
        return false;
      }
      for (std::size_t channel = 0; channel < config.channels.size();
           ++channel) {
        const auto &value = config.channels[channel];
        if (!isPowerOfTwo(value.minBlockSize) || value.minBlockSize < 2 ||
            !isPowerOfTwo(value.maxBlockSize) || value.maxBlockSize > 512) {
          error = "original-style Metal blocks must be powers of two in 2..512";
          return false;
        }
      }

      width_ = width;
      height_ = height;
      rootSize_ = nextPowerOfTwo(std::max(width_, height_));
      if (rootSize_ <= 0 || rootSize_ > 8192) {
        error = "original-style Metal root size exceeds the certified limit";
        return false;
      }
      config_ = config;
      pixelCount_ =
          static_cast<std::size_t>(width_) * static_cast<std::size_t>(height_);
      rootPixelCount_ = static_cast<std::size_t>(rootSize_) *
                        static_cast<std::size_t>(rootSize_);
      if (pixelCount_ > std::numeric_limits<std::size_t>::max() / 3u ||
          rootPixelCount_ > std::numeric_limits<std::size_t>::max() / 3u) {
        error = "original-style Metal allocation size overflow";
        return false;
      }

      planeBuffer_ =
          [device_ newBufferWithLength:3u * pixelCount_ * sizeof(int32_t)
                               options:MTLResourceStorageModeShared];
      matrixBuffer_ =
          [device_ newBufferWithLength:3u * rootPixelCount_ * sizeof(float)
                               options:MTLResourceStorageModePrivate];
      scratchBuffer_ =
          [device_ newBufferWithLength:3u * rootPixelCount_ * sizeof(float)
                               options:MTLResourceStorageModePrivate];
      segmentBuffer_ = [device_
          newBufferWithLength:3u * pixelCount_ * sizeof(MetalSegmentDescriptor)
                      options:MTLResourceStorageModeShared];
      uniformBuffer_ =
          [device_ newBufferWithLength:sizeof(MetalPresetUniform)
                               options:MTLResourceStorageModeShared];
      if (planeBuffer_ == nil || matrixBuffer_ == nil ||
          scratchBuffer_ == nil || segmentBuffer_ == nil ||
          uniformBuffer_ == nil) {
        error = "Failed to allocate persistent original-style Metal buffers";
        return false;
      }

      for (auto &segments : segments_) {
        segments.clear();
        segments.reserve(pixelCount_);
      }
      for (auto &map : dependencyLevels_)
        map.assign(pixelCount_, 0u);
      // A left/top dependency chain cannot exceed width + height leaves.
      const std::size_t levelCapacity = static_cast<std::size_t>(width_) +
                                        static_cast<std::size_t>(height_) + 2u;
      levelCounts_.assign(levelCapacity, 0u);
      levelOffsets_.assign(levelCapacity, 0u);
      levelCursors_.assign(levelCapacity, 0u);
      levelMaxBlockSizes_.assign(levelCapacity, 0u);

      const Color convertedReference =
          toColorSpace(makeColor(config_.borderColorR, config_.borderColorG,
                                 config_.borderColorB),
                       config_.colorSpace);
      referenceValues_ = {getR(convertedReference), getG(convertedReference),
                          getB(convertedReference)};
      for (std::size_t channel = 0; channel < segmentationRngs_.size();
           ++channel)
        segmentationRngs_[channel].seed(42u + static_cast<uint32_t>(channel) *
                                                  0x9e3779b9u);

      MetalPresetUniform uniform;
      uniform.width = static_cast<uint32_t>(width_);
      uniform.height = static_cast<uint32_t>(height_);
      uniform.rootSize = static_cast<uint32_t>(rootSize_);
      uniform.colorSpace = static_cast<uint32_t>(config_.colorSpace);
      uniform.reference0 = referenceValues_[0];
      uniform.reference1 = referenceValues_[1];
      uniform.reference2 = referenceValues_[2];
      for (std::size_t channel = 0; channel < config_.channels.size();
           ++channel) {
        const auto &source = config_.channels[channel];
        auto &destination = uniform.channels[channel];
        destination.predictionMethod =
            static_cast<int32_t>(source.predictionMethod);
        destination.quantizationValue =
            static_cast<uint32_t>(source.quantizationValue);
        destination.clampMethod = static_cast<uint32_t>(source.clampMethod);
        destination.originalWaveletId =
            static_cast<uint32_t>(source.originalWaveletId);
        destination.transformType =
            static_cast<uint32_t>(source.originalTransformType);
        destination.transformScale = source.transformScale;
        destination.transformCompress = source.transformCompress;
        destination.compressionThreshold = source.transformCompressionThreshold;
      }
      std::memcpy(uniformBuffer_.contents, &uniform, sizeof(uniform));

      try {
        startWorkers();
      } catch (const std::exception &exception) {
        stopWorkers();
        error = std::string("failed to start original-style Metal workers: ") +
                exception.what();
        return false;
      }
      prepared_ = true;
      return true;
    }
  }

  bool process(std::span<const Color> input, std::span<Color> output,
               uint64_t frameIndex, OriginalRealtimeMetalFrameStats *stats,
               std::string &error) override {
    error.clear();
    if (!prepared_) {
      error = "original-style Metal lane is not prepared";
      return false;
    }
    if (input.size() != pixelCount_ || output.size() != pixelCount_) {
      error = "original-style Metal lane input/output size mismatch";
      return false;
    }

    OriginalRealtimeMetalFrameStats frameStats;
    frameStats.frameIndex = frameIndex;
    const auto totalStart = Clock::now();
    try {
      input_ = input.data();
      output_ = output.data();

      const auto prepareStart = Clock::now();
      runWorkerPhase(WorkerPhase::ConvertInput, 6);
      runWorkerPhase(WorkerPhase::Segment, 3);
      if (!buildDispatchSchedule(frameStats, error))
        return false;
      const auto prepareStop = Clock::now();
      frameStats.cpuPrepareMilliseconds =
          milliseconds(prepareStart, prepareStop);

      const auto gpuStart = Clock::now();
      @autoreleasepool {
        id<MTLCommandBuffer> commandBuffer = [queue_ commandBuffer];
        id<MTLComputeCommandEncoder> encoder =
            [commandBuffer computeCommandEncoder];
        if (commandBuffer == nil || encoder == nil) {
          error = "Failed to create original-style Metal command objects";
          return false;
        }
        [encoder setComputePipelineState:pipeline_];
        [encoder setBuffer:planeBuffer_ offset:0 atIndex:0];
        [encoder setBuffer:matrixBuffer_ offset:0 atIndex:1];
        [encoder setBuffer:scratchBuffer_ offset:0 atIndex:2];
        [encoder setBuffer:segmentBuffer_ offset:0 atIndex:3];
        [encoder setBuffer:uniformBuffer_ offset:0 atIndex:4];
        // Metal validation requires each dynamic threadgroup allocation to be
        // 16-byte aligned even though the kernel consumes one int here.
        [encoder setThreadgroupMemoryLength:16u atIndex:0];

        std::size_t emitted = 0;
        for (std::size_t level = 0; level < frameStats.dispatchLevels;
             ++level) {
          const uint32_t count = levelCounts_[level];
          if (count == 0)
            continue;
          const uint32_t offset = levelOffsets_[level];
          const uint32_t maxBlockSize = levelMaxBlockSizes_[level];
          // Dense adaptive frames benefit from more SIMD groups per leaf;
          // sparse frames retain occupancy with smaller groups. This avoids
          // treating a few large transforms like tens of thousands of small
          // transforms while keeping one preallocated pipeline.
          const bool denseFrontiers = frameStats.totalSegments >= 30000u;
          NSUInteger levelThreads = 0;
          if (denseFrontiers) {
            levelThreads = maxBlockSize <= 4u    ? 32u
                           : maxBlockSize <= 16u ? 128u
                                                 : 256u;
          } else {
            levelThreads = maxBlockSize <= 4u    ? 32u
                           : maxBlockSize <= 16u ? 64u
                           : maxBlockSize <= 64u ? 128u
                                                 : 256u;
          }
          levelThreads = std::min(levelThreads, pipelineThreadLimit_);
          [encoder setThreadgroupMemoryLength:levelThreads * sizeof(float)
                                      atIndex:1];
          [encoder setBytes:&offset length:sizeof(offset) atIndex:5];
          [encoder dispatchThreadgroups:MTLSizeMake(count, 1, 1)
                  threadsPerThreadgroup:MTLSizeMake(levelThreads, 1, 1)];
          ++frameStats.gpuDispatches;
          emitted += count;
          if (emitted < frameStats.totalSegments)
            [encoder memoryBarrierWithScope:MTLBarrierScopeBuffers];
        }
        [encoder endEncoding];
        [commandBuffer commit];
        frameStats.commandBufferSubmissions = 1;
        [commandBuffer waitUntilCompleted];
        frameStats.completionWaits = 1;
        if (commandBuffer.status == MTLCommandBufferStatusError) {
          error = "original-style Metal command failed: " +
                  metalErrorString(commandBuffer.error);
          return false;
        }
        if (commandBuffer.GPUEndTime >= commandBuffer.GPUStartTime)
          frameStats.gpuMilliseconds =
              (commandBuffer.GPUEndTime - commandBuffer.GPUStartTime) * 1000.0;
      }
      const auto gpuStop = Clock::now();
      // Some drivers do not expose GPU timestamps for every command buffer.
      if (frameStats.gpuMilliseconds <= 0.0)
        frameStats.gpuMilliseconds = milliseconds(gpuStart, gpuStop);

      const auto outputStart = Clock::now();
      runWorkerPhase(WorkerPhase::ConvertOutput, 6);
      const auto outputStop = Clock::now();
      frameStats.cpuOutputMilliseconds = milliseconds(outputStart, outputStop);
      frameStats.totalMilliseconds = milliseconds(totalStart, outputStop);
      if (stats)
        *stats = frameStats;
      return true;
    } catch (const std::exception &exception) {
      error = std::string("original-style Metal processing failed: ") +
              exception.what();
    } catch (...) {
      error = "original-style Metal processing failed with an unknown error";
    }
    return false;
  }

  int width() const noexcept override { return width_; }
  int height() const noexcept override { return height_; }
  const OriginalPresetConfig &config() const noexcept override {
    return config_;
  }
  const char *name() const noexcept override { return "original_metal_visual"; }
  const char *executionMode() const noexcept override {
    return "hybrid_cpu_colorspace_segmentation_gpu_reconstruction";
  }
  const char *numericPrecision() const noexcept override {
    return "integer_prediction_precise_fp32_cdf97";
  }
  bool isPixelExact() const noexcept override { return false; }
  bool isHardwareAccelerated() const noexcept override { return true; }

private:
  struct WorkingSegment {
    int x = 0;
    int y = 0;
    int size = 0;
    uint32_t dependencyLevel = 0;
  };

  enum class WorkerPhase { Idle, ConvertInput, Segment, ConvertOutput };

  int *planes() noexcept { return static_cast<int *>(planeBuffer_.contents); }

  int planeValue(int channel, int x, int y) const noexcept {
    if (x < 0 || x >= width_ || y < 0 || y >= height_)
      return referenceValues_[static_cast<std::size_t>(channel)];
    const auto *values = static_cast<const int *>(planeBuffer_.contents);
    return values[static_cast<std::size_t>(channel) * pixelCount_ +
                  static_cast<std::size_t>(y) * width_ +
                  static_cast<std::size_t>(x)];
  }

  float sampledStandardDeviation(int channel, int x, int y, int size) {
    const int limit =
        std::max(static_cast<int>(0.1f * static_cast<float>(size) *
                                  static_cast<float>(size)),
                 4);
    std::uniform_int_distribution<int> position(0, size - 1);
    float average = 0.0f;
    float sum = 0.0f;
    for (int sample = 1; sample <= limit; ++sample) {
      auto &rng = segmentationRngs_[static_cast<std::size_t>(channel)];
      const int value =
          planeValue(channel, x + position(rng), y + position(rng));
      const float oldAverage = average;
      average +=
          (static_cast<float>(value) - average) / static_cast<float>(sample);
      sum += (static_cast<float>(value) - oldAverage) *
             (static_cast<float>(value) - average);
    }
    return std::sqrt(sum / static_cast<float>(limit - 1));
  }

  void skipUnusedDeviationSamples(int channel, int size) {
    // All certified block sizes are powers of two. libc++'s closed uniform
    // distribution therefore consumes one mt19937 result per coordinate.
    // Advancing the engine preserves the later adaptive decisions while
    // avoiding plane reads, online variance arithmetic, and sqrt at nodes
    // whose split/leaf result is already forced by the size bounds.
    const int limit =
        std::max(static_cast<int>(0.1f * static_cast<float>(size) *
                                  static_cast<float>(size)),
                 4);
    segmentationRngs_[static_cast<std::size_t>(channel)].discard(
        static_cast<unsigned long long>(limit) * 2ull);
  }

  void segmentChannelRecursive(int channel, int x, int y, int size, int minSize,
                               int maxSize, float threshold) {
    if (x >= width_ || y >= height_)
      return;
    const bool forcedSplit = size > maxSize;
    const bool forcedLeaf = size <= minSize;
    float deviation = 0.0f;
    if (forcedSplit || forcedLeaf)
      skipUnusedDeviationSamples(channel, size);
    else
      deviation = sampledStandardDeviation(channel, x, y, size);
    if (forcedSplit || (!forcedLeaf && deviation > threshold)) {
      const int half = size / 2;
      segmentChannelRecursive(channel, x, y, half, minSize, maxSize, threshold);
      segmentChannelRecursive(channel, x + half, y, half, minSize, maxSize,
                              threshold);
      segmentChannelRecursive(channel, x, y + half, half, minSize, maxSize,
                              threshold);
      segmentChannelRecursive(channel, x + half, y + half, half, minSize,
                              maxSize, threshold);
    } else {
      segments_[static_cast<std::size_t>(channel)].push_back({x, y, size, 0});
    }
  }

  void segmentChannel(int channel) {
    auto &segments = segments_[static_cast<std::size_t>(channel)];
    auto &levelMap = dependencyLevels_[static_cast<std::size_t>(channel)];
    segments.clear();
    std::fill(levelMap.begin(), levelMap.end(), 0u);
    const auto &channelConfig = config_.channels[channel];
    if (channelConfig.minBlockSize == channelConfig.maxBlockSize) {
      // The sampled deviation cannot affect a fixed-size quadtree. Emitting
      // its regular leaves directly removes RNG/stddev work without changing
      // any reconstructed boundary dependency or output pixel.
      const int size = channelConfig.minBlockSize;
      for (int y = 0; y < height_; y += size) {
        for (int x = 0; x < width_; x += size)
          segments.push_back({x, y, size, 0});
      }
    } else {
      segmentChannelRecursive(
          channel, 0, 0, rootSize_, channelConfig.minBlockSize,
          channelConfig.maxBlockSize, channelConfig.segmentationPrecision);
    }

    // Preserve codec.pde's DFS reconstruction semantics. A leaf can run only
    // after every leaf touching its full top and left boundaries has finished.
    for (auto &segment : segments) {
      const int usedWidth = std::min(segment.size, width_ - segment.x);
      const int usedHeight = std::min(segment.size, height_ - segment.y);
      uint32_t dependency = 0;
      if (segment.x > 0) {
        for (int y = 0; y < usedHeight; ++y)
          dependency = std::max(
              dependency,
              levelMap[static_cast<std::size_t>(segment.y + y) * width_ +
                       static_cast<std::size_t>(segment.x - 1)]);
      }
      if (segment.y > 0) {
        const std::size_t row =
            static_cast<std::size_t>(segment.y - 1) * width_;
        for (int x = 0; x < usedWidth; ++x)
          dependency =
              std::max(dependency,
                       levelMap[row + static_cast<std::size_t>(segment.x + x)]);
      }
      segment.dependencyLevel = dependency + 1u;
      for (int y = 0; y < usedHeight; ++y) {
        const std::size_t row =
            static_cast<std::size_t>(segment.y + y) * width_;
        std::fill_n(levelMap.begin() + row + segment.x, usedWidth,
                    segment.dependencyLevel);
      }
    }
  }

  void convertInputRange(int part, int partCount) {
    const std::size_t begin = pixelCount_ * static_cast<std::size_t>(part) /
                              static_cast<std::size_t>(partCount);
    const std::size_t end = pixelCount_ * static_cast<std::size_t>(part + 1) /
                            static_cast<std::size_t>(partCount);
    int *values = planes();
    for (std::size_t index = begin; index < end; ++index) {
      const Color converted = toColorSpace(input_[index], config_.colorSpace);
      values[index] = getR(converted);
      values[pixelCount_ + index] = getG(converted);
      values[pixelCount_ * 2u + index] = getB(converted);
    }
  }

  void convertOutputRange(int part, int partCount) {
    const std::size_t begin = pixelCount_ * static_cast<std::size_t>(part) /
                              static_cast<std::size_t>(partCount);
    const std::size_t end = pixelCount_ * static_cast<std::size_t>(part + 1) /
                            static_cast<std::size_t>(partCount);
    const int *values = planes();
    for (std::size_t index = begin; index < end; ++index) {
      output_[index] = fromColorSpace(
          makeColor(static_cast<uint8_t>(std::clamp(values[index], 0, 255)),
                    static_cast<uint8_t>(
                        std::clamp(values[pixelCount_ + index], 0, 255)),
                    static_cast<uint8_t>(
                        std::clamp(values[pixelCount_ * 2u + index], 0, 255)),
                    getA(input_[index])),
          config_.colorSpace);
    }
  }

  void executeWorkerPhase(WorkerPhase phase, int part, int partCount) {
    switch (phase) {
    case WorkerPhase::ConvertInput:
      convertInputRange(part, partCount);
      break;
    case WorkerPhase::Segment:
      segmentChannel(part);
      break;
    case WorkerPhase::ConvertOutput:
      convertOutputRange(part, partCount);
      break;
    case WorkerPhase::Idle:
      break;
    }
  }

  void startWorkers() {
    {
      std::lock_guard lock(workerMutex_);
      workerShutdown_ = false;
      pendingWorkers_ = 0;
      workerGeneration_ = 0;
      workerErrors_ = {};
      workerPhase_ = WorkerPhase::Idle;
    }
    for (int worker = 0; worker < static_cast<int>(workers_.size()); ++worker)
      workers_[static_cast<std::size_t>(worker)] =
          std::jthread([this, worker] { workerLoop(worker); });
  }

  void stopWorkers() noexcept {
    {
      std::lock_guard lock(workerMutex_);
      workerShutdown_ = true;
      ++workerGeneration_;
    }
    workerCondition_.notify_all();
    for (auto &worker : workers_) {
      if (worker.joinable())
        worker.join();
    }
    std::lock_guard lock(workerMutex_);
    pendingWorkers_ = 0;
    workerErrors_ = {};
  }

  void workerLoop(int worker) {
    uint64_t observedGeneration = 0;
    while (true) {
      WorkerPhase phase = WorkerPhase::Idle;
      int partCount = 1;
      {
        std::unique_lock lock(workerMutex_);
        workerCondition_.wait(lock, [&] {
          return workerShutdown_ || workerGeneration_ != observedGeneration;
        });
        if (workerShutdown_)
          return;
        observedGeneration = workerGeneration_;
        phase = workerPhase_;
        partCount = workerPartCount_;
      }
      std::exception_ptr failure;
      try {
        if (worker < partCount - 1)
          executeWorkerPhase(phase, worker, partCount);
      } catch (...) {
        failure = std::current_exception();
      }
      {
        std::lock_guard lock(workerMutex_);
        workerErrors_[static_cast<std::size_t>(worker)] = failure;
        --pendingWorkers_;
      }
      workerCompletion_.notify_one();
    }
  }

  void runWorkerPhase(WorkerPhase phase, int partCount) {
    if (partCount < 1 || partCount > static_cast<int>(workers_.size()) + 1)
      throw std::invalid_argument("invalid original-style worker part count");
    {
      std::lock_guard lock(workerMutex_);
      workerErrors_ = {};
      workerPhase_ = phase;
      workerPartCount_ = partCount;
      pendingWorkers_ = static_cast<int>(workers_.size());
      ++workerGeneration_;
    }
    workerCondition_.notify_all();
    std::exception_ptr callerError;
    try {
      executeWorkerPhase(phase, partCount - 1, partCount);
    } catch (...) {
      callerError = std::current_exception();
    }
    {
      std::unique_lock lock(workerMutex_);
      workerCompletion_.wait(lock, [&] { return pendingWorkers_ == 0; });
      for (const auto &workerError : workerErrors_) {
        if (workerError)
          std::rethrow_exception(workerError);
      }
    }
    if (callerError)
      std::rethrow_exception(callerError);
  }

  bool buildDispatchSchedule(OriginalRealtimeMetalFrameStats &stats,
                             std::string &error) {
    std::fill(levelCounts_.begin(), levelCounts_.end(), 0u);
    std::fill(levelMaxBlockSizes_.begin(), levelMaxBlockSizes_.end(), 0u);
    std::size_t total = 0;
    std::size_t maxLevel = 0;
    for (std::size_t channel = 0; channel < segments_.size(); ++channel) {
      stats.segmentCounts[channel] = segments_[channel].size();
      total += segments_[channel].size();
      for (const auto &segment : segments_[channel]) {
        if (segment.dependencyLevel == 0 ||
            segment.dependencyLevel > levelCounts_.size()) {
          error = "original-style Metal dependency frontier overflow";
          return false;
        }
        ++levelCounts_[segment.dependencyLevel - 1u];
        levelMaxBlockSizes_[segment.dependencyLevel - 1u] =
            std::max(levelMaxBlockSizes_[segment.dependencyLevel - 1u],
                     static_cast<uint32_t>(segment.size));
        maxLevel = std::max<std::size_t>(maxLevel, segment.dependencyLevel);
      }
    }
    if (total == 0 || total > 3u * pixelCount_) {
      error = "original-style Metal produced an invalid segment count";
      return false;
    }
    uint32_t offset = 0;
    for (std::size_t level = 0; level < maxLevel; ++level) {
      levelOffsets_[level] = offset;
      levelCursors_[level] = offset;
      offset += levelCounts_[level];
    }
    auto *descriptors =
        static_cast<MetalSegmentDescriptor *>(segmentBuffer_.contents);
    for (std::size_t channel = 0; channel < segments_.size(); ++channel) {
      for (const auto &segment : segments_[channel]) {
        const std::size_t level = segment.dependencyLevel - 1u;
        const uint32_t destination = levelCursors_[level]++;
        descriptors[destination] = {
            static_cast<uint32_t>(segment.x),
            static_cast<uint32_t>(segment.y),
            static_cast<uint32_t>(segment.size),
            static_cast<uint32_t>(channel),
        };
      }
    }
    stats.totalSegments = total;
    stats.dispatchLevels = maxLevel;
    return true;
  }

  id<MTLDevice> device_ = nil;
  id<MTLCommandQueue> queue_ = nil;
  id<MTLLibrary> library_ = nil;
  id<MTLComputePipelineState> pipeline_ = nil;
  id<MTLBuffer> planeBuffer_ = nil;
  id<MTLBuffer> matrixBuffer_ = nil;
  id<MTLBuffer> scratchBuffer_ = nil;
  id<MTLBuffer> segmentBuffer_ = nil;
  id<MTLBuffer> uniformBuffer_ = nil;
  NSUInteger pipelineThreadLimit_ = 0;

  int width_ = 0;
  int height_ = 0;
  int rootSize_ = 0;
  std::size_t pixelCount_ = 0;
  std::size_t rootPixelCount_ = 0;
  OriginalPresetConfig config_{};
  std::array<int, 3> referenceValues_{};
  std::array<std::vector<WorkingSegment>, 3> segments_{};
  std::array<std::vector<uint32_t>, 3> dependencyLevels_{};
  std::array<std::mt19937, 3> segmentationRngs_{};
  std::vector<uint32_t> levelCounts_;
  std::vector<uint32_t> levelOffsets_;
  std::vector<uint32_t> levelCursors_;
  std::vector<uint32_t> levelMaxBlockSizes_;

  const Color *input_ = nullptr;
  Color *output_ = nullptr;
  std::array<std::jthread, 5> workers_{};
  std::mutex workerMutex_;
  std::condition_variable workerCondition_;
  std::condition_variable workerCompletion_;
  std::array<std::exception_ptr, 5> workerErrors_{};
  WorkerPhase workerPhase_ = WorkerPhase::Idle;
  int workerPartCount_ = 1;
  uint64_t workerGeneration_ = 0;
  int pendingWorkers_ = 0;
  bool workerShutdown_ = false;
  bool prepared_ = false;
};

} // namespace

std::unique_ptr<OriginalRealtimeMetalLane>
createOriginalRealtimeMetalLane(std::string &error) {
  @autoreleasepool {
    auto lane = std::make_unique<MetalOriginalRealtimeLane>();
    if (!lane->initialize(error))
      return nullptr;
    error.clear();
    return lane;
  }
}

} // namespace glic
