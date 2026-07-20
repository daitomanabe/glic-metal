#include "realtime_cpu.hpp"

#include "colorspaces.hpp"
#include "realtime_internal.hpp"

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

    // One conversion per pixel. Channel workers operate on three contiguous
    // slabs after this point, avoiding nested-vector pointer chasing.
    for (size_t index = 0; index < pixelCount; ++index) {
      const Color converted =
          toColorSpace(input[index], options_.config.colorSpace);
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
      output[index] = fromColorSpace(converted, options_.config.colorSpace);
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

  void processChannel(int channel) noexcept {
    const auto &config = options_.config.channels[static_cast<size_t>(channel)];
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
        int reconstructed = predicted + static_cast<int>(std::round(residual));
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
