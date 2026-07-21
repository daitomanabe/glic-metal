#include "glic.hpp"
#include "original_realtime.hpp"
#include "original_realtime_metal.hpp"
#include "preset_loader.hpp"

#include <algorithm>
#include <array>
#include <bit>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <numeric>
#include <sstream>
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
  std::string backend = "cpu";
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
  std::string presetConfigFnv1a64;
  std::string outputPreviewFileFnv1a64;
  double meanMilliseconds = 0.0;
  double p95Milliseconds = 0.0;
  double framesPerSecond = 0.0;
  double meanSegments = 0.0;
  double meanGpuMilliseconds = 0.0;
  double meanCpuPrepareMilliseconds = 0.0;
  double meanCpuOutputMilliseconds = 0.0;
  double meanDependencyLevels = 0.0;
  double meanGpuDispatches = 0.0;
  double meanThreadgroupPipelineDispatches = 0.0;
  double meanThreadgroupPipelineSegments = 0.0;
  double meanGlobalPipelineDispatches = 0.0;
  double meanGlobalPipelineSegments = 0.0;
  double meanBufferBarriers = 0.0;
  double meanEarlyTerminatedNodes = 0.0;
  double meanEarlySkippedSamples = 0.0;
  std::string lastSegmentationRngState = "0000000000000000";
  std::string lastSegmentOrderFnv1a64 = "0000000000000000";
  double staticScheduleReuseRatio = 0.0;
  bool pipelineAccountingPassed = true;
  bool usesCdf97 = false;
  int colorSpace = 0;
  int minBlockSize = 0;
  int maxBlockSize = 0;
  int predictionMethod = 0;
  int transformType = 0;
  int transformScale = 0;
  float transformCompress = 0.0f;
  float transformCompressionThreshold = 0.0f;
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
      << "  --presets-dir <path>     Original preset directory (default: "
         "presets)\n"
      << "  --backend <cpu|metal>    Reconstruction backend (default: cpu)\n"
      << "  --frames <count>         Measured frames (default: 120)\n"
      << "  --warmup <count>         Warm-up frames (default: 10)\n"
      << "  --require-fps <value>    mean+p95 gate (default: 30)\n"
      << "  --output <png>           Save last frame (single preset only)\n"
      << "  --output-dir <path>      Save last frame for every supported "
         "preset\n"
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
    } else if (argument == "--backend") {
      const char *value = takeValue();
      if (!value || (std::string_view(value) != "cpu" &&
                     std::string_view(value) != "metal"))
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
      if (const auto code = static_cast<unsigned char>(character);
          code < 0x20) {
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

constexpr uint64_t kFnv1a64Offset = 14695981039346656037ull;
constexpr uint64_t kFnv1a64Prime = 1099511628211ull;

void fnvAppendByte(uint64_t &hash, uint8_t value) noexcept {
  hash ^= value;
  hash *= kFnv1a64Prime;
}

void fnvAppendU32(uint64_t &hash, uint32_t value) noexcept {
  for (unsigned shift = 0; shift < 32; shift += 8)
    fnvAppendByte(hash, static_cast<uint8_t>((value >> shift) & 0xffu));
}

void fnvAppendI32(uint64_t &hash, int value) noexcept {
  fnvAppendU32(hash, static_cast<uint32_t>(static_cast<int32_t>(value)));
}

void fnvAppendFloat(uint64_t &hash, float value) noexcept {
  fnvAppendU32(hash, std::bit_cast<uint32_t>(value));
}

std::string fnvHex(uint64_t hash) {
  std::ostringstream text;
  text << std::hex << std::setfill('0') << std::setw(16) << hash;
  return text.str();
}

std::string decodedPixelHash(const std::vector<glic::Color> &pixels) {
  uint64_t hash = kFnv1a64Offset;
  for (const glic::Color color : pixels) {
    const uint32_t value = static_cast<uint32_t>(color);
    fnvAppendU32(hash, value);
  }
  return fnvHex(hash);
}

std::string presetConfigHash(const glic::OriginalPresetConfig &config) {
  uint64_t hash = kFnv1a64Offset;
  constexpr std::string_view schema = "glic-original-preset-config-v1";
  for (const char value : schema)
    fnvAppendByte(hash, static_cast<uint8_t>(value));

  fnvAppendI32(hash, static_cast<int>(config.colorSpace));
  fnvAppendByte(hash, config.borderColorR);
  fnvAppendByte(hash, config.borderColorG);
  fnvAppendByte(hash, config.borderColorB);
  fnvAppendByte(hash, config.separateChannels ? 1u : 0u);
  for (const auto &channel : config.channels) {
    fnvAppendFloat(hash, channel.minBlockExponent);
    fnvAppendFloat(hash, channel.maxBlockExponent);
    fnvAppendI32(hash, channel.minBlockSize);
    fnvAppendI32(hash, channel.maxBlockSize);
    fnvAppendFloat(hash, channel.segmentationPrecision);
    fnvAppendI32(hash, channel.predictionListIndex);
    fnvAppendI32(hash, static_cast<int>(channel.predictionMethod));
    fnvAppendFloat(hash, channel.quantizationControllerValue);
    fnvAppendI32(hash, channel.quantizationValue);
    fnvAppendFloat(hash, channel.quantizationStep);
    fnvAppendI32(hash, static_cast<int>(channel.clampMethod));
    fnvAppendI32(hash, channel.originalWaveletId);
    fnvAppendFloat(hash, channel.transformCompressControllerValue);
    fnvAppendFloat(hash, channel.transformCompress);
    fnvAppendFloat(hash, channel.transformCompressionThreshold);
    fnvAppendFloat(hash, channel.transformScaleExponent);
    fnvAppendI32(hash, channel.transformScale);
    fnvAppendI32(hash, channel.originalTransformType);
    fnvAppendI32(hash, static_cast<int>(channel.encodingMethod));
  }
  return fnvHex(hash);
}

bool fileFnv1a64(const std::string &path, std::string &outputHash) {
  std::ifstream input(path, std::ios::binary);
  if (!input)
    return false;
  uint64_t hash = kFnv1a64Offset;
  std::array<char, 64 * 1024> buffer{};
  while (input) {
    input.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
    const auto count = input.gcount();
    for (std::streamsize index = 0; index < count; ++index)
      fnvAppendByte(hash, static_cast<uint8_t>(buffer[index]));
  }
  if (input.bad())
    return false;
  outputHash = fnvHex(hash);
  return true;
}

std::string absoluteInputPath(const std::string &input) {
  std::error_code error;
  const auto path = std::filesystem::absolute(input, error);
  return error ? input : path.lexically_normal().string();
}

bool writeJson(const Options &options, int width, int height,
               const std::string &inputPixelHash, std::size_t scannedPresets,
               std::size_t supportedPresets,
               const std::vector<UnsupportedPreset> &unsupportedPresets,
               const std::vector<std::string> &loadFailurePresets,
               const std::vector<Result> &results) {
  if (options.jsonReport.empty())
    return true;
  std::ofstream output(options.jsonReport);
  if (!output) {
    std::cerr << "Failed to write JSON report: " << options.jsonReport << '\n';
    return false;
  }
  const bool usesMetal = options.backend == "metal";
  output << std::fixed << std::setprecision(3);
  output
      << "{\n"
      << "  \"schema\": \""
      << (usesMetal ? "glic-original-realtime-metal-benchmark-v1"
                    : "glic-original-realtime-cpu-benchmark-v1")
      << "\",\n"
      << "  \"fidelity_lane\": "
         "\"upstream-colorspace-quadtree-fixed-predictor-quantize-"
      << (usesMetal ? "reconstruct-float-float-cdf97-fp32-storage-no-serialization"
                    : "reconstruct-exact-cdf97-no-serialization")
      << "\",\n"
      << "  \"fidelity_claim\": "
         "\"original-style-algorithmic-core-not-processing-pixel-exact\",\n"
      << "  \"processing_pixel_exact\": false,\n"
      << "  \"unsupported_policy\": \"fail-closed\",\n"
      << "  \"known_deviations\": [\n"
      << "    "
         "\"glic_header_payload_serialization_and_entropy_encoding_omitted\",\n"
      << "    "
         "\"colorspace_and_non_golden_arithmetic_not_byte_certified_against_"
         "processing_jvm\",\n"
      << "    "
         "\"processing_rng_seed_fixed_to_42_while_original_sketch_default_"
         "seed_is_unpinned\",\n"
      << "    "
         "\"non_cdf97_wavelets_random_transforms_and_predictor_search_modes_"
         "rejected\"";
  if (usesMetal)
    output
        << ",\n    \"metal_cdf97_fp32_matrix_storage_differs_from_cpu_float64_reference\"";
  output << "\n  ],\n"
         << "  \"processing_rounding_compatible\": true,\n"
         << "  \"processing_raw_plane_pack_compatible\": true,\n"
         << "  \"processing_rng_and_cross_channel_order_compatible\": true,\n"
         << "  \"backend\": \""
         << (usesMetal ? "metal-original-visual" : "cpu-reference") << "\",\n"
         << "  \"execution_mode\": \""
         << (usesMetal ? "hybrid_cpu_colorspace_segmentation_gpu_reconstruction"
                       : "cpu_parallel_channels")
         << "\",\n"
         << "  \"cdf97_precision\": \""
         << (usesMetal ? "float-float-accumulation-fp32-storage-safe-math"
                       : "float64")
         << "\",\n"
         << "  \"input_path\": \""
         << jsonEscape(absoluteInputPath(options.input)) << "\",\n"
         << "  \"input_decoded_color_fnv1a64\": \"" << inputPixelHash << "\",\n"
         << "  \"output_preview_semantics\": \"last_measured_frame\",\n"
         << "  \"output_preview_frame_index\": "
         << (static_cast<uint64_t>(options.warmupFrames) +
             static_cast<uint64_t>(options.frames) - 1u)
         << ",\n"
         << "  \"width\": " << width << ",\n"
         << "  \"height\": " << height << ",\n"
         << "  \"frames\": " << options.frames << ",\n"
         << "  \"warmup_frames\": " << options.warmupFrames << ",\n"
         << "  \"required_fps\": " << options.requiredFps << ",\n"
         << "  \"certification_policy\": {\n"
         << "    \"minimum_width\": " << kCertificationMinimumWidth << ",\n"
         << "    \"minimum_height\": " << kCertificationMinimumHeight << ",\n"
         << "    \"minimum_measured_frames\": " << kCertificationMinimumFrames
         << ",\n"
         << "    \"minimum_warmup_frames\": "
         << kCertificationMinimumWarmupFrames << ",\n"
         << "    \"minimum_required_fps\": " << kCertificationMinimumFps
         << ",\n"
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
         << "  \"unsupported_presets\": " << unsupportedPresets.size() << ",\n"
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
           << "\", \"preset_config_fnv1a64\": \""
           << result.presetConfigFnv1a64
           << "\", \"output_preview_file_fnv1a64\": \""
           << result.outputPreviewFileFnv1a64
           << "\", \"mean_ms\": " << result.meanMilliseconds
           << ", \"p95_ms\": " << result.p95Milliseconds
           << ", \"fps\": " << result.framesPerSecond
           << ", \"mean_segments\": " << result.meanSegments
           << ", \"mean_gpu_ms\": " << result.meanGpuMilliseconds
           << ", \"mean_cpu_prepare_ms\": " << result.meanCpuPrepareMilliseconds
           << ", \"mean_cpu_output_ms\": " << result.meanCpuOutputMilliseconds
           << ", \"mean_dependency_levels\": " << result.meanDependencyLevels
           << ", \"mean_gpu_dispatches\": " << result.meanGpuDispatches
           << ", \"mean_threadgroup_pipeline_dispatches\": "
           << result.meanThreadgroupPipelineDispatches
           << ", \"mean_threadgroup_pipeline_segments\": "
           << result.meanThreadgroupPipelineSegments
           << ", \"mean_global_pipeline_dispatches\": "
           << result.meanGlobalPipelineDispatches
           << ", \"mean_global_pipeline_segments\": "
           << result.meanGlobalPipelineSegments
           << ", \"mean_buffer_barriers\": " << result.meanBufferBarriers
           << ", \"mean_early_terminated_nodes\": "
           << result.meanEarlyTerminatedNodes
           << ", \"mean_early_skipped_samples\": "
           << result.meanEarlySkippedSamples
           << ", \"last_segmentation_rng_state\": \""
           << result.lastSegmentationRngState
           << "\", \"last_segment_order_fnv1a64\": \""
           << result.lastSegmentOrderFnv1a64 << "\""
           << ", \"pipeline_accounting_passed\": "
           << (result.pipelineAccountingPassed ? "true" : "false")
           << ", \"static_schedule_reuse_ratio\": "
           << result.staticScheduleReuseRatio
           << ", \"uses_cdf97\": " << (result.usesCdf97 ? "true" : "false")
           << ", \"color_space\": " << result.colorSpace
           << ", \"min_block_size\": " << result.minBlockSize
           << ", \"max_block_size\": " << result.maxBlockSize
           << ", \"prediction_method\": " << result.predictionMethod
           << ", \"transform_type\": " << result.transformType
           << ", \"transform_scale\": " << result.transformScale
           << ", \"transform_compress\": " << result.transformCompress
           << ", \"transform_compression_threshold\": "
           << result.transformCompressionThreshold << ", \"process_passed\": "
           << (result.processPassed ? "true" : "false")
           << ", \"timing_passed\": "
           << (result.timingPassed ? "true" : "false")
           << ", \"performance_passed\": "
           << (result.performancePassed ? "true" : "false") << ", \"error\": \""
           << jsonEscape(result.error) << "\"}";
    if (index + 1 != results.size())
      output << ',';
    output << '\n';
  }
  output << "  ]\n}\n";
  output.flush();
  if (!output) {
    std::cerr << "Failed to finish JSON report: " << options.jsonReport << '\n';
    return false;
  }
  return true;
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
    if (!glic::PresetLoader::loadOriginalPresetByName(options.presetsDirectory,
                                                      preset, config)) {
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

  const bool usesMetal = options.backend == "metal";
  std::cout << "resolution=" << width << 'x' << height << " backend="
            << (usesMetal ? "metal-original-visual" : "cpu-reference")
            << " scanned=" << scannedPresets
            << " supported=" << candidates.size()
            << " unsupported=" << unsupportedPresets.size()
            << " load_failures=" << loadFailurePresets.size() << '\n';
  std::cout << "preset\tmean_ms\tp95_ms\tfps\tmean_segments\tgpu_ms\t30fps\n";

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
    result.presetConfigFnv1a64 = presetConfigHash(candidate.config);
    result.usesCdf97 = std::any_of(
        candidate.config.channels.begin(), candidate.config.channels.end(),
        [](const auto &channel) { return channel.originalWaveletId == 65; });
    const auto &channel0 = candidate.config.channels[0];
    result.colorSpace = static_cast<int>(candidate.config.colorSpace);
    result.minBlockSize = channel0.minBlockSize;
    result.maxBlockSize = channel0.maxBlockSize;
    result.predictionMethod = static_cast<int>(channel0.predictionMethod);
    result.transformType = channel0.originalTransformType;
    result.transformScale = channel0.transformScale;
    result.transformCompress = channel0.transformCompress;
    result.transformCompressionThreshold =
        channel0.transformCompressionThreshold;
    std::string error;
    glic::OriginalRealtimeCpuLane cpuLane;
    std::unique_ptr<glic::OriginalRealtimeMetalLane> metalLane;
    bool prepared = false;
    if (usesMetal) {
      metalLane = glic::createOriginalRealtimeMetalLane(error);
      prepared = metalLane != nullptr &&
                 metalLane->prepare(width, height, candidate.config, error);
    } else {
      prepared = cpuLane.prepare(width, height, candidate.config, error);
    }
    if (!prepared) {
      result.error = error;
      results.push_back(result);
      allPassed = false;
      continue;
    }

    bool processPassed = true;
    uint64_t frameIndex = 0;
    for (int frame = 0; frame < options.warmupFrames; ++frame) {
      const bool processed =
          usesMetal
              ? metalLane->process(input, output, frameIndex++, nullptr, error)
              : cpuLane.process(input, output, nullptr, error);
      if (!processed) {
        processPassed = false;
        break;
      }
    }

    std::vector<double> frameTimes;
    frameTimes.reserve(static_cast<std::size_t>(options.frames));
    double segmentTotal = 0.0;
    double gpuTotal = 0.0;
    double cpuPrepareTotal = 0.0;
    double cpuOutputTotal = 0.0;
    double dependencyLevelTotal = 0.0;
    double gpuDispatchTotal = 0.0;
    double threadgroupPipelineDispatchTotal = 0.0;
    double threadgroupPipelineSegmentTotal = 0.0;
    double globalPipelineDispatchTotal = 0.0;
    double globalPipelineSegmentTotal = 0.0;
    double bufferBarrierTotal = 0.0;
    double earlyTerminatedNodeTotal = 0.0;
    double earlySkippedSampleTotal = 0.0;
    double staticScheduleReuseTotal = 0.0;
    bool pipelineAccountingPassed = true;
    for (int frame = 0; processPassed && frame < options.frames; ++frame) {
      glic::OriginalRealtimeFrameStats cpuStats;
      glic::OriginalRealtimeMetalFrameStats metalStats;
      const auto start = std::chrono::steady_clock::now();
      processPassed = usesMetal
                          ? metalLane->process(input, output, frameIndex++,
                                               &metalStats, error)
                          : cpuLane.process(input, output, &cpuStats, error);
      const auto stop = std::chrono::steady_clock::now();
      if (processPassed) {
        frameTimes.push_back(
            std::chrono::duration<double, std::milli>(stop - start).count());
        const auto &segmentCounts =
            usesMetal ? metalStats.segmentCounts : cpuStats.segmentCounts;
        segmentTotal +=
            std::accumulate(segmentCounts.begin(), segmentCounts.end(), 0.0);
        if (usesMetal) {
          gpuTotal += metalStats.gpuMilliseconds;
          cpuPrepareTotal += metalStats.cpuPrepareMilliseconds;
          cpuOutputTotal += metalStats.cpuOutputMilliseconds;
          dependencyLevelTotal += metalStats.dispatchLevels;
          gpuDispatchTotal += metalStats.gpuDispatches;
          threadgroupPipelineDispatchTotal +=
              metalStats.threadgroupPipelineDispatches;
          threadgroupPipelineSegmentTotal +=
              metalStats.threadgroupPipelineSegments;
          globalPipelineDispatchTotal += metalStats.globalPipelineDispatches;
          globalPipelineSegmentTotal += metalStats.globalPipelineSegments;
          bufferBarrierTotal += metalStats.bufferBarriers;
          staticScheduleReuseTotal += metalStats.staticScheduleReused ? 1.0 : 0.0;
          pipelineAccountingPassed =
              pipelineAccountingPassed && metalStats.pipelineAccountingPassed;
          earlyTerminatedNodeTotal += metalStats.earlyTerminatedNodes;
          earlySkippedSampleTotal += metalStats.earlySkippedSamples;
          result.lastSegmentationRngState =
              fnvHex(metalStats.segmentationRngState);
          result.lastSegmentOrderFnv1a64 =
              fnvHex(metalStats.segmentOrderFnv1a64);
        } else {
          earlyTerminatedNodeTotal += cpuStats.earlyTerminatedNodes;
          earlySkippedSampleTotal += cpuStats.earlySkippedSamples;
          result.lastSegmentationRngState =
              fnvHex(cpuStats.segmentationRngState);
          result.lastSegmentOrderFnv1a64 =
              fnvHex(cpuStats.segmentOrderFnv1a64);
        }
      }
    }

    result.processPassed =
        processPassed &&
        frameTimes.size() == static_cast<std::size_t>(options.frames);
    if (result.processPassed) {
      result.meanMilliseconds =
          std::accumulate(frameTimes.begin(), frameTimes.end(), 0.0) /
          static_cast<double>(frameTimes.size());
      result.p95Milliseconds = percentile(frameTimes, 0.95);
      result.framesPerSecond = 1000.0 / result.meanMilliseconds;
      result.meanSegments = segmentTotal / frameTimes.size();
      result.meanGpuMilliseconds = gpuTotal / frameTimes.size();
      result.meanCpuPrepareMilliseconds = cpuPrepareTotal / frameTimes.size();
      result.meanCpuOutputMilliseconds = cpuOutputTotal / frameTimes.size();
      result.meanDependencyLevels = dependencyLevelTotal / frameTimes.size();
      result.meanGpuDispatches = gpuDispatchTotal / frameTimes.size();
      result.meanThreadgroupPipelineDispatches =
          threadgroupPipelineDispatchTotal / frameTimes.size();
      result.meanThreadgroupPipelineSegments =
          threadgroupPipelineSegmentTotal / frameTimes.size();
      result.meanGlobalPipelineDispatches =
          globalPipelineDispatchTotal / frameTimes.size();
      result.meanGlobalPipelineSegments =
          globalPipelineSegmentTotal / frameTimes.size();
      result.meanBufferBarriers = bufferBarrierTotal / frameTimes.size();
      result.meanEarlyTerminatedNodes =
          earlyTerminatedNodeTotal / frameTimes.size();
      result.meanEarlySkippedSamples =
          earlySkippedSampleTotal / frameTimes.size();
      result.staticScheduleReuseRatio =
          staticScheduleReuseTotal / frameTimes.size();
      result.pipelineAccountingPassed = pipelineAccountingPassed;
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
      if (!outputPath.empty()) {
        if (!glic::saveImage(outputPath, output, width, height)) {
          result.processPassed = false;
          result.performancePassed = false;
          result.error = "failed to save output image";
        } else if (!fileFnv1a64(outputPath,
                                result.outputPreviewFileFnv1a64)) {
          result.processPassed = false;
          result.performancePassed = false;
          result.error = "failed to hash output image";
        }
      }
    } else {
      result.error = error;
    }

    allPassed = allPassed && result.processPassed && result.performancePassed;
    std::cout << std::fixed << std::setprecision(3) << candidate.preset << '\t'
              << result.meanMilliseconds << '\t' << result.p95Milliseconds
              << '\t' << result.framesPerSecond << '\t' << result.meanSegments
              << '\t' << result.meanGpuMilliseconds << '\t'
              << (result.performancePassed ? "PASS" : "FAIL") << '\n';
    results.push_back(std::move(result));
  }

  if (!writeJson(options, width, height, decodedPixelHash(input), scannedPresets,
                 candidates.size(), unsupportedPresets, loadFailurePresets,
                 results))
    return 6;
  return allPassed ? 0 : 1;
}
