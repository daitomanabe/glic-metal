#pragma once

#include "config.hpp"
#include "preset_loader.hpp"
#include "processing_random.hpp"

#include <array>
#include <cstddef>
#include <condition_variable>
#include <exception>
#include <mutex>
#include <span>
#include <string>
#include <thread>
#include <vector>

namespace glic {

// The original-visual-fidelity lane supports fixed upstream predictors with no
// wavelet, plus the exact JWave CDF 9/7 FWT/WPT path used by the largest
// deterministic wavelet family in the bundled upstream preset corpus.
// Unsupported fields are rejected instead of being projected to the legacy
// realtime approximation.
struct OriginalRealtimeSupport {
  bool supported = false;
  std::vector<std::string> reasons;
};

OriginalRealtimeSupport
evaluateOriginalRealtimeSupport(const OriginalPresetConfig &config);

struct OriginalRealtimeFrameStats {
  std::array<std::size_t, 3> segmentCounts{};
  std::uint64_t segmentationRngState = 0;
  std::uint64_t segmentOrderFnv1a64 = 0;
  std::size_t earlyTerminatedNodes = 0;
  std::size_t earlySkippedSamples = 0;
};

class OriginalRealtimeCpuLane {
public:
  OriginalRealtimeCpuLane() = default;
  ~OriginalRealtimeCpuLane();
  OriginalRealtimeCpuLane(const OriginalRealtimeCpuLane &) = delete;
  OriginalRealtimeCpuLane &operator=(const OriginalRealtimeCpuLane &) = delete;

  bool prepare(int width, int height, const OriginalPresetConfig &config,
               std::string &error);

  bool process(std::span<const Color> input, std::span<Color> output,
               OriginalRealtimeFrameStats *stats, std::string &error);

  int width() const noexcept { return width_; }
  int height() const noexcept { return height_; }
  const OriginalPresetConfig &config() const noexcept { return config_; }

private:
  struct WorkingSegment {
    int x = 0;
    int y = 0;
    int size = 0;
  };

  int planeValue(int channel, int x, int y) const noexcept;
  bool sampledStandardDeviationExceeds(int channel, int x, int y, int size,
                                       float threshold);
  void skipUnusedDeviationSamples(int size) noexcept;
  std::uint64_t fixedSegmentationRandomDraws(int blockSize) const noexcept;
  void emitFixedSegments(int x, int y, int size, int blockSize,
                         std::vector<WorkingSegment> &segments);
  void segmentChannel(int channel, int x, int y, int size, int minSize,
                      int maxSize, float threshold,
                      std::vector<WorkingSegment> &segments);
  void prepareChannelSegments(int channel);
  int predictionValue(PredictionMethod method, int channel,
                      const WorkingSegment &segment, int x, int y,
                      int dcValue, int cornerValue) const noexcept;
  void transformCdf97(int channel, int size, TransformType transformType,
                      bool reverse) noexcept;
  void transformCdf97Line(double *data, int size,
                          TransformType transformType, bool reverse,
                          double *scratch) noexcept;
  void cdf97Step(double *data, int offset, int size, bool reverse,
                 double *scratch) noexcept;
  void processCdf97Segment(int channel, const WorkingSegment &segment,
                           int dcValue, int cornerValue, float quantization);
  std::size_t processPreparedChannel(int channel);
  void startWorkers();
  void stopWorkers() noexcept;
  void workerLoop(int channel);

  int width_ = 0;
  int height_ = 0;
  int rootSegmentSize_ = 0;
  OriginalPresetConfig config_{};
  std::array<std::vector<int>, 3> planes_{};
  std::array<std::vector<WorkingSegment>, 3> segments_{};
  std::array<std::vector<double>, 3> transformMatrices_{};
  std::array<std::vector<double>, 3> transformLines_{};
  std::array<std::vector<double>, 3> transformScratch_{};
  std::array<int, 3> referenceValues_{};
  ProcessingRandom segmentationRng_{42};
  std::size_t earlyTerminatedNodes_ = 0;
  std::size_t earlySkippedSamples_ = 0;
  std::array<std::jthread, 2> workers_{};
  std::mutex workerMutex_;
  std::condition_variable workerCondition_;
  std::condition_variable workerCompletion_;
  std::array<std::size_t, 2> workerSegmentCounts_{};
  std::array<std::exception_ptr, 2> workerErrors_{};
  uint64_t workerGeneration_ = 0;
  int pendingWorkers_ = 0;
  bool workerShutdown_ = false;
  bool prepared_ = false;
};

} // namespace glic
