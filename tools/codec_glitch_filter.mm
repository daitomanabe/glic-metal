#include "codec_glitch.hpp"

#import <Foundation/Foundation.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <mutex>
#include <numeric>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unistd.h>
#include <vector>

namespace {

using Clock = std::chrono::steady_clock;

struct Options {
  int width = 960;
  int height = 540;
  int framesPerSecond = 30;
  glic::CodecGlitchControls controls;
  std::string statsPath;
  bool checkOnly = false;
};

struct OutputState {
  std::mutex mutex;
  std::condition_variable condition;
  std::deque<glic::CodecGlitchFrame> frames;
};

[[noreturn]] void failUsage(const std::string &message) {
  std::cerr << "error: " << message << '\n';
  std::exit(2);
}

int parseInt(const char *text, const char *name) {
  char *end = nullptr;
  errno = 0;
  const long value = std::strtol(text, &end, 0);
  if (errno != 0 || end == text || *end != '\0' ||
      value < std::numeric_limits<int>::min() ||
      value > std::numeric_limits<int>::max())
    failUsage(std::string("invalid ") + name + ": " + text);
  return static_cast<int>(value);
}

uint64_t parseUInt64(const char *text, const char *name) {
  char *end = nullptr;
  errno = 0;
  const unsigned long long value = std::strtoull(text, &end, 0);
  if (errno != 0 || end == text || *end != '\0')
    failUsage(std::string("invalid ") + name + ": " + text);
  return static_cast<uint64_t>(value);
}

float parseUnitFloat(const char *text, const char *name) {
  char *end = nullptr;
  errno = 0;
  const float value = std::strtof(text, &end);
  if (errno != 0 || end == text || *end != '\0' || !std::isfinite(value) ||
      value < 0.0f || value > 1.0f)
    failUsage(std::string(name) + " must be between 0 and 1");
  return value;
}

Options parseOptions(int argc, const char *argv[]) {
  Options options;
  for (int index = 1; index < argc; ++index) {
    const std::string_view argument(argv[index]);
    const auto value = [&](const char *name) -> const char * {
      if (++index >= argc)
        failUsage(std::string(name) + " requires a value");
      return argv[index];
    };
    if (argument == "--width")
      options.width = parseInt(value("--width"), "width");
    else if (argument == "--height")
      options.height = parseInt(value("--height"), "height");
    else if (argument == "--fps" || argument == "--target-fps")
      options.framesPerSecond = parseInt(value("--fps"), "frames per second");
    else if (argument == "--effect") {
      const char *name = value("--effect");
      if (!glic::codecGlitchEffectFromName(name, options.controls.effect))
        failUsage(std::string("unknown codec effect: ") + name);
    } else if (argument == "--amount")
      options.controls.amount = parseUnitFloat(value("--amount"), "--amount");
    else if (argument == "--rate")
      options.controls.rate = parseUnitFloat(value("--rate"), "--rate");
    else if (argument == "--feedback")
      options.controls.feedback =
          parseUnitFloat(value("--feedback"), "--feedback");
    else if (argument == "--seed")
      options.controls.seed = parseUInt64(value("--seed"), "seed");
    else if (argument == "--minimum-qp")
      options.controls.minimumQp =
          parseInt(value("--minimum-qp"), "minimum QP");
    else if (argument == "--maximum-qp")
      options.controls.maximumQp =
          parseInt(value("--maximum-qp"), "maximum QP");
    else if (argument == "--bit-rate")
      options.controls.crushedBitRate =
          parseInt(value("--bit-rate"), "bit rate");
    else if (argument == "--generations")
      options.controls.cascadeGenerations =
          parseInt(value("--generations"), "generations");
    else if (argument == "--resolution-scale")
      options.controls.reducedResolutionScale =
          parseUnitFloat(value("--resolution-scale"), "--resolution-scale");
    else if (argument == "--stats-json")
      options.statsPath = value("--stats-json");
    else if (argument == "--check")
      options.checkOnly = true;
    else if (argument == "--help") {
      std::cout << "Usage: glic_codec_glitch_filter [options] < BGRA > BGRA\n"
                << "  --width N --height N --fps N\n"
                << "  --effect NAME --amount 0..1 --rate 0..1 --feedback 0..1\n"
                << "  --seed N --stats-json PATH --check\n";
      std::exit(0);
    } else {
      failUsage(std::string("unknown argument: ") + std::string(argument));
    }
  }
  if (options.width <= 0 || options.height <= 0 || options.framesPerSecond <= 0)
    failUsage("width, height, and fps must be positive");
  return options;
}

bool readExact(int descriptor, uint8_t *destination, std::size_t size,
               bool &cleanEndOfFile) {
  cleanEndOfFile = false;
  std::size_t offset = 0;
  while (offset < size) {
    const ssize_t count =
        ::read(descriptor, destination + offset, size - offset);
    if (count == 0) {
      cleanEndOfFile = offset == 0;
      if (!cleanEndOfFile)
        std::cerr << "error: input ended in the middle of a BGRA frame\n";
      return false;
    }
    if (count < 0) {
      if (errno == EINTR)
        continue;
      std::cerr << "error: input read failed: " << std::strerror(errno) << '\n';
      return false;
    }
    offset += static_cast<std::size_t>(count);
  }
  return true;
}

bool writeExact(int descriptor, const uint8_t *source, std::size_t size) {
  std::size_t offset = 0;
  while (offset < size) {
    const ssize_t count = ::write(descriptor, source + offset, size - offset);
    if (count < 0) {
      if (errno == EINTR)
        continue;
      std::cerr << "error: output write failed: " << std::strerror(errno)
                << '\n';
      return false;
    }
    offset += static_cast<std::size_t>(count);
  }
  return true;
}

double percentile(std::vector<double> values, double probability) {
  if (values.empty())
    return 0.0;
  std::sort(values.begin(), values.end());
  const double position = probability * static_cast<double>(values.size() - 1);
  const auto lower = static_cast<std::size_t>(std::floor(position));
  const auto upper = static_cast<std::size_t>(std::ceil(position));
  const double fraction = position - static_cast<double>(lower);
  return values[lower] + (values[upper] - values[lower]) * fraction;
}

bool copyRawToPixelBuffer(const std::vector<uint8_t> &input,
                          CVPixelBufferRef pixelBuffer, int width, int height) {
  if (CVPixelBufferLockBaseAddress(pixelBuffer, 0) != kCVReturnSuccess)
    return false;
  auto *destination =
      static_cast<uint8_t *>(CVPixelBufferGetBaseAddress(pixelBuffer));
  const std::size_t destinationStride =
      CVPixelBufferGetBytesPerRow(pixelBuffer);
  const std::size_t sourceStride = static_cast<std::size_t>(width) * 4u;
  for (int y = 0; y < height; ++y)
    std::memcpy(destination + static_cast<std::size_t>(y) * destinationStride,
                input.data() + static_cast<std::size_t>(y) * sourceStride,
                sourceStride);
  CVPixelBufferUnlockBaseAddress(pixelBuffer, 0);
  return true;
}

bool writePixelBuffer(CVPixelBufferRef pixelBuffer, int width, int height) {
  if (pixelBuffer == nullptr ||
      CVPixelBufferGetWidth(pixelBuffer) != static_cast<std::size_t>(width) ||
      CVPixelBufferGetHeight(pixelBuffer) != static_cast<std::size_t>(height) ||
      CVPixelBufferGetPixelFormatType(pixelBuffer) != kCVPixelFormatType_32BGRA)
    return false;
  if (CVPixelBufferLockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly) !=
      kCVReturnSuccess)
    return false;
  const auto *source =
      static_cast<const uint8_t *>(CVPixelBufferGetBaseAddress(pixelBuffer));
  const std::size_t sourceStride = CVPixelBufferGetBytesPerRow(pixelBuffer);
  const std::size_t outputStride = static_cast<std::size_t>(width) * 4u;
  bool written = true;
  for (int y = 0; y < height && written; ++y)
    written = writeExact(STDOUT_FILENO,
                         source + static_cast<std::size_t>(y) * sourceStride,
                         outputStride);
  CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
  return written;
}

std::string jsonBool(bool value) { return value ? "true" : "false"; }

void writeStats(const Options &options,
                const glic::CodecGlitchStatistics &statistics,
                const std::vector<double> &latencies, uint64_t frames,
                uint64_t fallbackFrames, uint64_t intentionalRepeatFrames,
                uint64_t warmupFrames, double elapsedSeconds) {
  if (options.statsPath.empty())
    return;
  const double streamFps =
      elapsedSeconds > 0.0 ? static_cast<double>(frames) / elapsedSeconds : 0.0;
  const double latencySum =
      std::accumulate(latencies.begin(), latencies.end(), 0.0);
  const double averageLatency =
      latencies.empty() ? 0.0 : latencySum / latencies.size();
  const double processingFps =
      averageLatency > 0.0 ? 1000.0 / averageLatency : 0.0;
  const double p50 = percentile(latencies, 0.50);
  const double p95 = percentile(latencies, 0.95);
  const bool eligible20 = options.width >= 960 && options.height >= 540 &&
                          options.framesPerSecond >= 20 && frames >= 120 &&
                          statistics.hardwareEncoder &&
                          statistics.hardwareDecoder;
  const bool eligible30 = options.width >= 960 && options.height >= 540 &&
                          options.framesPerSecond >= 30 && frames >= 120 &&
                          statistics.hardwareEncoder &&
                          statistics.hardwareDecoder;
  const bool reliabilityPassed =
      fallbackFrames == 0 && statistics.codecErrors == 0 &&
      statistics.watchdogRecoveries == 0 && statistics.backpressureDrops == 0 &&
      statistics.pollQueueDrops == 0;
  const bool kernelPassed20 =
      reliabilityPassed && eligible20 && processingFps >= 20.0 && p95 <= 50.0;
  const bool kernelPassed30 =
      reliabilityPassed && eligible30 && processingFps >= 30.0 && p95 <= 33.334;
  const bool passed20 = kernelPassed20 && streamFps >= 20.0;
  const bool passed30 = kernelPassed30 && streamFps >= 30.0;
  std::ofstream output(options.statsPath);
  if (!output)
    throw std::runtime_error("could not open stats JSON: " + options.statsPath);
  output << std::fixed << std::setprecision(3) << "{\n"
         << "  \"schema\": \"glic-codec-glitch-filter-v1\",\n"
         << "  \"processing_mode\": \"codec_glitch\",\n"
         << "  \"preset\": \""
         << glic::codecGlitchEffectName(options.controls.effect) << "\",\n"
         << "  \"effect_family\": \""
         << glic::codecGlitchEffectName(options.controls.effect) << "\",\n"
         << "  \"amount\": " << options.controls.amount << ",\n"
         << "  \"rate\": " << options.controls.rate << ",\n"
         << "  \"feedback\": " << options.controls.feedback << ",\n"
         << "  \"seed\": " << options.controls.seed << ",\n"
         << "  \"width\": " << options.width << ",\n"
         << "  \"height\": " << options.height << ",\n"
         << "  \"target_fps\": " << options.framesPerSecond << ",\n"
         << "  \"frames\": " << frames << ",\n"
         << "  \"processing_fps\": " << processingFps << ",\n"
         << "  \"stream_observed_fps\": " << streamFps << ",\n"
         << "  \"latency_p50_ms\": " << p50 << ",\n"
         << "  \"latency_p95_ms\": " << p95 << ",\n"
         << "  \"average_latency_milliseconds\": "
         << statistics.averageLatencyMilliseconds << ",\n"
         << "  \"fallback_frames\": " << fallbackFrames << ",\n"
         << "  \"intentional_repeat_frames\": " << intentionalRepeatFrames
         << ",\n"
         << "  \"warmup_frames\": " << warmupFrames << ",\n"
         << "  \"submitted_frames\": " << statistics.submittedFrames << ",\n"
         << "  \"encoded_frames\": " << statistics.encodedFrames << ",\n"
         << "  \"decoded_frames\": " << statistics.decodedFrames << ",\n"
         << "  \"emitted_frames\": " << statistics.emittedFrames << ",\n"
         << "  \"intentional_packet_drops\": "
         << statistics.intentionalPacketDrops << ",\n"
         << "  \"backpressure_drops\": " << statistics.backpressureDrops
         << ",\n"
         << "  \"poll_queue_drops\": " << statistics.pollQueueDrops << ",\n"
         << "  \"codec_errors\": " << statistics.codecErrors << ",\n"
         << "  \"watchdog_recoveries\": " << statistics.watchdogRecoveries
         << ",\n"
         << "  \"reliability_passed\": " << jsonBool(reliabilityPassed) << ",\n"
         << "  \"hardware_encoder\": " << jsonBool(statistics.hardwareEncoder)
         << ",\n"
         << "  \"hardware_decoder\": " << jsonBool(statistics.hardwareDecoder)
         << ",\n"
         << "  \"base_frame_qp_supported\": "
         << jsonBool(statistics.baseFrameQpSupported) << ",\n"
         << "  \"kernel_realtime_20fps_passed\": " << jsonBool(kernelPassed20)
         << ",\n"
         << "  \"kernel_realtime_30fps_passed\": " << jsonBool(kernelPassed30)
         << ",\n"
         << "  \"realtime_20fps_passed\": " << jsonBool(passed20) << ",\n"
         << "  \"realtime_30fps_passed\": " << jsonBool(passed30) << "\n"
         << "}\n";
}

} // namespace

int main(int argc, const char *argv[]) {
  @autoreleasepool {
    try {
      const Options options = parseOptions(argc, argv);
      glic::CodecGlitchConfiguration configuration;
      configuration.width = options.width;
      configuration.height = options.height;
      configuration.framesPerSecond = options.framesPerSecond;
      configuration.maximumInFlightFrames = 4;
      configuration.pollQueueCapacity = 4;

      std::string error;
      auto engine = glic::createCodecGlitchEngine(configuration, error);
      if (!engine) {
        std::cerr << "error: codec engine initialization failed: " << error
                  << '\n';
        return 3;
      }
      engine->setControls(options.controls);

      NSDictionary *attributes = @{
        (id)kCVPixelBufferPixelFormatTypeKey : @(kCVPixelFormatType_32BGRA),
        (id)kCVPixelBufferWidthKey : @(options.width),
        (id)kCVPixelBufferHeightKey : @(options.height),
        (id)kCVPixelBufferBytesPerRowAlignmentKey : @64,
        (id)kCVPixelBufferMetalCompatibilityKey : @YES,
        (id)kCVPixelBufferIOSurfacePropertiesKey : @{},
      };
      CVPixelBufferPoolRef pool = nullptr;
      if (CVPixelBufferPoolCreate(kCFAllocatorDefault, nullptr,
                                  (__bridge CFDictionaryRef)attributes,
                                  &pool) != kCVReturnSuccess) {
        std::cerr << "error: input pixel-buffer pool creation failed\n";
        return 5;
      }

      auto outputState = std::make_shared<OutputState>();
      engine->setOutputCallback(
          [outputState](const glic::CodecGlitchFrame &frame) {
            {
              std::lock_guard lock(outputState->mutex);
              outputState->frames.push_back(frame);
            }
            outputState->condition.notify_one();
          });

      const std::size_t frameBytes =
          static_cast<std::size_t>(options.width) * options.height * 4u;
      std::vector<uint8_t> input(frameBytes);
      if (options.checkOnly) {
        for (int y = 0; y < options.height; ++y) {
          for (int x = 0; x < options.width; ++x) {
            const std::size_t offset =
                (static_cast<std::size_t>(y) * options.width + x) * 4u;
            input[offset + 0] = static_cast<uint8_t>((x + y) & 255);
            input[offset + 1] = static_cast<uint8_t>((y * 3) & 255);
            input[offset + 2] = static_cast<uint8_t>((x * 5) & 255);
            input[offset + 3] = 255;
          }
        }
        CVPixelBufferRef probe = nullptr;
        const bool allocated =
            CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, pool,
                                               &probe) == kCVReturnSuccess &&
            probe != nullptr;
        if (!allocated ||
            !copyRawToPixelBuffer(input, probe, options.width,
                                  options.height) ||
            !engine->submit(probe, 0, CMTimeMake(0, options.framesPerSecond),
                            error)) {
          if (probe != nullptr)
            CFRelease(probe);
          engine->setOutputCallback({});
          CFRelease(pool);
          std::cerr << "error: codec hardware probe failed: " << error << '\n';
          return 4;
        }
        CFRelease(probe);
        {
          std::unique_lock lock(outputState->mutex);
          outputState->condition.wait_for(lock, std::chrono::seconds(2), [&] {
            return !outputState->frames.empty();
          });
        }
        if (!engine->flush(std::chrono::seconds(2), error)) {
          engine->setOutputCallback({});
          CFRelease(pool);
          std::cerr << "error: codec hardware probe flush failed: " << error
                    << '\n';
          return 4;
        }
        engine->setOutputCallback({});
        const auto statistics = engine->stats();
        const bool decoded = !outputState->frames.empty();
        std::cout << "effect="
                  << glic::codecGlitchEffectName(options.controls.effect)
                  << " hardware_encoder=" << statistics.hardwareEncoder
                  << " hardware_decoder=" << statistics.hardwareDecoder
                  << " base_frame_qp=" << statistics.baseFrameQpSupported
                  << " decoded=" << decoded << '\n';
        CFRelease(pool);
        return statistics.hardwareEncoder && statistics.hardwareDecoder &&
                       decoded
                   ? 0
                   : 4;
      }
      std::vector<double> latencies;
      uint64_t frameIndex = 0;
      uint64_t fallbackFrames = 0;
      uint64_t intentionalRepeatFrames = 0;
      uint64_t warmupFrames = 0;
      const auto started = Clock::now();
      bool ioSucceeded = true;
      while (ioSucceeded) {
        bool cleanEndOfFile = false;
        if (!readExact(STDIN_FILENO, input.data(), input.size(),
                       cleanEndOfFile)) {
          ioSucceeded = cleanEndOfFile;
          break;
        }
        CVPixelBufferRef pixelBuffer = nullptr;
        if (CVPixelBufferPoolCreatePixelBuffer(
                kCFAllocatorDefault, pool, &pixelBuffer) != kCVReturnSuccess ||
            !copyRawToPixelBuffer(input, pixelBuffer, options.width,
                                  options.height)) {
          if (pixelBuffer != nullptr)
            CFRelease(pixelBuffer);
          std::cerr << "error: input pixel-buffer allocation/copy failed\n";
          ioSucceeded = false;
          break;
        }
        const CMTime pts = CMTimeMake(static_cast<int64_t>(frameIndex),
                                      options.framesPerSecond);
        if (!engine->submit(pixelBuffer, frameIndex, pts, error)) {
          CFRelease(pixelBuffer);
          std::cerr << "error: codec submit failed: " << error << '\n';
          ioSucceeded = false;
          break;
        }
        CFRelease(pixelBuffer);

        std::optional<glic::CodecGlitchFrame> frame;
        {
          std::unique_lock lock(outputState->mutex);
          outputState->condition.wait_for(
              lock, std::chrono::milliseconds(650),
              [&] { return !outputState->frames.empty(); });
          while (!outputState->frames.empty() &&
                 outputState->frames.front().frameIndex < frameIndex)
            outputState->frames.pop_front();
          if (!outputState->frames.empty() &&
              outputState->frames.front().frameIndex == frameIndex) {
            frame.emplace(std::move(outputState->frames.front()));
            outputState->frames.pop_front();
          }
        }
        if (frame && frame->pixelBuffer() != nullptr) {
          ioSucceeded = writePixelBuffer(frame->pixelBuffer(), options.width,
                                         options.height);
          if (frame->codecWarmupFrame)
            ++warmupFrames;
          else
            latencies.push_back(frame->latencyMilliseconds);
          if (frame->intentionalRepeat)
            ++intentionalRepeatFrames;
          else if (frame->nonIntentionalFallback ||
                   frame->repeatedPreviousFrame)
            ++fallbackFrames;
        } else {
          ioSucceeded = writeExact(STDOUT_FILENO, input.data(), input.size());
          ++fallbackFrames;
          latencies.push_back(650.0);
        }
        ++frameIndex;
      }

      if (!engine->flush(std::chrono::seconds(3), error)) {
        std::cerr << "error: codec flush failed: " << error << '\n';
        ioSucceeded = false;
      }
      engine->setOutputCallback({});
      const auto statistics = engine->stats();
      const double elapsedSeconds =
          std::chrono::duration<double>(Clock::now() - started).count();
      writeStats(options, statistics, latencies, frameIndex, fallbackFrames,
                 intentionalRepeatFrames, warmupFrames, elapsedSeconds);
      CFRelease(pool);
      if (!ioSucceeded)
        return 6;
      std::cerr << "effect="
                << glic::codecGlitchEffectName(options.controls.effect)
                << " frames=" << frameIndex << " fps="
                << (elapsedSeconds > 0.0 ? frameIndex / elapsedSeconds : 0.0)
                << " p95_ms=" << percentile(latencies, 0.95)
                << " fallback=" << fallbackFrames
                << " intentional_repeat=" << intentionalRepeatFrames << '\n';
      return 0;
    } catch (const std::exception &exception) {
      std::cerr << "error: " << exception.what() << '\n';
      return 1;
    }
  }
}
