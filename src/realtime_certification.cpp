#include "realtime_certification.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <numeric>
#include <string_view>

namespace glic {
namespace {

using Clock = std::chrono::steady_clock;

template <size_t Size>
double percentile(const std::array<double, Size> &sortedValues,
                  double fraction) {
  static_assert(Size > 0);
  const size_t index = static_cast<size_t>(
      std::ceil(static_cast<double>(Size - 1) * fraction));
  return sortedValues[std::min(index, Size - 1)];
}

bool finiteSamples(
    const std::array<double, kRealtimeCertificationMeasuredFrames> &samples) {
  return std::all_of(samples.begin(), samples.end(),
                     [](double value) { return std::isfinite(value); });
}

bool positiveSamples(
    const std::array<double, kRealtimeCertificationMeasuredFrames> &samples) {
  return std::all_of(samples.begin(), samples.end(),
                     [](double value) { return value > 0.0; });
}

} // namespace

RealtimeCertificationResult certifyRealtimePreset(
    RealtimeBackend &backend, std::span<const Color> input,
    std::span<Color> output, const RealtimeCertificationRequest &request) {
  RealtimeCertificationResult result;
  result.performed = true;

  if (!backend.isHardwareAccelerated() ||
      std::string_view(backend.name()) != "metal") {
    result.error = "realtime certification requires the Metal backend";
    return result;
  }

  constexpr size_t pixelCount =
      static_cast<size_t>(kRealtimeCertificationWidth) *
      static_cast<size_t>(kRealtimeCertificationHeight);
  if (input.size() != pixelCount || output.size() != pixelCount) {
    result.error = "realtime certification buffers must be exactly 960x540";
    return result;
  }

  const RealtimePrepareOptions prepareOptions{
      .width = kRealtimeCertificationWidth,
      .height = kRealtimeCertificationHeight,
      .config = request.config,
      .seed = request.seed,
      .effectStrength = request.effectStrength};
  if (!backend.prepare(prepareOptions, result.error))
    return result;

  for (uint32_t frame = 0; frame < kRealtimeCertificationWarmupFrames;
       ++frame) {
    if (!backend.process(input, output, request.frameIndexBase + frame,
                         result.error))
      return result;
  }

  std::array<double, kRealtimeCertificationMeasuredFrames> wallTimes{};
  std::array<double, kRealtimeCertificationMeasuredFrames> gpuTimes{};
  for (uint32_t frame = 0; frame < kRealtimeCertificationMeasuredFrames;
       ++frame) {
    const uint64_t frameIndex =
        request.frameIndexBase + kRealtimeCertificationWarmupFrames + frame;
    const Clock::time_point started = Clock::now();
    if (!backend.process(input, output, frameIndex, result.error))
      return result;
    wallTimes[frame] =
        std::chrono::duration<double, std::milli>(Clock::now() - started)
            .count();
    gpuTimes[frame] = backend.lastFrameStats().gpuMilliseconds;
    ++result.completedFrames;
  }

  if (!finiteSamples(wallTimes) || !positiveSamples(wallTimes) ||
      !finiteSamples(gpuTimes)) {
    result.error = "realtime certification produced invalid timing data";
    return result;
  }

  result.meanWallMilliseconds =
      std::accumulate(wallTimes.begin(), wallTimes.end(), 0.0) /
      static_cast<double>(wallTimes.size());
  result.meanGpuMilliseconds =
      std::accumulate(gpuTimes.begin(), gpuTimes.end(), 0.0) /
      static_cast<double>(gpuTimes.size());

  std::sort(wallTimes.begin(), wallTimes.end());
  std::sort(gpuTimes.begin(), gpuTimes.end());
  result.medianWallMilliseconds = percentile(wallTimes, 0.50);
  result.p95WallMilliseconds = percentile(wallTimes, 0.95);
  result.p99WallMilliseconds = percentile(wallTimes, 0.99);
  result.maxWallMilliseconds = wallTimes.back();
  result.p95GpuMilliseconds = percentile(gpuTimes, 0.95);
  result.processPassed = true;
  result.passed =
      result.meanWallMilliseconds <= result.frameBudgetMilliseconds &&
      result.p95WallMilliseconds <= result.frameBudgetMilliseconds;
  if (!result.passed)
    result.error = "below_30_fps_at_960x540";
  return result;
}

} // namespace glic
