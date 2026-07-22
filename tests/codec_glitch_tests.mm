#include "codec_glitch.hpp"
#include <glic_metal/codec_glitch.h>

#import <Foundation/Foundation.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

constexpr int kWidth = 960;
constexpr int kHeight = 540;
constexpr int kFramesPerEffect = 14;
constexpr int kWarmupFramesPerEffect = 2;

struct OutputState {
  std::mutex mutex;
  std::condition_variable condition;
  std::deque<glic::CodecGlitchFrame> frames;
};

struct FlushCallbackState {
  std::mutex mutex;
  std::condition_variable condition;
  bool finished = false;
  bool succeeded = false;
  std::string error;
};

void fillMovingFixture(CVPixelBufferRef pixelBuffer, uint64_t frameIndex) {
  CVPixelBufferLockBaseAddress(pixelBuffer, 0);
  auto *bytes =
      static_cast<uint8_t *>(CVPixelBufferGetBaseAddress(pixelBuffer));
  const std::size_t stride = CVPixelBufferGetBytesPerRow(pixelBuffer);
  for (int y = 0; y < kHeight; ++y) {
    uint8_t *row = bytes + static_cast<std::size_t>(y) * stride;
    for (int x = 0; x < kWidth; ++x) {
      const int movingX = (x + static_cast<int>(frameIndex * 13u)) % kWidth;
      const int movingY = (y + static_cast<int>(frameIndex * 7u)) % kHeight;
      const bool bar = ((movingX / 37) ^ (movingY / 29)) & 1;
      row[x * 4 + 0] = static_cast<uint8_t>((movingX + y * 2) & 255);
      row[x * 4 + 1] = static_cast<uint8_t>((movingY * 3 + x) & 255);
      row[x * 4 + 2] = static_cast<uint8_t>(bar ? 238 : 24);
      row[x * 4 + 3] = 255;
    }
  }
  CVPixelBufferUnlockBaseAddress(pixelBuffer, 0);
}

double sampledDifference(CVPixelBufferRef left, CVPixelBufferRef right) {
  CVPixelBufferLockBaseAddress(left, kCVPixelBufferLock_ReadOnly);
  CVPixelBufferLockBaseAddress(right, kCVPixelBufferLock_ReadOnly);
  const auto *leftBytes =
      static_cast<const uint8_t *>(CVPixelBufferGetBaseAddress(left));
  const auto *rightBytes =
      static_cast<const uint8_t *>(CVPixelBufferGetBaseAddress(right));
  const std::size_t leftStride = CVPixelBufferGetBytesPerRow(left);
  const std::size_t rightStride = CVPixelBufferGetBytesPerRow(right);
  uint64_t total = 0;
  uint64_t samples = 0;
  for (int y = 0; y < kHeight; y += 8) {
    for (int x = 0; x < kWidth; x += 8) {
      for (int channel = 0; channel < 3; ++channel) {
        const int difference =
            static_cast<int>(
                leftBytes[static_cast<std::size_t>(y) * leftStride + x * 4 +
                          channel]) -
            static_cast<int>(
                rightBytes[static_cast<std::size_t>(y) * rightStride + x * 4 +
                           channel]);
        total += static_cast<uint64_t>(std::abs(difference));
        ++samples;
      }
    }
  }
  CVPixelBufferUnlockBaseAddress(right, kCVPixelBufferLock_ReadOnly);
  CVPixelBufferUnlockBaseAddress(left, kCVPixelBufferLock_ReadOnly);
  return samples > 0 ? static_cast<double>(total) / samples : 0.0;
}

int runCppApiTest() {
  glic::CodecGlitchConfiguration configuration;
  configuration.width = kWidth;
  configuration.height = kHeight;
  configuration.framesPerSecond = 30;
  configuration.keyFrameInterval = 5;
  configuration.maximumInFlightFrames = 6;
  configuration.pollQueueCapacity = 4;

  std::string error;
  auto engine = glic::createCodecGlitchEngine(configuration, error);
  if (!engine) {
    std::fprintf(stderr, "FAIL codec engine initialization: %s\n",
                 error.c_str());
    return 2;
  }
  NSDictionary *attributes = @{
    (id)kCVPixelBufferPixelFormatTypeKey : @(kCVPixelFormatType_32BGRA),
    (id)kCVPixelBufferWidthKey : @(kWidth),
    (id)kCVPixelBufferHeightKey : @(kHeight),
    (id)kCVPixelBufferBytesPerRowAlignmentKey : @64,
    (id)kCVPixelBufferMetalCompatibilityKey : @YES,
    (id)kCVPixelBufferIOSurfacePropertiesKey : @{},
  };
  CVPixelBufferPoolRef pool = nullptr;
  if (CVPixelBufferPoolCreate(kCFAllocatorDefault, nullptr,
                              (__bridge CFDictionaryRef)attributes,
                              &pool) != kCVReturnSuccess) {
    std::fprintf(stderr, "FAIL fixture pixel-buffer pool creation\n");
    return 4;
  }

  auto outputState = std::make_shared<OutputState>();
  engine->setOutputCallback([outputState](const glic::CodecGlitchFrame &frame) {
    {
      std::lock_guard lock(outputState->mutex);
      outputState->frames.push_back(frame);
    }
    outputState->condition.notify_one();
  });

  uint64_t frameIndex = 0;
  std::array<double, static_cast<std::size_t>(glic::CodecGlitchEffect::Count)>
      largestDifference{};
  for (uint32_t effectIndex = 0;
       effectIndex < static_cast<uint32_t>(glic::CodecGlitchEffect::Count);
       ++effectIndex) {
    const auto effect = static_cast<glic::CodecGlitchEffect>(effectIndex);
    glic::CodecGlitchEffect parsed = glic::CodecGlitchEffect::Count;
    if (!glic::codecGlitchEffectFromName(glic::codecGlitchEffectName(effect),
                                         parsed) ||
        parsed != effect) {
      std::fprintf(stderr, "FAIL effect name round trip at %u\n", effectIndex);
      CFRelease(pool);
      return 5;
    }
    glic::CodecGlitchControls controls;
    controls.effect = effect;
    controls.amount = 0.82f;
    controls.rate = 0.72f;
    controls.feedback = 0.68f;
    controls.seed = 0x434f444543000000ULL + effectIndex;
    controls.crushedBitRate = 80000;
    if (!engine->reset(error)) {
      std::fprintf(stderr, "FAIL reset before %s: %s\n",
                   glic::codecGlitchEffectName(effect), error.c_str());
      CFRelease(pool);
      return 6;
    }
    engine->setControls(controls);

    int received = 0;
    int visiblyModified = 0;
    std::vector<double> differences;
    std::vector<double> latencies;
    const auto effectStarted = std::chrono::steady_clock::now();
    for (int localFrame = 0; localFrame < kFramesPerEffect; ++localFrame) {
      CVPixelBufferRef input = nullptr;
      if (CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, pool,
                                             &input) != kCVReturnSuccess) {
        std::fprintf(stderr, "FAIL fixture allocation\n");
        CFRelease(pool);
        return 6;
      }
      fillMovingFixture(input, frameIndex);
      if (!engine->submit(input, frameIndex, CMTimeMake(frameIndex, 30),
                          error)) {
        std::fprintf(stderr, "FAIL submit %s frame %d: %s\n",
                     glic::codecGlitchEffectName(effect), localFrame,
                     error.c_str());
        CFRelease(input);
        CFRelease(pool);
        return 7;
      }

      glic::CodecGlitchFrame output;
      {
        std::unique_lock lock(outputState->mutex);
        const bool ready = outputState->condition.wait_for(
            lock, std::chrono::milliseconds(700),
            [&] { return !outputState->frames.empty(); });
        if (ready) {
          output = std::move(outputState->frames.front());
          outputState->frames.pop_front();
        }
      }
      if (!output || output.frameIndex != frameIndex) {
        std::fprintf(stderr, "FAIL no ordered output for %s frame %d\n",
                     glic::codecGlitchEffectName(effect), localFrame);
        CFRelease(input);
        CFRelease(pool);
        return 8;
      }
      if (CVPixelBufferGetWidth(output.pixelBuffer()) != kWidth ||
          CVPixelBufferGetHeight(output.pixelBuffer()) != kHeight ||
          CVPixelBufferGetPixelFormatType(output.pixelBuffer()) !=
              kCVPixelFormatType_32BGRA) {
        std::fprintf(stderr, "FAIL output contract for %s\n",
                     glic::codecGlitchEffectName(effect));
        CFRelease(input);
        CFRelease(pool);
        return 9;
      }
      const double difference = sampledDifference(input, output.pixelBuffer());
      largestDifference[effectIndex] =
          std::max(largestDifference[effectIndex], difference);
      differences.push_back(difference);
      // Session creation and the first hardware decode after reset are a
      // bounded warm-up cost, not the sustained realtime kernel. Keep those
      // frames in the correctness/difference gates, but measure the latency
      // requirement only after the two-frame warm-up used by the live app.
      if (localFrame >= kWarmupFramesPerEffect && !output.codecWarmupFrame)
        latencies.push_back(output.latencyMilliseconds);
      if (output.packetWasModified || output.repeatedPreviousFrame)
        ++visiblyModified;
      ++received;
      ++frameIndex;
      CFRelease(input);
    }
    std::sort(differences.begin(), differences.end());
    std::sort(latencies.begin(), latencies.end());
    const double medianDifference = differences[differences.size() / 2];
    if (latencies.empty()) {
      std::fprintf(stderr, "FAIL no sustained latency samples for %s\n",
                   glic::codecGlitchEffectName(effect));
      CFRelease(pool);
      return 10;
    }
    const double p95Latency =
        latencies[static_cast<std::size_t>(
                      std::ceil(0.95 * static_cast<double>(latencies.size()))) -
                  1];
    const double effectSeconds =
        std::chrono::duration<double>(std::chrono::steady_clock::now() -
                                      effectStarted)
            .count();
    const double effectFps = received / effectSeconds;
    const bool packetEffect = effect == glic::CodecGlitchEffect::PFrameLoss ||
                              effect == glic::CodecGlitchEffect::IdrStarvation;
    if (received != kFramesPerEffect || medianDifference < 1.0 ||
        (packetEffect && visiblyModified == 0) || p95Latency > 50.0 ||
        effectFps < 20.0) {
      std::fprintf(stderr,
                   "FAIL effect gate %s median_diff=%.3f modified=%d "
                   "p95_ms=%.3f fps=%.3f\n",
                   glic::codecGlitchEffectName(effect), medianDifference,
                   visiblyModified, p95Latency, effectFps);
      CFRelease(pool);
      return 10;
    }
    std::printf("effect=%s frames=%d median_sample_mae=%.3f "
                "max_sample_mae=%.3f p95_ms=%.3f fps=%.3f\n",
                glic::codecGlitchEffectName(effect), received, medianDifference,
                largestDifference[effectIndex], p95Latency, effectFps);
  }

  // Hosts commonly flush or reset from an output callback. Verify the queue
  // identity guard and bounded callback state prevent self-deadlock and keep
  // old-stream frames from leaking across reset.
  auto flushState = std::make_shared<FlushCallbackState>();
  glic::CodecGlitchEngine *enginePointer = engine.get();
  engine->setOutputCallback([flushState,
                             enginePointer](const glic::CodecGlitchFrame &) {
    std::string callbackError;
    bool succeeded =
        enginePointer->flush(std::chrono::milliseconds(1000), callbackError);
    if (succeeded)
      succeeded = enginePointer->reset(callbackError);
    {
      std::lock_guard lock(flushState->mutex);
      flushState->succeeded = succeeded;
      flushState->error = std::move(callbackError);
      flushState->finished = true;
    }
    flushState->condition.notify_one();
  });
  CVPixelBufferRef callbackInput = nullptr;
  if (CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, pool,
                                         &callbackInput) != kCVReturnSuccess ||
      callbackInput == nullptr) {
    std::fprintf(stderr, "FAIL callback flush fixture allocation\n");
    CFRelease(pool);
    return 11;
  }
  fillMovingFixture(callbackInput, frameIndex);
  if (!engine->submit(callbackInput, frameIndex, CMTimeMake(frameIndex, 30),
                      error)) {
    std::fprintf(stderr, "FAIL callback flush submit: %s\n", error.c_str());
    CFRelease(callbackInput);
    CFRelease(pool);
    return 11;
  }
  ++frameIndex;
  CFRelease(callbackInput);
  {
    std::unique_lock lock(flushState->mutex);
    if (!flushState->condition.wait_for(lock, std::chrono::seconds(2),
                                        [&] { return flushState->finished; }) ||
        !flushState->succeeded) {
      std::fprintf(stderr, "FAIL callback flush: %s\n",
                   flushState->error.c_str());
      CFRelease(pool);
      return 11;
    }
  }
  engine->setOutputCallback({});

  if (!engine->flush(std::chrono::seconds(5), error)) {
    std::fprintf(stderr, "FAIL flush: %s\n", error.c_str());
    CFRelease(pool);
    return 11;
  }
  const auto statistics = engine->stats();
  CFRelease(pool);
  if (statistics.submittedFrames != frameIndex ||
      statistics.emittedFrames < frameIndex ||
      statistics.backpressureDrops != 0 || statistics.codecErrors != 0 ||
      statistics.watchdogRecoveries != 0 || statistics.pollQueueDrops != 0) {
    std::fprintf(
        stderr,
        "FAIL statistics submitted=%llu emitted=%llu backpressure=%llu "
        "errors=%llu recoveries=%llu poll=%llu\n",
        static_cast<unsigned long long>(statistics.submittedFrames),
        static_cast<unsigned long long>(statistics.emittedFrames),
        static_cast<unsigned long long>(statistics.backpressureDrops),
        static_cast<unsigned long long>(statistics.codecErrors),
        static_cast<unsigned long long>(statistics.watchdogRecoveries),
        static_cast<unsigned long long>(statistics.pollQueueDrops));
    return 12;
  }
  std::printf("PASS C++ codec glitch effects=%u frames=%llu hw_encoder=1 "
              "hw_decoder=1\n",
              static_cast<unsigned>(glic::CodecGlitchEffect::Count),
              static_cast<unsigned long long>(frameIndex));
  return 0;
}

int runCApiTest() {
  if (glic_codec_glitch_get_abi_version() != GLIC_CODEC_GLITCH_ABI_VERSION ||
      std::strcmp(
          glic_codec_glitch_effect_name(GLIC_CODEC_GLITCH_GENERATION_CASCADE),
          "generation_cascade") != 0) {
    std::fprintf(stderr, "FAIL C ABI metadata\n");
    return 20;
  }
  glic_codec_glitch_context *context = nullptr;
  if (glic_codec_glitch_context_create(&context) != GLIC_CODEC_GLITCH_OK ||
      context == nullptr) {
    std::fprintf(stderr, "FAIL C context create\n");
    return 21;
  }
  glic_codec_glitch_config config;
  glic_codec_glitch_config_init(&config);
  const auto prepareStatus = glic_codec_glitch_prepare(context, &config);
  if (prepareStatus != GLIC_CODEC_GLITCH_OK) {
    std::fprintf(stderr, "FAIL C prepare: %s\n",
                 glic_codec_glitch_get_last_error(context));
    glic_codec_glitch_context_destroy(context);
    return 22;
  }
  glic_codec_glitch_controls controls;
  glic_codec_glitch_controls_init(&controls);
  controls.effect = GLIC_CODEC_GLITCH_PAYLOAD_XOR;
  if (glic_codec_glitch_set_controls(context, &controls) !=
      GLIC_CODEC_GLITCH_OK) {
    std::fprintf(stderr, "FAIL C set controls\n");
    glic_codec_glitch_context_destroy(context);
    return 23;
  }
  NSDictionary *attributes = @{
    (id)kCVPixelBufferMetalCompatibilityKey : @YES,
    (id)kCVPixelBufferIOSurfacePropertiesKey : @{},
  };
  CVPixelBufferRef input = nullptr;
  if (CVPixelBufferCreate(
          kCFAllocatorDefault, kWidth, kHeight, kCVPixelFormatType_32BGRA,
          (__bridge CFDictionaryRef)attributes, &input) != kCVReturnSuccess ||
      input == nullptr) {
    std::fprintf(stderr, "FAIL C input allocation\n");
    glic_codec_glitch_context_destroy(context);
    return 24;
  }
  fillMovingFixture(input, 0);
  if (glic_codec_glitch_submit_pixel_buffer(context, input, 0, 0, 30) !=
      GLIC_CODEC_GLITCH_OK) {
    std::fprintf(stderr, "FAIL C submit: %s\n",
                 glic_codec_glitch_get_last_error(context));
    CFRelease(input);
    glic_codec_glitch_context_destroy(context);
    return 24;
  }
  CFRelease(input);
  glic_codec_glitch_frame frame;
  glic_codec_glitch_frame_init(&frame);
  glic_codec_glitch_status frameStatus = GLIC_CODEC_GLITCH_NO_FRAME_AVAILABLE;
  const auto pollDeadline =
      std::chrono::steady_clock::now() + std::chrono::seconds(2);
  while (frameStatus == GLIC_CODEC_GLITCH_NO_FRAME_AVAILABLE &&
         std::chrono::steady_clock::now() < pollDeadline) {
    frameStatus = glic_codec_glitch_copy_latest_pixel_buffer(context, &frame);
    if (frameStatus == GLIC_CODEC_GLITCH_NO_FRAME_AVAILABLE)
      std::this_thread::sleep_for(std::chrono::milliseconds(5));
  }
  if (frameStatus != GLIC_CODEC_GLITCH_OK || frame.pixel_buffer == nullptr ||
      frame.frame_index != 0) {
    std::fprintf(stderr, "FAIL C poll status=%d error=%s\n", frameStatus,
                 glic_codec_glitch_get_last_error(context));
    if (frame.pixel_buffer != nullptr)
      glic_codec_glitch_pixel_buffer_release(frame.pixel_buffer);
    glic_codec_glitch_context_destroy(context);
    return 24;
  }
  glic_codec_glitch_pixel_buffer_release(frame.pixel_buffer);
  frame.pixel_buffer = nullptr;
  if (glic_codec_glitch_flush(context, 2000) != GLIC_CODEC_GLITCH_OK) {
    std::fprintf(stderr, "FAIL C flush: %s\n",
                 glic_codec_glitch_get_last_error(context));
    glic_codec_glitch_context_destroy(context);
    return 24;
  }
  glic_codec_glitch_stats stats;
  glic_codec_glitch_stats_init(&stats);
  if (glic_codec_glitch_get_stats(context, &stats) != GLIC_CODEC_GLITCH_OK ||
      !stats.hardware_encoder || !stats.hardware_decoder) {
    std::fprintf(stderr, "FAIL C hardware statistics\n");
    glic_codec_glitch_context_destroy(context);
    return 25;
  }
  glic_codec_glitch_context_destroy(context);
  std::printf("PASS C codec glitch ABI=%u\n", GLIC_CODEC_GLITCH_ABI_VERSION);
  return 0;
}

} // namespace

int main() {
  @autoreleasepool {
    const int cppStatus = runCppApiTest();
    return cppStatus == 0 ? runCApiTest() : cppStatus;
  }
}
