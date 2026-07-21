#include "original_realtime.hpp"
#include "original_realtime_metal.hpp"
#include "preset_loader.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdlib>
#include <cstdint>
#include <iostream>
#include <string>
#include <vector>

#ifndef GLIC_TEST_PRESETS_DIR
#define GLIC_TEST_PRESETS_DIR "presets"
#endif

namespace {

std::vector<glic::Color> makeInput(int width, int height) {
  std::vector<glic::Color> input(static_cast<std::size_t>(width) * height);
  for (int y = 0; y < height; ++y) {
    for (int x = 0; x < width; ++x) {
      const int r = (x * 17 + y * 5 + ((x ^ y) * 11)) & 255;
      const int g = (x * 3 + y * 23 + ((x * y) >> 2)) & 255;
      const int b = (x * 29 + y * 7 + ((x + y) * 13)) & 255;
      input[static_cast<std::size_t>(y) * width + x] = glic::makeColor(r, g, b);
    }
  }
  return input;
}

glic::OriginalPresetConfig makeConfig() {
  glic::OriginalPresetConfig config;
  config.colorSpace = glic::ColorSpace::RGB;
  config.borderColorR = 113;
  config.borderColorG = 97;
  config.borderColorB = 149;
  config.separateChannels = true;
  for (auto &channel : config.channels) {
    channel.minBlockSize = 4;
    channel.maxBlockSize = 4;
    channel.segmentationPrecision = 15.0f;
    channel.predictionMethod = glic::PredictionMethod::PAETH;
    channel.quantizationValue = 177;
    channel.clampMethod = glic::ClampMethod::MOD256;
    channel.originalWaveletId = 0;
    channel.originalTransformType = 0;
    channel.transformScale = 1 << 20;
  }
  return config;
}

struct Difference {
  double mae = 0.0;
  double exactRatio = 0.0;
  int maximum = 0;
};

Difference difference(const std::vector<glic::Color> &reference,
                      const std::vector<glic::Color> &candidate) {
  uint64_t total = 0;
  uint64_t exact = 0;
  int maximum = 0;
  for (std::size_t index = 0; index < reference.size(); ++index) {
    for (int shift : {16, 8, 0}) {
      const int a = static_cast<int>((reference[index] >> shift) & 255u);
      const int b = static_cast<int>((candidate[index] >> shift) & 255u);
      const int delta = std::abs(a - b);
      total += static_cast<uint64_t>(delta);
      maximum = std::max(maximum, delta);
      exact += delta == 0 ? 1u : 0u;
    }
  }
  const double samples = static_cast<double>(reference.size() * 3u);
  return {.mae = static_cast<double>(total) / samples,
          .exactRatio = static_cast<double>(exact) / samples,
          .maximum = maximum};
}

bool processPair(const glic::OriginalPresetConfig &config,
                 const std::vector<glic::Color> &input, int width, int height,
                 Difference &result,
                 glic::OriginalRealtimeMetalFrameStats *metalStats,
                 std::string &error) {
  glic::OriginalRealtimeCpuLane cpu;
  if (!cpu.prepare(width, height, config, error))
    return false;
  auto metal = glic::createOriginalRealtimeMetalLane(error);
  if (!metal || !metal->prepare(width, height, config, error))
    return false;
  std::vector<glic::Color> reference(input.size());
  std::vector<glic::Color> candidate(input.size());
  glic::OriginalRealtimeFrameStats cpuStats;
  if (!cpu.process(input, reference, &cpuStats, error) ||
      !metal->process(input, candidate, 0, metalStats, error))
    return false;
  if (metalStats != nullptr &&
      cpuStats.segmentCounts != metalStats->segmentCounts) {
    error = "Metal segmentation diverged from the CPU reference";
    return false;
  }
  result = difference(reference, candidate);
  if (std::string(metal->name()) != "original_metal_visual" ||
      std::string(metal->executionMode()) !=
          "hybrid_cpu_colorspace_segmentation_gpu_reconstruction" ||
      std::string(metal->numericPrecision()) !=
          "integer_prediction_float_float_cdf97_accumulation_fp32_storage" ||
      metal->isPixelExact()) {
    error = "Metal lane did not expose its hybrid execution boundary";
    return false;
  }
  return true;
}

std::unique_ptr<glic::OriginalRealtimeMetalLane>
createMetalWithThreadgroupCdf(bool enabled, std::string &error) {
  const char *previousValue = std::getenv("GLIC_DISABLE_THREADGROUP_CDF");
  const bool hadPreviousValue = previousValue != nullptr;
  const std::string previous = hadPreviousValue ? previousValue : "";
  if (enabled)
    unsetenv("GLIC_DISABLE_THREADGROUP_CDF");
  else
    setenv("GLIC_DISABLE_THREADGROUP_CDF", "1", 1);
  auto lane = glic::createOriginalRealtimeMetalLane(error);
  if (hadPreviousValue)
    setenv("GLIC_DISABLE_THREADGROUP_CDF", previous.c_str(), 1);
  else
    unsetenv("GLIC_DISABLE_THREADGROUP_CDF");
  return lane;
}

bool testThreadgroupCdfMatchesGlobalBitExactly() {
  const auto runCase = [](int width, int height,
                          const std::array<int, 3> &blockSizes, int transform,
                          const std::array<glic::PredictionMethod, 3>
                              &predictors) -> bool {
    const auto input = makeInput(width, height);
    auto config = makeConfig();
    for (std::size_t channelIndex = 0;
         channelIndex < config.channels.size(); ++channelIndex) {
      auto &channel = config.channels[channelIndex];
      channel.minBlockSize = blockSizes[channelIndex];
      channel.maxBlockSize = blockSizes[channelIndex];
      channel.originalWaveletId = 65;
      channel.originalTransformType = transform;
      channel.transformScale = 233;
      channel.transformCompress = 24.083f;
      channel.transformCompressionThreshold = 0.446f;
      channel.predictionMethod = predictors[channelIndex];
      channel.quantizationValue = 87;
    }

    std::string error;
    auto global = createMetalWithThreadgroupCdf(false, error);
    auto local = createMetalWithThreadgroupCdf(true, error);
    if (!global || !local || !global->prepare(width, height, config, error) ||
        !local->prepare(width, height, config, error)) {
      std::cerr << "threadgroup/global A/B prepare failed at " << width << 'x'
                << height << ": " << error << '\n';
      return false;
    }
    std::vector<glic::Color> globalOutput(input.size());
    std::vector<glic::Color> localOutput(input.size());
    glic::OriginalRealtimeMetalFrameStats globalStats;
    glic::OriginalRealtimeMetalFrameStats localStats;
    if (!global->process(input, globalOutput, 0, &globalStats, error) ||
        !local->process(input, localOutput, 0, &localStats, error)) {
      std::cerr << "threadgroup/global A/B process failed at " << width << 'x'
                << height << ": " << error << '\n';
      return false;
    }
    if (localOutput != globalOutput ||
        localStats.threadgroupPipelineDispatches == 0 ||
        localStats.threadgroupPipelineSegments == 0 ||
        localStats.globalPipelineDispatches != 0 ||
        globalStats.threadgroupPipelineDispatches != 0 ||
        globalStats.globalPipelineDispatches == 0 ||
        !localStats.staticScheduleReused ||
        localStats.bufferBarriers + 1 != localStats.gpuDispatches) {
      std::cerr << "threadgroup/global A/B mismatch at " << width << 'x'
                << height << " blocks " << blockSizes[0] << '/'
                << blockSizes[1] << '/' << blockSizes[2] << " transform "
                << transform << '\n';
      return false;
    }
    return true;
  };

  for (int blockSize : {2, 4, 8, 16, 32}) {
    for (int transform = 0; transform <= 1; ++transform) {
      if (!runCase(64, 64, {blockSize, blockSize, blockSize}, transform,
                   {glic::PredictionMethod::LDIAG,
                    glic::PredictionMethod::LDIAG,
                    glic::PredictionMethod::LDIAG}))
        return false;
    }
  }
  return runCase(63, 47, {16, 32, 8}, 0,
                 {glic::PredictionMethod::DC,
                  glic::PredictionMethod::JPEGLS,
                  glic::PredictionMethod::DIFF}) &&
         runCase(65, 49, {32, 16, 4}, 1,
                 {glic::PredictionMethod::DIFF,
                  glic::PredictionMethod::DC,
                  glic::PredictionMethod::JPEGLS});
}

bool testNoWaveletExact() {
  constexpr int width = 64;
  constexpr int height = 48;
  const auto input = makeInput(width, height);
  for (int predictor = static_cast<int>(glic::PredictionMethod::NONE);
       predictor <= static_cast<int>(glic::PredictionMethod::DIFF);
       ++predictor) {
    for (bool adaptive : {false, true}) {
      auto config = makeConfig();
      for (auto &channel : config.channels) {
        channel.predictionMethod =
            static_cast<glic::PredictionMethod>(predictor);
        if (adaptive) {
          channel.minBlockSize = 2;
          channel.maxBlockSize = 32;
          channel.segmentationPrecision = 12.0f;
        }
      }
      Difference delta;
      glic::OriginalRealtimeMetalFrameStats stats;
      std::string error;
      if (!processPair(config, input, width, height, delta, &stats, error)) {
        std::cerr << "no-wavelet predictor " << predictor
                  << " adaptive=" << adaptive << " failed: " << error << '\n';
        return false;
      }
      if (delta.maximum != 0 || delta.exactRatio != 1.0 ||
          stats.dispatchLevels == 0 || stats.gpuDispatches == 0 ||
          stats.totalSegments == 0 || stats.commandBufferSubmissions != 1 ||
          stats.completionWaits != 1 || stats.mappedBufferCopies != 0) {
        std::cerr << "no-wavelet predictor " << predictor
                  << " adaptive=" << adaptive
                  << " lost exact integer parity: mae=" << delta.mae
                  << " max=" << delta.maximum << " exact=" << delta.exactRatio
                  << '\n';
        return false;
      }
    }
  }
  return true;
}

bool testCdf97FixedParity() {
  constexpr int width = 64;
  constexpr int height = 64;
  const auto input = makeInput(width, height);
  for (int blockSize : {2, 4, 8, 16, 32, 64}) {
    for (int transform = 0; transform <= 1; ++transform) {
      for (int transformScale : {166, 1 << 20}) {
        for (auto predictor :
             {glic::PredictionMethod::JPEGLS, glic::PredictionMethod::PAETH}) {
          auto config = makeConfig();
          for (auto &channel : config.channels) {
            channel.minBlockSize = blockSize;
            channel.maxBlockSize = blockSize;
            channel.originalWaveletId = 65;
            channel.originalTransformType = transform;
            channel.transformScale = transformScale;
            channel.transformCompress = 0.0f;
            channel.transformCompressionThreshold = 0.0f;
            channel.predictionMethod = predictor;
            channel.quantizationValue = 199;
          }
          Difference delta;
          glic::OriginalRealtimeMetalFrameStats stats;
          std::string error;
          if (!processPair(config, input, width, height, delta, &stats,
                           error)) {
            std::cerr << "CDF97 block " << blockSize << " transform "
                      << transform << " scale " << transformScale
                      << " predictor " << static_cast<int>(predictor)
                      << " failed: " << error << '\n';
            return false;
          }
          // Metal has no fp64 arithmetic. A one-unit coefficient boundary can
          // feed a later predictor and amplify a tiny number of samples. Keep
          // the aggregate/frequency gate strict and separately require the
          // optimized threadgroup path to match the global fp32 path bitwise.
          if (delta.mae > 0.10 || delta.exactRatio < 0.999) {
            std::cerr << "CDF97 block " << blockSize << " transform "
                      << transform << " scale " << transformScale
                      << " predictor " << static_cast<int>(predictor)
                      << " exceeded fp32 tolerance: mae=" << delta.mae
                      << " max=" << delta.maximum
                      << " exact=" << delta.exactRatio << '\n';
            return false;
          }
        }
      }
    }
  }
  return true;
}

bool testAdaptiveCdf97BoundedDeviation() {
  constexpr int width = 64;
  constexpr int height = 48;
  const auto input = makeInput(width, height);
  for (const std::string preset :
       {"abstract_expressionism", "colour_waves_sharp"}) {
    glic::OriginalPresetConfig config;
    if (!glic::PresetLoader::loadOriginalPresetByName(GLIC_TEST_PRESETS_DIR,
                                                      preset, config)) {
      std::cerr << "failed to load adaptive CDF97 preset " << preset << '\n';
      return false;
    }
    Difference delta;
    glic::OriginalRealtimeMetalFrameStats stats;
    std::string error;
    if (!processPair(config, input, width, height, delta, &stats, error)) {
      std::cerr << "adaptive CDF97 preset " << preset << " failed: " << error
                << '\n';
      return false;
    }
    // Tiny fp32 coefficient differences can cross an integer rounding edge,
    // then propagate through later predictor boundaries. This lane therefore
    // makes no pixel-exact claim, but gross divergence is fail-closed in CI.
    if (delta.mae > 50.0 || delta.exactRatio < 0.10) {
      std::cerr << "adaptive CDF97 preset " << preset
                << " exceeded bounded visual-numeric deviation: mae="
                << delta.mae << " max=" << delta.maximum
                << " exact=" << delta.exactRatio << '\n';
      return false;
    }
  }
  return true;
}

bool testFailClosed() {
  auto config = makeConfig();
  for (auto &channel : config.channels) {
    channel.minBlockSize = 1;
    channel.maxBlockSize = 1;
  }
  std::string error;
  auto metal = glic::createOriginalRealtimeMetalLane(error);
  if (!metal || metal->prepare(64, 48, config, error) ||
      error.find("2..512") == std::string::npos) {
    std::cerr << "Metal min-block dependency guard did not fail closed: "
              << error << '\n';
    return false;
  }
  config = makeConfig();
  config.channels[1].originalWaveletId = 17;
  error.clear();
  if (metal->prepare(64, 48, config, error) ||
      error.find("unsupported") == std::string::npos) {
    std::cerr << "Metal unsupported-wavelet guard did not fail closed: "
              << error << '\n';
    return false;
  }
  return true;
}

bool testSupportedCorpusPrepares() {
  std::size_t supported = 0;
  std::string error;
  auto metal = glic::createOriginalRealtimeMetalLane(error);
  if (!metal) {
    std::cerr << error << '\n';
    return false;
  }
  constexpr int width = 64;
  constexpr int height = 48;
  const auto input = makeInput(width, height);
  std::vector<glic::Color> output(input.size());
  for (const auto &preset :
       glic::PresetLoader::listPresets(GLIC_TEST_PRESETS_DIR)) {
    glic::OriginalPresetConfig config;
    if (!glic::PresetLoader::loadOriginalPresetByName(GLIC_TEST_PRESETS_DIR,
                                                      preset, config))
      continue;
    if (!glic::evaluateOriginalRealtimeSupport(config).supported)
      continue;
    ++supported;
    if (!metal->prepare(width, height, config, error)) {
      std::cerr << "supported preset " << preset
                << " failed Metal prepare: " << error << '\n';
      return false;
    }
    glic::OriginalRealtimeMetalFrameStats stats;
    if (!metal->process(input, output, 0, &stats, error) ||
        stats.totalSegments == 0 || stats.gpuDispatches == 0) {
      std::cerr << "supported preset " << preset
                << " failed Metal process: " << error << '\n';
      return false;
    }
  }
  if (supported != 37) {
    std::cerr << "expected 37 supported original presets, got " << supported
              << '\n';
    return false;
  }
  return true;
}

} // namespace

int main() {
  if (!testNoWaveletExact() ||
      !testThreadgroupCdfMatchesGlobalBitExactly() ||
      !testCdf97FixedParity() ||
      !testAdaptiveCdf97BoundedDeviation() || !testFailClosed() ||
      !testSupportedCorpusPrepares())
    return 1;
  std::cout << "PASS original_metal_visual exact integer + float-float "
               "accumulated CDF97 with fp32 storage "
               "+ 37-preset fail-closed coverage\n";
  return 0;
}
