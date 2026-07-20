#include "glic.hpp"
#include "realtime.hpp"
#include "realtime_certification.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <optional>
#include <set>
#include <sstream>
#include <span>
#include <string>
#include <string_view>
#include <system_error>
#include <vector>

namespace fs = std::filesystem;

namespace {

constexpr std::string_view kResultSchema =
    "glic-realtime-certification-result-v1";

struct Options {
  fs::path inputPath;
  fs::path recipesPath;
  fs::path outputPath;
  bool selftest = false;
};

struct Recipe {
  glic::CodecConfig config;
  float strength = 1.0f;
};

struct RecipeRecord {
  std::string recipeHash;
  std::string canonical;
  Recipe recipe;
};

void printUsage(const char *program) {
  std::cerr
      << "Usage: " << program
      << " --input INPUT.png --recipes RECIPES.tsv [--output RESULTS.ndjson]\n"
      << "       " << program << " --selftest\n"
      << "\n"
      << "RECIPES.tsv must contain recipe_hash<TAB>canonical on every line.\n"
      << "The certification policy is fixed: Metal, "
      << glic::kRealtimeCertificationWidth << 'x'
      << glic::kRealtimeCertificationHeight << ", "
      << glic::kRealtimeCertificationWarmupFrames << " warm-up frames, "
      << glic::kRealtimeCertificationMeasuredFrames << " measured frames, and "
      << glic::kRealtimeCertificationTargetFps << " fps.\n";
}

bool parseOptions(int argc, char **argv, Options &options) {
  for (int index = 1; index < argc; ++index) {
    const std::string_view argument = argv[index];
    auto takeValue = [&]() -> const char * {
      return index + 1 < argc ? argv[++index] : nullptr;
    };
    if (argument == "--input") {
      const char *value = takeValue();
      if (value == nullptr)
        return false;
      options.inputPath = value;
    } else if (argument == "--recipes") {
      const char *value = takeValue();
      if (value == nullptr)
        return false;
      options.recipesPath = value;
    } else if (argument == "--output") {
      const char *value = takeValue();
      if (value == nullptr)
        return false;
      options.outputPath = value;
    } else if (argument == "--selftest") {
      options.selftest = true;
    } else if (argument == "--help" || argument == "-h") {
      return false;
    } else {
      std::cerr << "Unknown argument: " << argument << '\n';
      return false;
    }
  }
  if (options.selftest)
    return options.inputPath.empty() && options.recipesPath.empty() &&
           options.outputPath.empty();
  return !options.inputPath.empty() && !options.recipesPath.empty();
}

std::string jsonEscape(std::string_view text) {
  std::string escaped;
  escaped.reserve(text.size() + 16);
  for (const unsigned char character : text) {
    switch (character) {
    case '\\':
      escaped += "\\\\";
      break;
    case '"':
      escaped += "\\\"";
      break;
    case '\n':
      escaped += "\\n";
      break;
    case '\r':
      escaped += "\\r";
      break;
    case '\t':
      escaped += "\\t";
      break;
    default:
      if (character < 0x20) {
        std::ostringstream code;
        code << "\\u" << std::hex << std::setw(4) << std::setfill('0')
             << static_cast<int>(character);
        escaped += code.str();
      } else {
        escaped += static_cast<char>(character);
      }
      break;
    }
  }
  return escaped;
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

void normalizeRecipe(Recipe &recipe) {
  recipe.config.colorSpace = static_cast<glic::ColorSpace>(std::clamp(
      static_cast<int>(recipe.config.colorSpace), 0,
      static_cast<int>(glic::ColorSpace::COUNT) - 1));
  recipe.strength =
      std::round(std::clamp(recipe.strength, 0.0f, 2.0f) * 1000.0f) /
      1000.0f;
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

std::string canonicalRecipe(const Recipe &recipe) {
  std::ostringstream output;
  output << "v1|" << static_cast<int>(recipe.config.colorSpace) << '|'
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
  return output.str();
}

bool decodeCanonical(std::string text, Recipe &recipe) {
  const std::string original = text;
  for (char &character : text) {
    if (character == '|' || character == ',' || character == ';')
      character = ' ';
  }
  std::istringstream input(text);
  std::string version;
  std::array<long long, 38> values{};
  if (!(input >> version) || version != "v1")
    return false;
  for (auto &value : values) {
    if (!(input >> value) ||
        value < static_cast<long long>(std::numeric_limits<int>::min()) ||
        value > static_cast<long long>(std::numeric_limits<int>::max()))
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
  normalizeRecipe(recipe);
  return canonicalRecipe(recipe) == original;
}

uint64_t fnv1a64(std::string_view text) {
  uint64_t hash = 1469598103934665603ULL;
  for (const unsigned char value : text) {
    hash ^= value;
    hash *= 1099511628211ULL;
  }
  return hash;
}

std::string hexHash(uint64_t hash) {
  std::ostringstream output;
  output << std::hex << std::setw(16) << std::setfill('0') << hash;
  return output.str();
}

bool validRecipeHash(std::string_view value) {
  return value.size() == 16 &&
         std::all_of(value.begin(), value.end(), [](unsigned char character) {
           return (character >= '0' && character <= '9') ||
                  (character >= 'a' && character <= 'f');
         });
}

bool loadRecipes(const fs::path &path, std::vector<RecipeRecord> &records,
                 std::string &error) {
  std::ifstream input(path);
  if (!input) {
    error = "failed to open recipes TSV: " + path.string();
    return false;
  }
  std::set<std::string> seen;
  std::string line;
  size_t lineNumber = 0;
  while (std::getline(input, line)) {
    ++lineNumber;
    if (!line.empty() && line.back() == '\r')
      line.pop_back();
    const size_t separator = line.find('\t');
    if (separator == std::string::npos || separator == 0 ||
        separator + 1 >= line.size() ||
        line.find('\t', separator + 1) != std::string::npos) {
      error = "invalid TSV row at line " + std::to_string(lineNumber);
      return false;
    }
    RecipeRecord record;
    record.recipeHash = line.substr(0, separator);
    record.canonical = line.substr(separator + 1);
    if (!validRecipeHash(record.recipeHash)) {
      error = "invalid recipe hash at line " + std::to_string(lineNumber);
      return false;
    }
    if (!seen.insert(record.recipeHash).second) {
      error = "duplicate recipe hash at line " + std::to_string(lineNumber);
      return false;
    }
    if (!decodeCanonical(record.canonical, record.recipe)) {
      error = "invalid canonical recipe at line " +
              std::to_string(lineNumber);
      return false;
    }
    if (hexHash(fnv1a64(record.canonical)) != record.recipeHash) {
      error = "canonical recipe hash mismatch at line " +
              std::to_string(lineNumber);
      return false;
    }
    records.push_back(std::move(record));
  }
  if (!input.eof()) {
    error = "failed while reading recipes TSV: " + path.string();
    return false;
  }
  if (records.empty()) {
    error = "recipes TSV is empty";
    return false;
  }
  return true;
}

std::vector<glic::Color>
resizeImage(const std::vector<glic::Color> &source, int sourceWidth,
            int sourceHeight, int targetWidth, int targetHeight) {
  std::vector<glic::Color> target(static_cast<size_t>(targetWidth) *
                                  static_cast<size_t>(targetHeight));
  const double xScale = static_cast<double>(sourceWidth) / targetWidth;
  const double yScale = static_cast<double>(sourceHeight) / targetHeight;
  for (int y = 0; y < targetHeight; ++y) {
    const double sourceY = (y + 0.5) * yScale - 0.5;
    const int y0 = std::clamp(static_cast<int>(std::floor(sourceY)), 0,
                              sourceHeight - 1);
    const int y1 = std::min(y0 + 1, sourceHeight - 1);
    const double fy = std::clamp(sourceY - std::floor(sourceY), 0.0, 1.0);
    for (int x = 0; x < targetWidth; ++x) {
      const double sourceX = (x + 0.5) * xScale - 0.5;
      const int x0 = std::clamp(static_cast<int>(std::floor(sourceX)), 0,
                                sourceWidth - 1);
      const int x1 = std::min(x0 + 1, sourceWidth - 1);
      const double fx = std::clamp(sourceX - std::floor(sourceX), 0.0, 1.0);
      const auto sample = [&](int sx, int sy, int channel) {
        const glic::Color color =
            source[static_cast<size_t>(sy) * sourceWidth + sx];
        switch (channel) {
        case 0:
          return static_cast<double>(glic::getR(color));
        case 1:
          return static_cast<double>(glic::getG(color));
        case 2:
          return static_cast<double>(glic::getB(color));
        default:
          return static_cast<double>(glic::getA(color));
        }
      };
      std::array<uint8_t, 4> channels{};
      for (int channel = 0; channel < 4; ++channel) {
        const double top = sample(x0, y0, channel) * (1.0 - fx) +
                           sample(x1, y0, channel) * fx;
        const double bottom = sample(x0, y1, channel) * (1.0 - fx) +
                              sample(x1, y1, channel) * fx;
        channels[static_cast<size_t>(channel)] = static_cast<uint8_t>(
            std::clamp(std::lround(top * (1.0 - fy) + bottom * fy), 0L,
                       255L));
      }
      target[static_cast<size_t>(y) * targetWidth + x] = glic::makeColor(
          channels[0], channels[1], channels[2], channels[3]);
    }
  }
  return target;
}

double percentileForSelftest(std::vector<double> values, double fraction) {
  if (values.empty())
    return 0.0;
  std::sort(values.begin(), values.end());
  const size_t index =
      static_cast<size_t>(std::ceil((values.size() - 1) * fraction));
  return values[std::min(index, values.size() - 1)];
}

bool boundaryPasses(double meanMilliseconds, double p95Milliseconds) {
  return std::isfinite(meanMilliseconds) &&
         std::isfinite(p95Milliseconds) && meanMilliseconds > 0.0 &&
         p95Milliseconds > 0.0 &&
         meanMilliseconds <= glic::kRealtimeCertificationFrameBudgetMilliseconds &&
         p95Milliseconds <= glic::kRealtimeCertificationFrameBudgetMilliseconds;
}

bool expect(bool condition, std::string_view message) {
  if (!condition)
    std::cerr << "SELFTEST FAILED: " << message << '\n';
  return condition;
}

int runSelftest() {
  bool passed = true;
  passed &= expect(percentileForSelftest({1, 2, 3, 4, 5}, 0.50) == 3.0,
                   "median percentile");
  passed &= expect(percentileForSelftest({1, 2, 3, 4, 5}, 0.95) == 5.0,
                   "p95 percentile");
  passed &= expect(percentileForSelftest({7}, 0.99) == 7.0,
                   "single-value percentile");

  const double budget =
      glic::kRealtimeCertificationFrameBudgetMilliseconds;
  passed &= expect(boundaryPasses(budget, budget), "inclusive 30 fps boundary");
  passed &= expect(!boundaryPasses(budget, std::nextafter(
                                               budget,
                                               std::numeric_limits<double>::infinity())),
                   "over-budget p95 rejection");
  passed &= expect(glic::kRealtimeCertificationWidth == 960 &&
                       glic::kRealtimeCertificationHeight == 540 &&
                       glic::kRealtimeCertificationWarmupFrames >= 10 &&
                       glic::kRealtimeCertificationMeasuredFrames >= 120 &&
                       glic::kRealtimeCertificationTargetFps >= 30.0,
                   "minimum certification policy");

  Recipe recipe;
  recipe.config.colorSpace = glic::ColorSpace::RGB;
  recipe.config.borderColorR = 1;
  recipe.config.borderColorG = 2;
  recipe.config.borderColorB = 3;
  recipe.strength = 1.25f;
  for (size_t index = 0; index < recipe.config.channels.size(); ++index) {
    auto &channel = recipe.config.channels[index];
    channel.minBlockSize = 2 << static_cast<int>(index);
    channel.maxBlockSize = 32 << static_cast<int>(index);
    channel.segmentationPrecision = 10.0f + static_cast<float>(index);
    channel.predictionMethod = glic::PredictionMethod::PAETH;
    channel.quantizationValue = 100 + static_cast<int>(index);
    channel.clampMethod = glic::ClampMethod::NONE;
    channel.transformType = glic::TransformType::FWT;
    channel.waveletType = glic::WaveletType::SYMLET2;
    channel.transformCompress = 30.0f + static_cast<float>(index);
    channel.transformScale = 20 + static_cast<int>(index);
    channel.encodingMethod = glic::EncodingMethod::PACKED;
  }
  normalizeRecipe(recipe);
  const std::string canonical = canonicalRecipe(recipe);
  Recipe decoded;
  passed &= expect(decodeCanonical(canonical, decoded),
                   "canonical recipe parsing");
  passed &= expect(canonicalRecipe(decoded) == canonical,
                   "canonical recipe round trip");
  passed &= expect(!decodeCanonical(canonical + "0", decoded),
                   "canonical trailing-data rejection");
  passed &= expect(validRecipeHash(hexHash(fnv1a64(canonical))),
                   "canonical FNV hash formatting");

  if (!passed)
    return 1;
  std::cout << "SELFTEST PASSED\n";
  return 0;
}

bool finiteResult(const glic::RealtimeCertificationResult &result) {
  return std::isfinite(result.meanWallMilliseconds) &&
         std::isfinite(result.medianWallMilliseconds) &&
         std::isfinite(result.p95WallMilliseconds) &&
         std::isfinite(result.p99WallMilliseconds) &&
         std::isfinite(result.maxWallMilliseconds) &&
         std::isfinite(result.meanGpuMilliseconds) &&
         std::isfinite(result.p95GpuMilliseconds);
}

void appendResultJson(std::ostream &output, const RecipeRecord &record,
                      std::string_view backend,
                      glic::RealtimeCertificationResult result) {
  if (!finiteResult(result)) {
    result.processPassed = false;
    result.passed = false;
    result.error = result.error.empty() ? "non-finite timing result"
                                        : result.error + "; non-finite timing result";
    result.meanWallMilliseconds = 0.0;
    result.medianWallMilliseconds = 0.0;
    result.p95WallMilliseconds = 0.0;
    result.p99WallMilliseconds = 0.0;
    result.maxWallMilliseconds = 0.0;
    result.meanGpuMilliseconds = 0.0;
    result.p95GpuMilliseconds = 0.0;
  }
  const double p95Fps = result.p95WallMilliseconds > 0.0
                            ? 1000.0 / result.p95WallMilliseconds
                            : 0.0;
  output << std::setprecision(17)
         << "{\"schema\":\"" << kResultSchema << "\""
         << ",\"recipe_hash\":\"" << record.recipeHash << "\""
         << ",\"backend\":\"" << jsonEscape(backend) << "\""
         << ",\"width\":" << result.width
         << ",\"height\":" << result.height
         << ",\"target_fps\":" << result.targetFps
         << ",\"frame_budget_ms\":" << result.frameBudgetMilliseconds
         << ",\"warmup_frames\":" << result.warmupFrames
         << ",\"measured_frames\":" << result.measuredFrames
         << ",\"completed_frames\":" << result.completedFrames
         << ",\"mean_ms\":" << result.meanWallMilliseconds
         << ",\"median_ms\":" << result.medianWallMilliseconds
         << ",\"p95_ms\":" << result.p95WallMilliseconds
         << ",\"p99_ms\":" << result.p99WallMilliseconds
         << ",\"max_ms\":" << result.maxWallMilliseconds
         << ",\"mean_gpu_ms\":" << result.meanGpuMilliseconds
         << ",\"p95_gpu_ms\":" << result.p95GpuMilliseconds
         << ",\"p95_fps\":" << p95Fps
         << ",\"performed\":" << (result.performed ? "true" : "false")
         << ",\"process_passed\":"
         << (result.processPassed ? "true" : "false")
         << ",\"performance_passed\":"
         << (result.passed ? "true" : "false")
         << ",\"error\":\"" << jsonEscape(result.error) << "\"}\n";
}

bool writeAtomically(const fs::path &path, std::string_view contents,
                     std::string &error) {
  std::error_code filesystemError;
  const fs::path parent = path.has_parent_path() ? path.parent_path() : fs::path(".");
  if (!fs::is_directory(parent, filesystemError)) {
    error = "output directory does not exist: " + parent.string();
    return false;
  }
  const auto nonce = std::chrono::steady_clock::now().time_since_epoch().count();
  const fs::path temporary =
      parent / ("." + path.filename().string() + "." +
                std::to_string(nonce) + ".tmp");
  {
    std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
    if (!output) {
      error = "failed to open temporary output: " + temporary.string();
      return false;
    }
    output.write(contents.data(), static_cast<std::streamsize>(contents.size()));
    output.flush();
    if (!output) {
      error = "failed to write temporary output: " + temporary.string();
      output.close();
      fs::remove(temporary, filesystemError);
      return false;
    }
  }
  fs::rename(temporary, path, filesystemError);
  if (filesystemError) {
    error = "failed to publish output: " + filesystemError.message();
    fs::remove(temporary, filesystemError);
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
  if (options.selftest)
    return runSelftest();

  std::vector<RecipeRecord> recipes;
  std::string error;
  if (!loadRecipes(options.recipesPath, recipes, error)) {
    std::cerr << error << '\n';
    return 3;
  }

  std::vector<glic::Color> original;
  int sourceWidth = 0;
  int sourceHeight = 0;
  if (!glic::loadImage(options.inputPath.string(), original, sourceWidth,
                       sourceHeight) ||
      sourceWidth <= 0 || sourceHeight <= 0) {
    std::cerr << "failed to load input PNG: " << options.inputPath << '\n';
    return 3;
  }
  std::vector<glic::Color> input = resizeImage(
      original, sourceWidth, sourceHeight, glic::kRealtimeCertificationWidth,
      glic::kRealtimeCertificationHeight);
  std::vector<glic::Color> output(input.size());

  auto backend =
      glic::createRealtimeBackend(glic::RealtimeBackendKind::METAL, error);
  if (!backend || !backend->isHardwareAccelerated() ||
      std::string_view(backend->name()) != "metal") {
    std::cerr << "failed to create mandatory Metal backend: " << error << '\n';
    return 4;
  }

  std::ostringstream ndjson;
  uint64_t frameIndexBase = 0;
  for (const auto &record : recipes) {
    glic::RealtimeCertificationRequest request;
    request.config = record.recipe.config;
    request.effectStrength = record.recipe.strength;
    request.frameIndexBase = frameIndexBase;
    auto result = glic::certifyRealtimePreset(*backend, input, output, request);
    appendResultJson(ndjson, record, backend->name(), std::move(result));
    frameIndexBase +=
        static_cast<uint64_t>(glic::kRealtimeCertificationWarmupFrames) +
        static_cast<uint64_t>(glic::kRealtimeCertificationMeasuredFrames) + 1;
  }

  if (options.outputPath.empty()) {
    std::cout << ndjson.str();
  } else if (!writeAtomically(options.outputPath, ndjson.str(), error)) {
    std::cerr << error << '\n';
    return 5;
  }
  return 0;
}
