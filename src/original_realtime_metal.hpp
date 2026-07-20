#pragma once

#include "original_realtime.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <span>
#include <string>

namespace glic {

// Timing and scheduling evidence for the original-style Metal lane.  This is
// intentionally separate from RealtimeFrameStats: unlike the visual-only
// realtime backend, this lane keeps upstream quadtree segmentation semantics
// on the CPU and sends the reconstruction workload to Metal.
struct OriginalRealtimeMetalFrameStats {
  std::array<std::size_t, 3> segmentCounts{};
  std::size_t totalSegments = 0;
  std::size_t dispatchLevels = 0;
  std::size_t gpuDispatches = 0;
  uint32_t commandBufferSubmissions = 0;
  uint32_t completionWaits = 0;
  uint32_t mappedBufferCopies = 0;
  double cpuPrepareMilliseconds = 0.0;
  double gpuMilliseconds = 0.0;
  double cpuOutputMilliseconds = 0.0;
  double totalMilliseconds = 0.0;
  uint64_t frameIndex = 0;
};

class OriginalRealtimeMetalLane {
public:
  virtual ~OriginalRealtimeMetalLane() = default;

  virtual bool prepare(int width, int height,
                       const OriginalPresetConfig &config,
                       std::string &error) = 0;
  virtual bool process(std::span<const Color> input, std::span<Color> output,
                       uint64_t frameIndex,
                       OriginalRealtimeMetalFrameStats *stats,
                       std::string &error) = 0;

  [[nodiscard]] virtual int width() const noexcept = 0;
  [[nodiscard]] virtual int height() const noexcept = 0;
  [[nodiscard]] virtual const OriginalPresetConfig &config() const noexcept = 0;
  [[nodiscard]] virtual const char *name() const noexcept = 0;
  [[nodiscard]] virtual const char *executionMode() const noexcept = 0;
  [[nodiscard]] virtual const char *numericPrecision() const noexcept = 0;
  [[nodiscard]] virtual bool isPixelExact() const noexcept = 0;
  [[nodiscard]] virtual bool isHardwareAccelerated() const noexcept = 0;
};

// Returns null with a diagnostic when Metal is unavailable. Unsupported
// original preset fields are rejected by prepare(); they are never projected
// to the compatibility realtime shader.
std::unique_ptr<OriginalRealtimeMetalLane>
createOriginalRealtimeMetalLane(std::string &error);

} // namespace glic
