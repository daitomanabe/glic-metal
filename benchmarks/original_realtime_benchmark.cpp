#include "glic.hpp"
#include "original_realtime.hpp"
#include "preset_loader.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <string>
#include <string_view>
#include <vector>

namespace {

constexpr int kCertificationMinimumWidth = 960;
constexpr int kCertificationMinimumHeight = 540;
constexpr int kCertificationMinimumFrames = 120;
constexpr int kCertificationMinimumWarmupFrames = 10;
constexpr double kCertificationMinimumFps = 30.0;

struct Options {
  std::string input;
  std::string preset = "default";
  std::string presetsDirectory = "presets";
  std::string outputImage;
  std::string outputDirectory;
  std::string jsonReport;
  int frames = kCertificationMinimumFrames;
  int warmupFrames = kCertificationMinimumWarmupFrames;
  double requiredFps = 30.0;
  bool allSupported = false;
};

struct Candidate {
  std::string preset;
  glic::OriginalPresetConfig config;
};

struct UnsupportedPreset {
  std::string preset;
  std::vector<std::string> reasons;
};

struct Result {
  std::string preset;
  double meanMilliseconds = 0.0;
  double p95Milliseconds = 0.0;
  double framesPerSecond = 0.0;
  double meanSegments = 0.0;
  bool processPassed = false;
  bool timingPassed = false;
  bool performancePassed = false;
  std::string error;
};

void printUsage(const char *program) {
  std::cout
      << "Usage: " << program << " <input-image> [options]\n"
      << "  --preset <name>          Original preset (default: default)\n"
      << "  --all-supported          Benchmark every preset supported by the\n"
         "                           fixed-predictor + exact CDF97 lane\n"
      << "  --presets-dir <path>     Original preset directory (default: presets)\n"
      << "  --frames <count>         Measured frames (default: 120)\n"
      << "  --warmup <count>         Warm-up frames (default: 10)\n"
      << "  --require-fps <value>    mean+p95 gate (default: 30)\n"
      << "  --output <png>           Save last frame (single preset only)\n"
      << "  --output-dir <path>      Save last frame for every supported preset\n"
      << "  --json <path>            Write machine-readable report\n";
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
  for (int index = 2; index < argc; ++index) {
    const std::string_view argument = argv[index];
    auto takeValue = [&]() -> const char * {
      return index + 1 < argc ? argv[++index] : nullptr;
    };

    if (argument == "--preset") {
      const char *value = takeValue();
      if (!value)
        return false;
      options.preset = value;
    } else if (argument == "--all-supported") {
      options.allSupported = true;
    } else if (argument == "--presets-dir") {
      const char *value = takeValue();
      if (!value)
        return false;
      options.presetsDirectory = value;
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
      if (!std::isfinite(options.requiredFps) || options.requiredFps <= 0.0)
        return false;
    } else if (argument == "--output") {
      const char *value = takeValue();
      if (!value)
        return false;
      options.outputImage = value;
    } else if (argument == "--output-dir") {
      const char *value = takeValue();
      if (!value)
        return false;
      options.outputDirectory = value;
    } else if (argument == "--json") {
      const char *value = takeValue();
      if (!value)
        return false;
      options.jsonReport = value;
    } else {
      return false;
    }
  }
  return !options.allSupported || options.outputImage.empty();
}

double percentile(std::vector<double> values, double fraction) {
  if (values.empty())
    return 0.0;
  std::sort(values.begin(), values.end());
  const auto index = static_cast<std::size_t>(
      std::ceil(static_cast<double>(values.size() - 1) * fraction));
  return values[std::min(index, values.size() - 1)];
}

std::string jsonEscape(std::string_view value) {
  std::string output;
  for (const char character : value) {
    switch (character) {
    case '\\':
      output += "\\\\";
      break;
    case '"':
      output += "\\\"";
      break;
    case '\n':
      output += "\\n";
      break;
    case '\r':
      output += "\\r";
      break;
    case '\t':
      output += "\\t";
      break;
    default:
      if (const auto code = static_cast<unsigned char>(character); code < 0x20) {
        static constexpr char digits[] = "0123456789abcdef";
        output += "\\u00";
        output += digits[(code >> 4) & 0x0f];
        output += digits[code & 0x0f];
      } else {
        output += character;
      }
      break;
    }
  }
  return output;
}

std::string outputFilename(std::string preset) {
  for (char &character : preset) {
    if (character == '/' || character == '\\' || character == ':')
      character = '_';
  }
  return preset + ".png";
}

void writeJson(const Options &options, int width, int height,
               std::size_t scannedPresets, std::size_t supportedPresets,
               const std::vector<UnsupportedPreset> &unsupportedPresets,
               const std::vector<std::string> &loadFailurePresets,
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
         << "  \"schema\": \"glic-original-realtime-cpu-benchmark-v1\",\n"
         << "  \"fidelity_lane\": "
            "\"upstream-colorspace-quadtree-fixed-predictor-quantize-"
            "reconstruct-exact-cdf97-no-serialization\",\n"
         << "  \"fidelity_claim\": "
            "\"original-style-algorithmic-core-not-processing-pixel-exact\",\n"
         << "  \"processing_pixel_exact\": false,\n"
         << "  \"unsupported_policy\": \"fail-closed\",\n"
         << "  \"known_deviations\": [\n"
         << "    \"glic_header_payload_serialization_and_entropy_encoding_omitted\",\n"
         << "    \"cpp_colorspace_and_arithmetic_not_byte_certified_against_processing_jvm\",\n"
         << "    \"processing_global_rng_replaced_by_independent_seeded_mt19937_per_channel\",\n"
         << "    \"non_cdf97_wavelets_random_transforms_and_predictor_search_modes_rejected\"\n"
         << "  ],\n"
         << "  \"backend\": \"cpu-reference\",\n"
         << "  \"width\": " << width << ",\n"
         << "  \"height\": " << height << ",\n"
         << "  \"frames\": " << options.frames << ",\n"
         << "  \"warmup_frames\": " << options.warmupFrames << ",\n"
         << "  \"required_fps\": " << options.requiredFps << ",\n"
         << "  \"certification_policy\": {\n"
         << "    \"minimum_width\": " << kCertificationMinimumWidth << ",\n"
         << "    \"minimum_height\": " << kCertificationMinimumHeight
         << ",\n"
         << "    \"minimum_measured_frames\": "
         << kCertificationMinimumFrames << ",\n"
         << "    \"minimum_warmup_frames\": "
         << kCertificationMinimumWarmupFrames << ",\n"
         << "    \"minimum_required_fps\": "
         << kCertificationMinimumFps << ",\n"
         << "    \"mean_and_p95_must_fit_frame_budget\": true\n"
         << "  },\n"
         << "  \"certification_evidence_passed\": "
         << ((width >= kCertificationMinimumWidth &&
              height >= kCertificationMinimumHeight &&
              options.frames >= kCertificationMinimumFrames &&
              options.warmupFrames >= kCertificationMinimumWarmupFrames &&
              options.requiredFps >= kCertificationMinimumFps)
                 ? "true"
                 : "false")
         << ",\n"
         << "  \"scanned_presets\": " << scannedPresets << ",\n"
         << "  \"decoded_presets\": "
         << (supportedPresets + unsupportedPresets.size()) << ",\n"
         << "  \"supported_presets\": " << supportedPresets << ",\n"
         << "  \"unsupported_presets\": "
         << unsupportedPresets.size() << ",\n"
         << "  \"load_failures\": " << loadFailurePresets.size() << ",\n"
         << "  \"unsupported_results\": [\n";
  for (std::size_t index = 0; index < unsupportedPresets.size(); ++index) {
    const auto &unsupported = unsupportedPresets[index];
    output << "    {\"preset\": \"" << jsonEscape(unsupported.preset)
           << "\", \"reasons\": [";
    for (std::size_t reasonIndex = 0; reasonIndex < unsupported.reasons.size();
         ++reasonIndex) {
      if (reasonIndex != 0)
        output << ", ";
      output << '"' << jsonEscape(unsupported.reasons[reasonIndex]) << '"';
    }
    output << "]}";
    if (index + 1 != unsupportedPresets.size())
      output << ',';
    output << '\n';
  }
  output << "  ],\n"
         << "  \"load_failure_presets\": [";
  for (std::size_t index = 0; index < loadFailurePresets.size(); ++index) {
    if (index != 0)
      output << ", ";
    output << '"' << jsonEscape(loadFailurePresets[index]) << '"';
  }
  output << "],\n"
         << "  \"results\": [\n";
  for (std::size_t index = 0; index < results.size(); ++index) {
    const auto &result = results[index];
    output << "    {\"preset\": \"" << jsonEscape(result.preset)
           << "\", \"mean_ms\": " << result.meanMilliseconds
           << ", \"p95_ms\": " << result.p95Milliseconds
           << ", \"fps\": " << result.framesPerSecond
           << ", \"mean_segments\": " << result.meanSegments
           << ", \"process_passed\": "
           << (result.processPassed ? "true" : "false")
           << ", \"timing_passed\": "
           << (result.timingPassed ? "true" : "false")
           << ", \"performance_passed\": "
           << (result.performancePassed ? "true" : "false")
           << ", \"error\": \"" << jsonEscape(result.error) << "\"}";
    if (index + 1 != results.size())
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
    std::cerr << "Failed to load input: " << options.input << '\n';
    return 3;
  }
  std::vector<glic::Color> output(input.size());

  std::vector<std::string> presetNames;
  if (options.allSupported) {
    presetNames = glic::PresetLoader::listPresets(options.presetsDirectory);
  } else {
    presetNames.push_back(options.preset);
  }
  const std::size_t scannedPresets = presetNames.size();

  std::vector<Candidate> candidates;
  std::vector<UnsupportedPreset> unsupportedPresets;
  std::vector<std::string> loadFailurePresets;
  for (const auto &preset : presetNames) {
    glic::OriginalPresetConfig config;
    if (!glic::PresetLoader::loadOriginalPresetByName(
            options.presetsDirectory, preset, config)) {
      loadFailurePresets.push_back(preset);
      std::cerr << preset << ": original preset load failed\n";
      continue;
    }
    const auto support = glic::evaluateOriginalRealtimeSupport(config);
    if (!support.supported) {
      if (!options.allSupported) {
        std::cerr << preset << ": unsupported original preset";
        for (const auto &reason : support.reasons)
          std::cerr << "; " << reason;
        std::cerr << '\n';
        return 5;
      }
      unsupportedPresets.push_back({preset, support.reasons});
      continue;
    }
    candidates.push_back({preset, config});
  }

  if (candidates.empty()) {
    std::cerr << "No supported original presets found\n";
    return 5;
  }
  if (!options.outputDirectory.empty()) {
    std::filesystem::create_directories(options.outputDirectory);
  }

  std::cout << "resolution=" << width << 'x' << height
            << " backend=cpu-reference scanned=" << scannedPresets
            << " supported=" << candidates.size()
            << " unsupported=" << unsupportedPresets.size()
            << " load_failures=" << loadFailurePresets.size() << '\n';
  std::cout << "preset\tmean_ms\tp95_ms\tfps\tmean_segments\t30fps\n";

  const double frameBudgetMilliseconds = 1000.0 / options.requiredFps;
  const bool certificationEvidencePassed =
      width >= kCertificationMinimumWidth &&
      height >= kCertificationMinimumHeight &&
      options.frames >= kCertificationMinimumFrames &&
      options.warmupFrames >= kCertificationMinimumWarmupFrames &&
      options.requiredFps >= kCertificationMinimumFps;
  bool allPassed = loadFailurePresets.empty();
  std::vector<Result> results;
  results.reserve(candidates.size());

  for (const auto &candidate : candidates) {
    Result result;
    result.preset = candidate.preset;
    std::string error;
    glic::OriginalRealtimeCpuLane lane;
    if (!lane.prepare(width, height, candidate.config, error)) {
      result.error = error;
      results.push_back(result);
      allPassed = false;
      continue;
    }

    bool processPassed = true;
    for (int frame = 0; frame < options.warmupFrames; ++frame) {
      if (!lane.process(input, output, nullptr, error)) {
        processPassed = false;
        break;
      }
    }

    std::vector<double> frameTimes;
    frameTimes.reserve(static_cast<std::size_t>(options.frames));
    double segmentTotal = 0.0;
    for (int frame = 0; processPassed && frame < options.frames; ++frame) {
      glic::OriginalRealtimeFrameStats stats;
      const auto start = std::chrono::steady_clock::now();
      processPassed = lane.process(input, output, &stats, error);
      const auto stop = std::chrono::steady_clock::now();
      if (processPassed) {
        frameTimes.push_back(
            std::chrono::duration<double, std::milli>(stop - start).count());
        segmentTotal += std::accumulate(stats.segmentCounts.begin(),
                                        stats.segmentCounts.end(), 0.0);
      }
    }

    result.processPassed = processPassed &&
                           frameTimes.size() ==
                               static_cast<std::size_t>(options.frames);
    if (result.processPassed) {
      result.meanMilliseconds =
          std::accumulate(frameTimes.begin(), frameTimes.end(), 0.0) /
          static_cast<double>(frameTimes.size());
      result.p95Milliseconds = percentile(frameTimes, 0.95);
      result.framesPerSecond = 1000.0 / result.meanMilliseconds;
      result.meanSegments = segmentTotal / frameTimes.size();
      result.timingPassed =
          result.meanMilliseconds <= frameBudgetMilliseconds &&
          result.p95Milliseconds <= frameBudgetMilliseconds;
      result.performancePassed =
          certificationEvidencePassed && result.timingPassed;

      std::string outputPath;
      if (!options.outputImage.empty())
        outputPath = options.outputImage;
      else if (!options.outputDirectory.empty())
        outputPath = (std::filesystem::path(options.outputDirectory) /
                      outputFilename(candidate.preset))
                         .string();
      if (!outputPath.empty() &&
          !glic::saveImage(outputPath, output, width, height)) {
        result.processPassed = false;
        result.performancePassed = false;
        result.error = "failed to save output image";
      }
    } else {
      result.error = error;
    }

    allPassed = allPassed && result.processPassed && result.performancePassed;
    std::cout << std::fixed << std::setprecision(3) << candidate.preset << '\t'
              << result.meanMilliseconds << '\t' << result.p95Milliseconds
              << '\t' << result.framesPerSecond << '\t'
              << result.meanSegments << '\t'
              << (result.performancePassed ? "PASS" : "FAIL") << '\n';
    results.push_back(std::move(result));
  }

  writeJson(options, width, height, scannedPresets, candidates.size(),
            unsupportedPresets, loadFailurePresets, results);
  return allPassed ? 0 : 1;
}
