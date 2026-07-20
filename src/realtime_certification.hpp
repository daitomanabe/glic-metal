#pragma once

#include "realtime.hpp"

#include <cstdint>
#include <span>
#include <string>

namespace glic {

inline constexpr int kRealtimeCertificationWidth = 960;
inline constexpr int kRealtimeCertificationHeight = 540;
inline constexpr uint32_t kRealtimeCertificationWarmupFrames = 10;
inline constexpr uint32_t kRealtimeCertificationMeasuredFrames = 120;
inline constexpr double kRealtimeCertificationTargetFps = 30.0;
inline constexpr double kRealtimeCertificationFrameBudgetMilliseconds =
    1000.0 / kRealtimeCertificationTargetFps;

struct RealtimeCertificationRequest {
  CodecConfig config{};
  uint32_t seed = 0x474C4943u;
  float effectStrength = 1.0f;
  uint64_t frameIndexBase = 0;
};

struct RealtimeCertificationResult {
  bool performed = false;
  bool processPassed = false;
  bool passed = false;
  int width = kRealtimeCertificationWidth;
  int height = kRealtimeCertificationHeight;
  uint32_t warmupFrames = kRealtimeCertificationWarmupFrames;
  uint32_t measuredFrames = kRealtimeCertificationMeasuredFrames;
  uint32_t completedFrames = 0;
  double targetFps = kRealtimeCertificationTargetFps;
  double frameBudgetMilliseconds =
      kRealtimeCertificationFrameBudgetMilliseconds;
  double meanWallMilliseconds = 0.0;
  double medianWallMilliseconds = 0.0;
  double p95WallMilliseconds = 0.0;
  double p99WallMilliseconds = 0.0;
  double maxWallMilliseconds = 0.0;
  double meanGpuMilliseconds = 0.0;
  double p95GpuMilliseconds = 0.0;
  std::string error;
};

// Certifies the synchronous buffer path used by the realtime video filter.
// The criteria are intentionally fixed: Metal, 960x540, ten warm-up frames,
// 120 measured frames, and both mean and p95 wall time within a 30 fps frame.
RealtimeCertificationResult certifyRealtimePreset(
    RealtimeBackend &backend, std::span<const Color> input,
    std::span<Color> output, const RealtimeCertificationRequest &request);

} // namespace glic
