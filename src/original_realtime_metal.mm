#include "original_realtime_metal.hpp"

#include "colorspaces.hpp"
#include "processing_math.hpp"
#include "processing_random.hpp"
#include "segmentation_trace.hpp"

#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <limits>
#include <mutex>
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
constexpr uint32_t kThreadgroupCdfMaxBlockSize = 32u;

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
      threadgroupCdfPipeline_ = nil;
      pipeline_ = nil;
      library_ = nil;
      queue_ = nil;
      device_ = nil;
    }
  }

  bool initialize(std::string &error) {
    const char *disableThreadgroupCdf =
        std::getenv("GLIC_DISABLE_THREADGROUP_CDF");
    threadgroupCdfEnabled_ =
        disableThreadgroupCdf == nullptr ||
        std::string_view(disableThreadgroupCdf) == "0";
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
    id<MTLFunction> threadgroupCdfFunction =
        [library_ newFunctionWithName:@"glicOriginalSegmentsThreadgroupCdf"];
    if (threadgroupCdfFunction == nil) {
      error =
          "Metal library does not contain glicOriginalSegmentsThreadgroupCdf";
      return false;
    }
    pipelineError = nil;
    threadgroupCdfPipeline_ =
        [device_ newComputePipelineStateWithFunction:threadgroupCdfFunction
                                               error:&pipelineError];
    if (threadgroupCdfPipeline_ == nil) {
      error = "Failed to create glicOriginalSegmentsThreadgroupCdf pipeline: " +
              metalErrorString(pipelineError);
      return false;
    }
    pipelineThreadLimit_ =
        std::min({NSUInteger(256), pipeline_.maxTotalThreadsPerThreadgroup,
                  threadgroupCdfPipeline_.maxTotalThreadsPerThreadgroup});
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
      staticSchedule_ = false;
      cachedScheduleStats_ = {};
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

      for (std::size_t channel = 0; channel < segments_.size(); ++channel) {
        const int unit = config_.channels[channel].minBlockSize;
        const std::size_t gridWidth =
            (static_cast<std::size_t>(width_) + unit - 1u) /
            static_cast<std::size_t>(unit);
        const std::size_t gridHeight =
            (static_cast<std::size_t>(height_) + unit - 1u) /
            static_cast<std::size_t>(unit);
        dependencyGridWidths_[channel] = gridWidth;
        dependencyGrids_[channel].assign(gridWidth * gridHeight, 0u);
        segments_[channel].clear();
        segments_[channel].reserve(gridWidth * gridHeight);
      }
      // A left/top dependency chain cannot exceed width + height leaves.
      const std::size_t levelCapacity = static_cast<std::size_t>(width_) +
                                        static_cast<std::size_t>(height_) + 2u;
      levelCounts_.assign(levelCapacity, 0u);
      levelOffsets_.assign(levelCapacity, 0u);
      levelThreadgroupCounts_.assign(levelCapacity, 0u);
      levelThreadgroupCursors_.assign(levelCapacity, 0u);
      levelGlobalCursors_.assign(levelCapacity, 0u);
      levelThreadgroupMaxBlockSizes_.assign(levelCapacity, 0u);
      levelGlobalMaxBlockSizes_.assign(levelCapacity, 0u);

      const Color convertedReference =
          toColorSpace(makeColor(config_.borderColorR, config_.borderColorG,
                                 config_.borderColorB),
                       config_.colorSpace);
      referenceValues_ = {getR(convertedReference), getG(convertedReference),
                          getB(convertedReference)};
      segmentationRng_.setSeed(42);

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

      staticSchedule_ = std::all_of(
          config_.channels.begin(), config_.channels.end(),
          [](const OriginalPresetChannel &channel) {
            return channel.minBlockSize == channel.maxBlockSize;
          });
      if (staticSchedule_) {
        // Fixed quadtree geometry is input-independent. Build its exact
        // top/left frontier and descriptor order once, then reuse the same
        // persistent mapped buffer for every frame.
        for (int channel = 0; channel < 3; ++channel)
          segmentChannel(channel, false);
        if (!buildDispatchSchedule(cachedScheduleStats_, error))
          return false;
      }

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

    ProcessingRandomCheckpoint rngCheckpoint(segmentationRng_);
    OriginalRealtimeMetalFrameStats frameStats;
    frameStats.frameIndex = frameIndex;
    const auto totalStart = Clock::now();
    try {
      input_ = input.data();
      output_ = output.data();

      const auto prepareStart = Clock::now();
      runWorkerPhase(WorkerPhase::ConvertInput, 6);
      earlyTerminatedNodes_ = 0;
      earlySkippedSamples_ = 0;
      if (staticSchedule_) {
        // Cached geometry removes all per-frame tree work, but upstream still
        // calls calcStdDev at every fixed-tree node. Advance the exact Java RNG
        // state by those otherwise-unused samples for every frame.
        for (const auto &channel : config_.channels)
          segmentationRng_.discardNextFloats(
              fixedSegmentationRandomDraws(channel.minBlockSize));
        frameStats.segmentCounts = cachedScheduleStats_.segmentCounts;
        frameStats.totalSegments = cachedScheduleStats_.totalSegments;
        frameStats.dispatchLevels = cachedScheduleStats_.dispatchLevels;
        frameStats.segmentOrderFnv1a64 =
            cachedScheduleStats_.segmentOrderFnv1a64;
        frameStats.staticScheduleReused = true;
      } else {
        // Preserve the one evolving Processing RNG stream in original
        // channel/DFS order. Once those exact leaf lists exist, dependency
        // levels are independent per channel and can be built in parallel.
        for (int channel = 0; channel < 3; ++channel)
          segmentChannelGeometry(channel);
        runWorkerPhase(WorkerPhase::BuildDependencyLevels, 3);
        if (!buildDispatchSchedule(frameStats, error))
          return false;
      }
      frameStats.segmentationRngState = segmentationRng_.state();
      frameStats.earlyTerminatedNodes = earlyTerminatedNodes_;
      frameStats.earlySkippedSamples = earlySkippedSamples_;
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
        [encoder setBuffer:planeBuffer_ offset:0 atIndex:0];
        [encoder setBuffer:matrixBuffer_ offset:0 atIndex:1];
        [encoder setBuffer:scratchBuffer_ offset:0 atIndex:2];
        [encoder setBuffer:segmentBuffer_ offset:0 atIndex:3];
        [encoder setBuffer:uniformBuffer_ offset:0 atIndex:4];
        // Metal validation requires each dynamic threadgroup allocation to be
        // 16-byte aligned even though the kernel consumes one int here.
        [encoder setThreadgroupMemoryLength:16u atIndex:0];

        std::size_t emitted = 0;
        id<MTLComputePipelineState> activePipeline = nil;
        id<MTLResource> dependencyResources[] = {planeBuffer_};
        for (std::size_t level = 0; level < frameStats.dispatchLevels;
             ++level) {
          const uint32_t count = levelCounts_[level];
          if (count == 0)
            continue;
          const uint32_t threadgroupCount = levelThreadgroupCounts_[level];
          const uint32_t globalCount = count - threadgroupCount;
          // Leaves in one dependency frontier are mutually independent. Fixed
          // mixed-channel schedules can therefore keep their <=32 leaves in
          // threadgroup memory while large leaves use global scratch. Dense
          // adaptive frames containing large leaves are routed wholly through
          // the global kernel when the schedule is built: on Apple silicon
          // that avoids hundreds of pipeline switches/extra dispatches.
          // Both buckets (when present) complete before the one barrier below.
          for (int bucket = 0; bucket < 2; ++bucket) {
            const bool useThreadgroupPipeline = bucket == 0;
            const uint32_t bucketCount =
                useThreadgroupPipeline ? threadgroupCount : globalCount;
            if (bucketCount == 0)
              continue;
            const uint32_t offset =
                useThreadgroupPipeline
                    ? levelOffsets_[level]
                    : levelOffsets_[level] + threadgroupCount;
            const uint32_t maxBlockSize =
                useThreadgroupPipeline
                    ? levelThreadgroupMaxBlockSizes_[level]
                    : levelGlobalMaxBlockSizes_[level];
            // Dense adaptive frames benefit from more SIMD groups per leaf;
            // sparse frames retain occupancy with smaller groups.
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
            const NSUInteger matrixFloats =
                static_cast<NSUInteger>(maxBlockSize) * maxBlockSize;
            const NSUInteger threadgroupWorkingBytes =
                (matrixFloats + std::max(matrixFloats, NSUInteger(32))) *
                    sizeof(float) +
                2u * static_cast<NSUInteger>(maxBlockSize) * sizeof(int32_t);
            id<MTLComputePipelineState> desiredPipeline =
                useThreadgroupPipeline ? threadgroupCdfPipeline_ : pipeline_;
            if (activePipeline != desiredPipeline) {
              [encoder setComputePipelineState:desiredPipeline];
              activePipeline = desiredPipeline;
            }
            [encoder setThreadgroupMemoryLength:
                         useThreadgroupPipeline
                             ? threadgroupWorkingBytes
                             : NSUInteger(128)
                                        atIndex:1];
            [encoder setBytes:&offset length:sizeof(offset) atIndex:5];
            [encoder dispatchThreadgroups:MTLSizeMake(bucketCount, 1, 1)
                    threadsPerThreadgroup:MTLSizeMake(levelThreads, 1, 1)];
            ++frameStats.gpuDispatches;
            if (useThreadgroupPipeline) {
              ++frameStats.threadgroupPipelineDispatches;
              frameStats.threadgroupPipelineSegments += bucketCount;
            } else {
              ++frameStats.globalPipelineDispatches;
              frameStats.globalPipelineSegments += bucketCount;
            }
            emitted += bucketCount;
          }
          if (emitted < frameStats.totalSegments) {
            // Only reconstructed planes cross dependency frontiers. Transform
            // workspaces are disjoint per quadtree leaf; segment/uniform data
            // is read-only. Avoid fencing every bound buffer hundreds of times.
            [encoder memoryBarrierWithResources:dependencyResources count:1];
            ++frameStats.bufferBarriers;
          }
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

      const bool dispatchAccounting =
          frameStats.gpuDispatches ==
          frameStats.threadgroupPipelineDispatches +
              frameStats.globalPipelineDispatches;
      const bool segmentAccounting =
          frameStats.totalSegments ==
          frameStats.threadgroupPipelineSegments +
              frameStats.globalPipelineSegments;
      const bool frontierAccounting =
          frameStats.dispatchLevels > 0 &&
          frameStats.bufferBarriers + 1u == frameStats.dispatchLevels;
      if (!dispatchAccounting || !segmentAccounting || !frontierAccounting) {
        error = "original-style Metal pipeline accounting invariant failed";
        return false;
      }
      frameStats.pipelineAccountingPassed = true;

      const auto outputStart = Clock::now();
      runWorkerPhase(WorkerPhase::ConvertOutput, 6);
      const auto outputStop = Clock::now();
      frameStats.cpuOutputMilliseconds = milliseconds(outputStart, outputStop);
      frameStats.totalMilliseconds = milliseconds(totalStart, outputStop);
      if (stats)
        *stats = frameStats;
      rngCheckpoint.commit();
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
    return "integer_prediction_float_float_cdf97_accumulation_fp32_storage";
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

  enum class WorkerPhase {
    Idle,
    ConvertInput,
    BuildDependencyLevels,
    ConvertOutput
  };

  int *planes() noexcept { return static_cast<int *>(planeBuffer_.contents); }

  int planeValue(int channel, int x, int y) const noexcept {
    if (x < 0 || x >= width_ || y < 0 || y >= height_)
      return referenceValues_[static_cast<std::size_t>(channel)];
    const auto *values = static_cast<const int *>(planeBuffer_.contents);
    return values[static_cast<std::size_t>(channel) * pixelCount_ +
                  static_cast<std::size_t>(y) * width_ +
                  static_cast<std::size_t>(x)];
  }

  bool sampledStandardDeviationExceeds(int channel, int x, int y, int size,
                                       float threshold) {
#if defined(__clang__)
#pragma clang fp contract(off)
#endif
    const int limit =
        std::max(static_cast<int>(0.1f * static_cast<float>(size) *
                                  static_cast<float>(size)),
                 4);
    float average = 0.0f;
    float sum = 0.0f;
    int nextDecisionSample = 1;
    const unsigned positionMagnitude =
        std::countr_zero(static_cast<unsigned>(size));
    for (int sample = 1; sample <= limit; ++sample) {
      const int value = planeValue(
          channel,
          x + segmentationRng_.nextPowerOfTwoPosition(positionMagnitude),
          y + segmentationRng_.nextPowerOfTwoPosition(positionMagnitude));
      const float oldAverage = average;
      average +=
          (static_cast<float>(value) - average) / static_cast<float>(sample);
      sum += (static_cast<float>(value) - oldAverage) *
             (static_cast<float>(value) - average);
      if (sample == nextDecisionSample || sample == limit) {
        if (std::sqrt(sum / static_cast<float>(limit - 1)) > threshold) {
          const int remaining = limit - sample;
          if (remaining > 0) {
            ++earlyTerminatedNodes_;
            earlySkippedSamples_ += static_cast<std::size_t>(remaining);
            segmentationRng_.discardNextFloats(
                static_cast<std::uint64_t>(remaining) * 2u);
          }
          return true;
        }
        nextDecisionSample = std::min(limit, nextDecisionSample * 2);
      }
    }
    return false;
  }

  void skipUnusedDeviationSamples(int size) {
    const int limit =
        std::max(static_cast<int>(0.1f * static_cast<float>(size) *
                                  static_cast<float>(size)),
                 4);
    segmentationRng_.discardNextFloats(static_cast<std::uint64_t>(limit) *
                                       2u);
  }

  std::uint64_t fixedSegmentationRandomDraws(int blockSize) const noexcept {
    std::uint64_t draws = 0;
    for (int size = rootSize_;; size >>= 1) {
      const std::uint64_t columns =
          (static_cast<std::uint64_t>(width_) + size - 1u) /
          static_cast<std::uint64_t>(size);
      const std::uint64_t rows =
          (static_cast<std::uint64_t>(height_) + size - 1u) /
          static_cast<std::uint64_t>(size);
      const std::uint64_t limit = static_cast<std::uint64_t>(std::max(
          static_cast<int>(0.1f * static_cast<float>(size) *
                           static_cast<float>(size)),
          4));
      draws += columns * rows * limit * 2u;
      if (size <= blockSize)
        break;
    }
    return draws;
  }

  void emitFixedSegmentsRecursive(int x, int y, int size, int blockSize,
                                  std::vector<WorkingSegment> &segments) {
    if (x >= width_ || y >= height_)
      return;
    if (size > blockSize) {
      const int half = size / 2;
      emitFixedSegmentsRecursive(x, y, half, blockSize, segments);
      emitFixedSegmentsRecursive(x + half, y, half, blockSize, segments);
      emitFixedSegmentsRecursive(x, y + half, half, blockSize, segments);
      emitFixedSegmentsRecursive(x + half, y + half, half, blockSize,
                                 segments);
      return;
    }
    segments.push_back({x, y, size, 0});
  }

  void segmentChannelRecursive(int channel, int x, int y, int size, int minSize,
                               int maxSize, float threshold) {
    if (x >= width_ || y >= height_)
      return;
    const bool forcedSplit = size > maxSize;
    const bool forcedLeaf = size <= minSize;
    bool splitByDeviation = false;
    if (forcedSplit || forcedLeaf)
      skipUnusedDeviationSamples(size);
    else
      splitByDeviation =
          sampledStandardDeviationExceeds(channel, x, y, size, threshold);
    if (forcedSplit || (!forcedLeaf && splitByDeviation)) {
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

  void segmentChannelGeometry(int channel, bool consumeRandom = true) {
    auto &segments = segments_[static_cast<std::size_t>(channel)];
    segments.clear();
    const auto &channelConfig = config_.channels[channel];
    if (channelConfig.minBlockSize == channelConfig.maxBlockSize) {
      // The sampled deviation cannot affect a fixed-size quadtree. Emitting
      // its regular leaves directly removes RNG/stddev work without changing
      // any reconstructed boundary dependency or output pixel.
      emitFixedSegmentsRecursive(0, 0, rootSize_,
                                 channelConfig.minBlockSize, segments);
      if (consumeRandom)
        segmentationRng_.discardNextFloats(
            fixedSegmentationRandomDraws(channelConfig.minBlockSize));
    } else {
      segmentChannelRecursive(
          channel, 0, 0, rootSize_, channelConfig.minBlockSize,
          channelConfig.maxBlockSize, channelConfig.segmentationPrecision);
    }
  }

  void buildChannelDependencyLevels(int channel) {
    auto &segments = segments_[static_cast<std::size_t>(channel)];
    auto &levelGrid = dependencyGrids_[static_cast<std::size_t>(channel)];
    std::fill(levelGrid.begin(), levelGrid.end(), 0u);
    const int unit = config_.channels[static_cast<std::size_t>(channel)]
                         .minBlockSize;
    const std::size_t gridWidth =
        dependencyGridWidths_[static_cast<std::size_t>(channel)];

    // Preserve codec.pde's DFS reconstruction semantics. A leaf can run only
    // after every leaf touching its full top and left boundaries has finished.
    // Quadtree leaves and boundaries are aligned to minBlockSize, so one grid
    // cell per minimum leaf is exactly equivalent to the former per-pixel map.
    for (auto &segment : segments) {
      const int usedWidth = std::min(segment.size, width_ - segment.x);
      const int usedHeight = std::min(segment.size, height_ - segment.y);
      const std::size_t gridX =
          static_cast<std::size_t>(segment.x / unit);
      const std::size_t gridY =
          static_cast<std::size_t>(segment.y / unit);
      const std::size_t usedGridWidth =
          (static_cast<std::size_t>(usedWidth) + unit - 1u) /
          static_cast<std::size_t>(unit);
      const std::size_t usedGridHeight =
          (static_cast<std::size_t>(usedHeight) + unit - 1u) /
          static_cast<std::size_t>(unit);
      uint32_t dependency = 0;
      if (segment.x > 0) {
        for (std::size_t y = 0; y < usedGridHeight; ++y)
          dependency = std::max(
              dependency,
              levelGrid[(gridY + y) * gridWidth + gridX - 1u]);
      }
      if (segment.y > 0) {
        const std::size_t row = (gridY - 1u) * gridWidth;
        for (std::size_t x = 0; x < usedGridWidth; ++x)
          dependency =
              std::max(dependency, levelGrid[row + gridX + x]);
      }
      segment.dependencyLevel = dependency + 1u;
      // Future leaves can only touch this leaf through its bottom row or
      // right column. Each grid cell represents a constant minBlockSize-wide
      // boundary run, reducing clears and strided writes without approximation.
      const std::size_t bottomRow =
          (gridY + usedGridHeight - 1u) * gridWidth;
      std::fill_n(levelGrid.begin() + bottomRow + gridX, usedGridWidth,
                  segment.dependencyLevel);
      const std::size_t rightColumn =
          gridX + usedGridWidth - 1u;
      for (std::size_t y = 0; y < usedGridHeight; ++y)
        levelGrid[(gridY + y) * gridWidth + rightColumn] =
            segment.dependencyLevel;
    }
  }

  void segmentChannel(int channel, bool consumeRandom = true) {
    segmentChannelGeometry(channel, consumeRandom);
    buildChannelDependencyLevels(channel);
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
          processingPackPlanes(values[index], values[pixelCount_ + index],
                               values[pixelCount_ * 2u + index], input_[index]),
          config_.colorSpace);
    }
  }

  void executeWorkerPhase(WorkerPhase phase, int part, int partCount) {
    switch (phase) {
    case WorkerPhase::ConvertInput:
      convertInputRange(part, partCount);
      break;
    case WorkerPhase::BuildDependencyLevels:
      buildChannelDependencyLevels(part);
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
    std::fill(levelThreadgroupCounts_.begin(), levelThreadgroupCounts_.end(),
              0u);
    std::fill(levelThreadgroupMaxBlockSizes_.begin(),
              levelThreadgroupMaxBlockSizes_.end(), 0u);
    std::fill(levelGlobalMaxBlockSizes_.begin(),
              levelGlobalMaxBlockSizes_.end(), 0u);
    // Keep routing stable across frames. Adaptive CDF97 presets whose declared
    // bounds admit large leaves use one global-workspace route even when a
    // particular RNG frame happens not to emit one. This avoids discontinuous
    // pipeline switches and dispatch-count spikes; no-wavelet adaptive frames
    // retain the cheap small-leaf threadgroup route.
    const bool adaptiveCdfCanUseLargeLeaves =
        !staticSchedule_ &&
        std::any_of(config_.channels.begin(), config_.channels.end(),
                    [](const OriginalPresetChannel &channel) {
                      return channel.originalWaveletId == 65 &&
                             channel.maxBlockSize >
                                 static_cast<int>(
                                     kThreadgroupCdfMaxBlockSize);
                    });
    const auto useThreadgroupForSegment = [&](int blockSize) {
      return !adaptiveCdfCanUseLargeLeaves &&
             canUseThreadgroupPipeline(blockSize);
    };
    std::size_t total = 0;
    std::size_t maxLevel = 0;
    stats.segmentOrderFnv1a64 = kSegmentationTraceFnvOffset;
    for (std::size_t channel = 0; channel < segments_.size(); ++channel) {
      stats.segmentCounts[channel] = segments_[channel].size();
      total += segments_[channel].size();
      for (const auto &segment : segments_[channel]) {
        appendSegmentationTraceLeaf(stats.segmentOrderFnv1a64,
                                    static_cast<int>(channel), segment.x,
                                    segment.y, segment.size);
        if (segment.dependencyLevel == 0 ||
            segment.dependencyLevel > levelCounts_.size()) {
          error = "original-style Metal dependency frontier overflow";
          return false;
        }
        const std::size_t level = segment.dependencyLevel - 1u;
        ++levelCounts_[level];
        if (useThreadgroupForSegment(segment.size)) {
          ++levelThreadgroupCounts_[level];
          levelThreadgroupMaxBlockSizes_[level] =
              std::max(levelThreadgroupMaxBlockSizes_[level],
                       static_cast<uint32_t>(segment.size));
        } else {
          levelGlobalMaxBlockSizes_[level] =
              std::max(levelGlobalMaxBlockSizes_[level],
                       static_cast<uint32_t>(segment.size));
        }
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
      levelThreadgroupCursors_[level] = offset;
      levelGlobalCursors_[level] = offset + levelThreadgroupCounts_[level];
      offset += levelCounts_[level];
    }
    auto *descriptors =
        static_cast<MetalSegmentDescriptor *>(segmentBuffer_.contents);
    for (std::size_t channel = 0; channel < segments_.size(); ++channel) {
      for (const auto &segment : segments_[channel]) {
        const std::size_t level = segment.dependencyLevel - 1u;
        const uint32_t destination =
            useThreadgroupForSegment(segment.size)
                ? levelThreadgroupCursors_[level]++
                : levelGlobalCursors_[level]++;
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

  bool canUseThreadgroupPipeline(int blockSize) const noexcept {
    if (!threadgroupCdfEnabled_ || blockSize <= 0 ||
        blockSize > static_cast<int>(kThreadgroupCdfMaxBlockSize))
      return false;
    const NSUInteger matrixFloats =
        static_cast<NSUInteger>(blockSize) * blockSize;
    const NSUInteger workingBytes =
        (matrixFloats + std::max(matrixFloats, NSUInteger(32))) *
            sizeof(float) +
        2u * static_cast<NSUInteger>(blockSize) * sizeof(int32_t);
    return workingBytes <= device_.maxThreadgroupMemoryLength;
  }

  id<MTLDevice> device_ = nil;
  id<MTLCommandQueue> queue_ = nil;
  id<MTLLibrary> library_ = nil;
  id<MTLComputePipelineState> pipeline_ = nil;
  id<MTLComputePipelineState> threadgroupCdfPipeline_ = nil;
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
  std::array<std::vector<uint32_t>, 3> dependencyGrids_{};
  std::array<std::size_t, 3> dependencyGridWidths_{};
  ProcessingRandom segmentationRng_{42};
  std::size_t earlyTerminatedNodes_ = 0;
  std::size_t earlySkippedSamples_ = 0;
  std::vector<uint32_t> levelCounts_;
  std::vector<uint32_t> levelOffsets_;
  std::vector<uint32_t> levelThreadgroupCounts_;
  std::vector<uint32_t> levelThreadgroupCursors_;
  std::vector<uint32_t> levelGlobalCursors_;
  std::vector<uint32_t> levelThreadgroupMaxBlockSizes_;
  std::vector<uint32_t> levelGlobalMaxBlockSizes_;

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
  OriginalRealtimeMetalFrameStats cachedScheduleStats_{};
  bool threadgroupCdfEnabled_ = true;
  bool staticSchedule_ = false;
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
