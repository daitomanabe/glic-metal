#include "preset_loader.hpp"
#include "realtime.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <string>
#include <string_view>
#include <vector>

#if defined(_WIN32)
#include <fcntl.h>
#include <io.h>
#endif

namespace {

struct Options {
  int width = 0;
  int height = 0;
  std::string preset = "default";
  std::string presetsDirectory = "presets";
  std::string backend = "auto";
  std::string statsJson;
  float strength = 1.0f;
  bool passthrough = false;
};

void printUsage(const char *program) {
  std::cerr
      << "Usage: " << program
      << " --width <pixels> --height <pixels> [options]\n"
      << "Reads packed BGRA8 frames from stdin and writes packed BGRA8 frames "
         "to stdout.\n"
      << "  --preset <name>          Preset name (default: default)\n"
      << "  --presets-dir <path>     Preset directory (default: presets)\n"
      << "  --backend <auto|cpu|metal>\n"
      << "  --strength <0..2>        Glitch intensity (default: 1)\n"
      << "  --passthrough             Copy frames unchanged for A/B "
         "calibration\n"
      << "  --stats-json <path>      Write processing statistics\n";
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

bool parseStrength(std::string_view text, float &value) {
  try {
    const float parsed = std::stof(std::string(text));
    if (!std::isfinite(parsed) || parsed < 0.0f || parsed > 2.0f)
      return false;
    value = parsed;
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
      if (value == nullptr || !parsePositiveInt(value, options.width))
        return false;
    } else if (argument == "--height") {
      const char *value = takeValue();
      if (value == nullptr || !parsePositiveInt(value, options.height))
        return false;
    } else if (argument == "--preset") {
      const char *value = takeValue();
      if (value == nullptr)
        return false;
      options.preset = value;
    } else if (argument == "--presets-dir") {
      const char *value = takeValue();
      if (value == nullptr)
        return false;
      options.presetsDirectory = value;
    } else if (argument == "--backend") {
      const char *value = takeValue();
      if (value == nullptr)
        return false;
      options.backend = value;
    } else if (argument == "--strength") {
      const char *value = takeValue();
      if (value == nullptr || !parseStrength(value, options.strength))
        return false;
    } else if (argument == "--stats-json") {
      const char *value = takeValue();
      if (value == nullptr)
        return false;
      options.statsJson = value;
    } else if (argument == "--passthrough") {
      options.passthrough = true;
    } else if (argument == "--help" || argument == "-h") {
      return false;
    } else {
      std::cerr << "Unknown argument: " << argument << '\n';
      return false;
    }
  }

  return options.width > 0 && options.height > 0 &&
         (options.backend == "auto" || options.backend == "cpu" ||
          options.backend == "metal");
}

void writeStats(const Options &options, const std::string &preset,
                const char *backend, uint64_t frames, double totalMilliseconds,
                double maximumMilliseconds, double totalGpuMilliseconds) {
  if (options.statsJson.empty())
    return;

  std::ofstream output(options.statsJson);
  if (!output) {
    std::cerr << "Failed to write stats JSON: " << options.statsJson << '\n';
    return;
  }

  const double meanMilliseconds =
      frames == 0 ? 0.0 : totalMilliseconds / static_cast<double>(frames);
  const double meanGpuMilliseconds =
      frames == 0 ? 0.0 : totalGpuMilliseconds / static_cast<double>(frames);
  const double processingFps =
      totalMilliseconds <= 0.0
          ? 0.0
          : static_cast<double>(frames) * 1000.0 / totalMilliseconds;

  output << std::fixed << std::setprecision(3) << "{\n"
         << "  \"schema\": \"glic-realtime-filter-v1\",\n"
         << "  \"width\": " << options.width << ",\n"
         << "  \"height\": " << options.height << ",\n"
         << "  \"preset\": \"" << preset << "\",\n"
         << "  \"backend\": \"" << backend << "\",\n"
         << "  \"strength\": " << options.strength << ",\n"
         << "  \"frames\": " << frames << ",\n"
         << "  \"mean_process_ms\": " << meanMilliseconds << ",\n"
         << "  \"max_process_ms\": " << maximumMilliseconds << ",\n"
         << "  \"mean_gpu_ms\": " << meanGpuMilliseconds << ",\n"
         << "  \"processing_fps\": " << processingFps << "\n"
         << "}\n";
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
  std::ios::sync_with_stdio(false);
  std::cin.tie(nullptr);

  std::string error;
  std::unique_ptr<glic::RealtimeBackend> backend;
  std::string backendName = "passthrough";
  std::string presetName = "passthrough";
  if (!options.passthrough) {
    glic::CodecConfig config;
    if (!glic::PresetLoader::loadPresetByName(options.presetsDirectory,
                                              options.preset, config)) {
      std::cerr << "Failed to load preset: " << options.preset << '\n';
      return 3;
    }

    backend = glic::createRealtimeBackend(
        glic::realtimeBackendKindFromName(options.backend), error);
    if (!backend) {
      std::cerr << "Failed to create realtime backend: " << error << '\n';
      return 4;
    }

    glic::RealtimePrepareOptions prepareOptions{.width = options.width,
                                                .height = options.height,
                                                .config = config,
                                                .seed = 0x474C4943u,
                                                .effectStrength =
                                                    options.strength};
    if (!backend->prepare(prepareOptions, error)) {
      std::cerr << "Failed to prepare realtime backend: " << error << '\n';
      return 4;
    }
    backendName = backend->name();
    presetName = options.preset;
  }

  const size_t width = static_cast<size_t>(options.width);
  const size_t height = static_cast<size_t>(options.height);
  if (width > std::numeric_limits<size_t>::max() / height) {
    std::cerr << "Frame dimensions overflow the address space\n";
    return 2;
  }
  const size_t pixelCount = width * height;
  if (pixelCount > std::numeric_limits<size_t>::max() / sizeof(glic::Color)) {
    std::cerr << "Frame dimensions overflow the address space\n";
    return 2;
  }
  const size_t frameBytes = pixelCount * sizeof(glic::Color);
  if (frameBytes >
      static_cast<size_t>(std::numeric_limits<std::streamsize>::max())) {
    std::cerr << "Frame is too large for the standard stream API\n";
    return 2;
  }
  std::vector<glic::Color> input(pixelCount);
  std::vector<glic::Color> output(pixelCount);

  uint64_t frameIndex = 0;
  double totalMilliseconds = 0.0;
  double maximumMilliseconds = 0.0;
  double totalGpuMilliseconds = 0.0;

  while (true) {
    std::cin.read(reinterpret_cast<char *>(input.data()),
                  static_cast<std::streamsize>(frameBytes));
    const std::streamsize bytesRead = std::cin.gcount();
    if (bytesRead == 0 && std::cin.eof())
      break;
    if (bytesRead != static_cast<std::streamsize>(frameBytes)) {
      std::cerr << "Incomplete BGRA frame at index " << frameIndex << ": got "
                << bytesRead << " of " << frameBytes << " bytes\n";
      return 5;
    }

    const auto start = std::chrono::steady_clock::now();
    if (options.passthrough) {
      std::copy(input.begin(), input.end(), output.begin());
    } else {
      if (!backend->process(input, output, frameIndex, error)) {
        std::cerr << "Frame " << frameIndex << " failed: " << error << '\n';
        return 5;
      }
    }
    const auto finish = std::chrono::steady_clock::now();
    const double milliseconds =
        std::chrono::duration<double, std::milli>(finish - start).count();
    totalMilliseconds += milliseconds;
    maximumMilliseconds = std::max(maximumMilliseconds, milliseconds);
    if (backend)
      totalGpuMilliseconds += backend->lastFrameStats().gpuMilliseconds;

    std::cout.write(reinterpret_cast<const char *>(output.data()),
                    static_cast<std::streamsize>(frameBytes));
    if (!std::cout) {
      std::cerr << "Failed to write BGRA frame " << frameIndex << '\n';
      return 6;
    }
    ++frameIndex;
  }

  writeStats(options, presetName, backendName.c_str(), frameIndex,
             totalMilliseconds, maximumMilliseconds, totalGpuMilliseconds);
  const double fps =
      totalMilliseconds <= 0.0
          ? 0.0
          : static_cast<double>(frameIndex) * 1000.0 / totalMilliseconds;
  std::cerr << "frames=" << frameIndex << " backend=" << backendName
            << " preset=" << presetName << " strength=" << options.strength
            << " processing_fps=" << std::fixed << std::setprecision(3) << fps
            << '\n';
  return frameIndex == 0 ? 5 : 0;
}
