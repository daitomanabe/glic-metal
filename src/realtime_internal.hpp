#pragma once

#include "realtime.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>

namespace glic::realtime {

inline uint32_t hash32(uint32_t value) noexcept {
  value ^= value >> 16;
  value *= 0x7feb352du;
  value ^= value >> 15;
  value *= 0x846ca68bu;
  value ^= value >> 16;
  return value;
}

inline uint32_t pixelHash(int x, int y, int channel, uint64_t frameIndex,
                          uint32_t seed) noexcept {
  uint32_t value = seed;
  value ^= static_cast<uint32_t>(x) * 0x9e3779b9u;
  value ^= static_cast<uint32_t>(y) * 0x85ebca6bu;
  value ^= static_cast<uint32_t>(channel) * 0xc2b2ae35u;
  value ^= static_cast<uint32_t>(frameIndex) * 0x27d4eb2du;
  return hash32(value);
}

inline int clampByte(int value) noexcept { return std::clamp(value, 0, 255); }

inline int wrapByte(int value) noexcept {
  value %= 256;
  return value < 0 ? value + 256 : value;
}

inline int median3(int a, int b, int c) noexcept {
  return std::max(std::min(a, b), std::min(std::max(a, b), c));
}

inline int paeth(int left, int top, int corner) noexcept {
  const int candidate = left + top - corner;
  const int distanceLeft = std::abs(candidate - left);
  const int distanceTop = std::abs(candidate - top);
  const int distanceCorner = std::abs(candidate - corner);
  if (distanceLeft <= distanceTop && distanceLeft <= distanceCorner)
    return left;
  if (distanceTop <= distanceCorner)
    return top;
  return corner;
}

inline int normalizeBlockSize(int value) noexcept {
  value = std::clamp(value, 1, 256);
  int result = 1;
  while (result < value && result < 256)
    result <<= 1;
  return result;
}

inline float waveletGain(WaveletType type) noexcept {
  switch (type) {
  case WaveletType::NONE:
    return 0.0f;
  case WaveletType::HAAR:
  case WaveletType::HAAR_ORTHOGONAL:
    return 0.28f;
  case WaveletType::COIFLET1:
  case WaveletType::COIFLET2:
  case WaveletType::COIFLET3:
  case WaveletType::COIFLET4:
  case WaveletType::COIFLET5:
    return 0.42f;
  case WaveletType::SYMLET2:
  case WaveletType::SYMLET3:
  case WaveletType::SYMLET4:
    return 0.36f;
  default:
    return 0.5f;
  }
}

struct MetalChannelUniform {
  uint32_t minBlockSize = 1;
  uint32_t maxBlockSize = 1;
  int32_t predictionMethod = 0;
  uint32_t quantizationValue = 0;

  uint32_t waveletType = 0;
  uint32_t transformType = 0;
  uint32_t clampMethod = 0;
  int32_t transformScale = 0;

  float segmentationPrecision = 0.0f;
  float transformCompress = 0.0f;
  float waveletStrength = 0.0f;
  uint32_t encodingMethod = 0;
};

static_assert(sizeof(MetalChannelUniform) == 48);

struct alignas(16) MetalPresetUniform {
  uint32_t width = 0;
  uint32_t height = 0;
  uint32_t colorSpace = 0;
  uint32_t seed = 0;

  float borderR = 0.5f;
  float borderG = 0.5f;
  float borderB = 0.5f;
  float effectStrength = 1.0f;

  std::array<MetalChannelUniform, 3> channels{};
};

static_assert(sizeof(MetalPresetUniform) == 176);

inline MetalPresetUniform
makeMetalPresetUniform(const RealtimePrepareOptions &options) {
  MetalPresetUniform uniform;
  uniform.width = static_cast<uint32_t>(options.width);
  uniform.height = static_cast<uint32_t>(options.height);
  uniform.colorSpace = static_cast<uint32_t>(options.config.colorSpace);
  uniform.seed = options.seed;
  uniform.borderR = options.config.borderColorR / 255.0f;
  uniform.borderG = options.config.borderColorG / 255.0f;
  uniform.borderB = options.config.borderColorB / 255.0f;
  uniform.effectStrength = std::clamp(options.effectStrength, 0.0f, 2.0f);

  for (size_t i = 0; i < uniform.channels.size(); ++i) {
    const auto &source = options.config.channels[i];
    auto &destination = uniform.channels[i];
    destination.minBlockSize =
        static_cast<uint32_t>(normalizeBlockSize(source.minBlockSize));
    destination.maxBlockSize =
        static_cast<uint32_t>(normalizeBlockSize(source.maxBlockSize));
    destination.predictionMethod =
        static_cast<int32_t>(source.predictionMethod);
    destination.quantizationValue =
        static_cast<uint32_t>(std::clamp(source.quantizationValue, 0, 255));
    destination.waveletType = static_cast<uint32_t>(source.waveletType);
    destination.transformType = static_cast<uint32_t>(source.transformType);
    destination.clampMethod = static_cast<uint32_t>(source.clampMethod);
    destination.transformScale = source.transformScale;
    destination.segmentationPrecision = source.segmentationPrecision;
    destination.transformCompress = source.transformCompress;
    destination.waveletStrength = waveletGain(source.waveletType);
    destination.encodingMethod = static_cast<uint32_t>(source.encodingMethod);
  }
  return uniform;
}

} // namespace glic::realtime
