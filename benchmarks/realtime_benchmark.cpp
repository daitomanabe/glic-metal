#include "glic.hpp"
#include "preset_loader.hpp"
#include "realtime.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace {

struct Options {
  std::string input;
  std::string preset = "default";
  std::string presetsDirectory = "presets";
  std::string backend = "auto";
  std::string outputImage;
  std::string jsonReport;
  int frames = 120;
  int warmupFrames = 10;
  double requiredFps = 15.0;
  bool allPresets = false;
};

struct Result {
  std::string preset;
  std::string backend;
  double medianMilliseconds = 0.0;
  double p95Milliseconds = 0.0;
  double meanMilliseconds = 0.0;
  double medianGpuMilliseconds = 0.0;
  double framesPerSecond = 0.0;
  bool processPassed = false;
  bool performancePassed = false;
  std::string error;
};

void printUsage(const char *program) {
  std::cout
      << "Usage: " << program << " <input-image> [options]\n"
      << "  --preset <name>          Preset to benchmark (default: default)\n"
      << "  --all-presets            Benchmark every preset\n"
      << "  --presets-dir <path>     Preset directory (default: presets)\n"
      << "  --backend <auto|cpu|metal>\n"
      << "  --frames <count>         Measured frames (default: 120)\n"
      << "  --warmup <count>         Warm-up frames (default: 10)\n"
      << "  --require-fps <value>    p95 performance gate (default: 15)\n"
      << "  --output <png>           Save the final processed frame\n"
      << "  --json <path>            Write a machine-readable report\n";
}

bool parsePositiveInt(std::string_view value, int &destination) {
  try {
    const int parsed = std::stoi(std::string(value));
    if (parsed <= 0)
      return false;
    destination = parsed;
    return true;
  } catch (...) {
    return false;
  }
}

bool parseOptions(int argc, char **argv, Options &options) {
  if (argc < 2)
    return false;
  options.input = argv[1];
  for (int i = 2; i < argc; ++i) {
    const std::string_view argument = argv[i];
    auto takeValue = [&]() -> const char * {
      return i + 1 < argc ? argv[++i] : nullptr;
    };

    if (argument == "--preset") {
      const char *value = takeValue();
      if (!value)
        return false;
      options.preset = value;
    } else if (argument == "--all-presets") {
      options.allPresets = true;
    } else if (argument == "--presets-dir") {
      const char *value = takeValue();
      if (!value)
        return false;
      options.presetsDirectory = value;
    } else if (argument == "--backend") {
      const char *value = takeValue();
      if (!value)
        return false;
      options.backend = value;
    } else if (argument == "--frames") {
      const char *value = takeValue();
      if (!value || !parsePositiveInt(value, options.frames))
        return false;
    } else if (argument == "--warmup") {
      const char *value = takeValue();
      if (!value || !parsePositiveInt(value, options.warmupFrames))
        return false;
    } else if (argument == "--require-fps") {
      const char *value = takeValue();
      if (!value)
        return false;
      try {
        options.requiredFps = std::stod(value);
      } catch (...) {
        return false;
      }
      if (options.requiredFps <= 0.0)
        return false;
    } else if (argument == "--output") {
      const char *value = takeValue();
      if (!value)
        return false;
      options.outputImage = value;
    } else if (argument == "--json") {
      const char *value = takeValue();
      if (!value)
        return false;
      options.jsonReport = value;
    } else if (argument == "--help" || argument == "-h") {
      return false;
    } else {
      std::cerr << "Unknown argument: " << argument << '\n';
      return false;
    }
  }
  return true;
}

double percentile(std::vector<double> values, double fraction) {
  if (values.empty())
    return 0.0;
  std::sort(values.begin(), values.end());
  const size_t index =
      static_cast<size_t>(std::ceil((values.size() - 1) * fraction));
  return values[std::min(index, values.size() - 1)];
}

std::string jsonEscape(std::string_view value) {
  std::string result;
  result.reserve(value.size() + 8);
  for (const char character : value) {
    switch (character) {
    case '\\':
      result += "\\\\";
      break;
    case '"':
      result += "\\\"";
      break;
    case '\n':
      result += "\\n";
      break;
    case '\r':
      result += "\\r";
      break;
    case '\t':
      result += "\\t";
      break;
    default:
      result += character;
      break;
    }
  }
  return result;
}

void writeJsonReport(const Options &options, int width, int height,
                     const std::vector<Result> &results) {
  if (options.jsonReport.empty())
    return;
  std::ofstream output(options.jsonReport);
  if (!output) {
    std::cerr << "Failed to write JSON report: " << options.jsonReport << '\n';
    return;
  }

  output << std::fixed << std::setprecision(3);
  output << "{\n"
         << "  \"schema\": \"glic-realtime-benchmark-v1\",\n"
         << "  \"width\": " << width << ",\n"
         << "  \"height\": " << height << ",\n"
         << "  \"frames\": " << options.frames << ",\n"
         << "  \"warmup_frames\": " << options.warmupFrames << ",\n"
         << "  \"required_fps\": " << options.requiredFps << ",\n"
         << "  \"results\": [\n";
  for (size_t i = 0; i < results.size(); ++i) {
    const auto &result = results[i];
    output << "    {\"preset\": \"" << jsonEscape(result.preset)
           << "\", \"backend\": \"" << jsonEscape(result.backend)
           << "\", \"median_ms\": " << result.medianMilliseconds
           << ", \"p95_ms\": " << result.p95Milliseconds
           << ", \"mean_ms\": " << result.meanMilliseconds
           << ", \"median_gpu_ms\": " << result.medianGpuMilliseconds
           << ", \"fps\": " << result.framesPerSecond
           << ", \"process_passed\": "
           << (result.processPassed ? "true" : "false")
           << ", \"performance_passed\": "
           << (result.performancePassed ? "true" : "false") << ", \"error\": \""
           << jsonEscape(result.error) << "\"}";
    if (i + 1 != results.size())
      output << ',';
    output << '\n';
  }
  output << "  ]\n}\n";
}

} // namespace

int main(int argc, char **argv) {
  Options options;
  if (!parseOptions(argc, argv, options)) {
    printUsage(argv[0]);
    return 2;
  }

  std::vector<glic::Color> input;
  int width = 0;
  int height = 0;
  if (!glic::loadImage(options.input, input, width, height)) {
    std::cerr << "Failed to load input image: " << options.input << '\n';
    return 3;
  }
  std::vector<glic::Color> output(input.size());

  std::vector<std::string> presets;
  if (options.allPresets)
    presets = glic::PresetLoader::listPresets(options.presetsDirectory);
  else
    presets.push_back(options.preset);
  if (presets.empty()) {
    std::cerr << "No presets found in: " << options.presetsDirectory << '\n';
    return 3;
  }

  std::string error;
  const auto requestedBackend =
      glic::realtimeBackendKindFromName(options.backend);
  auto backend = glic::createRealtimeBackend(requestedBackend, error);
  if (!backend) {
    std::cerr << "Failed to create realtime backend: " << error << '\n';
    return 4;
  }

  std::cout << "resolution=" << width << 'x' << height
            << " backend=" << backend->name() << " presets=" << presets.size()
            << " frames=" << options.frames
            << " warmup=" << options.warmupFrames << '\n';
  std::cout << "preset\tbackend\tmedian_ms\tp95_ms\tmean_ms\tmedian_gpu_"
               "ms\tfps\tpass\n";

  std::vector<Result> results;
  results.reserve(presets.size());
  bool allPassed = true;
  uint64_t frameBase = 0;

  for (const auto &preset : presets) {
    Result result;
    result.preset = preset;
    result.backend = backend->name();

    glic::CodecConfig config;
    if (!glic::PresetLoader::loadPresetByName(options.presetsDirectory, preset,
                                              config)) {
      result.error = "preset load failed";
      results.push_back(result);
      allPassed = false;
      continue;
    }

    glic::RealtimePrepareOptions prepareOptions{.width = width,
                                                .height = height,
                                                .config = config,
                                                .seed = 0x474C4943u};
    if (!backend->prepare(prepareOptions, error)) {
      result.error = error;
      results.push_back(result);
      allPassed = false;
      continue;
    }

    bool processPassed = true;
    for (int frame = 0; frame < options.warmupFrames; ++frame) {
      if (!backend->process(input, output,
                            frameBase + static_cast<uint64_t>(frame), error)) {
        processPassed = false;
        break;
      }
    }

    std::vector<double> frameTimes;
    std::vector<double> gpuTimes;
    frameTimes.reserve(static_cast<size_t>(options.frames));
    gpuTimes.reserve(static_cast<size_t>(options.frames));
    if (processPassed) {
      for (int frame = 0; frame < options.frames; ++frame) {
        const uint64_t frameIndex =
            frameBase + static_cast<uint64_t>(options.warmupFrames + frame);
        const auto start = std::chrono::steady_clock::now();
        if (!backend->process(input, output, frameIndex, error)) {
          processPassed = false;
          break;
        }
        const auto finish = std::chrono::steady_clock::now();
        frameTimes.push_back(
            std::chrono::duration<double, std::milli>(finish - start).count());
        gpuTimes.push_back(backend->lastFrameStats().gpuMilliseconds);
      }
    }

    result.processPassed =
        processPassed &&
        frameTimes.size() == static_cast<size_t>(options.frames);
    if (result.processPassed) {
      result.medianMilliseconds = percentile(frameTimes, 0.5);
      result.p95Milliseconds = percentile(frameTimes, 0.95);
      result.meanMilliseconds =
          std::accumulate(frameTimes.begin(), frameTimes.end(), 0.0) /
          frameTimes.size();
      result.medianGpuMilliseconds = percentile(gpuTimes, 0.5);
      result.framesPerSecond = 1000.0 / result.medianMilliseconds;
      result.performancePassed =
          result.p95Milliseconds <= (1000.0 / options.requiredFps);
    } else {
      result.error = error;
    }

    allPassed = allPassed && result.processPassed && result.performancePassed;
    std::cout << result.preset << '\t' << result.backend << '\t' << std::fixed
              << std::setprecision(3) << result.medianMilliseconds << '\t'
              << result.p95Milliseconds << '\t' << result.meanMilliseconds
              << '\t' << result.medianGpuMilliseconds << '\t'
              << result.framesPerSecond << '\t'
              << (result.processPassed && result.performancePassed ? "PASS"
                                                                   : "FAIL")
              << '\n';

    results.push_back(result);
    frameBase +=
        static_cast<uint64_t>(options.frames + options.warmupFrames + 1);
  }

  if (!options.outputImage.empty() && presets.size() == 1) {
    if (!glic::saveImage(options.outputImage, output, width, height)) {
      std::cerr << "Failed to save output image: " << options.outputImage
                << '\n';
      allPassed = false;
    }
  }
  writeJsonReport(options, width, height, results);

  return allPassed ? 0 : 5;
}
