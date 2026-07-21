#include "original_realtime.hpp"
#include "planes.hpp"
#include "prediction.hpp"
#include "processing_math.hpp"
#include "processing_random.hpp"
#include "segmentation_trace.hpp"
#include "quantization.hpp"
#include "segment.hpp"
#include "wavelet.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>
#include <set>
#include <string>
#include <tuple>
#include <vector>

namespace {

glic::OriginalPresetConfig makeSupportedConfig() {
  glic::OriginalPresetConfig config;
  config.colorSpace = glic::ColorSpace::RGB;
  config.borderColorR = 10;
  config.borderColorG = 20;
  config.borderColorB = 30;
  for (auto &channel : config.channels) {
    channel.minBlockSize = 4;
    channel.maxBlockSize = 4;
    channel.segmentationPrecision = 15.0f;
    channel.predictionMethod = glic::PredictionMethod::H;
    channel.quantizationValue = 8; // Upstream divisor is 8 / 2 = 4.
    channel.clampMethod = glic::ClampMethod::NONE;
    channel.originalWaveletId = 0;
  }
  return config;
}

std::vector<glic::Color> makeFixture() {
  std::vector<glic::Color> pixels;
  pixels.reserve(16);
  for (int index = 0; index < 16; ++index) {
    pixels.push_back(glic::makeColor(
        static_cast<uint8_t>(17 + index * 7),
        static_cast<uint8_t>(29 + index * 5),
        static_cast<uint8_t>(41 + index * 3),
        static_cast<uint8_t>(90 + index)));
  }
  return pixels;
}

int reconstructChannel(int value, int reference, float divisor) {
  const int residual = value - reference;
  const int quantized = glic::processingRound(
      static_cast<float>(glic::processingRound(residual / divisor)) * divisor);
  return std::clamp(quantized + reference, 0, 255);
}

bool testProcessingRoundGolden() {
  struct Golden {
    float input;
    int output;
  };
  for (const Golden golden :
       std::array<Golden, 6>{{{-43.5f, -43}, {-1.5f, -1}, {-0.5f, 0},
                              {0.49f, 0}, {0.5f, 1}, {1.5f, 2}}}) {
    if (glic::processingRound(golden.input) != golden.output) {
      std::cerr << "Processing round golden mismatch for " << golden.input
                << '\n';
      return false;
    }
  }
  if (glic::processingRound(std::numeric_limits<float>::quiet_NaN()) != 0 ||
      glic::processingRound(std::numeric_limits<float>::infinity()) !=
          std::numeric_limits<int>::max() ||
      glic::processingRound(-std::numeric_limits<float>::infinity()) !=
          std::numeric_limits<int>::min()) {
    std::cerr << "Processing round special-value golden mismatch\n";
    return false;
  }
  const auto packed = glic::processingPackPlanes(299, 0, 0, 0x7f000000u);
  if (packed != 0x7f2b0000u || glic::getR(packed) != 43 ||
      glic::getA(packed) != 127) {
    std::cerr << "Processing raw plane packing golden mismatch\n";
    return false;
  }
  return true;
}

bool testProcessingRandomGolden() {
  glic::ProcessingRandom random(42);
  constexpr std::array<int, 8> expected = {11, 0, 10, 0, 4, 15, 4, 11};
  for (const int position : expected) {
    const int actual = random.nextPosition(16);
    if (actual != position) {
      std::cerr << "Processing random golden mismatch: expected " << position
                << " got " << actual << '\n';
      return false;
    }
  }
  if (random.state() != 0xb52c856dae6fULL) {
    std::cerr << "Processing random state golden mismatch\n";
    return false;
  }

  glic::ProcessingRandom checkedPowerOfTwo(42);
  glic::ProcessingRandom hoistedPowerOfTwo(42);
  for (unsigned magnitude = 0; magnitude <= 24; ++magnitude) {
    const int size = 1 << magnitude;
    constexpr int drawsPerMagnitude = 64;
    for (int draw = 0; draw < drawsPerMagnitude; ++draw) {
      const float high = static_cast<float>(size);
      float value = 0.0f;
      do {
        value = checkedPowerOfTwo.nextFloat() * high;
      } while (value == high);
      const int expected = static_cast<int>(value);
      if (expected !=
          hoistedPowerOfTwo.nextPowerOfTwoPosition(magnitude)) {
        std::cerr << "Processing hoisted power-of-two random mismatch\n";
        return false;
      }
    }
  }
  if (checkedPowerOfTwo.state() != hoistedPowerOfTwo.state()) {
    std::cerr << "Processing hoisted power-of-two RNG state mismatch\n";
    return false;
  }
  glic::ProcessingRandom invalidMagnitude(42);
  glic::ProcessingRandom invalidMagnitudeReference(42);
  const int invalidPosition = invalidMagnitude.nextPowerOfTwoPosition(25);
  if (invalidPosition != 0 ||
      invalidMagnitude.state() != invalidMagnitudeReference.state()) {
    std::cerr << "Processing power-of-two magnitude guard mismatch\n";
    return false;
  }

  glic::ProcessingRandom checkpointed(42);
  const std::uint64_t checkpointState = checkpointed.state();
  {
    glic::ProcessingRandomCheckpoint checkpoint(checkpointed);
    (void)checkpointed.nextFloat();
  }
  if (checkpointed.state() != checkpointState) {
    std::cerr << "Processing random checkpoint rollback mismatch\n";
    return false;
  }
  {
    glic::ProcessingRandomCheckpoint checkpoint(checkpointed);
    (void)checkpointed.nextFloat();
    checkpoint.commit();
  }
  if (checkpointed.state() == checkpointState) {
    std::cerr << "Processing random checkpoint commit mismatch\n";
    return false;
  }

  glic::ProcessingRandom sequential(42);
  glic::ProcessingRandom skipped(42);
  constexpr std::uint64_t skipCount = 12345;
  for (std::uint64_t index = 0; index < skipCount; ++index)
    (void)sequential.nextFloat();
  skipped.discardNextFloats(skipCount);
  if (sequential.state() != skipped.state() ||
      sequential.nextFloat() != skipped.nextFloat()) {
    std::cerr << "Processing random skip-ahead mismatch\n";
    return false;
  }
  glic::ProcessingRandom fixedTree(42);
  fixedTree.discardNextFloats(922208);
  if (fixedTree.state() != 0x9deba58d7c27ULL) {
    std::cerr << "Processing fixed-tree RNG state golden mismatch\n";
    return false;
  }
  return true;
}

struct ProbeLeaf {
  int frame = 0;
  int channel = 0;
  int x = 0;
  int y = 0;
  int size = 0;

  auto operator<=>(const ProbeLeaf &) const = default;
};

using ProbePlanes = std::array<std::vector<int>, 3>;

int probePlaneValue(const ProbePlanes &planes, int width, int height,
                    int channel, int x, int y) {
  if (x < 0 || x >= width || y < 0 || y >= height)
    return 17 + channel * 31;
  return planes[static_cast<std::size_t>(channel)]
               [static_cast<std::size_t>(y) * width + x];
}

bool probeDeviationExceeds(glic::ProcessingRandom &random,
                           const ProbePlanes &planes, int width, int height,
                           int channel, int x, int y, int size,
                           float threshold, bool early,
                           std::size_t &evaluatedSamples) {
#if defined(__clang__)
#pragma clang fp contract(off)
#endif
  const int limit = std::max(
      static_cast<int>(0.1f * static_cast<float>(size) *
                       static_cast<float>(size)),
      4);
  float average = 0.0f;
  float sum = 0.0f;
  int nextDecisionSample = 1;
  const unsigned magnitude = std::countr_zero(static_cast<unsigned>(size));
  const auto slowProcessingPosition = [&](int extent) {
    const float high = static_cast<float>(extent);
    float value = 0.0f;
    do {
      value = random.nextFloat() * high;
    } while (value == high);
    return static_cast<int>(value);
  };
  for (int sample = 1; sample <= limit; ++sample) {
    const int sampleX =
        early ? random.nextPowerOfTwoPosition(magnitude)
              : slowProcessingPosition(size);
    const int sampleY =
        early ? random.nextPowerOfTwoPosition(magnitude)
              : slowProcessingPosition(size);
    ++evaluatedSamples;
    const int value = probePlaneValue(planes, width, height, channel,
                                      x + sampleX, y + sampleY);
    const float oldAverage = average;
    average += (static_cast<float>(value) - average) /
               static_cast<float>(sample);
    sum += (static_cast<float>(value) - oldAverage) *
           (static_cast<float>(value) - average);
    if (early && (sample == nextDecisionSample || sample == limit)) {
      if (std::sqrt(sum / static_cast<float>(limit - 1)) > threshold) {
        random.discardNextFloats(
            static_cast<std::uint64_t>(limit - sample) * 2u);
        return true;
      }
      nextDecisionSample = std::min(limit, nextDecisionSample * 2);
    }
  }
  return std::sqrt(sum / static_cast<float>(limit - 1)) > threshold;
}

void probeSegmentTree(glic::ProcessingRandom &random,
                      const ProbePlanes &planes, int width, int height,
                      int frame, int channel, int x, int y, int size,
                      int minSize, int maxSize, float threshold, bool early,
                      std::vector<ProbeLeaf> &leaves,
                      std::array<std::uint64_t, 2> &frameHashes,
                      std::size_t &evaluatedSamples) {
  if (x >= width || y >= height)
    return;
  const int limit = std::max(
      static_cast<int>(0.1f * static_cast<float>(size) *
                       static_cast<float>(size)),
      4);
  const bool forcedSplit = size > maxSize;
  const bool forcedLeaf = size <= minSize;
  bool splitByDeviation = false;
  if (forcedSplit || forcedLeaf) {
    if (early) {
      random.discardNextFloats(static_cast<std::uint64_t>(limit) * 2u);
    } else {
      for (int sample = 0; sample < limit; ++sample) {
        (void)random.nextFloat();
        (void)random.nextFloat();
      }
    }
  } else {
    splitByDeviation = probeDeviationExceeds(
        random, planes, width, height, channel, x, y, size, threshold, early,
        evaluatedSamples);
  }
  if (forcedSplit || (!forcedLeaf && splitByDeviation)) {
    const int half = size / 2;
    probeSegmentTree(random, planes, width, height, frame, channel, x, y, half,
                     minSize, maxSize, threshold, early, leaves, frameHashes,
                     evaluatedSamples);
    probeSegmentTree(random, planes, width, height, frame, channel, x + half, y,
                     half, minSize, maxSize, threshold, early, leaves,
                     frameHashes, evaluatedSamples);
    probeSegmentTree(random, planes, width, height, frame, channel, x, y + half,
                     half, minSize, maxSize, threshold, early, leaves,
                     frameHashes, evaluatedSamples);
    probeSegmentTree(random, planes, width, height, frame, channel, x + half,
                     y + half, half, minSize, maxSize, threshold, early,
                     leaves, frameHashes, evaluatedSamples);
    return;
  }
  leaves.push_back({frame, channel, x, y, size});
  glic::appendSegmentationTraceLeaf(frameHashes[frame], channel, x, y, size);
}

struct ProbeRun {
  std::vector<ProbeLeaf> leaves;
  std::array<std::uint64_t, 2> frameHashes{
      glic::kSegmentationTraceFnvOffset, glic::kSegmentationTraceFnvOffset};
  std::array<std::uint64_t, 2> terminalStates{};
  std::size_t evaluatedSamples = 0;
};

ProbeRun
runSegmentationProbe(const ProbePlanes &planes, int width, int height,
                     float threshold, bool early) {
  glic::ProcessingRandom random(42);
  ProbeRun result;
  for (int frame = 0; frame < 2; ++frame) {
    for (int channel = 0; channel < 3; ++channel) {
      probeSegmentTree(random, planes, width, height, frame, channel, 0, 0, 128,
                       2, 64, threshold, early, result.leaves,
                       result.frameHashes, result.evaluatedSamples);
    }
    result.terminalStates[frame] = random.state();
  }
  return result;
}

bool testEarlySegmentationMatchesFullSamplingOracle() {
  constexpr int width = 65;
  constexpr int height = 49;
  ProbePlanes planes;
  for (auto &plane : planes)
    plane.resize(width * height);
  for (int y = 0; y < height; ++y) {
    for (int x = 0; x < width; ++x) {
      const std::size_t index = static_cast<std::size_t>(y) * width + x;
      planes[0][index] = (x * 17 + y * 5 + ((x ^ y) * 11)) & 255;
      planes[1][index] = (x * 3 + y * 23 + ((x * y) >> 2)) & 255;
      planes[2][index] = (x * 29 + y * 7 + ((x + y) * 13)) & 255;
    }
  }
  std::vector<glic::Color> input(width * height);
  for (std::size_t index = 0; index < input.size(); ++index) {
    input[index] = glic::makeColor(
        static_cast<std::uint8_t>(planes[0][index]),
        static_cast<std::uint8_t>(planes[1][index]),
        static_cast<std::uint8_t>(planes[2][index]));
  }
  bool observedOracleSavings = false;
  bool observedProductionSavings = false;
  for (const float threshold : {-0.25f, 0.0f, 7.205f, 40.0f, 1000.0f}) {
    const auto full =
        runSegmentationProbe(planes, width, height, threshold, false);
    const auto early =
        runSegmentationProbe(planes, width, height, threshold, true);
    if (early.leaves != full.leaves ||
        early.frameHashes != full.frameHashes ||
        early.terminalStates != full.terminalStates) {
      std::cerr << "early segmentation/full sampling oracle mismatch at "
                << threshold << '\n';
      return false;
    }
    observedOracleSavings =
        observedOracleSavings || early.evaluatedSamples < full.evaluatedSamples;

    // Negative thresholds exercise the mathematical oracle boundary but are
    // rejected by the public preset preflight, so production comparison starts
    // at the supported non-negative domain.
    if (threshold < 0.0f)
      continue;

    auto config = makeSupportedConfig();
    config.borderColorR = 17;
    config.borderColorG = 48;
    config.borderColorB = 79;
    for (auto &channel : config.channels) {
      channel.minBlockSize = 2;
      channel.maxBlockSize = 64;
      channel.segmentationPrecision = threshold;
      channel.predictionMethod = glic::PredictionMethod::NONE;
      channel.quantizationValue = 0;
      channel.originalWaveletId = 0;
    }
    glic::OriginalRealtimeCpuLane lane;
    std::vector<glic::Color> output(input.size());
    std::string error;
    if (!lane.prepare(width, height, config, error)) {
      std::cerr << "production segmentation probe prepare failed: " << error
                << '\n';
      return false;
    }
    for (int frame = 0; frame < 2; ++frame) {
      glic::OriginalRealtimeFrameStats stats;
      if (!lane.process(input, output, &stats, error) || output != input ||
          stats.segmentOrderFnv1a64 != full.frameHashes[frame] ||
          stats.segmentationRngState != full.terminalStates[frame]) {
        std::cerr << "production segmentation/full oracle mismatch at "
                  << threshold << " frame " << frame << ": " << error
                  << '\n';
        return false;
      }
      observedProductionSavings =
          observedProductionSavings ||
          (stats.earlyTerminatedNodes > 0 &&
           stats.earlySkippedSamples > 0);
    }
  }
  if (!observedOracleSavings || !observedProductionSavings) {
    std::cerr << "early segmentation optimization was not exercised\n";
    return false;
  }
  return true;
}

bool testFixedPredictorQuantization() {
  const auto config = makeSupportedConfig();
  const auto input = makeFixture();
  std::vector<glic::Color> output(input.size());
  std::string error;
  glic::OriginalRealtimeCpuLane lane;
  if (!lane.prepare(4, 4, config, error)) {
    std::cerr << "supported prepare failed: " << error << '\n';
    return false;
  }

  glic::OriginalRealtimeFrameStats stats;
  if (!lane.process(input, output, &stats, error)) {
    std::cerr << "supported process failed: " << error << '\n';
    return false;
  }
  if (stats.segmentCounts != std::array<std::size_t, 3>{1, 1, 1}) {
    std::cerr << "unexpected segment counts\n";
    return false;
  }

  for (std::size_t index = 0; index < input.size(); ++index) {
    const auto expected = glic::makeColor(
        static_cast<uint8_t>(reconstructChannel(glic::getR(input[index]), 10,
                                                4.0f)),
        static_cast<uint8_t>(reconstructChannel(glic::getG(input[index]), 20,
                                                4.0f)),
        static_cast<uint8_t>(reconstructChannel(glic::getB(input[index]), 30,
                                                4.0f)),
        glic::getA(input[index]));
    if (output[index] != expected) {
      std::cerr << "fixed-predictor reconstruction mismatch at " << index
                << '\n';
      return false;
    }
  }
  return true;
}

bool testZeroQuantizationRoundTrip() {
  auto config = makeSupportedConfig();
  config.colorSpace = glic::ColorSpace::CMY;
  for (auto &channel : config.channels) {
    channel.predictionMethod = glic::PredictionMethod::PAETH;
    channel.quantizationValue = 0;
  }

  const auto input = makeFixture();
  std::vector<glic::Color> output(input.size());
  std::string error;
  glic::OriginalRealtimeCpuLane lane;
  if (!lane.prepare(4, 4, config, error) ||
      !lane.process(input, output, nullptr, error)) {
    std::cerr << "zero-quantization roundtrip failed: " << error << '\n';
    return false;
  }
  if (output != input) {
    std::cerr << "zero-quantization roundtrip changed pixels\n";
    return false;
  }
  return true;
}

bool testAdaptiveProcessingTreeGolden() {
  constexpr int width = 8;
  constexpr int height = 8;
  std::vector<glic::Color> input;
  input.reserve(width * height);
  for (int y = 0; y < height; ++y) {
    for (int x = 0; x < width; ++x) {
      input.push_back(glic::makeColor(
          (x * 17 + y * 5 + ((x ^ y) * 11)) & 255,
          (x * 3 + y * 23 + ((x * y) >> 2)) & 255,
          (x * 29 + y * 7 + ((x + y) * 13)) & 255));
    }
  }
  auto config = makeSupportedConfig();
  for (auto &channel : config.channels) {
    channel.minBlockSize = 2;
    channel.maxBlockSize = 8;
    channel.segmentationPrecision = 40.0f;
    channel.quantizationValue = 0;
    channel.originalWaveletId = 0;
  }

  glic::OriginalRealtimeCpuLane lane;
  std::vector<glic::Color> output(input.size());
  std::string error;
  if (!lane.prepare(width, height, config, error)) {
    std::cerr << "adaptive Processing tree prepare failed: " << error << '\n';
    return false;
  }
  for (const auto expected :
       {std::array<std::size_t, 3>{1, 4, 10},
        std::array<std::size_t, 3>{4, 4, 13}}) {
    glic::OriginalRealtimeFrameStats stats;
    if (!lane.process(input, output, &stats, error) ||
        stats.segmentCounts != expected || output != input) {
      std::cerr << "adaptive Processing tree/RNG golden mismatch: " << error
                << '\n';
      return false;
    }
  }
  return true;
}

std::vector<glic::Color>
slowSingleSegmentReference(const std::vector<glic::Color> &input,
                           const glic::OriginalPresetConfig &config,
                           glic::PredictionMethod predictionMethod,
                           glic::ClampMethod clampMethod) {
  const glic::RefColor reference(
      glic::makeColor(config.borderColorR, config.borderColorG,
                      config.borderColorB),
      config.colorSpace);
  glic::Planes planes(input.data(), 4, 4, config.colorSpace, reference);
  for (int channel = 0; channel < 3; ++channel) {
    glic::Segment segment{.x = 0, .y = 0, .size = 4};
    auto prediction =
        glic::predict(predictionMethod, planes, channel, segment);
    planes.subtract(channel, segment, prediction, clampMethod);
    const float quantization =
        glic::quantValue(config.channels[channel].quantizationValue);
    if (quantization > 0.0f) {
      glic::quantize(planes, channel, segment, quantization, true);
      glic::quantize(planes, channel, segment, quantization, false);
    }
    prediction = glic::predict(predictionMethod, planes, channel, segment);
    planes.add(channel, segment, prediction, clampMethod);
  }
  return planes.toPixels(input.data());
}

bool testEverySupportedPredictorMatchesReference() {
  const auto input = makeFixture();
  for (int prediction = static_cast<int>(glic::PredictionMethod::NONE);
       prediction <= static_cast<int>(glic::PredictionMethod::DIFF);
       ++prediction) {
    for (const auto clamp : {glic::ClampMethod::NONE,
                             glic::ClampMethod::MOD256}) {
      auto config = makeSupportedConfig();
      for (auto &channel : config.channels) {
        channel.predictionMethod =
            static_cast<glic::PredictionMethod>(prediction);
        channel.clampMethod = clamp;
        channel.quantizationValue = 87;
        channel.quantizationStep = 43.5f;
      }

      glic::OriginalRealtimeCpuLane lane;
      std::vector<glic::Color> output(input.size());
      std::string error;
      if (!lane.prepare(4, 4, config, error) ||
          !lane.process(input, output, nullptr, error)) {
        std::cerr << "predictor parity process failed: " << prediction << ' '
                  << error << '\n';
        return false;
      }
      const auto reference = slowSingleSegmentReference(
          input, config, static_cast<glic::PredictionMethod>(prediction),
          clamp);
      if (output != reference) {
        std::cerr << "predictor parity mismatch: " << prediction << " clamp "
                  << static_cast<int>(clamp) << '\n';
        return false;
      }
    }
  }
  return true;
}

bool testUnsupportedConfigurationsFailClosed() {
  std::string error;
  glic::OriginalRealtimeCpuLane lane;

  auto wavelet = makeSupportedConfig();
  wavelet.channels[1].originalWaveletId = 17;
  if (lane.prepare(4, 4, wavelet, error) ||
      error.find("wavelet id 17") == std::string::npos) {
    std::cerr << "wavelet preset did not fail closed: " << error << '\n';
    return false;
  }

  auto search = makeSupportedConfig();
  search.channels[2].predictionMethod = glic::PredictionMethod::SAD;
  if (lane.prepare(4, 4, search, error) ||
      error.find("requires search") == std::string::npos) {
    std::cerr << "search predictor did not fail closed: " << error << '\n';
    return false;
  }

  auto reference = makeSupportedConfig();
  reference.channels[0].predictionMethod = glic::PredictionMethod::REF;
  if (lane.prepare(4, 4, reference, error) ||
      error.find("requires search") == std::string::npos) {
    std::cerr << "reference predictor did not fail closed: " << error << '\n';
    return false;
  }

  auto deadTransformControls = makeSupportedConfig();
  for (auto &channel : deadTransformControls.channels) {
    channel.originalTransformType = -1;
    channel.transformCompress = 255.0f;
    channel.transformScale = 1 << 20;
    channel.encodingMethod = glic::EncodingMethod::RLE;
  }
  if (!lane.prepare(4, 4, deadTransformControls, error)) {
    std::cerr << "dead no-wavelet transform/encoding controls were rejected: "
              << error << '\n';
    return false;
  }
  return true;
}

class Cdf97ReferenceWavelet final : public glic::Wavelet {
public:
  const std::vector<double> &getLowPassDecomposition() const override {
    return scaling_;
  }
  const std::vector<double> &getHighPassDecomposition() const override {
    return wavelet_;
  }
  const std::vector<double> &getLowPassReconstruction() const override {
    return scaling_;
  }
  const std::vector<double> &getHighPassReconstruction() const override {
    return wavelet_;
  }
  std::string getName() const override { return "CDF 9/7 upstream reference"; }
  int getLength() const override { return 9; }

private:
  const std::vector<double> scaling_ = {
      0.026748757411,  -0.016864118443, -0.078223266529,
      0.266864118443, 0.602949018236,  0.266864118443,
      -0.078223266529, -0.016864118443, 0.026748757411,
  };
  const std::vector<double> wavelet_ = {
      0.0,          0.091271763114, -0.057543526229,
      -0.591271763114, 1.11508705,    -0.591271763114,
      -0.057543526229, 0.091271763114, 0.0,
  };
};

std::vector<glic::Color>
slowCdf97Reference(const std::vector<glic::Color> &input,
                   const glic::OriginalPresetConfig &config,
                   glic::TransformType transformType) {
  const glic::RefColor reference(
      glic::makeColor(config.borderColorR, config.borderColorG,
                      config.borderColorB),
      config.colorSpace);
  glic::Planes planes(input.data(), 4, 4, config.colorSpace, reference);
  const auto wavelet = std::make_shared<Cdf97ReferenceWavelet>();
  auto transform = glic::createTransform(transformType, wavelet);
  for (int channel = 0; channel < 3; ++channel) {
    const auto &channelConfig = config.channels[channel];
    glic::Segment segment{.x = 0, .y = 0, .size = 4};
    auto prediction =
        glic::predict(channelConfig.predictionMethod, planes, channel, segment);
    planes.subtract(channel, segment, prediction, channelConfig.clampMethod);
    const float quantization =
        glic::quantValue(channelConfig.quantizationValue);
    if (quantization > 0.0f)
      glic::quantize(planes, channel, segment, quantization, true);

    auto coefficients = transform->forward(planes.getSegment(channel, segment));
    if (channelConfig.transformCompress > 0.0f) {
      double magnitude = 0.0;
      for (const auto &row : coefficients)
        for (const double value : row)
          magnitude += std::abs(value);
      magnitude /= 16.0;
      const double cutoff =
          magnitude * channelConfig.transformCompressionThreshold;
      for (auto &row : coefficients)
        for (double &value : row)
          if (std::abs(value) < cutoff)
            value = 0.0;
    }
    for (int x = 0; x < 4; ++x)
      for (int y = 0; y < 4; ++y)
        planes.set(channel, x, y,
                   glic::processingRound(static_cast<float>(
                       coefficients[x][y] * channelConfig.transformScale /
                       4.0)));

    for (int x = 0; x < 4; ++x)
      for (int y = 0; y < 4; ++y)
        coefficients[x][y] =
            4.0 * planes.get(channel, x, y) /
            static_cast<double>(channelConfig.transformScale);
    coefficients = transform->reverse(coefficients);
    planes.setSegment(channel, segment, coefficients,
                      channelConfig.clampMethod);
    if (quantization > 0.0f)
      glic::quantize(planes, channel, segment, quantization, false);
    prediction =
        glic::predict(channelConfig.predictionMethod, planes, channel, segment);
    planes.add(channel, segment, prediction, channelConfig.clampMethod);
  }
  return planes.toPixels(input.data());
}

bool testCdf97FwtAndWptMatchReference() {
  const auto input = makeFixture();
  for (const auto transformType : {glic::TransformType::FWT,
                                   glic::TransformType::WPT}) {
    for (const auto clamp : {glic::ClampMethod::NONE,
                             glic::ClampMethod::MOD256}) {
      auto config = makeSupportedConfig();
      for (auto &channel : config.channels) {
        channel.predictionMethod = glic::PredictionMethod::PAETH;
        channel.quantizationValue = 87;
        channel.clampMethod = clamp;
        channel.originalWaveletId = 65;
        channel.originalTransformType =
            transformType == glic::TransformType::WPT ? 1 : 0;
        channel.transformCompressControllerValue = 42.0f;
        channel.transformCompress = 42.0f;
        channel.transformCompressionThreshold = 1.3888889f;
        channel.transformScale = 233;
      }

      glic::OriginalRealtimeCpuLane lane;
      std::vector<glic::Color> output(input.size());
      std::string error;
      if (!lane.prepare(4, 4, config, error) ||
          !lane.process(input, output, nullptr, error)) {
        std::cerr << "CDF97 parity process failed: " << error << '\n';
        return false;
      }
      const auto reference = slowCdf97Reference(input, config, transformType);
      if (output != reference) {
        std::cerr << "CDF97 parity mismatch: transform "
                  << static_cast<int>(transformType) << " clamp "
                  << static_cast<int>(clamp) << '\n';
        return false;
      }
    }
  }
  return true;
}

bool testUpstreamSupportedCorpus() {
  const std::set<std::string> expected = {
      "0rg4n1c-___",       "0rg4n1c-t1ny4ngl3z", "0rg4n1c-tr1angl3",
      "0rg4n1c-tr1f0rc3", "0rg4n1c-tr33",       "0rg4n1c-v1n3z",
      "1amblu",           "bi0g4n1c",           "burn",
      "colour_glow",      "default",            "lightblur",
      "vv03",             "vv07",               "vv08",
      "vv10",              "abstract_expressionism", "beautifulwave",
      "bl33dyl1n3z",       "bl33dyl1n3z-2",          "colour_mess",
      "colour_mess2",      "colour_waves_sharp",     "colour_waves_sharp2",
      "colourful_disturbances", "constrctivist_minimal",
      "diagonalcolourbleed", "minimaldiag", "vv01", "vv02", "vv13",
      "vv17", "vv25", "web_p_like", "webp", "wtf", "wtf2",
  };
  std::set<std::string> actual;
  for (const auto &preset :
       glic::PresetLoader::listPresets(GLIC_TEST_PRESETS_DIR)) {
    glic::OriginalPresetConfig config;
    if (!glic::PresetLoader::loadOriginalPresetByName(
            GLIC_TEST_PRESETS_DIR, preset, config)) {
      std::cerr << "failed to decode original preset: " << preset << '\n';
      return false;
    }
    if (glic::evaluateOriginalRealtimeSupport(config).supported)
      actual.insert(preset);
  }
  if (actual != expected) {
    std::cerr << "original fidelity supported corpus mismatch: expected "
              << expected.size() << " got " << actual.size() << '\n';
    return false;
  }
  return true;
}

bool testBufferValidation() {
  const auto config = makeSupportedConfig();
  std::string error;
  glic::OriginalRealtimeCpuLane lane;
  if (!lane.prepare(4, 4, config, error))
    return false;

  const auto input = makeFixture();
  std::vector<glic::Color> shortOutput(15);
  if (lane.process(input, shortOutput, nullptr, error) ||
      error.find("size mismatch") == std::string::npos) {
    std::cerr << "invalid output size was accepted\n";
    return false;
  }
  return true;
}

} // namespace

int main() {
  if (!testProcessingRoundGolden() || !testProcessingRandomGolden() ||
      !testEarlySegmentationMatchesFullSamplingOracle() ||
      !testFixedPredictorQuantization() ||
      !testZeroQuantizationRoundTrip() || !testAdaptiveProcessingTreeGolden() ||
      !testEverySupportedPredictorMatchesReference() ||
      !testUnsupportedConfigurationsFailClosed() ||
      !testCdf97FwtAndWptMatchReference() ||
      !testUpstreamSupportedCorpus() || !testBufferValidation()) {
    return 1;
  }
  std::cout << "PASS original fixed-predictor + exact CDF97 CPU lane\n";
  return 0;
}
