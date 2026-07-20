#include "original_realtime.hpp"
#include "planes.hpp"
#include "prediction.hpp"
#include "quantization.hpp"
#include "segment.hpp"
#include "wavelet.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <set>
#include <string>
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
  const int quantized =
      static_cast<int>(std::round(std::round(residual / divisor) * divisor));
  return std::clamp(quantized + reference, 0, 255);
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
                   static_cast<int>(std::round(
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
  if (!testFixedPredictorQuantization() ||
      !testZeroQuantizationRoundTrip() ||
      !testEverySupportedPredictorMatchesReference() ||
      !testUnsupportedConfigurationsFailClosed() ||
      !testCdf97FwtAndWptMatchReference() ||
      !testUpstreamSupportedCorpus() || !testBufferValidation()) {
    return 1;
  }
  std::cout << "PASS original fixed-predictor + exact CDF97 CPU lane\n";
  return 0;
}
