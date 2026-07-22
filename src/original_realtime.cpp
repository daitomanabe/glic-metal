#include "original_realtime.hpp"

#include "colorspaces.hpp"
#include "processing_math.hpp"
#include "segmentation_trace.hpp"
#include "quantization.hpp"

#include <algorithm>
#include <cmath>
#include <exception>
#include <thread>
#include <sstream>

namespace glic {
namespace {

// Coefficients are copied from the CDF97 class in the exact JWave.jar bundled
// by GlitchCodec/GLIC at commit 460e61b. The upstream class deliberately uses
// the same odd-length filters for decomposition and reconstruction.
constexpr std::array<double, 9> kCdf97Scaling = {
    0.026748757411,  -0.016864118443, -0.078223266529,
    0.266864118443, 0.602949018236,  0.266864118443,
    -0.078223266529, -0.016864118443, 0.026748757411,
};
constexpr std::array<double, 9> kCdf97Wavelet = {
    0.0,          0.091271763114, -0.057543526229,
    -0.591271763114, 1.11508705,    -0.591271763114,
    -0.057543526229, 0.091271763114, 0.0,
};

bool isPowerOfTwo(int value) {
  return value > 0 && (value & (value - 1)) == 0;
}

int nextPowerOfTwo(int value) {
  int result = 1;
  while (result < value && result <= (1 << 29))
    result <<= 1;
  return result;
}

int medianOfThree(int a, int b, int c) {
  return std::max(std::min(a, b), std::min(std::max(a, b), c));
}

bool isSupportedFixedPredictor(PredictionMethod method) {
  const int value = static_cast<int>(method);
  // These are the non-search predictors in the upstream Processing codec.
  return value >= static_cast<int>(PredictionMethod::NONE) &&
         value <= static_cast<int>(PredictionMethod::DIFF);
}

bool isSupportedOriginalWavelet(const OriginalPresetChannel &channel) {
  if (channel.originalWaveletId == 0)
    return true;
  return channel.originalWaveletId == 65 &&
         (channel.originalTransformType == 0 ||
          channel.originalTransformType == 1);
}

std::string channelPrefix(std::size_t channel) {
  return "channel " + std::to_string(channel) + ": ";
}

} // namespace

OriginalRealtimeSupport
evaluateOriginalRealtimeSupport(const OriginalPresetConfig &config) {
  OriginalRealtimeSupport support;

  const int colorSpace = static_cast<int>(config.colorSpace);
  if (colorSpace < 0 || colorSpace >= static_cast<int>(ColorSpace::COUNT)) {
    support.reasons.emplace_back("colorspace is outside the upstream range");
  }

  for (std::size_t channel = 0; channel < config.channels.size(); ++channel) {
    const auto &value = config.channels[channel];
    const std::string prefix = channelPrefix(channel);

    if (!isSupportedOriginalWavelet(value)) {
      support.reasons.push_back(
          prefix + "wavelet id " + std::to_string(value.originalWaveletId) +
          " with transform type " +
          std::to_string(value.originalTransformType) +
          " is unsupported by the exact CDF97 fidelity tier");
    }
    if (!isSupportedFixedPredictor(value.predictionMethod)) {
      support.reasons.push_back(
          prefix + "predictor " + predictionName(value.predictionMethod) +
          " requires search or is not an upstream fixed predictor");
    }
    if (!isPowerOfTwo(value.minBlockSize) || value.minBlockSize > 512) {
      support.reasons.push_back(
          prefix + "minimum block size must be a power of two in 1..512");
    }
    if (!isPowerOfTwo(value.maxBlockSize) || value.maxBlockSize > 512) {
      support.reasons.push_back(
          prefix + "maximum block size must be a power of two in 1..512");
    }
    if (value.minBlockSize > value.maxBlockSize) {
      support.reasons.push_back(prefix +
                                "minimum block size exceeds maximum block size");
    }
    if (!std::isfinite(value.segmentationPrecision) ||
        value.segmentationPrecision < 0.0f) {
      support.reasons.push_back(
          prefix + "segmentation precision must be finite and non-negative");
    }
    if (value.quantizationValue < 0 || value.quantizationValue > 255) {
      support.reasons.push_back(prefix + "quantization must be in 0..255");
    }
    if (value.originalWaveletId == 65 && value.transformScale <= 0) {
      support.reasons.push_back(prefix +
                                "CDF97 transform scale must be positive");
    }
    if (!std::isfinite(value.transformCompress) ||
        !std::isfinite(value.transformCompressionThreshold)) {
      support.reasons.push_back(
          prefix + "transform compression values must be finite");
    }
  }

  support.supported = support.reasons.empty();
  return support;
}

OriginalRealtimeCpuLane::~OriginalRealtimeCpuLane() { stopWorkers(); }

void OriginalRealtimeCpuLane::startWorkers() {
  {
    std::lock_guard lock(workerMutex_);
    workerShutdown_ = false;
    pendingWorkers_ = 0;
    workerGeneration_ = 0;
    workerErrors_ = {};
    workerSegmentCounts_ = {};
  }
  for (int channel = 0; channel < 2; ++channel) {
    workers_[static_cast<std::size_t>(channel)] =
        std::thread([this, channel] { workerLoop(channel); });
  }
}

void OriginalRealtimeCpuLane::stopWorkers() noexcept {
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
  {
    std::lock_guard lock(workerMutex_);
    pendingWorkers_ = 0;
    workerErrors_ = {};
  }
}

void OriginalRealtimeCpuLane::workerLoop(int channel) {
  uint64_t observedGeneration = 0;
  while (true) {
    {
      std::unique_lock lock(workerMutex_);
      workerCondition_.wait(lock, [&] {
        return workerShutdown_ || workerGeneration_ != observedGeneration;
      });
      if (workerShutdown_)
        return;
      observedGeneration = workerGeneration_;
    }

    std::size_t segmentCount = 0;
    std::exception_ptr error;
    try {
      segmentCount = processPreparedChannel(channel);
    } catch (...) {
      error = std::current_exception();
    }

    {
      std::lock_guard lock(workerMutex_);
      const auto index = static_cast<std::size_t>(channel);
      workerSegmentCounts_[index] = segmentCount;
      workerErrors_[index] = error;
      --pendingWorkers_;
    }
    workerCompletion_.notify_one();
  }
}

bool OriginalRealtimeCpuLane::prepare(int width, int height,
                                      const OriginalPresetConfig &config,
                                      std::string &error) {
  stopWorkers();
  prepared_ = false;
  error.clear();
  if (width <= 0 || height <= 0) {
    error = "original fidelity lane requires positive dimensions";
    return false;
  }

  const auto support = evaluateOriginalRealtimeSupport(config);
  if (!support.supported) {
    std::ostringstream message;
    message << "unsupported original preset";
    for (std::size_t index = 0; index < support.reasons.size(); ++index) {
      message << (index == 0 ? ": " : "; ") << support.reasons[index];
    }
    error = message.str();
    return false;
  }

  width_ = width;
  height_ = height;
  rootSegmentSize_ = nextPowerOfTwo(std::max(width, height));
  config_ = config;
  const std::size_t pixelCount = static_cast<std::size_t>(width_) *
                                 static_cast<std::size_t>(height_);
  for (auto &plane : planes_)
    plane.assign(pixelCount, 0);
  constexpr std::size_t maximumTransformPixels = 512u * 512u;
  for (auto &matrix : transformMatrices_)
    matrix.assign(maximumTransformPixels, 0.0);
  for (auto &line : transformLines_)
    line.assign(512, 0.0);
  for (auto &scratch : transformScratch_)
    scratch.assign(512, 0.0);
  for (auto &segments : segments_) {
    segments.clear();
    // A 1x1 quadtree can emit at most one in-bounds leaf per source pixel.
    // Reserve that ceiling during prepare so frame processing never grows the
    // segment vectors, including presets outside the current 16-item corpus.
    segments.reserve(pixelCount);
  }
  const Color convertedReference = toColorSpace(
      makeColor(config_.borderColorR, config_.borderColorG,
                config_.borderColorB),
      config_.colorSpace);
  referenceValues_ = {getR(convertedReference), getG(convertedReference),
                      getB(convertedReference)};
  // Processing's randomSeed(42) semantics, shared by channel 0 -> 1 -> 2.
  // The original sketch does not pin a seed; the deterministic seed here makes
  // tests and video renders reproducible while retaining its exact RNG family
  // and cross-channel consumption order.
  segmentationRng_.setSeed(42);
  try {
    startWorkers();
  } catch (const std::exception &exception) {
    stopWorkers();
    error = std::string("failed to start original fidelity workers: ") +
            exception.what();
    return false;
  }
  prepared_ = true;
  return true;
}

int OriginalRealtimeCpuLane::planeValue(int channel, int x,
                                        int y) const noexcept {
  if (x < 0 || x >= width_ || y < 0 || y >= height_)
    return referenceValues_[static_cast<std::size_t>(channel)];
  return planes_[static_cast<std::size_t>(channel)]
                [static_cast<std::size_t>(y) *
                     static_cast<std::size_t>(width_) +
                 static_cast<std::size_t>(x)];
}

bool OriginalRealtimeCpuLane::sampledStandardDeviationExceeds(
    int channel, int x, int y, int size, float threshold) {
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
  const unsigned positionMagnitude =
      std::countr_zero(static_cast<unsigned>(size));
  for (int sample = 1; sample <= limit; ++sample) {
    const int value = planeValue(
        channel,
        x + segmentationRng_.nextPowerOfTwoPosition(positionMagnitude),
        y + segmentationRng_.nextPowerOfTwoPosition(positionMagnitude));
    const float oldAverage = average;
    average += (static_cast<float>(value) - average) /
               static_cast<float>(sample);
    sum += (static_cast<float>(value) - oldAverage) *
           (static_cast<float>(value) - average);
    if (sample == nextDecisionSample || sample == limit) {
      // Welford's accumulated sum only increases for these bounded integer
      // samples. Once the final-denominator deviation exceeds the threshold,
      // later samples cannot turn this split back into a leaf. Preserve the
      // exact Processing RNG state while skipping only dead arithmetic/reads.
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

void OriginalRealtimeCpuLane::skipUnusedDeviationSamples(int size) noexcept {
  const int limit = std::max(
      static_cast<int>(0.1f * static_cast<float>(size) *
                       static_cast<float>(size)),
      4);
  segmentationRng_.discardNextFloats(static_cast<std::uint64_t>(limit) * 2u);
}

std::uint64_t
OriginalRealtimeCpuLane::fixedSegmentationRandomDraws(int blockSize) const
    noexcept {
  std::uint64_t draws = 0;
  for (int size = rootSegmentSize_;; size >>= 1) {
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

void OriginalRealtimeCpuLane::emitFixedSegments(
    int x, int y, int size, int blockSize,
    std::vector<WorkingSegment> &segments) {
  if (x >= width_ || y >= height_)
    return;
  if (size > blockSize) {
    const int half = size / 2;
    emitFixedSegments(x, y, half, blockSize, segments);
    emitFixedSegments(x + half, y, half, blockSize, segments);
    emitFixedSegments(x, y + half, half, blockSize, segments);
    emitFixedSegments(x + half, y + half, half, blockSize, segments);
    return;
  }
  segments.push_back({x, y, size});
}

void OriginalRealtimeCpuLane::segmentChannel(
    int channel, int x, int y, int size, int minSize, int maxSize,
    float threshold, std::vector<WorkingSegment> &segments) {
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
    segmentChannel(channel, x, y, half, minSize, maxSize, threshold,
                   segments);
    segmentChannel(channel, x + half, y, half, minSize, maxSize, threshold,
                   segments);
    segmentChannel(channel, x, y + half, half, minSize, maxSize, threshold,
                   segments);
    segmentChannel(channel, x + half, y + half, half, minSize, maxSize,
                   threshold, segments);
  } else {
    segments.push_back({x, y, size});
  }
}

void OriginalRealtimeCpuLane::prepareChannelSegments(int channel) {
  const auto &channelConfig = config_.channels[channel];
  auto &segments = segments_[static_cast<std::size_t>(channel)];
  segments.clear();
  if (channelConfig.minBlockSize == channelConfig.maxBlockSize) {
    segmentationRng_.discardNextFloats(
        fixedSegmentationRandomDraws(channelConfig.minBlockSize));
    emitFixedSegments(0, 0, rootSegmentSize_, channelConfig.minBlockSize,
                      segments);
    return;
  }
  segmentChannel(channel, 0, 0, rootSegmentSize_,
                 channelConfig.minBlockSize, channelConfig.maxBlockSize,
                 channelConfig.segmentationPrecision, segments);
}

int OriginalRealtimeCpuLane::predictionValue(
    PredictionMethod method, int channel, const WorkingSegment &segment, int x,
    int y, int dcValue, int cornerValue) const noexcept {
  const int top = planeValue(channel, segment.x + x, segment.y - 1);
  const int left = planeValue(channel, segment.x - 1, segment.y + y);

  switch (method) {
  case PredictionMethod::CORNER:
    return cornerValue;
  case PredictionMethod::H:
    return left;
  case PredictionMethod::V:
    return top;
  case PredictionMethod::DC:
    return dcValue;
  case PredictionMethod::DCMEDIAN:
    return medianOfThree(dcValue, top, left);
  case PredictionMethod::MEDIAN:
    return medianOfThree(cornerValue, top, left);
  case PredictionMethod::AVG:
    return (top + left) >> 1;
  case PredictionMethod::TRUEMOTION:
    return std::clamp(top + left - cornerValue, 0, 255);
  case PredictionMethod::PAETH: {
    const int estimate = top + left - cornerValue;
    const int distanceToLeft = std::abs(estimate - left);
    const int distanceToTop = std::abs(estimate - top);
    const int distanceToCorner = std::abs(estimate - cornerValue);
    return std::clamp((distanceToLeft <= distanceToTop &&
                       distanceToLeft <= distanceToCorner)
                          ? left
                          : (distanceToTop <= distanceToCorner ? top
                                                               : cornerValue),
                      0, 255);
  }
  case PredictionMethod::LDIAG: {
    const int diagonal = x + y;
    const int topValue = planeValue(
        channel,
        segment.x +
            (diagonal + 1 < segment.size ? diagonal + 1 : segment.size - 1),
        segment.y - 1);
    const int leftValue = planeValue(
        channel, segment.x - 1,
        segment.y +
            (diagonal < segment.size ? diagonal : segment.size - 1));
    return ((x + 1) * topValue + (y + 1) * leftValue) / (x + y + 2);
  }
  case PredictionMethod::HV:
    if (x > y)
      return top;
    if (y > x)
      return left;
    return (top + left) >> 1;
  case PredictionMethod::JPEGLS: {
    const int upperLeft =
        planeValue(channel, segment.x + x - 1, segment.y - 1);
    if (upperLeft >= std::max(top, left))
      return std::min(top, left);
    if (upperLeft <= std::min(top, left))
      return std::max(top, left);
    return top + left - upperLeft;
  }
  case PredictionMethod::DIFF: {
    const int top2 = planeValue(channel, segment.x + x, segment.y - 2);
    const int left2 = planeValue(channel, segment.x - 2, segment.y + y);
    return std::clamp((left2 + left2 - left + top2 + top2 - top) >> 1,
                      0, 255);
  }
  default:
    return 0;
  }
}

void OriginalRealtimeCpuLane::cdf97Step(double *data, int offset, int size,
                                        bool reverse,
                                        double *scratch) noexcept {
  std::fill_n(scratch, size, 0.0);
  const int half = size >> 1;
  const int wrapMask = size - 1;
  if (!reverse) {
    for (int coefficient = 0; coefficient < half; ++coefficient) {
      for (std::size_t tap = 0; tap < kCdf97Scaling.size(); ++tap) {
        // Segment sizes are certified powers of two, so this is exactly the
        // JWave modulo with substantially less cost for the very common 2x2
        // and 4x4 preset blocks.
        const int source = offset +
                           ((coefficient * 2 + static_cast<int>(tap)) &
                            wrapMask);
        scratch[coefficient] += data[source] * kCdf97Scaling[tap];
        scratch[coefficient + half] += data[source] * kCdf97Wavelet[tap];
      }
    }
  } else {
    for (int coefficient = 0; coefficient < half; ++coefficient) {
      for (std::size_t tap = 0; tap < kCdf97Scaling.size(); ++tap) {
        const int destination =
            (coefficient * 2 + static_cast<int>(tap)) & wrapMask;
        scratch[destination] +=
            data[offset + coefficient] * kCdf97Scaling[tap] +
            data[offset + coefficient + half] * kCdf97Wavelet[tap];
      }
    }
  }
  std::copy_n(scratch, size, data + offset);
}

void OriginalRealtimeCpuLane::transformCdf97Line(
    double *data, int size, TransformType transformType, bool reverse,
    double *scratch) noexcept {
  if (transformType == TransformType::FWT) {
    if (!reverse) {
      for (int length = size; length >= 2; length >>= 1)
        cdf97Step(data, 0, length, false, scratch);
    } else {
      for (int length = 2; length <= size; length <<= 1)
        cdf97Step(data, 0, length, true, scratch);
    }
    return;
  }

  // JWave's WPT transforms every packet at a level, not only the leading
  // approximation packet used by FWT.
  if (!reverse) {
    for (int length = size; length >= 2; length >>= 1) {
      const int packets = size / length;
      for (int packet = 0; packet < packets; ++packet)
        cdf97Step(data, packet * length, length, false, scratch);
    }
  } else {
    for (int length = 2; length <= size; length <<= 1) {
      const int packets = size / length;
      for (int packet = 0; packet < packets; ++packet)
        cdf97Step(data, packet * length, length, true, scratch);
    }
  }
}

void OriginalRealtimeCpuLane::transformCdf97(int channel, int size,
                                             TransformType transformType,
                                             bool reverse) noexcept {
  auto &matrix = transformMatrices_[static_cast<std::size_t>(channel)];
  auto &line = transformLines_[static_cast<std::size_t>(channel)];
  auto &scratch = transformScratch_[static_cast<std::size_t>(channel)];

  if (!reverse) {
    for (int x = 0; x < size; ++x) {
      transformCdf97Line(matrix.data() + static_cast<std::size_t>(x) * size,
                         size, transformType, false, scratch.data());
    }
    for (int y = 0; y < size; ++y) {
      for (int x = 0; x < size; ++x)
        line[static_cast<std::size_t>(x)] =
            matrix[static_cast<std::size_t>(x) * size + y];
      transformCdf97Line(line.data(), size, transformType, false,
                         scratch.data());
      for (int x = 0; x < size; ++x)
        matrix[static_cast<std::size_t>(x) * size + y] =
            line[static_cast<std::size_t>(x)];
    }
  } else {
    for (int y = 0; y < size; ++y) {
      for (int x = 0; x < size; ++x)
        line[static_cast<std::size_t>(x)] =
            matrix[static_cast<std::size_t>(x) * size + y];
      transformCdf97Line(line.data(), size, transformType, true,
                         scratch.data());
      for (int x = 0; x < size; ++x)
        matrix[static_cast<std::size_t>(x) * size + y] =
            line[static_cast<std::size_t>(x)];
    }
    for (int x = 0; x < size; ++x) {
      transformCdf97Line(matrix.data() + static_cast<std::size_t>(x) * size,
                         size, transformType, true, scratch.data());
    }
  }
}

void OriginalRealtimeCpuLane::processCdf97Segment(
    int channel, const WorkingSegment &segment, int dcValue, int cornerValue,
    float quantization) {
  const auto &channelConfig = config_.channels[channel];
  auto &plane = planes_[static_cast<std::size_t>(channel)];
  auto &matrix = transformMatrices_[static_cast<std::size_t>(channel)];
  const int size = segment.size;
  const int usedWidth = std::min(size, width_ - segment.x);
  const int usedHeight = std::min(size, height_ - segment.y);

  // Upstream mutates only in-bounds Plane cells. Its padded portion therefore
  // reads back as the configured border value when the transform matrix is
  // assembled.
  for (int x = 0; x < usedWidth; ++x) {
    for (int y = 0; y < usedHeight; ++y) {
      const int prediction = predictionValue(
          channelConfig.predictionMethod, channel, segment, x, y, dcValue,
          cornerValue);
      const std::size_t planeIndex =
          static_cast<std::size_t>(segment.y + y) * width_ + segment.x + x;
      int residual = plane[planeIndex] - prediction;
      if (channelConfig.clampMethod == ClampMethod::MOD256) {
        residual = residual < 0     ? residual + 256
                   : residual > 255 ? residual - 256
                                    : residual;
      }
      if (quantization > 1.0f)
        residual =
            processingRound(static_cast<float>(residual) / quantization);
      plane[planeIndex] = residual;
    }
  }

  const double border =
      static_cast<double>(referenceValues_[static_cast<std::size_t>(channel)]) /
      255.0;
  for (int x = 0; x < size; ++x) {
    for (int y = 0; y < size; ++y) {
      const std::size_t matrixIndex = static_cast<std::size_t>(x) * size + y;
      if (x < usedWidth && y < usedHeight) {
        const std::size_t planeIndex =
            static_cast<std::size_t>(segment.y + y) * width_ + segment.x + x;
        matrix[matrixIndex] = static_cast<double>(plane[planeIndex]) / 255.0;
      } else {
        matrix[matrixIndex] = border;
      }
    }
  }

  const TransformType transformType = channelConfig.originalTransformType == 1
                                          ? TransformType::WPT
                                          : TransformType::FWT;
  transformCdf97(channel, size, transformType, false);

  if (channelConfig.transformCompress > 0.0f) {
    const std::size_t coefficientCount =
        static_cast<std::size_t>(size) * static_cast<std::size_t>(size);
    double magnitude = 0.0;
    for (std::size_t index = 0; index < coefficientCount; ++index)
      magnitude += std::abs(matrix[index]);
    magnitude /= static_cast<double>(coefficientCount);
    const double cutoff = magnitude *
                          static_cast<double>(
                              channelConfig.transformCompressionThreshold);
    for (std::size_t index = 0; index < coefficientCount; ++index) {
      if (std::abs(matrix[index]) < cutoff)
        matrix[index] = 0.0;
    }
  }

  const double scale = static_cast<double>(channelConfig.transformScale);
  for (int x = 0; x < usedWidth; ++x) {
    for (int y = 0; y < usedHeight; ++y) {
      const std::size_t planeIndex =
          static_cast<std::size_t>(segment.y + y) * width_ + segment.x + x;
      // codec.pde narrows the transformed double coefficient to Processing
      // float before round(). Preserve that boundary instead of treating the
      // CPU double result as the upstream rounding input.
      plane[planeIndex] = processingRound(static_cast<float>(
          matrix[static_cast<std::size_t>(x) * size + y] * scale / size));
    }
  }

  for (int x = 0; x < size; ++x) {
    for (int y = 0; y < size; ++y) {
      const std::size_t matrixIndex = static_cast<std::size_t>(x) * size + y;
      if (x < usedWidth && y < usedHeight) {
        const std::size_t planeIndex =
            static_cast<std::size_t>(segment.y + y) * width_ + segment.x + x;
        matrix[matrixIndex] = size * static_cast<double>(plane[planeIndex]) /
                              scale;
      } else {
        matrix[matrixIndex] = border;
      }
    }
  }
  transformCdf97(channel, size, transformType, true);

  for (int x = 0; x < usedWidth; ++x) {
    for (int y = 0; y < usedHeight; ++y) {
      const std::size_t planeIndex =
          static_cast<std::size_t>(segment.y + y) * width_ + segment.x + x;
      // Planes.setSegment likewise performs its final round in float.
      int value = processingRound(static_cast<float>(
          matrix[static_cast<std::size_t>(x) * size + y] * 255.0));
      if (channelConfig.clampMethod == ClampMethod::MOD256)
        value = std::clamp(value, 0, 255);
      else
        value = std::clamp(value, -255, 255);
      if (quantization > 1.0f)
        value = processingRound(static_cast<float>(value) * quantization);
      plane[planeIndex] = value;
    }
  }

  // Re-evaluate the fixed predictor after lossy reconstruction, matching
  // codec.pde rather than reusing a per-segment prediction matrix.
  for (int x = 0; x < usedWidth; ++x) {
    for (int y = 0; y < usedHeight; ++y) {
      const int prediction = predictionValue(
          channelConfig.predictionMethod, channel, segment, x, y, dcValue,
          cornerValue);
      const std::size_t planeIndex =
          static_cast<std::size_t>(segment.y + y) * width_ + segment.x + x;
      int reconstructed = plane[planeIndex] + prediction;
      if (channelConfig.clampMethod == ClampMethod::MOD256) {
        reconstructed = reconstructed < 0     ? reconstructed + 256
                        : reconstructed > 255 ? reconstructed - 256
                                              : reconstructed;
      } else {
        reconstructed = std::clamp(reconstructed, 0, 255);
      }
      plane[planeIndex] = reconstructed;
    }
  }
}

std::size_t OriginalRealtimeCpuLane::processPreparedChannel(int channel) {
  const auto &channelConfig = config_.channels[channel];
  auto &segments = segments_[static_cast<std::size_t>(channel)];

  const float quantization = quantValue(channelConfig.quantizationValue);
  auto &plane = planes_[static_cast<std::size_t>(channel)];
  for (const auto &segment : segments) {
    const int corner = planeValue(channel, segment.x - 1, segment.y - 1);
    int dc = 0;
    if (channelConfig.predictionMethod == PredictionMethod::DC ||
        channelConfig.predictionMethod == PredictionMethod::DCMEDIAN) {
      for (int offset = 0; offset < segment.size; ++offset) {
        dc += planeValue(channel, segment.x - 1, segment.y + offset);
        dc += planeValue(channel, segment.x + offset, segment.y - 1);
      }
      dc += corner;
      dc /= segment.size + segment.size + 1;
    }

    if (channelConfig.originalWaveletId == 65) {
      processCdf97Segment(channel, segment, dc, corner, quantization);
      continue;
    }

    const int usedWidth = std::min(segment.size, width_ - segment.x);
    const int usedHeight = std::min(segment.size, height_ - segment.y);
    for (int y = 0; y < usedHeight; ++y) {
      const std::size_t row =
          static_cast<std::size_t>(segment.y + y) *
          static_cast<std::size_t>(width_);
      for (int x = 0; x < usedWidth; ++x) {
        const int prediction = predictionValue(
            channelConfig.predictionMethod, channel, segment, x, y, dc,
            corner);
        const std::size_t index =
            row + static_cast<std::size_t>(segment.x + x);
        int residual = plane[index] - prediction;
        if (channelConfig.clampMethod == ClampMethod::MOD256) {
          residual = residual < 0   ? residual + 256
                     : residual > 255 ? residual - 256
                                      : residual;
        }
        if (quantization > 1.0f) {
          residual = processingRound(
              static_cast<float>(processingRound(
                  static_cast<float>(residual) / quantization)) *
              quantization);
        }
        int reconstructed = residual + prediction;
        if (channelConfig.clampMethod == ClampMethod::MOD256) {
          reconstructed = reconstructed < 0   ? reconstructed + 256
                          : reconstructed > 255 ? reconstructed - 256
                                                : reconstructed;
        } else {
          reconstructed = std::clamp(reconstructed, 0, 255);
        }
        plane[index] = reconstructed;
      }
    }
  }
  return segments.size();
}

bool OriginalRealtimeCpuLane::process(std::span<const Color> input,
                                      std::span<Color> output,
                                      OriginalRealtimeFrameStats *stats,
                                      std::string &error) {
  error.clear();
  if (!prepared_) {
    error = "original fidelity lane is not prepared";
    return false;
  }

  const std::size_t required = static_cast<std::size_t>(width_) *
                               static_cast<std::size_t>(height_);
  if (input.size() != required || output.size() != required) {
    error = "original fidelity lane input/output size mismatch";
    return false;
  }

  ProcessingRandomCheckpoint rngCheckpoint(segmentationRng_);
  try {
    for (std::size_t index = 0; index < required; ++index) {
      const Color converted = toColorSpace(input[index], config_.colorSpace);
      planes_[0][index] = getR(converted);
      planes_[1][index] = getG(converted);
      planes_[2][index] = getB(converted);
    }

    OriginalRealtimeFrameStats frameStats;
    earlyTerminatedNodes_ = 0;
    earlySkippedSamples_ = 0;
    // Upstream consumes one Processing RNG in channel order. Segmentation is
    // kept sequential for that small fidelity-critical stage; reconstruction
    // below remains parallel across all three channels.
    for (int channel = 0; channel < 3; ++channel)
      prepareChannelSegments(channel);
    frameStats.segmentationRngState = segmentationRng_.state();
    frameStats.segmentOrderFnv1a64 = kSegmentationTraceFnvOffset;
    for (std::size_t channel = 0; channel < segments_.size(); ++channel) {
      for (const auto &segment : segments_[channel]) {
        appendSegmentationTraceLeaf(frameStats.segmentOrderFnv1a64,
                                    static_cast<int>(channel), segment.x,
                                    segment.y, segment.size);
      }
    }
    frameStats.earlyTerminatedNodes = earlyTerminatedNodes_;
    frameStats.earlySkippedSamples = earlySkippedSamples_;
    {
      std::lock_guard lock(workerMutex_);
      workerErrors_ = {};
      pendingWorkers_ = 2;
      ++workerGeneration_;
    }
    workerCondition_.notify_all();

    std::exception_ptr mainChannelError;
    try {
      frameStats.segmentCounts[2] = processPreparedChannel(2);
    } catch (...) {
      mainChannelError = std::current_exception();
    }

    {
      std::unique_lock lock(workerMutex_);
      workerCompletion_.wait(lock, [&] { return pendingWorkers_ == 0; });
      frameStats.segmentCounts[0] = workerSegmentCounts_[0];
      frameStats.segmentCounts[1] = workerSegmentCounts_[1];
      for (const auto &workerError : workerErrors_) {
        if (workerError)
          std::rethrow_exception(workerError);
      }
    }
    if (mainChannelError)
      std::rethrow_exception(mainChannelError);

    for (std::size_t index = 0; index < required; ++index) {
      output[index] = fromColorSpace(
          processingPackPlanes(planes_[0][index], planes_[1][index],
                               planes_[2][index], input[index]),
          config_.colorSpace);
    }
    if (stats)
      *stats = frameStats;
    rngCheckpoint.commit();
    return true;
  } catch (const std::exception &exception) {
    error = std::string("original fidelity processing failed: ") +
            exception.what();
  } catch (...) {
    error = "original fidelity processing failed with an unknown exception";
  }
  return false;
}

} // namespace glic
