#include "original_realtime.hpp"
#include "original_realtime_metal.hpp"
#include "preset_loader.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <numeric>
#include <span>
#include <sstream>
#include <string>
#include <string_view>
#include <vector>

#if defined(_WIN32)
#include <fcntl.h>
#include <io.h>
#endif

namespace {

constexpr int kCertificationMinimumWidth = 960;
constexpr int kCertificationMinimumHeight = 540;
constexpr std::size_t kCertificationWarmupFrames = 10;
constexpr std::size_t kCertificationMinimumMeasuredFrames = 120;
constexpr double kCertificationFrameBudgetMilliseconds = 1000.0 / 30.0;
constexpr std::size_t kDefaultTimingCapacity = 65536;
constexpr std::size_t kMaximumExpectedFrames = 2000000;

struct Options {
  int width = 0;
  int height = 0;
  double targetFps = 0.0;
  std::string preset = "default";
  std::string presetsDirectory = "presets";
  std::string statsJson;
  std::string backend = "cpu";
  std::size_t expectedFrames = 0;
  bool checkOnly = false;
};

void printUsage(const char *program) {
  std::cerr
      << "Usage: " << program
      << " --width <pixels> --height <pixels> [options]\n"
      << "Reads packed BGRA8 frames from stdin and writes packed BGRA8 frames "
         "to stdout. This dedicated filter only runs the fail-closed "
         "original_visual CPU or Metal lane.\n"
      << "  --preset <name>          Original named preset (default: default)\n"
      << "  --presets-dir <path>     Original preset directory (default: "
         "presets)\n"
      << "  --backend <cpu|metal>    Reconstruction backend (default: cpu)\n"
      << "  --target-fps <fps>       Delivery rate used by the 30 fps gate\n"
      << "  --expected-frames <n>    Preallocate timing storage for the "
         "stream\n"
      << "  --check                  Validate support and allocation, then "
         "exit\n"
      << "  --stats-json <path>      Write timing and fidelity statistics\n";
}

bool parsePositiveInt(std::string_view text, int &value) {
  try {
    const long parsed = std::stol(std::string(text));
    if (parsed <= 0 || parsed > std::numeric_limits<int>::max())
      return false;
    value = static_cast<int>(parsed);
    return true;
  } catch (...) {
    return false;
  }
}

bool parsePositiveDouble(std::string_view text, double &value) {
  try {
    const double parsed = std::stod(std::string(text));
    if (!std::isfinite(parsed) || parsed <= 0.0)
      return false;
    value = parsed;
    return true;
  } catch (...) {
    return false;
  }
}

bool parseExpectedFrames(std::string_view text, std::size_t &value) {
  try {
    const unsigned long long parsed = std::stoull(std::string(text));
    if (parsed == 0 || parsed > kMaximumExpectedFrames)
      return false;
    value = static_cast<std::size_t>(parsed);
    return true;
  } catch (...) {
    return false;
  }
}

bool parseOptions(int argc, char **argv, Options &options) {
  for (int index = 1; index < argc; ++index) {
    const std::string_view argument = argv[index];
    auto takeValue = [&]() -> const char * {
      return index + 1 < argc ? argv[++index] : nullptr;
    };
    if (argument == "--width") {
      const char *value = takeValue();
      if (!value || !parsePositiveInt(value, options.width))
        return false;
    } else if (argument == "--height") {
      const char *value = takeValue();
      if (!value || !parsePositiveInt(value, options.height))
        return false;
    } else if (argument == "--preset") {
      const char *value = takeValue();
      if (!value || value[0] == '\0')
        return false;
      options.preset = value;
    } else if (argument == "--target-fps") {
      const char *value = takeValue();
      if (!value || !parsePositiveDouble(value, options.targetFps))
        return false;
    } else if (argument == "--expected-frames") {
      const char *value = takeValue();
      if (!value || !parseExpectedFrames(value, options.expectedFrames))
        return false;
    } else if (argument == "--presets-dir") {
      const char *value = takeValue();
      if (!value || value[0] == '\0')
        return false;
      options.presetsDirectory = value;
    } else if (argument == "--stats-json") {
      const char *value = takeValue();
      if (!value || value[0] == '\0')
        return false;
      options.statsJson = value;
    } else if (argument == "--backend") {
      const char *value = takeValue();
      if (!value || (std::string_view(value) != "cpu" &&
                     std::string_view(value) != "metal"))
        return false;
      options.backend = value;
    } else if (argument == "--check") {
      options.checkOnly = true;
    } else {
      return false;
    }
  }
  return options.width > 0 && options.height > 0;
}

std::string jsonEscape(std::string_view value) {
  std::ostringstream output;
  output << std::hex << std::setfill('0');
  for (const unsigned char character : value) {
    switch (character) {
    case '"':
      output << "\\\"";
      break;
    case '\\':
      output << "\\\\";
      break;
    case '\b':
      output << "\\b";
      break;
    case '\f':
      output << "\\f";
      break;
    case '\n':
      output << "\\n";
      break;
    case '\r':
      output << "\\r";
      break;
    case '\t':
      output << "\\t";
      break;
    default:
      if (character < 0x20)
        output << "\\u" << std::setw(4) << static_cast<int>(character);
      else
        output << static_cast<char>(character);
      break;
    }
  }
  return output.str();
}

double percentile(std::vector<double> values, double fraction) {
  if (values.empty())
    return 0.0;
  std::sort(values.begin(), values.end());
  const auto index = static_cast<std::size_t>(
      std::ceil(static_cast<double>(values.size() - 1) * fraction));
  return values[std::min(index, values.size() - 1)];
}

struct TimingSummary {
  double meanMilliseconds = 0.0;
  double p95Milliseconds = 0.0;
  double maximumMilliseconds = 0.0;
  double framesPerSecond = 0.0;
};

TimingSummary summarizeTiming(std::span<const double> frameTimes) {
  TimingSummary summary;
  if (frameTimes.empty())
    return summary;
  const double totalMilliseconds =
      std::accumulate(frameTimes.begin(), frameTimes.end(), 0.0);
  summary.meanMilliseconds =
      totalMilliseconds / static_cast<double>(frameTimes.size());
  summary.p95Milliseconds = percentile(
      std::vector<double>(frameTimes.begin(), frameTimes.end()), 0.95);
  summary.maximumMilliseconds =
      *std::max_element(frameTimes.begin(), frameTimes.end());
  if (summary.meanMilliseconds > 0.0)
    summary.framesPerSecond = 1000.0 / summary.meanMilliseconds;
  return summary;
}

bool writeStats(const Options &options,
                std::span<const double> kernelFrameTimes,
                std::span<const double> streamFrameTimes,
                const std::array<double, 3> &segmentTotals,
                std::span<const double> gpuFrameTimes,
                std::span<const double> cpuPrepareTimes,
                std::span<const double> cpuOutputTimes,
                double dependencyLevelTotal, double gpuDispatchTotal,
                double commandBufferTotal, double completionWaitTotal,
                double mappedBufferCopyTotal, std::size_t initialTimingCapacity,
                std::size_t timingCapacityGrowthEvents) {
  if (options.statsJson.empty())
    return true;
  std::ofstream output(options.statsJson);
  if (!output) {
    std::cerr << "Failed to write stats JSON: " << options.statsJson << '\n';
    return false;
  }

  if (kernelFrameTimes.size() != streamFrameTimes.size()) {
    std::cerr << "Kernel and stream timing sample counts differ\n";
    return false;
  }
  if (gpuFrameTimes.size() != kernelFrameTimes.size() ||
      cpuPrepareTimes.size() != kernelFrameTimes.size() ||
      cpuOutputTimes.size() != kernelFrameTimes.size()) {
    std::cerr << "Backend timing sample counts differ\n";
    return false;
  }
  const std::size_t warmupFrames =
      std::min(kernelFrameTimes.size(), kCertificationWarmupFrames);
  const auto measuredKernelTimes = kernelFrameTimes.subspan(warmupFrames);
  const auto measuredStreamTimes = streamFrameTimes.subspan(warmupFrames);
  const TimingSummary allKernel = summarizeTiming(kernelFrameTimes);
  const TimingSummary allStream = summarizeTiming(streamFrameTimes);
  const TimingSummary kernel = summarizeTiming(measuredKernelTimes);
  const TimingSummary stream = summarizeTiming(measuredStreamTimes);
  const TimingSummary gpu =
      summarizeTiming(gpuFrameTimes.subspan(warmupFrames));
  const TimingSummary cpuPrepare =
      summarizeTiming(cpuPrepareTimes.subspan(warmupFrames));
  const TimingSummary cpuOutput =
      summarizeTiming(cpuOutputTimes.subspan(warmupFrames));
  const bool usesMetal = options.backend == "metal";
  const bool hasCertificationEvidence =
      options.width >= kCertificationMinimumWidth &&
      options.height >= kCertificationMinimumHeight &&
      options.targetFps >= 30.0 && warmupFrames == kCertificationWarmupFrames &&
      measuredKernelTimes.size() >= kCertificationMinimumMeasuredFrames;
  const bool kernelRealtimePassed =
      hasCertificationEvidence &&
      kernel.meanMilliseconds <= kCertificationFrameBudgetMilliseconds &&
      kernel.p95Milliseconds <= kCertificationFrameBudgetMilliseconds;
  const bool streamRealtimePassed =
      hasCertificationEvidence &&
      stream.meanMilliseconds <= kCertificationFrameBudgetMilliseconds &&
      stream.p95Milliseconds <= kCertificationFrameBudgetMilliseconds;

  output << std::fixed << std::setprecision(3) << "{\n"
         << "  \"schema\": \"glic-original-visual-filter-v1\",\n"
         << "  \"processing_mode\": \"original_visual\",\n"
         << "  \"preset_semantics\": \"original\",\n"
         << "  \"preset\": \"" << jsonEscape(options.preset) << "\",\n"
         << "  \"backend\": \""
         << (usesMetal ? "metal-original-visual" : "cpu-original-visual")
         << "\",\n"
         << "  \"execution_mode\": \""
         << (usesMetal ? "hybrid_cpu_colorspace_segmentation_gpu_reconstruction"
                       : "cpu_parallel_channels")
         << "\",\n"
         << "  \"cdf97_precision\": \""
         << (usesMetal ? "float32-safe-math" : "float64") << "\",\n"
         << "  \"fidelity_claim\": "
            "\"original-style-algorithmic-core-not-processing-pixel-exact\",\n"
         << "  \"processing_pixel_exact\": false,\n"
         << "  \"unsupported_policy\": \"fail-closed\",\n"
         << "  \"known_deviations\": [\n"
         << "    \"glic_serialization_and_entropy_encoding_omitted\",\n"
         << "    "
            "\"cpp_colorspace_and_arithmetic_not_byte_certified_against_"
            "processing_jvm\",\n"
         << "    "
            "\"processing_global_rng_replaced_by_independent_seeded_mt19937_"
            "per_channel\",\n"
         << "    "
            "\"cdf97_fwt_and_wpt_supported_other_wavelets_and_predictor_search_"
            "modes_rejected\"";
  if (usesMetal)
    output
        << ",\n    \"metal_cdf97_float32_differs_from_cpu_float64_reference\"";
  output
      << "\n  ],\n"
      << "  \"width\": " << options.width << ",\n"
      << "  \"height\": " << options.height << ",\n"
      << "  \"target_fps\": " << options.targetFps << ",\n"
      << "  \"frames\": " << kernelFrameTimes.size() << ",\n"
      << "  \"expected_frames\": " << options.expectedFrames << ",\n"
      << "  \"initial_timing_capacity\": " << initialTimingCapacity << ",\n"
      << "  \"timing_capacity_growth_events\": " << timingCapacityGrowthEvents
      << ",\n"
      << "  \"warmup_frames\": " << warmupFrames << ",\n"
      << "  \"measured_frames\": " << measuredKernelTimes.size() << ",\n"
      << "  \"kernel_timing_scope\": \"post-warmup-lane-process-call-only\",\n"
      << "  \"stream_wall_timing_scope\": "
         "\"post-warmup-pre-read-through-completed-write\",\n"
      << "  \"all_frames_mean_process_ms\": " << allKernel.meanMilliseconds
      << ",\n"
      << "  \"all_frames_stream_wall_mean_ms\": " << allStream.meanMilliseconds
      << ",\n"
      << "  \"mean_process_ms\": " << kernel.meanMilliseconds << ",\n"
      << "  \"p95_process_ms\": " << kernel.p95Milliseconds << ",\n"
      << "  \"max_process_ms\": " << kernel.maximumMilliseconds << ",\n"
      << "  \"processing_fps\": " << kernel.framesPerSecond << ",\n"
      << "  \"kernel_mean_process_ms\": " << kernel.meanMilliseconds << ",\n"
      << "  \"kernel_p95_process_ms\": " << kernel.p95Milliseconds << ",\n"
      << "  \"kernel_max_process_ms\": " << kernel.maximumMilliseconds << ",\n"
      << "  \"kernel_processing_fps\": " << kernel.framesPerSecond << ",\n"
      << "  \"stream_wall_mean_ms\": " << stream.meanMilliseconds << ",\n"
      << "  \"stream_wall_p95_ms\": " << stream.p95Milliseconds << ",\n"
      << "  \"stream_wall_max_ms\": " << stream.maximumMilliseconds << ",\n"
      << "  \"stream_observed_fps\": " << stream.framesPerSecond << ",\n"
      << "  \"gpu_mean_ms\": " << gpu.meanMilliseconds << ",\n"
      << "  \"gpu_p95_ms\": " << gpu.p95Milliseconds << ",\n"
      << "  \"cpu_prepare_mean_ms\": " << cpuPrepare.meanMilliseconds << ",\n"
      << "  \"cpu_output_mean_ms\": " << cpuOutput.meanMilliseconds << ",\n"
      << "  \"mean_dependency_levels\": "
      << (kernelFrameTimes.empty()
              ? 0.0
              : dependencyLevelTotal /
                    static_cast<double>(kernelFrameTimes.size()))
      << ",\n"
      << "  \"mean_gpu_dispatches\": "
      << (kernelFrameTimes.empty()
              ? 0.0
              : gpuDispatchTotal / static_cast<double>(kernelFrameTimes.size()))
      << ",\n"
      << "  \"command_buffers_per_frame\": "
      << (kernelFrameTimes.empty()
              ? 0.0
              : commandBufferTotal /
                    static_cast<double>(kernelFrameTimes.size()))
      << ",\n"
      << "  \"cpu_gpu_waits_per_frame\": "
      << (kernelFrameTimes.empty()
              ? 0.0
              : completionWaitTotal /
                    static_cast<double>(kernelFrameTimes.size()))
      << ",\n"
      << "  \"mapped_buffer_copies_per_frame\": "
      << (kernelFrameTimes.empty()
              ? 0.0
              : mappedBufferCopyTotal /
                    static_cast<double>(kernelFrameTimes.size()))
      << ",\n"
      << "  \"realtime_policy\": {\n"
      << "    \"minimum_width\": " << kCertificationMinimumWidth << ",\n"
      << "    \"minimum_height\": " << kCertificationMinimumHeight << ",\n"
      << "    \"warmup_frames\": " << kCertificationWarmupFrames << ",\n"
      << "    \"minimum_measured_frames\": "
      << kCertificationMinimumMeasuredFrames << ",\n"
      << "    \"minimum_fps\": 30.000,\n"
      << "    \"target_fps_must_meet_minimum\": true,\n"
      << "    \"mean_and_p95_must_fit_frame_budget\": true,\n"
      << "    \"scope\": \"filter-process ingress-to-egress including pipe "
         "wait/backpressure; final mux excluded\"\n"
      << "  },\n"
      << "  \"kernel_realtime_30fps_passed\": "
      << (kernelRealtimePassed ? "true" : "false") << ",\n"
      << "  \"realtime_30fps_passed\": "
      << (streamRealtimePassed ? "true" : "false") << ",\n"
      << "  \"mean_segments_per_channel\": [";
  for (std::size_t channel = 0; channel < segmentTotals.size(); ++channel) {
    if (channel != 0)
      output << ", ";
    const double meanSegments =
        kernelFrameTimes.empty()
            ? 0.0
            : segmentTotals[channel] /
                  static_cast<double>(kernelFrameTimes.size());
    output << meanSegments;
  }
  output << "]\n}\n";
  return true;
}

} // namespace

int main(int argc, char **argv) {
  Options options;
  if (!parseOptions(argc, argv, options)) {
    printUsage(argv[0]);
    return 2;
  }

#if defined(_WIN32)
  _setmode(_fileno(stdin), _O_BINARY);
  _setmode(_fileno(stdout), _O_BINARY);
#endif
  glic::OriginalPresetConfig config;
  if (!glic::PresetLoader::loadOriginalPresetByName(options.presetsDirectory,
                                                    options.preset, config)) {
    std::cerr << "Failed to load original preset: " << options.preset << '\n';
    return 3;
  }
  const auto support = glic::evaluateOriginalRealtimeSupport(config);
  if (!support.supported) {
    std::cerr << "Unsupported original_visual preset: " << options.preset;
    for (const auto &reason : support.reasons)
      std::cerr << "; " << reason;
    std::cerr << '\n';
    return 4;
  }

  const bool usesMetal = options.backend == "metal";
  glic::OriginalRealtimeCpuLane cpuLane;
  std::unique_ptr<glic::OriginalRealtimeMetalLane> metalLane;
  std::string error;
  bool prepared = false;
  if (usesMetal) {
    metalLane = glic::createOriginalRealtimeMetalLane(error);
    prepared = metalLane != nullptr &&
               metalLane->prepare(options.width, options.height, config, error);
  } else {
    prepared = cpuLane.prepare(options.width, options.height, config, error);
  }
  if (!prepared) {
    std::cerr << "Failed to prepare original_visual lane: " << error << '\n';
    return 4;
  }
  if (options.checkOnly) {
    std::cerr << "supported original_visual preset=" << options.preset
              << " backend=" << options.backend << " width=" << options.width
              << " height=" << options.height << '\n';
    return 0;
  }

  const std::size_t width = static_cast<std::size_t>(options.width);
  const std::size_t height = static_cast<std::size_t>(options.height);
  if (width > std::numeric_limits<std::size_t>::max() / height) {
    std::cerr << "Frame dimensions overflow the address space\n";
    return 2;
  }
  const std::size_t pixelCount = width * height;
  if (pixelCount >
      std::numeric_limits<std::size_t>::max() / sizeof(glic::Color)) {
    std::cerr << "Frame dimensions overflow the address space\n";
    return 2;
  }
  const std::size_t frameBytes = pixelCount * sizeof(glic::Color);
  if (frameBytes >
      static_cast<std::size_t>(std::numeric_limits<std::streamsize>::max())) {
    std::cerr << "Frame is too large for the standard stream API\n";
    return 2;
  }

  std::vector<glic::Color> input(pixelCount);
  std::vector<glic::Color> output(pixelCount);
  constexpr std::size_t kIoBufferBytes = 1024 * 1024;
  std::vector<char> inputIoBuffer(kIoBufferBytes);
  std::vector<char> outputIoBuffer(kIoBufferBytes);
  if (std::setvbuf(stdin, inputIoBuffer.data(), _IOFBF, inputIoBuffer.size()) !=
          0 ||
      std::setvbuf(stdout, outputIoBuffer.data(), _IOFBF,
                   outputIoBuffer.size()) != 0) {
    std::cerr << "Failed to configure buffered frame I/O\n";
    return 2;
  }
  std::vector<double> kernelFrameTimes;
  std::vector<double> streamFrameTimes;
  std::vector<double> gpuFrameTimes;
  std::vector<double> cpuPrepareTimes;
  std::vector<double> cpuOutputTimes;
  const std::size_t initialTimingCapacity = options.expectedFrames > 0
                                                ? options.expectedFrames
                                                : kDefaultTimingCapacity;
  kernelFrameTimes.reserve(initialTimingCapacity);
  streamFrameTimes.reserve(initialTimingCapacity);
  gpuFrameTimes.reserve(initialTimingCapacity);
  cpuPrepareTimes.reserve(initialTimingCapacity);
  cpuOutputTimes.reserve(initialTimingCapacity);
  std::size_t timingCapacityGrowthEvents = 0;
  std::array<double, 3> segmentTotals{};
  double dependencyLevelTotal = 0.0;
  double gpuDispatchTotal = 0.0;
  double commandBufferTotal = 0.0;
  double completionWaitTotal = 0.0;
  double mappedBufferCopyTotal = 0.0;

  while (true) {
    const auto streamStart = std::chrono::steady_clock::now();
    const std::size_t bytesRead = std::fread(
        reinterpret_cast<char *>(input.data()), 1, frameBytes, stdin);
    if (bytesRead == 0) {
      if (std::feof(stdin))
        break;
      std::cerr << "Failed to read BGRA frame " << kernelFrameTimes.size()
                << '\n';
      return 5;
    }
    if (bytesRead != frameBytes) {
      std::cerr << "Incomplete BGRA frame at index " << kernelFrameTimes.size()
                << ": got " << bytesRead << " of " << frameBytes << " bytes\n";
      return 5;
    }

    glic::OriginalRealtimeFrameStats cpuFrameStats;
    glic::OriginalRealtimeMetalFrameStats metalFrameStats;
    const auto kernelStart = std::chrono::steady_clock::now();
    const bool processed =
        usesMetal ? metalLane->process(input, output, kernelFrameTimes.size(),
                                       &metalFrameStats, error)
                  : cpuLane.process(input, output, &cpuFrameStats, error);
    if (!processed) {
      std::cerr << "Frame " << kernelFrameTimes.size() << " failed: " << error
                << '\n';
      return 5;
    }
    const auto kernelFinish = std::chrono::steady_clock::now();
    if (kernelFrameTimes.size() == kernelFrameTimes.capacity())
      ++timingCapacityGrowthEvents;
    kernelFrameTimes.push_back(
        std::chrono::duration<double, std::milli>(kernelFinish - kernelStart)
            .count());
    for (std::size_t channel = 0; channel < segmentTotals.size(); ++channel) {
      segmentTotals[channel] += usesMetal
                                    ? metalFrameStats.segmentCounts[channel]
                                    : cpuFrameStats.segmentCounts[channel];
    }
    gpuFrameTimes.push_back(usesMetal ? metalFrameStats.gpuMilliseconds : 0.0);
    cpuPrepareTimes.push_back(usesMetal ? metalFrameStats.cpuPrepareMilliseconds
                                        : 0.0);
    cpuOutputTimes.push_back(usesMetal ? metalFrameStats.cpuOutputMilliseconds
                                       : 0.0);
    if (usesMetal) {
      dependencyLevelTotal +=
          static_cast<double>(metalFrameStats.dispatchLevels);
      gpuDispatchTotal += static_cast<double>(metalFrameStats.gpuDispatches);
      commandBufferTotal +=
          static_cast<double>(metalFrameStats.commandBufferSubmissions);
      completionWaitTotal +=
          static_cast<double>(metalFrameStats.completionWaits);
      mappedBufferCopyTotal +=
          static_cast<double>(metalFrameStats.mappedBufferCopies);
    }

    const std::size_t bytesWritten = std::fwrite(
        reinterpret_cast<const char *>(output.data()), 1, frameBytes, stdout);
    if (bytesWritten != frameBytes || std::fflush(stdout) != 0) {
      std::cerr << "Failed to write BGRA frame " << kernelFrameTimes.size() - 1
                << '\n';
      return 6;
    }
    const auto streamFinish = std::chrono::steady_clock::now();
    streamFrameTimes.push_back(
        std::chrono::duration<double, std::milli>(streamFinish - streamStart)
            .count());
  }

  if (kernelFrameTimes.empty()) {
    std::cerr << "No complete input frames were received\n";
    return 5;
  }
  if (!writeStats(options, kernelFrameTimes, streamFrameTimes, segmentTotals,
                  gpuFrameTimes, cpuPrepareTimes, cpuOutputTimes,
                  dependencyLevelTotal, gpuDispatchTotal, commandBufferTotal,
                  completionWaitTotal, mappedBufferCopyTotal,
                  initialTimingCapacity, timingCapacityGrowthEvents))
    return 7;

  const TimingSummary kernel = summarizeTiming(kernelFrameTimes);
  const TimingSummary stream = summarizeTiming(streamFrameTimes);
  std::cerr << "frames=" << kernelFrameTimes.size() << " backend="
            << (usesMetal ? "metal-original-visual" : "cpu-original-visual")
            << " preset=" << options.preset
            << " processing_mode=original_visual preset_semantics=original"
            << " kernel_fps=" << std::fixed << std::setprecision(3)
            << kernel.framesPerSecond
            << " stream_observed_fps=" << stream.framesPerSecond << '\n';
  return 0;
}
