#include "realtime_cpu.hpp"

#include "colorspaces.hpp"
#include "realtime_internal.hpp"

#include <algorithm>
#include <array>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <span>
#include <string>
#include <thread>
#include <vector>

namespace glic {
namespace {

class CpuRealtimeBackend final : public RealtimeBackend {
public:
  CpuRealtimeBackend() {
    for (int channel = 0; channel < 3; ++channel) {
      workers_[static_cast<size_t>(channel)] =
          std::thread([this, channel] { workerLoop(channel); });
    }
  }

  ~CpuRealtimeBackend() override {
    {
      std::lock_guard lock(mutex_);
      stopping_ = true;
      ++generation_;
    }
    startCondition_.notify_all();
    for (auto &worker : workers_) {
      if (worker.joinable())
        worker.join();
    }
  }

  bool prepare(const RealtimePrepareOptions &options,
               std::string &error) override {
    if (options.width <= 0 || options.height <= 0) {
      error = "Realtime dimensions must be positive";
      return false;
    }

    std::unique_lock lock(mutex_);
    doneCondition_.wait(
        lock, [this] { return completedWorkers_ == 3 || generation_ == 0; });

    options_ = options;
    uniform_ = realtime::makeMetalPresetUniform(options);
    const size_t pixelCount = static_cast<size_t>(options.width) *
                              static_cast<size_t>(options.height);
    sourcePlanes_.resize(pixelCount * 3);
    outputPlanes_.resize(pixelCount * 3);
    prepared_ = true;
    error.clear();
    return true;
  }

  bool process(std::span<const Color> input, std::span<Color> output,
               uint64_t frameIndex, std::string &error) override {
    if (!prepared_) {
      error = "CPU realtime backend is not prepared";
      return false;
    }

    const size_t pixelCount = static_cast<size_t>(options_.width) *
                              static_cast<size_t>(options_.height);
    if (input.size() != pixelCount || output.size() != pixelCount) {
      error =
          "Realtime input/output span size does not match prepared dimensions";
      return false;
    }
    if (options_.effectStrength <= 0.0f) {
      std::copy(input.begin(), input.end(), output.begin());
      lastStats_.frameIndex = frameIndex;
      lastStats_.gpuMilliseconds = 0.0;
      error.clear();
      return true;
    }

    const bool directRgbEffect =
        uniform_.effectFamily !=
        static_cast<uint32_t>(RealtimeEffectFamily::LEGACY_BLOCK);

    // Explicit realtime effect families are RGB mechanisms and intentionally
    // ignore the legacy codec preset's colour space. This keeps the effect
    // portable across presets and makes the CPU and Metal paths equivalent.
    // Channel workers still operate on three contiguous slabs after this
    // conversion, avoiding nested-vector pointer chasing.
    for (size_t index = 0; index < pixelCount; ++index) {
      const Color converted = directRgbEffect
                                  ? input[index]
                                  : toColorSpace(input[index],
                                                 options_.config.colorSpace);
      sourcePlanes_[index] = static_cast<int16_t>(getR(converted));
      sourcePlanes_[pixelCount + index] = static_cast<int16_t>(getG(converted));
      sourcePlanes_[pixelCount * 2 + index] =
          static_cast<int16_t>(getB(converted));
    }

    {
      std::lock_guard lock(mutex_);
      frameIndex_ = frameIndex;
      completedWorkers_ = 0;
      ++generation_;
    }
    startCondition_.notify_all();

    {
      std::unique_lock lock(mutex_);
      doneCondition_.wait(lock, [this] { return completedWorkers_ == 3; });
    }

    for (size_t index = 0; index < pixelCount; ++index) {
      const Color converted =
          makeColor(static_cast<uint8_t>(outputPlanes_[index]),
                    static_cast<uint8_t>(outputPlanes_[pixelCount + index]),
                    static_cast<uint8_t>(outputPlanes_[pixelCount * 2 + index]),
                    getA(input[index]));
      output[index] = directRgbEffect
                          ? converted
                          : fromColorSpace(converted,
                                           options_.config.colorSpace);
    }

    lastStats_.frameIndex = frameIndex;
    lastStats_.gpuMilliseconds = 0.0;
    error.clear();
    return true;
  }

  const char *name() const noexcept override { return "cpu-parallel"; }
  bool isHardwareAccelerated() const noexcept override { return false; }
  RealtimeFrameStats lastFrameStats() const noexcept override {
    return lastStats_;
  }

private:
  int source(int channel, int x, int y) const noexcept {
    if (x < 0 || y < 0 || x >= options_.width || y >= options_.height) {
      switch (channel) {
      case 0:
        return options_.config.borderColorR;
      case 1:
        return options_.config.borderColorG;
      default:
        return options_.config.borderColorB;
      }
    }
    const size_t pixelCount = static_cast<size_t>(options_.width) *
                              static_cast<size_t>(options_.height);
    const size_t index =
        static_cast<size_t>(y) * static_cast<size_t>(options_.width) +
        static_cast<size_t>(x);
    return sourcePlanes_[static_cast<size_t>(channel) * pixelCount + index];
  }

  int sourceWrapped(int channel, int x, int y) const noexcept {
    const int wrappedX =
        ((x % options_.width) + options_.width) % options_.width;
    const int wrappedY =
        ((y % options_.height) + options_.height) % options_.height;
    return source(channel, wrappedX, wrappedY);
  }

  int predictor(PredictionMethod requested, int channel, int x, int y,
                int originX, int originY, int blockSize, int current,
                uint64_t frameIndex) const noexcept {
    const int localX = x - originX;
    const int localY = y - originY;
    const int left = source(channel, originX - 1, y);
    const int top = source(channel, x, originY - 1);
    const int corner = source(channel, originX - 1, originY - 1);
    const int top2 = source(channel, x, originY - 2);
    const int left2 = source(channel, originX - 2, y);

    PredictionMethod method = requested;
    if (method == PredictionMethod::RANDOM) {
      constexpr int methodCount = 16;
      method = static_cast<PredictionMethod>(
          realtime::pixelHash(originX, originY, channel, frameIndex,
                              options_.seed) %
          methodCount);
    }

    switch (method) {
    case PredictionMethod::NONE:
      return 0;
    case PredictionMethod::CORNER:
      return corner;
    case PredictionMethod::H:
      return left;
    case PredictionMethod::V:
      return top;
    case PredictionMethod::DC:
      return (left + top + corner) / 3;
    case PredictionMethod::DCMEDIAN:
    case PredictionMethod::MEDIAN:
      return realtime::median3(left, top, corner);
    case PredictionMethod::AVG:
      return (left + top) >> 1;
    case PredictionMethod::TRUEMOTION:
      return realtime::clampByte(left + top - corner);
    case PredictionMethod::PAETH:
      return realtime::paeth(left, top, corner);
    case PredictionMethod::LDIAG: {
      const int sum = localX + localY;
      const int topSample = source(
          channel, originX + std::min(sum + 1, blockSize - 1), originY - 1);
      const int leftSample =
          source(channel, originX - 1, originY + std::min(sum, blockSize - 1));
      return ((localX + 1) * topSample + (localY + 1) * leftSample) /
             std::max(1, localX + localY + 2);
    }
    case PredictionMethod::HV:
      return localX > localY ? top
                             : (localY > localX ? left : ((left + top) >> 1));
    case PredictionMethod::JPEGLS:
      if (corner >= std::max(left, top))
        return std::min(left, top);
      else if (corner <= std::min(left, top))
        return std::max(left, top);
      return left + top - corner;
    case PredictionMethod::DIFF:
      return realtime::clampByte((left2 + left2 - left + top2 + top2 - top) >>
                                 1);
    case PredictionMethod::REF: {
      const uint32_t hash = realtime::pixelHash(originX, originY, channel,
                                                frameIndex / 4, options_.seed);
      const int blocksBack = 1 + static_cast<int>(hash % 4u);
      const int refX = originX - blocksBack * blockSize + localX;
      const int refY = originY - ((hash >> 3) & 1u ? blockSize : 0) + localY;
      return source(channel, refX, refY);
    }
    case PredictionMethod::ANGLE: {
      const uint32_t hash = realtime::pixelHash(originX, originY, channel,
                                                frameIndex / 6, options_.seed);
      const int slope =
          1 + static_cast<int>(hash %
                               static_cast<uint32_t>(std::max(1, blockSize)));
      if ((hash & 1u) == 0) {
        return source(channel, originX + (localX + localY * slope) % blockSize,
                      originY - 1);
      }
      return source(channel, originX - 1,
                    originY + (localY + localX * slope) % blockSize);
    }
    case PredictionMethod::SAD:
    case PredictionMethod::BSAD: {
      const std::array candidates = {left, top, corner, (left + top) >> 1,
                                     realtime::paeth(left, top, corner)};
      int best = candidates[0];
      int bestDistance = std::abs(current - best);
      for (size_t i = 1; i < candidates.size(); ++i) {
        const int distance = std::abs(current - candidates[i]);
        const bool replace = method == PredictionMethod::SAD
                                 ? distance < bestDistance
                                 : distance > bestDistance;
        if (replace) {
          best = candidates[i];
          bestDistance = distance;
        }
      }
      return best;
    }
    case PredictionMethod::SPIRAL: {
      const float dx = static_cast<float>(localX - blockSize / 2);
      const float dy = static_cast<float>(localY - blockSize / 2);
      const float angle =
          std::atan2(dy, dx) + static_cast<float>(frameIndex % 360) * 0.01f;
      const int offset = realtime::wrapByte(static_cast<int>(
                             (angle + 3.14159265f) * blockSize / 6.2831853f)) %
                         blockSize;
      return (localX + localY < blockSize)
                 ? source(channel, originX + offset, originY - 1)
                 : source(channel, originX - 1, originY + offset);
    }
    case PredictionMethod::NOISE: {
      const int noise =
          static_cast<int>(
              realtime::pixelHash(x, y, channel, frameIndex, options_.seed) &
              63u) -
          32;
      return realtime::clampByte(corner + noise);
    }
    case PredictionMethod::GRADIENT: {
      const int topRight =
          source(channel, originX + blockSize - 1, originY - 1);
      const int bottomLeft =
          source(channel, originX - 1, originY + blockSize - 1);
      const float fx =
          blockSize > 1 ? static_cast<float>(localX) / (blockSize - 1) : 0.0f;
      const float fy =
          blockSize > 1 ? static_cast<float>(localY) / (blockSize - 1) : 0.0f;
      const float horizontal = corner + (topRight - corner) * fx;
      const float vertical = corner + (bottomLeft - corner) * fy;
      return realtime::clampByte(
          static_cast<int>((horizontal + vertical) * 0.5f));
    }
    case PredictionMethod::MIRROR:
      return source(channel, originX - 1, originY + blockSize - 1 - localY);
    case PredictionMethod::WAVE: {
      const float phase = static_cast<float>(frameIndex) * 0.08f;
      const float wave = std::sin((localX + phase) * 6.2831853f / blockSize) +
                         std::sin((localY + phase) * 6.2831853f / blockSize);
      return realtime::clampByte(static_cast<int>(corner + wave * 32.0f));
    }
    case PredictionMethod::CHECKERBOARD:
      return ((localX / 4 + localY / 4 + static_cast<int>(frameIndex / 8)) & 1)
                 ? left
                 : top;
    case PredictionMethod::RADIAL: {
      const float dx = static_cast<float>(localX - blockSize / 2);
      const float dy = static_cast<float>(localY - blockSize / 2);
      const float distance =
          std::sqrt(dx * dx + dy * dy) / std::max(1.0f, blockSize * 0.7071f);
      return realtime::clampByte(static_cast<int>(
          corner * (1.0f - distance) + ((left + top) >> 1) * distance));
    }
    case PredictionMethod::EDGE: {
      const int edge = current * 5 - source(channel, x - 1, y) -
                       source(channel, x + 1, y) - source(channel, x, y - 1) -
                       source(channel, x, y + 1);
      return realtime::clampByte(edge);
    }
    default:
      return (left + top) >> 1;
    }
  }

  static float triangleWave(int value, int halfPeriod) noexcept {
    halfPeriod = std::max(1, halfPeriod);
    const int period = halfPeriod * 2;
    int position = value % period;
    if (position < 0)
      position += period;
    const int ramp =
        position <= halfPeriod ? position : period - position;
    return static_cast<float>(ramp * 2 - halfPeriod) /
           static_cast<float>(halfPeriod);
  }

  static int triangleOffset(int value, int halfPeriod, int amplitude,
                            int divisor = 1) noexcept {
    halfPeriod = std::max(1, halfPeriod);
    divisor = std::max(1, divisor);
    const int period = halfPeriod * 2;
    int position = value % period;
    if (position < 0)
      position += period;
    const int ramp =
        position <= halfPeriod ? position : period - position;
    const int numerator = (ramp * 2 - halfPeriod) * amplitude;
    const int denominator = halfPeriod * divisor;
    return numerator >= 0 ? (numerator + denominator / 2) / denominator
                          : -((-numerator + denominator / 2) / denominator);
  }

  uint64_t heldEffectFrame() const noexcept {
    const float rate = std::clamp(uniform_.effectRate, 0.0f, 1.0f);
    const uint64_t holdFrames =
        1u + static_cast<uint64_t>(std::lround((1.0f - rate) * 11.0f));
    return frameIndex_ / std::max<uint64_t>(1u, holdFrames);
  }

  int effectValue(int channel, int x, int y, int current) const noexcept {
    constexpr std::array<uint8_t, 16> bayer4x4 = {
        0,  8, 2,  10, 12, 4, 14, 6,
        3, 11, 1, 9,  15, 7, 13, 5};

    const float amount = std::clamp(uniform_.effectAmount, 0.0f, 1.0f);
    const float scale = std::clamp(uniform_.effectScale, 0.0f, 1.0f);
    const float mixAmount =
        std::clamp(amount * uniform_.effectStrength, 0.0f, 1.0f);
    const uint64_t heldFrame = heldEffectFrame();
    float affected = static_cast<float>(current);

    switch (static_cast<RealtimeEffectFamily>(uniform_.effectFamily)) {
    case RealtimeEffectFamily::LINE_TEAR: {
      const int bandHeight = 1 + static_cast<int>(std::lround(scale * 15.0f));
      const int band = y / bandHeight;
      const uint32_t bandHash = realtime::pixelHash(
          0, band, 0, heldFrame, options_.seed);
      const float density = 0.10f + amount * 0.65f;
      if (static_cast<float>(bandHash & 0xffffu) < density * 65535.0f) {
        const int maximum = std::min(320, std::max(4, options_.width / 3));
        const int maximumShift =
            4 + static_cast<int>(std::lround(amount * (maximum - 4)));
        int shift =
            1 + static_cast<int>((bandHash >> 16u) %
                                 static_cast<uint32_t>(std::max(1, maximumShift)));
        if ((bandHash & 0x80000000u) != 0u)
          shift = -shift;
        affected = static_cast<float>(sourceWrapped(channel, x + shift, y));
      }
      break;
    }
    case RealtimeEffectFamily::CHANNEL_SHEAR: {
      const int halfPeriod = 8 + static_cast<int>(std::lround(scale * 120.0f));
      const int phase =
          static_cast<int>(heldFrame % static_cast<uint64_t>(halfPeriod * 2));
      const float wave = triangleWave(y + phase, halfPeriod);
      const int maximumOffset =
          2 + static_cast<int>(std::lround(amount * 96.0f));
      const int channelDirection = channel - 1;
      const int offset =
          channelDirection * maximumOffset +
          static_cast<int>(std::lround(channelDirection * maximumOffset * wave *
                                       0.5f));
      affected = static_cast<float>(sourceWrapped(channel, x + offset, y));
      break;
    }
    case RealtimeEffectFamily::ANALOG_SYNC: {
      const int halfPeriod = 6 + static_cast<int>(std::lround(scale * 72.0f));
      const int speed =
          1 + static_cast<int>(std::lround(uniform_.effectRate * 3.0f));
      const int phase = static_cast<int>(heldFrame) * speed;
      const float wave = triangleWave(y + phase, halfPeriod);
      const int amplitude =
          1 + static_cast<int>(std::lround(amount * 32.0f));
      int wobble = static_cast<int>(std::lround(wave * amplitude));
      const int lineGroup =
          y / std::max(1, 1 + static_cast<int>(std::lround(scale * 5.0f)));
      const uint32_t lineHash = realtime::pixelHash(
          0, lineGroup, 0, heldFrame / 2u, options_.seed);
      if (static_cast<float>(lineHash & 0xffu) < amount * 90.0f) {
        const int jitter =
            1 + static_cast<int>((lineHash >> 8u) %
                                 static_cast<uint32_t>(std::max(1, amplitude * 2)));
        wobble += (lineHash & 0x10000u) == 0u ? -jitter : jitter;
      }
      const int rollSpeed =
          1 + static_cast<int>(std::lround(uniform_.effectRate * 4.0f));
      const int roll = static_cast<int>(
          (heldFrame * static_cast<uint64_t>(rollSpeed)) %
          static_cast<uint64_t>(std::max(1, options_.height)));
      const int chromaOffset =
          (channel - 1) *
          std::max(1, static_cast<int>(std::lround(amount * 3.0f)));
      affected = static_cast<float>(
          sourceWrapped(channel, x + wobble + chromaOffset, y + roll));
      if (((y + phase) & 1) != 0)
        affected *= 1.0f - amount * 0.25f;
      break;
    }
    case RealtimeEffectFamily::MIRROR_FOLD: {
      const int halfPeriod = std::min(
          12 + static_cast<int>(std::lround(scale * 148.0f)),
          std::max(2, options_.width / 2));
      const int period = halfPeriod * 2;
      const int phase =
          static_cast<int>(heldFrame % static_cast<uint64_t>(period));
      const int shiftedX = x + phase;
      const int cell = shiftedX >= 0
                           ? shiftedX / period
                           : -((-shiftedX + period - 1) / period);
      int local = shiftedX % period;
      if (local < 0)
        local += period;
      const int folded =
          local <= halfPeriod ? local : period - 1 - local;
      const int sampleX = cell * period + folded - phase;
      affected = static_cast<float>(sourceWrapped(channel, sampleX, y));
      break;
    }
    case RealtimeEffectFamily::EDGE_ECHO: {
      const int left = sourceWrapped(channel, x - 1, y);
      const int right = sourceWrapped(channel, x + 1, y);
      const int top = sourceWrapped(channel, x, y - 1);
      const int bottom = sourceWrapped(channel, x, y + 1);
      const float edge =
          (std::abs(right - left) + std::abs(bottom - top)) * 0.5f / 255.0f;
      const int distance =
          2 + static_cast<int>(std::lround(scale * 46.0f));
      const int direction = (heldFrame & 1u) == 0u ? -1 : 1;
      const int echo =
          sourceWrapped(channel, x + direction * distance, y + distance / 2);
      const float displacementEdge =
          std::abs(static_cast<float>(echo - current)) / 255.0f;
      const float threshold = 0.12f + (0.012f - 0.12f) * scale;
      const float edgeMix = std::clamp(
          std::max((edge - threshold) * 10.0f,
                   (displacementEdge - threshold * 0.55f) * 4.5f),
          0.0f, 1.0f);
      affected = current + (echo - current) * edgeMix;
      break;
    }
    case RealtimeEffectFamily::BITPLANE_DITHER: {
      const int grainPower =
          std::clamp(static_cast<int>(std::lround(scale * 2.0f)), 0, 2);
      const int grain = 1 << grainPower;
      const int matrixX = (x / grain) & 3;
      const int matrixY = (y / grain) & 3;
      const uint32_t threshold =
          bayer4x4[static_cast<size_t>(matrixY * 4 + matrixX)];
      const uint32_t coverage = static_cast<uint32_t>(
          std::clamp(static_cast<int>(std::lround(amount * 16.0f)), 0, 16));
      if (threshold < coverage) {
        const int baseBit =
            std::clamp(1 + static_cast<int>(std::floor(amount * 6.0f)), 1, 6);
        const int bit =
            (baseBit + channel + static_cast<int>(heldFrame & 1u)) % 7;
        affected = static_cast<float>(realtime::clampByte(current) ^ (1 << bit));
      }
      break;
    }
    case RealtimeEffectFamily::WAVE_WARP: {
      const int halfPeriod =
          12 + static_cast<int>(std::lround(scale * 120.0f));
      const int speed =
          1 + static_cast<int>(std::lround(uniform_.effectRate * 3.0f));
      const int phase = static_cast<int>(heldFrame) * speed;
      const int amplitude =
          1 + static_cast<int>(std::lround(amount * 48.0f));
      // Keep sampling coordinates bit-exact with Metal. Computing the triangle
      // ratio in floating point can move a half-integer to opposite sides of
      // the rounding boundary on the two backends.
      const int offsetX = triangleOffset(y + phase, halfPeriod, amplitude);
      const int offsetY =
          triangleOffset(x - phase, halfPeriod, amplitude, 2);
      affected =
          static_cast<float>(sourceWrapped(channel, x + offsetX, y + offsetY));
      break;
    }
    case RealtimeEffectFamily::POSTER_SOLAR: {
      const int levels = 2 + static_cast<int>(std::lround(scale * 14.0f));
      const float stepSize = 1.0f / std::max(1, levels - 1);
      const float normalized = current / 255.0f;
      const float quantized = std::round(normalized / stepSize) * stepSize;
      const float drift =
          triangleWave(static_cast<int>(heldFrame), 32) * 0.15f;
      const float threshold =
          std::clamp(0.25f + scale * 0.50f + drift, 0.10f, 0.90f);
      const float solarized =
          quantized > threshold ? 1.0f - quantized : quantized;
      affected = solarized * 255.0f;
      break;
    }
    case RealtimeEffectFamily::LEGACY_BLOCK:
    case RealtimeEffectFamily::COUNT:
      break;
    }

    return realtime::clampByte(static_cast<int>(
        std::lround(current + (affected - current) * mixAmount)));
  }

  void processEffectChannel(int channel) noexcept {
    const size_t pixelCount = static_cast<size_t>(options_.width) *
                              static_cast<size_t>(options_.height);
    const size_t planeOffset = static_cast<size_t>(channel) * pixelCount;
    for (int y = 0; y < options_.height; ++y) {
      for (int x = 0; x < options_.width; ++x) {
        const size_t index =
            static_cast<size_t>(y) * static_cast<size_t>(options_.width) +
            static_cast<size_t>(x);
        outputPlanes_[planeOffset + index] = static_cast<int16_t>(effectValue(
            channel, x, y, sourcePlanes_[planeOffset + index]));
      }
    }
  }

  void processChannel(int channel) noexcept {
    const auto &config = options_.config.channels[static_cast<size_t>(channel)];
    if (uniform_.effectFamily !=
        static_cast<uint32_t>(RealtimeEffectFamily::LEGACY_BLOCK)) {
      processEffectChannel(channel);
      return;
    }
    int minBlock = realtime::normalizeBlockSize(config.minBlockSize);
    int maxBlock = realtime::normalizeBlockSize(config.maxBlockSize);
    if (minBlock > maxBlock)
      std::swap(minBlock, maxBlock);

    const size_t pixelCount = static_cast<size_t>(options_.width) *
                              static_cast<size_t>(options_.height);
    const size_t planeOffset = static_cast<size_t>(channel) * pixelCount;
    const float quantizer = std::max(1.0f, config.quantizationValue / 2.0f);
    float transformGain = realtime::waveletGain(config.waveletType);
    transformGain *=
        std::clamp(std::abs(config.transformScale) / 20.0f, 0.25f, 4.0f);
    if (config.transformType == TransformType::WPT)
      transformGain *= 1.2f;
    const float compressionThreshold =
        50.0f * std::pow(config.transformCompress / 255.0f, 2.0f);
    const float quantizationDrive =
        std::clamp(config.quantizationValue / 255.0f, 0.0f, 1.0f);
    const float compressionDrive =
        std::clamp(config.transformCompress / 255.0f, 0.0f, 1.0f);
    const float presetDrive =
        0.55f + quantizationDrive * 0.25f + compressionDrive * 0.15f +
        (config.predictionMethod == PredictionMethod::NONE ? 0.0f : 0.15f) +
        (config.waveletType == WaveletType::NONE ? 0.0f : 0.10f);
    const float drive =
        std::clamp(options_.effectStrength * presetDrive, 0.0f, 1.35f);
    const float density =
        options_.effectStrength <= 0.0f
            ? 0.0f
            : std::clamp(0.30f + 0.42f * std::min(drive, 1.0f) +
                             0.10f * quantizationDrive,
                         0.25f, 0.90f);
    const float residualKeep = std::clamp(
        0.58f - drive * 0.40f - quantizationDrive * 0.18f, 0.04f, 0.58f);
    const float corruptionMix =
        std::clamp(0.55f + 0.35f * std::min(drive, 1.0f), 0.55f, 0.90f);
    const int predictionCode =
        std::abs(static_cast<int>(config.predictionMethod));
    const uint64_t holdFrames =
        3u +
        static_cast<uint64_t>(
            (predictionCode + static_cast<int>(config.encodingMethod)) % 6);
    const uint64_t heldFrame = frameIndex_ / holdFrames;
    const auto &anchorConfig = options_.config.channels[0];
    int anchorMin = realtime::normalizeBlockSize(anchorConfig.minBlockSize);
    int anchorMax = realtime::normalizeBlockSize(anchorConfig.maxBlockSize);
    if (anchorMin > anchorMax)
      std::swap(anchorMin, anchorMax);
    const int effectBlock = std::clamp(
        std::max(anchorMin * 8, std::min(anchorMax * 2, 64)), 16, 64);

    for (int y = 0; y < options_.height; ++y) {
      for (int x = 0; x < options_.width; ++x) {
        const size_t index =
            static_cast<size_t>(y) * static_cast<size_t>(options_.width) +
            static_cast<size_t>(x);
        const int current = sourcePlanes_[planeOffset + index];
        const int edge = (std::abs(current - source(channel, x - 1, y)) +
                          std::abs(current - source(channel, x, y - 1))) >>
                         1;
        const int blockSize =
            edge > config.segmentationPrecision ? minBlock : maxBlock;
        const int originX = (x / blockSize) * blockSize;
        const int originY = (y / blockSize) * blockSize;

        const int predicted =
            predictor(config.predictionMethod, channel, x, y, originX, originY,
                      blockSize, current, frameIndex_);
        float residual = static_cast<float>(current - predicted);

        if (config.waveletType != WaveletType::NONE) {
          const float neighborAverage =
              0.25f * (source(channel, x - 1, y) + source(channel, x + 1, y) +
                       source(channel, x, y - 1) + source(channel, x, y + 1));
          residual += (current - neighborAverage) * transformGain;
          if (std::abs(residual) < compressionThreshold)
            residual = 0.0f;
        }

        if (quantizer > 1.0f)
          residual = std::round(residual / quantizer) * quantizer;

        const int effectOriginX = (x / effectBlock) * effectBlock;
        const int effectOriginY = (y / effectBlock) * effectBlock;
        const uint32_t blockHash = realtime::pixelHash(
            effectOriginX, effectOriginY, 0, heldFrame, options_.seed);
        const bool affected =
            static_cast<float>(blockHash & 0xffffu) < density * 65535.0f;

        float value = static_cast<float>(current);
        if (affected) {
          const int direction = (blockHash & 0x10000u) == 0u ? -1 : 1;
          const int distance =
              effectBlock * (1 + static_cast<int>((blockHash >> 17u) % 3u));
          const int channelShift =
              (channel - 1) *
              (1 + static_cast<int>(
                       (blockHash >> 21u) %
                       static_cast<uint32_t>(std::max(2, effectBlock / 4))));
          int sampleX = x + direction * distance + channelShift;
          int sampleY =
              y + (static_cast<int>((blockHash >> 25u) % 3u) - 1) * effectBlock;
          const int mode =
              (static_cast<int>(config.encodingMethod) + predictionCode) % 6;
          if (mode == 2) {
            sampleX = effectOriginX +
                      static_cast<int>(
                          (blockHash >> 9u) %
                          static_cast<uint32_t>(std::max(1, effectBlock / 4))) +
                      channelShift;
          } else if (mode == 5 && ((effectOriginY / effectBlock) & 1) != 0) {
            sampleX = x - direction * distance + channelShift;
            sampleY = y - (static_cast<int>((blockHash >> 25u) % 3u) - 1) *
                              effectBlock;
          }

          const int displaced = sourceWrapped(channel, sampleX, sampleY);
          const float broken = predicted + residual * residualKeep;
          float corrupted = static_cast<float>(displaced);
          switch (mode) {
          case 0:
            corrupted = broken * 0.55f + displaced * 0.45f;
            break;
          case 1:
            corrupted = static_cast<float>(displaced);
            break;
          case 2:
            corrupted = static_cast<float>(displaced);
            break;
          case 3:
            corrupted =
                current + (displaced - predicted) * (0.70f + drive * 0.22f);
            break;
          case 4:
            corrupted = static_cast<float>(realtime::clampByte(current) ^
                                           realtime::clampByte(displaced));
            break;
          case 5:
            corrupted = displaced * 0.80f + broken * 0.20f;
            break;
          default:
            break;
          }
          const float colorSpaceDamageScale =
              static_cast<int>(options_.config.colorSpace) <= 2 ? 1.0f : 0.22f;
          const float bitPlaneDamage =
              colorSpaceDamageScale * (12.0f + 24.0f * drive) *
              (0.65f +
               0.35f * static_cast<float>((blockHash >> 8u) & 0xffu) / 255.0f);
          const bool positiveDamage =
              ((blockHash >> static_cast<uint32_t>(3 + channel * 5)) & 1u) !=
              0u;
          corrupted += positiveDamage ? bitPlaneDamage : -bitPlaneDamage;
          value = current + (corrupted - current) * corruptionMix;
        }

        int reconstructed = static_cast<int>(std::round(value));
        reconstructed = config.clampMethod == ClampMethod::MOD256
                            ? realtime::wrapByte(reconstructed)
                            : realtime::clampByte(reconstructed);
        outputPlanes_[planeOffset + index] =
            static_cast<int16_t>(reconstructed);
      }
    }
  }

  void workerLoop(int channel) {
    uint64_t observedGeneration = 0;
    while (true) {
      {
        std::unique_lock lock(mutex_);
        startCondition_.wait(lock, [this, &observedGeneration] {
          return stopping_ || generation_ != observedGeneration;
        });
        if (stopping_)
          return;
        observedGeneration = generation_;
      }

      processChannel(channel);

      {
        std::lock_guard lock(mutex_);
        ++completedWorkers_;
      }
      doneCondition_.notify_one();
    }
  }

  std::array<std::thread, 3> workers_;
  std::mutex mutex_;
  std::condition_variable startCondition_;
  std::condition_variable doneCondition_;
  uint64_t generation_ = 0;
  int completedWorkers_ = 3;
  bool stopping_ = false;
  bool prepared_ = false;

  RealtimePrepareOptions options_{};
  realtime::MetalPresetUniform uniform_{};
  std::vector<int16_t> sourcePlanes_;
  std::vector<int16_t> outputPlanes_;
  uint64_t frameIndex_ = 0;
  RealtimeFrameStats lastStats_{};
};

} // namespace

std::unique_ptr<RealtimeBackend> createCpuRealtimeBackend() {
  return std::make_unique<CpuRealtimeBackend>();
}

} // namespace glic
