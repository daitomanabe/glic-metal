#include "preset_loader.hpp"
#include "realtime.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <sstream>
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
  std::string presetSemantics = "legacy";
  std::string backend = "auto";
  std::string statsJson;
  std::string canonical;
  std::string effectFamily = "legacy_block";
  uint32_t seed = 0x474C4943u;
  float strength = 1.0f;
  float effectAmount = 0.7f;
  float effectScale = 0.5f;
  float effectRate = 0.5f;
  bool passthrough = false;
};

struct CanonicalRecipe {
  glic::CodecConfig config;
  glic::RealtimeEffectConfig effect{};
  float strength = 1.0f;
};

void printUsage(const char *program) {
  std::cerr
      << "Usage: " << program
      << " --width <pixels> --height <pixels> [options]\n"
      << "Reads packed BGRA8 frames from stdin and writes packed BGRA8 frames "
         "to stdout.\n"
      << "  --preset <name>          Preset name (default: default)\n"
      << "  --canonical <recipe>     Strict v1/v2 canonical recipe; bypasses "
         "the named preset\n"
      << "  --presets-dir <path>     Preset directory (default: presets)\n"
      << "  --preset-semantics <legacy|original>\n"
      << "                            Preset field decoding (default: legacy)\n"
      << "  --backend <auto|cpu|metal>\n"
      << "  --strength <0..2>        Glitch intensity (default: 1)\n"
      << "  --effect-family <name>   legacy_block, line_tear, "
         "channel_shear, analog_sync, mirror_fold, edge_echo, "
         "bitplane_dither, wave_warp, poster_solar, tile_shuffle, "
         "vertical_tear, diagonal_slip, scanline_weave, or quad_mirror\n"
      << "  --effect-amount <0..1>   Family-specific amount (default: 0.7)\n"
      << "  --effect-scale <0..1>    Family-specific spatial scale (default: 0.5)\n"
      << "  --effect-rate <0..1>     Family-specific animation rate (default: 0.5)\n"
      << "  --seed <u32>             Pattern seed, decimal or 0x-prefixed\n"
      << "Canonical recipe values override --strength and all --effect-* "
         "options; --seed remains independent. Quote recipes in the shell.\n"
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

bool parseUnitFloat(std::string_view text, float &value) {
  try {
    const float parsed = std::stof(std::string(text));
    if (!std::isfinite(parsed) || parsed < 0.0f || parsed > 1.0f)
      return false;
    value = parsed;
    return true;
  } catch (...) {
    return false;
  }
}

bool parseSeed(std::string_view text, uint32_t &value) {
  try {
    size_t consumed = 0;
    const auto parsed = std::stoull(std::string(text), &consumed, 0);
    if (consumed != text.size() || parsed > std::numeric_limits<uint32_t>::max())
      return false;
    value = static_cast<uint32_t>(parsed);
    return true;
  } catch (...) {
    return false;
  }
}

bool parseEffectFamily(std::string_view name,
                       glic::RealtimeEffectFamily &family) {
  if (name == "legacy_block")
    family = glic::RealtimeEffectFamily::LEGACY_BLOCK;
  else if (name == "line_tear")
    family = glic::RealtimeEffectFamily::LINE_TEAR;
  else if (name == "channel_shear")
    family = glic::RealtimeEffectFamily::CHANNEL_SHEAR;
  else if (name == "analog_sync")
    family = glic::RealtimeEffectFamily::ANALOG_SYNC;
  else if (name == "mirror_fold")
    family = glic::RealtimeEffectFamily::MIRROR_FOLD;
  else if (name == "edge_echo")
    family = glic::RealtimeEffectFamily::EDGE_ECHO;
  else if (name == "bitplane_dither")
    family = glic::RealtimeEffectFamily::BITPLANE_DITHER;
  else if (name == "wave_warp")
    family = glic::RealtimeEffectFamily::WAVE_WARP;
  else if (name == "poster_solar")
    family = glic::RealtimeEffectFamily::POSTER_SOLAR;
  else if (name == "tile_shuffle")
    family = glic::RealtimeEffectFamily::TILE_SHUFFLE;
  else if (name == "vertical_tear")
    family = glic::RealtimeEffectFamily::VERTICAL_TEAR;
  else if (name == "diagonal_slip")
    family = glic::RealtimeEffectFamily::DIAGONAL_SLIP;
  else if (name == "scanline_weave")
    family = glic::RealtimeEffectFamily::SCANLINE_WEAVE;
  else if (name == "quad_mirror")
    family = glic::RealtimeEffectFamily::QUAD_MIRROR;
  else
    return false;
  return true;
}

const char *effectFamilyName(glic::RealtimeEffectFamily family) noexcept {
  switch (family) {
  case glic::RealtimeEffectFamily::LEGACY_BLOCK:
    return "legacy_block";
  case glic::RealtimeEffectFamily::LINE_TEAR:
    return "line_tear";
  case glic::RealtimeEffectFamily::CHANNEL_SHEAR:
    return "channel_shear";
  case glic::RealtimeEffectFamily::ANALOG_SYNC:
    return "analog_sync";
  case glic::RealtimeEffectFamily::MIRROR_FOLD:
    return "mirror_fold";
  case glic::RealtimeEffectFamily::EDGE_ECHO:
    return "edge_echo";
  case glic::RealtimeEffectFamily::BITPLANE_DITHER:
    return "bitplane_dither";
  case glic::RealtimeEffectFamily::WAVE_WARP:
    return "wave_warp";
  case glic::RealtimeEffectFamily::POSTER_SOLAR:
    return "poster_solar";
  case glic::RealtimeEffectFamily::TILE_SHUFFLE:
    return "tile_shuffle";
  case glic::RealtimeEffectFamily::VERTICAL_TEAR:
    return "vertical_tear";
  case glic::RealtimeEffectFamily::DIAGONAL_SLIP:
    return "diagonal_slip";
  case glic::RealtimeEffectFamily::SCANLINE_WEAVE:
    return "scanline_weave";
  case glic::RealtimeEffectFamily::QUAD_MIRROR:
    return "quad_mirror";
  case glic::RealtimeEffectFamily::COUNT:
    break;
  }
  return "legacy_block";
}

glic::WaveletType canonicalWavelet(glic::WaveletType type) {
  const int value = static_cast<int>(type);
  if (type == glic::WaveletType::NONE)
    return glic::WaveletType::NONE;
  if (type == glic::WaveletType::HAAR_ORTHOGONAL ||
      type == glic::WaveletType::HAAR)
    return glic::WaveletType::HAAR_ORTHOGONAL;
  if (value >= static_cast<int>(glic::WaveletType::COIFLET1) &&
      value <= static_cast<int>(glic::WaveletType::COIFLET5))
    return glic::WaveletType::COIFLET1;
  if (value >= static_cast<int>(glic::WaveletType::SYMLET2) &&
      value <= static_cast<int>(glic::WaveletType::SYMLET4))
    return glic::WaveletType::SYMLET2;
  return glic::WaveletType::BIORTHOGONAL11;
}

int normalizeBlockSize(int value) {
  value = std::clamp(value, 1, 256);
  int result = 1;
  while (result < value && result < 256)
    result <<= 1;
  return result;
}

float normalizedEffectParameter(float value) {
  return std::round(std::clamp(value, 0.0f, 1.0f) * 1000.0f) / 1000.0f;
}

void normalizeEffect(glic::RealtimeEffectConfig &effect) {
  const int family = std::clamp(
      static_cast<int>(effect.family),
      static_cast<int>(glic::RealtimeEffectFamily::LEGACY_BLOCK),
      static_cast<int>(glic::RealtimeEffectFamily::COUNT) - 1);
  effect.family = static_cast<glic::RealtimeEffectFamily>(family);
  if (effect.family == glic::RealtimeEffectFamily::LEGACY_BLOCK) {
    effect.amount = 0.7f;
    effect.scale = 0.5f;
    effect.rate = 0.5f;
    return;
  }
  effect.amount = normalizedEffectParameter(effect.amount);
  effect.scale = normalizedEffectParameter(effect.scale);
  effect.rate = normalizedEffectParameter(effect.rate);
}

void normalizeRecipe(CanonicalRecipe &recipe) {
  recipe.config.colorSpace = static_cast<glic::ColorSpace>(std::clamp(
      static_cast<int>(recipe.config.colorSpace), 0,
      static_cast<int>(glic::ColorSpace::COUNT) - 1));
  recipe.strength =
      std::round(std::clamp(recipe.strength, 0.0f, 2.0f) * 1000.0f) /
      1000.0f;
  normalizeEffect(recipe.effect);
  for (auto &channel : recipe.config.channels) {
    channel.minBlockSize = normalizeBlockSize(channel.minBlockSize);
    channel.maxBlockSize = normalizeBlockSize(channel.maxBlockSize);
    if (channel.minBlockSize > channel.maxBlockSize)
      std::swap(channel.minBlockSize, channel.maxBlockSize);
    channel.segmentationPrecision =
        std::round(std::clamp(channel.segmentationPrecision, 0.0f, 128.0f) *
                   1000.0f) /
        1000.0f;
    channel.quantizationValue =
        std::clamp(channel.quantizationValue, 0, 255);
    channel.waveletType = canonicalWavelet(channel.waveletType);
    channel.transformCompress =
        std::round(std::clamp(channel.transformCompress, 0.0f, 255.0f) *
                   1000.0f) /
        1000.0f;
    channel.transformScale = std::abs(channel.transformScale);
    if (channel.waveletType == glic::WaveletType::NONE) {
      channel.transformType = glic::TransformType::FWT;
      channel.transformCompress = 0.0f;
      channel.transformScale = 20;
    }
  }
}

void appendCanonicalCodec(std::ostringstream &output,
                          const CanonicalRecipe &recipe) {
  output << static_cast<int>(recipe.config.colorSpace) << '|'
         << static_cast<int>(recipe.config.borderColorR) << '|'
         << static_cast<int>(recipe.config.borderColorG) << '|'
         << static_cast<int>(recipe.config.borderColorB) << '|'
         << std::lround(recipe.strength * 1000.0f) << '|';
  for (const auto &channel : recipe.config.channels) {
    output << channel.minBlockSize << ',' << channel.maxBlockSize << ','
           << std::lround(channel.segmentationPrecision * 1000.0f) << ','
           << static_cast<int>(channel.predictionMethod) << ','
           << channel.quantizationValue << ','
           << static_cast<int>(channel.clampMethod) << ','
           << static_cast<int>(channel.transformType) << ','
           << static_cast<int>(channel.waveletType) << ','
           << std::lround(channel.transformCompress * 1000.0f) << ','
           << channel.transformScale << ','
           << static_cast<int>(channel.encodingMethod) << ';';
  }
}

std::string canonicalRecipeV1(const CanonicalRecipe &recipe) {
  std::ostringstream output;
  output << "v1|";
  appendCanonicalCodec(output, recipe);
  return output.str();
}

std::string canonicalRecipeV2(const CanonicalRecipe &recipe) {
  std::ostringstream output;
  output << "v2|";
  appendCanonicalCodec(output, recipe);
  output << '|' << static_cast<int>(recipe.effect.family) << ','
         << std::lround(recipe.effect.amount * 1000.0f) << ','
         << std::lround(recipe.effect.scale * 1000.0f) << ','
         << std::lround(recipe.effect.rate * 1000.0f);
  return output.str();
}

bool decodeCanonical(std::string text, CanonicalRecipe &recipe,
                     std::string &version) {
  const std::string original = text;
  for (char &character : text) {
    if (character == '|' || character == ',' || character == ';')
      character = ' ';
  }
  std::istringstream input(text);
  std::array<long long, 42> values{};
  if (!(input >> version) || (version != "v1" && version != "v2"))
    return false;
  const size_t valueCount = version == "v1" ? 38 : values.size();
  for (size_t index = 0; index < valueCount; ++index) {
    if (!(input >> values[index]) ||
        values[index] <
            static_cast<long long>(std::numeric_limits<int>::min()) ||
        values[index] >
            static_cast<long long>(std::numeric_limits<int>::max()))
      return false;
  }
  std::string extra;
  if (input >> extra)
    return false;

  size_t position = 0;
  const auto take = [&]() { return static_cast<int>(values[position++]); };
  recipe.config.colorSpace = static_cast<glic::ColorSpace>(take());
  recipe.config.borderColorR = static_cast<uint8_t>(take());
  recipe.config.borderColorG = static_cast<uint8_t>(take());
  recipe.config.borderColorB = static_cast<uint8_t>(take());
  recipe.strength = static_cast<float>(take()) / 1000.0f;
  for (auto &channel : recipe.config.channels) {
    channel.minBlockSize = take();
    channel.maxBlockSize = take();
    channel.segmentationPrecision = static_cast<float>(take()) / 1000.0f;
    channel.predictionMethod = static_cast<glic::PredictionMethod>(take());
    channel.quantizationValue = take();
    channel.clampMethod = static_cast<glic::ClampMethod>(take());
    channel.transformType = static_cast<glic::TransformType>(take());
    channel.waveletType = static_cast<glic::WaveletType>(take());
    channel.transformCompress = static_cast<float>(take()) / 1000.0f;
    channel.transformScale = take();
    channel.encodingMethod = static_cast<glic::EncodingMethod>(take());
  }
  if (version == "v2") {
    recipe.effect.family = static_cast<glic::RealtimeEffectFamily>(take());
    recipe.effect.amount = static_cast<float>(take()) / 1000.0f;
    recipe.effect.scale = static_cast<float>(take()) / 1000.0f;
    recipe.effect.rate = static_cast<float>(take()) / 1000.0f;
  } else {
    recipe.effect.family = glic::RealtimeEffectFamily::LEGACY_BLOCK;
    recipe.effect.amount = 0.7f;
    recipe.effect.scale = 0.5f;
    recipe.effect.rate = 0.5f;
  }
  normalizeRecipe(recipe);
  return (version == "v1" ? canonicalRecipeV1(recipe)
                           : canonicalRecipeV2(recipe)) == original;
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
    } else if (argument == "--canonical") {
      const char *value = takeValue();
      if (value == nullptr || value[0] == '\0')
        return false;
      options.canonical = value;
    } else if (argument == "--presets-dir") {
      const char *value = takeValue();
      if (value == nullptr)
        return false;
      options.presetsDirectory = value;
    } else if (argument == "--preset-semantics") {
      const char *value = takeValue();
      if (value == nullptr)
        return false;
      options.presetSemantics = value;
      if (options.presetSemantics != "legacy" &&
          options.presetSemantics != "original")
        return false;
    } else if (argument == "--backend") {
      const char *value = takeValue();
      if (value == nullptr)
        return false;
      options.backend = value;
    } else if (argument == "--strength") {
      const char *value = takeValue();
      if (value == nullptr || !parseStrength(value, options.strength))
        return false;
    } else if (argument == "--effect-family") {
      const char *value = takeValue();
      glic::RealtimeEffectFamily parsed{};
      if (value == nullptr || !parseEffectFamily(value, parsed))
        return false;
      options.effectFamily = value;
    } else if (argument == "--effect-amount") {
      const char *value = takeValue();
      if (value == nullptr || !parseUnitFloat(value, options.effectAmount))
        return false;
    } else if (argument == "--effect-scale") {
      const char *value = takeValue();
      if (value == nullptr || !parseUnitFloat(value, options.effectScale))
        return false;
    } else if (argument == "--effect-rate") {
      const char *value = takeValue();
      if (value == nullptr || !parseUnitFloat(value, options.effectRate))
        return false;
    } else if (argument == "--seed") {
      const char *value = takeValue();
      if (value == nullptr || !parseSeed(value, options.seed))
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

std::string jsonEscape(std::string_view value) {
  std::string output;
  output.reserve(value.size() + 8);
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

void writeStats(const Options &options, const std::string &preset,
                std::string_view recipeSource,
                std::string_view canonicalVersion,
                std::string_view mappingFidelity,
                const std::vector<std::string> &mappingReasons,
                const char *backend,
                uint64_t frames, double totalMilliseconds,
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
  const std::string_view reportedSemantics =
      recipeSource == "preset" ? std::string_view(options.presetSemantics)
                               : std::string_view("not-applicable");

  output << std::fixed << std::setprecision(3) << "{\n"
         << "  \"schema\": \"glic-realtime-filter-v1\",\n"
         << "  \"width\": " << options.width << ",\n"
         << "  \"height\": " << options.height << ",\n"
         << "  \"preset\": \"" << jsonEscape(preset) << "\",\n"
         << "  \"preset_semantics\": \"" << jsonEscape(reportedSemantics)
         << "\",\n"
         << "  \"processing_mode\": \"compat_realtime\",\n"
         << "  \"preset_mapping_fidelity\": \"" << jsonEscape(mappingFidelity)
         << "\",\n"
         << "  \"preset_mapping_reasons\": [";
  for (size_t index = 0; index < mappingReasons.size(); ++index) {
    if (index != 0)
      output << ", ";
    output << '"' << jsonEscape(mappingReasons[index]) << '"';
  }
  output << "],\n"
         << "  \"recipe_source\": \"" << jsonEscape(recipeSource) << "\",\n"
         << "  \"canonical_version\": ";
  if (canonicalVersion.empty())
    output << "null,\n";
  else
    output << '"' << jsonEscape(canonicalVersion) << "\",\n";
  output
         << "  \"backend\": \"" << jsonEscape(backend) << "\",\n"
         << "  \"strength\": " << options.strength << ",\n"
         << "  \"seed\": " << options.seed << ",\n"
         << "  \"effect_family\": \"" << jsonEscape(options.effectFamily) << "\",\n"
         << "  \"effect_amount\": " << options.effectAmount << ",\n"
         << "  \"effect_scale\": " << options.effectScale << ",\n"
         << "  \"effect_rate\": " << options.effectRate << ",\n"
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
  if (options.passthrough && !options.canonical.empty()) {
    std::cerr << "--passthrough and --canonical are mutually exclusive\n";
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
  std::string mappingFidelity = "not-applicable";
  std::vector<std::string> mappingReasons;
  const std::string recipeSource =
      options.passthrough
          ? "passthrough"
          : (options.canonical.empty() ? "preset" : "canonical");
  std::string canonicalVersion;
  glic::CodecConfig config;
  glic::RealtimeEffectFamily effectFamily{};
  if (!parseEffectFamily(options.effectFamily, effectFamily)) {
    std::cerr << "Invalid effect family: " << options.effectFamily << '\n';
    return 2;
  }
  glic::RealtimeEffectConfig effect{.family = effectFamily,
                                    .amount = options.effectAmount,
                                    .scale = options.effectScale,
                                    .rate = options.effectRate};

  if (!options.canonical.empty()) {
    CanonicalRecipe recipe;
    if (!decodeCanonical(options.canonical, recipe, canonicalVersion)) {
      std::cerr << "Invalid canonical recipe: expected the exact canonical "
                   "v1 form with 38 integers or v2 form with 42 integers\n";
      return 3;
    }
    config = recipe.config;
    effect = recipe.effect;
    options.strength = recipe.strength;
    options.effectFamily = effectFamilyName(effect.family);
    options.effectAmount = effect.amount;
    options.effectScale = effect.scale;
    options.effectRate = effect.rate;
    mappingFidelity = "canonical";
  }

  if (!options.passthrough) {
    if (options.canonical.empty()) {
      bool loaded = false;
      if (options.presetSemantics == "original") {
        glic::PresetMappingInfo mapping;
        loaded = glic::PresetLoader::loadOriginalPresetByName(
            options.presetsDirectory, options.preset, config, &mapping);
        mappingFidelity =
            glic::presetMappingFidelityName(mapping.fidelity);
        mappingReasons = std::move(mapping.reasons);
      } else {
        loaded = glic::PresetLoader::loadPresetByName(
            options.presetsDirectory, options.preset, config);
        mappingFidelity = "legacy";
      }
      if (!loaded) {
        std::cerr << "Failed to load preset: " << options.preset << '\n';
        return 3;
      }
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
                                                .seed = options.seed,
                                                .effectStrength = options.strength,
                                                .effect = effect};
    if (!backend->prepare(prepareOptions, error)) {
      std::cerr << "Failed to prepare realtime backend: " << error << '\n';
      return 4;
    }
    backendName = backend->name();
    presetName = options.canonical.empty() ? options.preset : "canonical";
  } else {
    options.strength = 0.0f;
    options.effectFamily = "passthrough";
    options.effectAmount = 0.0f;
    options.effectScale = 0.0f;
    options.effectRate = 0.0f;
    options.seed = 0;
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

  writeStats(options, presetName, recipeSource, canonicalVersion,
             mappingFidelity, mappingReasons, backendName.c_str(), frameIndex,
             totalMilliseconds,
             maximumMilliseconds, totalGpuMilliseconds);
  const double fps =
      totalMilliseconds <= 0.0
          ? 0.0
          : static_cast<double>(frameIndex) * 1000.0 / totalMilliseconds;
  std::cerr << "frames=" << frameIndex << " backend=" << backendName
            << " preset=" << presetName << " strength=" << options.strength
            << " effect_family=" << options.effectFamily
            << " preset_semantics=" << options.presetSemantics
            << " preset_mapping_fidelity=" << mappingFidelity
            << " recipe_source=" << recipeSource
            << " canonical_version="
            << (canonicalVersion.empty() ? "none" : canonicalVersion)
            << " seed=" << options.seed
            << " processing_fps=" << std::fixed << std::setprecision(3) << fps
            << '\n';
  return frameIndex == 0 ? 5 : 0;
}
