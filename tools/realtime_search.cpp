#include "glic.hpp"
#include "realtime.hpp"
#include "realtime_certification.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <iterator>
#include <limits>
#include <map>
#include <numeric>
#include <optional>
#include <set>
#include <sstream>
#include <string>
#include <string_view>
#include <system_error>
#include <unordered_set>
#include <utility>
#include <vector>

#if !defined(_WIN32)
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#endif

namespace {

namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;

constexpr std::array<uint64_t, 8> kFramePhases = {0, 3, 7, 13,
                                                  23, 37, 61, 89};
constexpr std::array<uint32_t, 2> kEvaluationSeeds = {0x13579bdfu,
                                                      0x8badf00du};

volatile std::sig_atomic_t gStopRequested = 0;

void handleSignal(int) { gStopRequested = 1; }

struct Options {
  std::vector<fs::path> inputPaths;
  fs::path outputDirectory;
  std::string backend = "auto";
  uint64_t durationSeconds = 18'000;
  uint64_t maxCandidates = 0;
  uint64_t seed = 0x474c494353454152ULL;
  double statusIntervalSeconds = 30.0;
  double renderScale = 1.0;
  bool resume = false;
};

struct Recipe {
  glic::CodecConfig config;
  glic::RealtimeEffectConfig effect{};
  float strength = 1.0f;
};

struct Metrics {
  double mae = 0.0;
  double changedRatio = 0.0;
  double lumaCorrelation = 0.0;
  double structure = 0.0;
  double clippingRatio = 0.0;
  double entropy = 0.0;
  double temporalResidualDelta = 0.0;
  double contentDependency = 0.0;
  double outputStandardDeviation = 0.0;
  double minimumInputChangedRatio = 0.0;
  double meanProcessMilliseconds = 0.0;
};

struct Elite {
  uint64_t candidateId = 0;
  uint64_t recipeHash = 0;
  uint64_t previewHash = 0;
  uint64_t evaluationHash = 0;
  std::string canonical;
  Recipe recipe;
  Metrics metrics;
  glic::RealtimeCertificationResult realtimeGate;
  std::string cell;
  std::string previewPath;
  double quality = 0.0;
};

struct SearchCounters {
  uint64_t attempted = 0;
  uint64_t evaluated = 0;
  uint64_t accepted = 0;
  uint64_t rejected = 0;
  uint64_t duplicates = 0;
};

struct InputImage {
  fs::path path;
  std::vector<glic::Color> pixels;
};

void printUsage(const char *program) {
  std::cerr
      << "Usage: " << program << " --input <png> --input <png> "
         "--output-dir <path> [options]\n"
      << "Runs a deterministic, API-free realtime glitch preset search.\n"
      << "  --input <png>              Input image; repeat at least twice\n"
      << "  --output-dir <path>        Search database/checkpoint directory\n"
      << "  --duration-seconds <n>     Wall-clock budget (default: 18000)\n"
      << "  --max-candidates <n>       Global candidate limit, 0=unlimited\n"
      << "  --seed <u64>               Deterministic search seed\n"
      << "  --backend <auto|cpu|metal> Realtime backend (default: auto)\n"
      << "  --scale <0.25..1>          Linear render scale (default: 1)\n"
      << "  --status-interval <sec>    Console heartbeat interval\n"
      << "  --resume                   Replay candidates.ndjson and continue\n";
}

template <typename T>
bool parseUnsigned(std::string_view text, T &destination) {
  try {
    size_t consumed = 0;
    const auto parsed = std::stoull(std::string(text), &consumed, 0);
    if (consumed != text.size() ||
        parsed > static_cast<unsigned long long>(
                     std::numeric_limits<T>::max()))
      return false;
    destination = static_cast<T>(parsed);
    return true;
  } catch (...) {
    return false;
  }
}

bool parseDouble(std::string_view text, double &destination) {
  try {
    size_t consumed = 0;
    const double parsed = std::stod(std::string(text), &consumed);
    if (consumed != text.size() || !std::isfinite(parsed))
      return false;
    destination = parsed;
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

    if (argument == "--input") {
      const char *value = takeValue();
      if (value == nullptr)
        return false;
      options.inputPaths.emplace_back(value);
    } else if (argument == "--output-dir") {
      const char *value = takeValue();
      if (value == nullptr)
        return false;
      options.outputDirectory = value;
    } else if (argument == "--duration-seconds" || argument == "--duration") {
      const char *value = takeValue();
      if (value == nullptr ||
          !parseUnsigned(value, options.durationSeconds) ||
          options.durationSeconds == 0)
        return false;
    } else if (argument == "--max-candidates") {
      const char *value = takeValue();
      if (value == nullptr || !parseUnsigned(value, options.maxCandidates))
        return false;
    } else if (argument == "--seed") {
      const char *value = takeValue();
      if (value == nullptr || !parseUnsigned(value, options.seed))
        return false;
    } else if (argument == "--backend") {
      const char *value = takeValue();
      if (value == nullptr)
        return false;
      options.backend = value;
    } else if (argument == "--scale") {
      const char *value = takeValue();
      if (value == nullptr || !parseDouble(value, options.renderScale) ||
          options.renderScale < 0.25 || options.renderScale > 1.0)
        return false;
    } else if (argument == "--status-interval") {
      const char *value = takeValue();
      if (value == nullptr ||
          !parseDouble(value, options.statusIntervalSeconds) ||
          options.statusIntervalSeconds <= 0.0)
        return false;
    } else if (argument == "--resume") {
      options.resume = true;
    } else if (argument == "--help" || argument == "-h") {
      return false;
    } else {
      std::cerr << "Unknown argument: " << argument << '\n';
      return false;
    }
  }

  if (options.inputPaths.size() < 2 || options.outputDirectory.empty())
    return false;
  if (options.backend != "auto" && options.backend != "cpu" &&
      options.backend != "metal")
    return false;
  return true;
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

std::string utcTimestamp() {
  const std::time_t now = std::time(nullptr);
  std::tm value{};
#if defined(_WIN32)
  gmtime_s(&value, &now);
#else
  gmtime_r(&now, &value);
#endif
  std::ostringstream result;
  result << std::put_time(&value, "%Y-%m-%dT%H:%M:%SZ");
  return result.str();
}

bool hasPngExtension(const fs::path &path) {
  std::string extension = path.extension().string();
  std::transform(extension.begin(), extension.end(), extension.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  return extension == ".png";
}

bool syncFile(const fs::path &path, std::string &error) {
#if defined(_WIN32)
  (void)path;
  (void)error;
  return true;
#else
  const int descriptor = ::open(path.c_str(), O_RDONLY);
  if (descriptor < 0) {
    error = "open for fsync failed: " + path.string();
    return false;
  }
  const bool succeeded = ::fsync(descriptor) == 0;
  ::close(descriptor);
  if (!succeeded)
    error = "fsync failed: " + path.string();
  return succeeded;
#endif
}

bool syncDirectory(const fs::path &directory, std::string &error) {
#if defined(_WIN32)
  (void)directory;
  (void)error;
  return true;
#else
  const int descriptor = ::open(directory.c_str(), O_RDONLY);
  if (descriptor < 0) {
    error = "open directory for fsync failed: " + directory.string();
    return false;
  }
  const bool succeeded = ::fsync(descriptor) == 0;
  ::close(descriptor);
  if (!succeeded)
    error = "directory fsync failed: " + directory.string();
  return succeeded;
#endif
}

bool atomicWrite(const fs::path &path, std::string_view contents,
                 bool keepPrevious, std::string &error) {
  const fs::path temporary = path.string() + ".tmp";
  const fs::path previous = path.string() + ".previous";
  const fs::path previousTemporary = previous.string() + ".tmp";

  {
    std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
    if (!output) {
      error = "failed to open temporary checkpoint: " + temporary.string();
      return false;
    }
    output.write(contents.data(), static_cast<std::streamsize>(contents.size()));
    output.flush();
    if (!output) {
      error = "failed to write temporary checkpoint: " + temporary.string();
      return false;
    }
  }
  if (!syncFile(temporary, error))
    return false;

  std::error_code filesystemError;
  if (keepPrevious && fs::exists(path)) {
    fs::copy_file(path, previousTemporary,
                  fs::copy_options::overwrite_existing, filesystemError);
    if (filesystemError) {
      error = "failed to copy previous checkpoint: " +
              filesystemError.message();
      return false;
    }
    if (!syncFile(previousTemporary, error))
      return false;
    fs::rename(previousTemporary, previous, filesystemError);
    if (filesystemError) {
      error = "failed to publish previous checkpoint: " +
              filesystemError.message();
      return false;
    }
  }

  filesystemError.clear();
  fs::rename(temporary, path, filesystemError);
  if (filesystemError) {
    error = "failed to publish checkpoint: " + filesystemError.message();
    return false;
  }
  return syncDirectory(path.parent_path(), error);
}

bool appendDurableLine(const fs::path &path, std::string_view line,
                       std::string &error) {
  std::string record(line);
  record.push_back('\n');
#if defined(_WIN32)
  std::ofstream output(path, std::ios::binary | std::ios::app);
  output << record;
  output.flush();
  if (!output) {
    error = "failed to append candidate log: " + path.string();
    return false;
  }
  return true;
#else
  const int descriptor =
      ::open(path.c_str(), O_WRONLY | O_CREAT | O_APPEND, 0644);
  if (descriptor < 0) {
    error = "failed to open candidate log: " + path.string();
    return false;
  }
  size_t written = 0;
  while (written < record.size()) {
    const ssize_t count =
        ::write(descriptor, record.data() + written, record.size() - written);
    if (count <= 0) {
      ::close(descriptor);
      error = "failed to append candidate log: " + path.string();
      return false;
    }
    written += static_cast<size_t>(count);
  }
  const bool succeeded = ::fsync(descriptor) == 0;
  ::close(descriptor);
  if (!succeeded) {
    error = "failed to fsync candidate log: " + path.string();
    return false;
  }
  return true;
#endif
}

bool recoverPartialLastLine(const fs::path &path, std::string &error) {
  if (!fs::exists(path) || fs::file_size(path) == 0)
    return true;
#if defined(_WIN32)
  std::ifstream input(path, std::ios::binary);
  std::string data((std::istreambuf_iterator<char>(input)),
                   std::istreambuf_iterator<char>());
  if (!data.empty() && data.back() != '\n') {
    const size_t finalNewline = data.find_last_of('\n');
    data.resize(finalNewline == std::string::npos ? 0 : finalNewline + 1);
    return atomicWrite(path, data, false, error);
  }
  return true;
#else
  const int descriptor = ::open(path.c_str(), O_RDWR);
  if (descriptor < 0) {
    error = "failed to open candidate log for recovery: " + path.string();
    return false;
  }
  const off_t size = ::lseek(descriptor, 0, SEEK_END);
  if (size <= 0) {
    ::close(descriptor);
    return true;
  }
  char finalCharacter = 0;
  if (::pread(descriptor, &finalCharacter, 1, size - 1) != 1) {
    ::close(descriptor);
    error = "failed to inspect candidate log tail";
    return false;
  }
  if (finalCharacter == '\n') {
    ::close(descriptor);
    return true;
  }

  constexpr size_t chunkSize = 4096;
  std::array<char, chunkSize> chunk{};
  off_t cursor = size;
  off_t truncateAt = 0;
  while (cursor > 0) {
    const size_t amount =
        static_cast<size_t>(std::min<off_t>(cursor, chunkSize));
    cursor -= static_cast<off_t>(amount);
    if (::pread(descriptor, chunk.data(), amount, cursor) !=
        static_cast<ssize_t>(amount)) {
      ::close(descriptor);
      error = "failed to scan candidate log tail";
      return false;
    }
    for (size_t index = amount; index > 0; --index) {
      if (chunk[index - 1] == '\n') {
        truncateAt = cursor + static_cast<off_t>(index);
        cursor = 0;
        break;
      }
    }
  }
  const bool succeeded = ::ftruncate(descriptor, truncateAt) == 0 &&
                         ::fsync(descriptor) == 0;
  ::close(descriptor);
  if (!succeeded)
    error = "failed to truncate incomplete candidate log record";
  return succeeded;
#endif
}

uint64_t splitMix64(uint64_t value) {
  value += 0x9e3779b97f4a7c15ULL;
  value = (value ^ (value >> 30)) * 0xbf58476d1ce4e5b9ULL;
  value = (value ^ (value >> 27)) * 0x94d049bb133111ebULL;
  return value ^ (value >> 31);
}

class DeterministicRng {
public:
  explicit DeterministicRng(uint64_t seed) : state_(seed) {}

  uint64_t next() {
    state_ += 0x9e3779b97f4a7c15ULL;
    return splitMix64(state_);
  }

  int integer(int minimum, int maximum) {
    const uint64_t range = static_cast<uint64_t>(maximum - minimum + 1);
    return minimum + static_cast<int>(next() % range);
  }

  bool chance(int numerator, int denominator) {
    return integer(1, denominator) <= numerator;
  }

private:
  uint64_t state_;
};

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
  case glic::RealtimeEffectFamily::COUNT:
    break;
  }
  return "legacy_block";
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
    // Legacy output does not consume these fields. Canonicalizing them to the
    // public defaults prevents visually identical legacy recipes from gaining
    // distinct hashes through inert parameters.
    effect.amount = 0.7f;
    effect.scale = 0.5f;
    effect.rate = 0.5f;
    return;
  }
  effect.amount = normalizedEffectParameter(effect.amount);
  effect.scale = normalizedEffectParameter(effect.scale);
  effect.rate = normalizedEffectParameter(effect.rate);
}

void makeNeutralCodec(glic::CodecConfig &config) {
  config.colorSpace = glic::ColorSpace::RGB;
  config.borderColorR = 128;
  config.borderColorG = 128;
  config.borderColorB = 128;
  glic::ChannelConfig channel;
  channel.minBlockSize = 8;
  channel.maxBlockSize = 32;
  channel.segmentationPrecision = 48.0f;
  channel.predictionMethod = glic::PredictionMethod::PAETH;
  channel.quantizationValue = 32;
  channel.clampMethod = glic::ClampMethod::NONE;
  channel.transformType = glic::TransformType::FWT;
  channel.waveletType = glic::WaveletType::NONE;
  channel.transformCompress = 0.0f;
  channel.transformScale = 20;
  channel.encodingMethod = glic::EncodingMethod::RAW;
  config.channels.fill(channel);
}

void normalizeRecipe(Recipe &recipe) {
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

glic::ChannelConfig randomChannel(DeterministicRng &rng) {
  constexpr std::array blockSizes = {2, 4, 8, 16, 32, 64, 128, 256};
  glic::ChannelConfig channel;
  const int minimumIndex = rng.integer(0, 4);
  const int maximumIndex = rng.integer(std::max(2, minimumIndex), 7);
  channel.minBlockSize = blockSizes[static_cast<size_t>(minimumIndex)];
  channel.maxBlockSize = blockSizes[static_cast<size_t>(maximumIndex)];
  channel.segmentationPrecision = static_cast<float>(rng.integer(2, 112));
  channel.predictionMethod =
      static_cast<glic::PredictionMethod>(rng.integer(-3, 23));
  channel.quantizationValue = rng.integer(8, 255);
  channel.clampMethod = static_cast<glic::ClampMethod>(rng.integer(0, 1));
  channel.transformType = static_cast<glic::TransformType>(rng.integer(0, 1));
  channel.waveletType = static_cast<glic::WaveletType>(rng.integer(0, 40));
  channel.transformCompress = static_cast<float>(rng.integer(0, 255));
  channel.transformScale = rng.integer(-80, 80);
  channel.encodingMethod =
      static_cast<glic::EncodingMethod>(rng.integer(0, 5));
  return channel;
}

glic::RealtimeEffectConfig
randomEffect(glic::RealtimeEffectFamily family, DeterministicRng &rng) {
  glic::RealtimeEffectConfig effect;
  effect.family = family;
  effect.amount = static_cast<float>(rng.integer(350, 1000)) / 1000.0f;
  int minimumScale = 0;
  if (family == glic::RealtimeEffectFamily::EDGE_ECHO)
    minimumScale = 150;
  else if (family == glic::RealtimeEffectFamily::POSTER_SOLAR)
    minimumScale = 50;
  effect.scale =
      static_cast<float>(rng.integer(minimumScale, 1000)) / 1000.0f;
  effect.rate = static_cast<float>(rng.integer(0, 1000)) / 1000.0f;
  normalizeEffect(effect);
  return effect;
}

void randomizeLegacyCodec(Recipe &recipe, DeterministicRng &rng) {
  recipe.config.colorSpace = static_cast<glic::ColorSpace>(rng.integer(0, 15));
  recipe.config.borderColorR = static_cast<uint8_t>(rng.integer(0, 255));
  recipe.config.borderColorG = static_cast<uint8_t>(rng.integer(0, 255));
  recipe.config.borderColorB = static_cast<uint8_t>(rng.integer(0, 255));
  recipe.strength = static_cast<float>(rng.integer(350, 2000)) / 1000.0f;

  const bool coupledChannels = rng.chance(2, 5);
  recipe.config.channels[0] = randomChannel(rng);
  for (size_t channelIndex = 1; channelIndex < 3; ++channelIndex) {
    if (!coupledChannels) {
      recipe.config.channels[channelIndex] = randomChannel(rng);
      continue;
    }
    auto channel = recipe.config.channels[0];
    channel.quantizationValue =
        std::clamp(channel.quantizationValue + rng.integer(-32, 32), 0, 255);
    channel.transformCompress = std::clamp(
        channel.transformCompress + static_cast<float>(rng.integer(-40, 40)),
        0.0f, 255.0f);
    if (rng.chance(1, 3))
      channel.predictionMethod =
          static_cast<glic::PredictionMethod>(rng.integer(-3, 23));
    if (rng.chance(1, 3))
      channel.encodingMethod =
          static_cast<glic::EncodingMethod>(rng.integer(0, 5));
    recipe.config.channels[channelIndex] = channel;
  }
}

Recipe generateRecipe(uint64_t searchSeed, uint64_t candidateId) {
  DeterministicRng rng(
      splitMix64(searchSeed ^ (candidateId * 0xd6e8feb86659fd93ULL)));
  Recipe recipe;
  constexpr uint64_t familyCount =
      static_cast<uint64_t>(glic::RealtimeEffectFamily::COUNT);
  const auto family = static_cast<glic::RealtimeEffectFamily>(
      candidateId % familyCount);
  recipe.effect = randomEffect(family, rng);
  if (family == glic::RealtimeEffectFamily::LEGACY_BLOCK) {
    randomizeLegacyCodec(recipe, rng);
  } else {
    // Non-codec effects get one neutral codec substrate. Their diversity must
    // come from a real effect mechanism and its explicit parameters, not from
    // inert color-space/channel combinations.
    makeNeutralCodec(recipe.config);
    recipe.strength = static_cast<float>(rng.integer(650, 1600)) / 1000.0f;
  }
  normalizeRecipe(recipe);
  return recipe;
}

Recipe mutateRecipe(uint64_t searchSeed, uint64_t candidateId,
                    const Recipe &parent,
                    glic::RealtimeEffectFamily desiredFamily) {
  if (parent.effect.family != desiredFamily)
    return generateRecipe(searchSeed, candidateId);

  DeterministicRng rng(splitMix64(searchSeed ^
                                  (candidateId * 0xa0761d6478bd642fULL) ^
                                  0xe7037ed1a0b428dbULL));
  Recipe recipe = parent;

  // Family transitions are scheduled by the deterministic nine-candidate
  // sweep. Keeping mutation inside its requested family guarantees equal trial
  // counts while still evolving every family independently.

  if (recipe.effect.family != glic::RealtimeEffectFamily::LEGACY_BLOCK) {
    const int parameterMutations = rng.integer(2, 5);
    for (int mutation = 0; mutation < parameterMutations; ++mutation) {
      const float delta =
          static_cast<float>(rng.integer(-350, 350)) / 1000.0f;
      switch (rng.integer(0, 5)) {
      case 0:
        recipe.effect.amount += delta;
        break;
      case 1:
        recipe.effect.scale += delta;
        break;
      case 2:
        recipe.effect.rate += delta;
        break;
      case 3:
        recipe.strength += delta;
        break;
      case 4:
        recipe.effect.amount += delta;
        recipe.effect.scale -= delta * 0.5f;
        break;
      case 5:
        recipe.effect.rate += delta;
        recipe.strength -= delta * 0.35f;
        break;
      }
    }
    // Keep the codec substrate identical across non-legacy descendants.
    makeNeutralCodec(recipe.config);
    normalizeRecipe(recipe);
    return recipe;
  }

  const int mutationCount = rng.integer(2, 7);
  for (int mutation = 0; mutation < mutationCount; ++mutation) {
    auto &channel = recipe.config.channels[static_cast<size_t>(rng.integer(0, 2))];
    switch (rng.integer(0, 16)) {
    case 0:
      recipe.strength += static_cast<float>(rng.integer(-350, 350)) / 1000.0f;
      break;
    case 1:
      recipe.config.colorSpace =
          static_cast<glic::ColorSpace>(rng.integer(0, 15));
      break;
    case 2: {
      uint8_t *border[] = {&recipe.config.borderColorR,
                           &recipe.config.borderColorG,
                           &recipe.config.borderColorB};
      auto &component = *border[static_cast<size_t>(rng.integer(0, 2))];
      component = static_cast<uint8_t>(std::clamp(
          static_cast<int>(component) + rng.integer(-64, 64), 0, 255));
      break;
    }
    case 3:
      channel.minBlockSize = std::clamp(
          channel.minBlockSize * (rng.chance(1, 2) ? 2 : 1) /
              (rng.chance(1, 2) ? 2 : 1),
          2, 256);
      break;
    case 4:
      channel.maxBlockSize = std::clamp(
          channel.maxBlockSize * (rng.chance(1, 2) ? 2 : 1) /
              (rng.chance(1, 2) ? 2 : 1),
          2, 256);
      break;
    case 5:
      channel.segmentationPrecision +=
          static_cast<float>(rng.integer(-24, 24));
      break;
    case 6:
      channel.predictionMethod =
          static_cast<glic::PredictionMethod>(rng.integer(-3, 23));
      break;
    case 7:
      channel.quantizationValue += rng.integer(-48, 48);
      break;
    case 8:
      channel.clampMethod = channel.clampMethod == glic::ClampMethod::NONE
                                ? glic::ClampMethod::MOD256
                                : glic::ClampMethod::NONE;
      break;
    case 9:
      channel.transformType =
          channel.transformType == glic::TransformType::FWT
              ? glic::TransformType::WPT
              : glic::TransformType::FWT;
      break;
    case 10: {
      constexpr std::array wavelets = {
          glic::WaveletType::NONE, glic::WaveletType::HAAR_ORTHOGONAL,
          glic::WaveletType::BIORTHOGONAL11, glic::WaveletType::COIFLET1,
          glic::WaveletType::SYMLET2};
      channel.waveletType =
          wavelets[static_cast<size_t>(rng.integer(0, wavelets.size() - 1))];
      break;
    }
    case 11:
      channel.transformCompress += static_cast<float>(rng.integer(-56, 56));
      break;
    case 12:
      channel.transformScale += rng.integer(-24, 24);
      break;
    case 13:
      channel.encodingMethod =
          static_cast<glic::EncodingMethod>(rng.integer(0, 5));
      break;
    case 14:
      // A full channel restart escapes families of locally similar block
      // patterns while retaining the other two channels from the parent.
      channel = randomChannel(rng);
      break;
    case 15: {
      const size_t first = static_cast<size_t>(rng.integer(0, 2));
      size_t second = static_cast<size_t>(rng.integer(0, 1));
      if (second >= first)
        ++second;
      std::swap(recipe.config.channels[first], recipe.config.channels[second]);
      break;
    }
    case 16:
      // Rare macro restart: keep the parent's global color treatment but
      // generate a substantially different three-channel glitch topology.
      for (auto &replacement : recipe.config.channels)
        replacement = randomChannel(rng);
      break;
    }
  }
  normalizeRecipe(recipe);
  return recipe;
}

void appendCanonicalCodec(std::ostringstream &output, const Recipe &recipe) {
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

std::string canonicalRecipeV1(const Recipe &recipe) {
  std::ostringstream output;
  output << "v1|";
  appendCanonicalCodec(output, recipe);
  return output.str();
}

std::string canonicalRecipe(const Recipe &recipe) {
  std::ostringstream output;
  output << "v2|";
  appendCanonicalCodec(output, recipe);
  output << '|' << static_cast<int>(recipe.effect.family) << ','
         << std::lround(recipe.effect.amount * 1000.0f) << ','
         << std::lround(recipe.effect.scale * 1000.0f) << ','
         << std::lround(recipe.effect.rate * 1000.0f);
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
  std::array<long long, 42> values{};
  if (!(input >> version) || (version != "v1" && version != "v2"))
    return false;
  const size_t valueCount = version == "v1" ? 38 : values.size();
  for (size_t index = 0; index < valueCount; ++index) {
    if (!(input >> values[index]) ||
        values[index] < static_cast<long long>(std::numeric_limits<int>::min()) ||
        values[index] > static_cast<long long>(std::numeric_limits<int>::max()))
      return false;
  }
  std::string extra;
  if (input >> extra)
    return false;

  size_t position = 0;
  recipe.config.colorSpace =
      static_cast<glic::ColorSpace>(values[position++]);
  recipe.config.borderColorR = static_cast<uint8_t>(values[position++]);
  recipe.config.borderColorG = static_cast<uint8_t>(values[position++]);
  recipe.config.borderColorB = static_cast<uint8_t>(values[position++]);
  recipe.strength = static_cast<float>(values[position++]) / 1000.0f;
  for (auto &channel : recipe.config.channels) {
    channel.minBlockSize = static_cast<int>(values[position++]);
    channel.maxBlockSize = static_cast<int>(values[position++]);
    channel.segmentationPrecision =
        static_cast<float>(values[position++]) / 1000.0f;
    channel.predictionMethod =
        static_cast<glic::PredictionMethod>(values[position++]);
    channel.quantizationValue = static_cast<int>(values[position++]);
    channel.clampMethod =
        static_cast<glic::ClampMethod>(values[position++]);
    channel.transformType =
        static_cast<glic::TransformType>(values[position++]);
    channel.waveletType =
        static_cast<glic::WaveletType>(values[position++]);
    channel.transformCompress =
        static_cast<float>(values[position++]) / 1000.0f;
    channel.transformScale = static_cast<int>(values[position++]);
    channel.encodingMethod =
        static_cast<glic::EncodingMethod>(values[position++]);
  }
  if (version == "v2") {
    recipe.effect.family =
        static_cast<glic::RealtimeEffectFamily>(values[position++]);
    recipe.effect.amount = static_cast<float>(values[position++]) / 1000.0f;
    recipe.effect.scale = static_cast<float>(values[position++]) / 1000.0f;
    recipe.effect.rate = static_cast<float>(values[position++]) / 1000.0f;
  } else {
    recipe.effect.family = glic::RealtimeEffectFamily::LEGACY_BLOCK;
    recipe.effect.amount = 0.7f;
    recipe.effect.scale = 0.5f;
    recipe.effect.rate = 0.5f;
  }
  normalizeRecipe(recipe);
  return (version == "v1" ? canonicalRecipeV1(recipe)
                           : canonicalRecipe(recipe)) == original;
}

uint64_t recipeHash(std::string_view canonical) {
  uint64_t hash = 1469598103934665603ULL;
  for (const unsigned char value : canonical) {
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

void appendHashByte(uint64_t &hash, uint8_t value) {
    hash ^= value;
    hash *= 1099511628211ULL;
}

void appendHashValue(uint64_t &hash, uint64_t value) {
  for (int byte = 0; byte < 8; ++byte)
    appendHashByte(hash, static_cast<uint8_t>(value >> (byte * 8)));
}

void appendPixelsHash(uint64_t &hash,
                      const std::vector<glic::Color> &pixels) {
  for (const glic::Color color : pixels) {
    appendHashByte(hash, glic::getR(color));
    appendHashByte(hash, glic::getG(color));
    appendHashByte(hash, glic::getB(color));
    appendHashByte(hash, glic::getA(color));
  }
}

uint64_t hashPixels(const std::vector<glic::Color> &pixels) {
  uint64_t hash = 1469598103934665603ULL;
  appendPixelsHash(hash, pixels);
  return hash;
}

std::string recipeJson(const Recipe &recipe) {
  std::ostringstream output;
  output << std::fixed << std::setprecision(3)
         << "{\"color_space\":"
         << static_cast<int>(recipe.config.colorSpace)
         << ",\"border_rgb\":["
         << static_cast<int>(recipe.config.borderColorR) << ','
         << static_cast<int>(recipe.config.borderColorG) << ','
         << static_cast<int>(recipe.config.borderColorB)
         << "],\"strength\":" << recipe.strength
         << ",\"family_name\":\""
         << effectFamilyName(recipe.effect.family)
         << "\",\"effect\":{\"family\":"
         << static_cast<int>(recipe.effect.family)
         << ",\"family_name\":\""
         << effectFamilyName(recipe.effect.family)
         << "\",\"amount\":" << recipe.effect.amount
         << ",\"scale\":" << recipe.effect.scale
         << ",\"rate\":" << recipe.effect.rate << "},\"channels\":[";
  for (size_t index = 0; index < recipe.config.channels.size(); ++index) {
    const auto &channel = recipe.config.channels[index];
    output << "{\"min_block\":" << channel.minBlockSize
           << ",\"max_block\":" << channel.maxBlockSize
           << ",\"segmentation_precision\":"
           << channel.segmentationPrecision << ",\"prediction\":"
           << static_cast<int>(channel.predictionMethod)
           << ",\"quantization\":" << channel.quantizationValue
           << ",\"clamp\":" << static_cast<int>(channel.clampMethod)
           << ",\"transform\":" << static_cast<int>(channel.transformType)
           << ",\"wavelet\":" << static_cast<int>(channel.waveletType)
           << ",\"transform_compress\":" << channel.transformCompress
           << ",\"transform_scale\":" << channel.transformScale
           << ",\"encoding\":" << static_cast<int>(channel.encodingMethod)
           << '}';
    if (index + 1 != recipe.config.channels.size())
      output << ',';
  }
  output << "]}";
  return output.str();
}

class PearsonAccumulator {
public:
  void reset() { *this = PearsonAccumulator{}; }

  void add(double x, double y) {
    ++count_;
    sumX_ += x;
    sumY_ += y;
    sumXX_ += x * x;
    sumYY_ += y * y;
    sumXY_ += x * y;
  }

  double correlation() const {
    if (count_ < 2)
      return 0.0;
    const double count = static_cast<double>(count_);
    const double numerator = count * sumXY_ - sumX_ * sumY_;
    const double varianceX = count * sumXX_ - sumX_ * sumX_;
    const double varianceY = count * sumYY_ - sumY_ * sumY_;
    const double denominator = std::sqrt(std::max(0.0, varianceX) *
                                         std::max(0.0, varianceY));
    return denominator <= 1e-12 ? 0.0 : numerator / denominator;
  }

private:
  uint64_t count_ = 0;
  double sumX_ = 0.0;
  double sumY_ = 0.0;
  double sumXX_ = 0.0;
  double sumYY_ = 0.0;
  double sumXY_ = 0.0;
};

double luma(glic::Color color) {
  return 0.2126 * glic::getR(color) + 0.7152 * glic::getG(color) +
         0.0722 * glic::getB(color);
}

struct PerInputAccumulator {
  double absoluteError = 0.0;
  uint64_t channelSamples = 0;
  uint64_t changedPixels = 0;
  uint64_t pixelSamples = 0;
};

class MetricAccumulator {
public:
  explicit MetricAccumulator(size_t inputCount)
      : perInput_(inputCount) {}

  void reset() {
    absoluteError_ = 0.0;
    channelSamples_ = 0;
    changedPixels_ = 0;
    pixelSamples_ = 0;
    clippedChannels_ = 0;
    outputSum_ = 0.0;
    outputSumSquares_ = 0.0;
    outputSamples_ = 0;
    temporalDifference_ = 0.0;
    temporalSamples_ = 0;
    histogram_.fill(0);
    lumaPearson_.reset();
    gradientPearson_.reset();
    std::fill(perInput_.begin(), perInput_.end(), PerInputAccumulator{});
  }

  void addFrame(size_t inputIndex, const std::vector<glic::Color> &input,
                const std::vector<glic::Color> &output, int width, int height,
                std::vector<int16_t> &previousResidual,
                bool hasPreviousResidual) {
    auto &inputAccumulator = perInput_[inputIndex];
    for (size_t index = 0; index < input.size(); ++index) {
      const int dr = std::abs(static_cast<int>(glic::getR(output[index])) -
                              static_cast<int>(glic::getR(input[index])));
      const int dg = std::abs(static_cast<int>(glic::getG(output[index])) -
                              static_cast<int>(glic::getG(input[index])));
      const int db = std::abs(static_cast<int>(glic::getB(output[index])) -
                              static_cast<int>(glic::getB(input[index])));
      const double error = static_cast<double>(dr + dg + db);
      absoluteError_ += error;
      channelSamples_ += 3;
      inputAccumulator.absoluteError += error;
      inputAccumulator.channelSamples += 3;
      ++pixelSamples_;
      ++inputAccumulator.pixelSamples;
      if (std::max({dr, dg, db}) >= 10) {
        ++changedPixels_;
        ++inputAccumulator.changedPixels;
      }

      const std::array outputChannels = {glic::getR(output[index]),
                                         glic::getG(output[index]),
                                         glic::getB(output[index])};
      for (const uint8_t channel : outputChannels) {
        if (channel <= 2 || channel >= 253)
          ++clippedChannels_;
      }

      const double inputLuma = luma(input[index]);
      const double outputLuma = luma(output[index]);
      lumaPearson_.add(inputLuma, outputLuma);
      outputSum_ += outputLuma;
      outputSumSquares_ += outputLuma * outputLuma;
      ++outputSamples_;
      const size_t bin = std::min<size_t>(
          63, static_cast<size_t>(std::max(0.0, outputLuma)) / 4);
      ++histogram_[bin];

      const int residual =
          static_cast<int>(std::lround(outputLuma - inputLuma));
      if (hasPreviousResidual) {
        temporalDifference_ +=
            std::abs(residual - static_cast<int>(previousResidual[index]));
        ++temporalSamples_;
      }
      previousResidual[index] = static_cast<int16_t>(residual);
    }

    for (int y = 1; y < height; y += 2) {
      for (int x = 1; x < width; x += 2) {
        const size_t index = static_cast<size_t>(y) *
                                 static_cast<size_t>(width) +
                             static_cast<size_t>(x);
        const double dryGradient =
            std::abs(luma(input[index]) - luma(input[index - 1])) +
            std::abs(luma(input[index]) -
                     luma(input[index - static_cast<size_t>(width)]));
        const double wetGradient =
            std::abs(luma(output[index]) - luma(output[index - 1])) +
            std::abs(luma(output[index]) -
                     luma(output[index - static_cast<size_t>(width)]));
        gradientPearson_.add(dryGradient, wetGradient);
      }
    }
  }

  Metrics finalize(const std::vector<InputImage> &inputs,
                   const std::vector<std::vector<glic::Color>> &representatives,
                   double totalProcessMilliseconds,
                   uint64_t processedFrames) const {
    Metrics metrics;
    if (channelSamples_ != 0)
      metrics.mae = absoluteError_ / static_cast<double>(channelSamples_);
    if (pixelSamples_ != 0)
      metrics.changedRatio =
          static_cast<double>(changedPixels_) / pixelSamples_;
    metrics.lumaCorrelation =
        std::clamp(lumaPearson_.correlation(), -1.0, 1.0);
    const double gradientCorrelation =
        std::clamp(gradientPearson_.correlation(), -1.0, 1.0);
    metrics.structure =
        std::clamp(0.7 * std::max(0.0, gradientCorrelation) +
                       0.3 * std::max(0.0, metrics.lumaCorrelation),
                   0.0, 1.0);
    if (channelSamples_ != 0)
      metrics.clippingRatio =
          static_cast<double>(clippedChannels_) / channelSamples_;
    if (outputSamples_ != 0) {
      const double count = static_cast<double>(outputSamples_);
      const double mean = outputSum_ / count;
      const double variance =
          std::max(0.0, outputSumSquares_ / count - mean * mean);
      metrics.outputStandardDeviation = std::sqrt(variance) / 255.0;
      double entropy = 0.0;
      for (const uint64_t binCount : histogram_) {
        if (binCount == 0)
          continue;
        const double probability = static_cast<double>(binCount) / count;
        entropy -= probability * std::log2(probability);
      }
      metrics.entropy = entropy / 6.0;
    }
    if (temporalSamples_ != 0)
      metrics.temporalResidualDelta =
          temporalDifference_ /
          (static_cast<double>(temporalSamples_) * 255.0);

    double dependencySum = 0.0;
    uint64_t dependencyPairs = 0;
    for (size_t first = 0; first < inputs.size(); ++first) {
      for (size_t second = first + 1; second < inputs.size(); ++second) {
        double dryDifference = 0.0;
        double wetDifference = 0.0;
        const size_t count = inputs[first].pixels.size();
        for (size_t index = 0; index < count; ++index) {
          dryDifference += std::abs(luma(inputs[first].pixels[index]) -
                                    luma(inputs[second].pixels[index]));
          wetDifference +=
              std::abs(luma(representatives[first][index]) -
                       luma(representatives[second][index]));
        }
        dryDifference /= static_cast<double>(count);
        wetDifference /= static_cast<double>(count);
        if (dryDifference >= 1.0) {
          dependencySum += std::min(4.0, wetDifference / dryDifference);
          ++dependencyPairs;
        }
      }
    }
    if (dependencyPairs != 0)
      metrics.contentDependency =
          dependencySum / static_cast<double>(dependencyPairs);

    metrics.minimumInputChangedRatio = 1.0;
    for (const auto &input : perInput_) {
      const double changed =
          input.pixelSamples == 0
              ? 0.0
              : static_cast<double>(input.changedPixels) / input.pixelSamples;
      metrics.minimumInputChangedRatio =
          std::min(metrics.minimumInputChangedRatio, changed);
    }
    if (perInput_.empty())
      metrics.minimumInputChangedRatio = 0.0;
    if (processedFrames != 0)
      metrics.meanProcessMilliseconds =
          totalProcessMilliseconds / static_cast<double>(processedFrames);
    return metrics;
  }

private:
  double absoluteError_ = 0.0;
  uint64_t channelSamples_ = 0;
  uint64_t changedPixels_ = 0;
  uint64_t pixelSamples_ = 0;
  uint64_t clippedChannels_ = 0;
  double outputSum_ = 0.0;
  double outputSumSquares_ = 0.0;
  uint64_t outputSamples_ = 0;
  double temporalDifference_ = 0.0;
  uint64_t temporalSamples_ = 0;
  std::array<uint64_t, 64> histogram_{};
  PearsonAccumulator lumaPearson_;
  PearsonAccumulator gradientPearson_;
  std::vector<PerInputAccumulator> perInput_;
};

std::vector<glic::Color> resizeImage(const std::vector<glic::Color> &source,
                                     int sourceWidth, int sourceHeight,
                                     int targetWidth, int targetHeight) {
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
      target[static_cast<size_t>(y) * targetWidth + x] =
          glic::makeColor(channels[0], channels[1], channels[2], channels[3]);
    }
  }
  return target;
}

bool loadInputs(const Options &options, std::vector<InputImage> &inputs,
                int &width, int &height, std::string &error) {
  int referenceWidth = 0;
  int referenceHeight = 0;
  std::vector<std::vector<glic::Color>> originals;
  std::vector<std::pair<int, int>> dimensions;
  originals.reserve(options.inputPaths.size());
  dimensions.reserve(options.inputPaths.size());

  for (const auto &path : options.inputPaths) {
    if (!hasPngExtension(path)) {
      error = "input must be a PNG: " + path.string();
      return false;
    }
    std::vector<glic::Color> pixels;
    int inputWidth = 0;
    int inputHeight = 0;
    if (!glic::loadImage(path.string(), pixels, inputWidth, inputHeight)) {
      error = "failed to load input PNG: " + path.string();
      return false;
    }
    if (inputWidth <= 0 || inputHeight <= 0) {
      error = "invalid input dimensions: " + path.string();
      return false;
    }
    if (originals.empty()) {
      referenceWidth = inputWidth;
      referenceHeight = inputHeight;
    }
    originals.push_back(std::move(pixels));
    dimensions.emplace_back(inputWidth, inputHeight);
  }

  width = std::max(32, static_cast<int>(
                           std::lround(referenceWidth * options.renderScale)));
  height = std::max(32, static_cast<int>(
                            std::lround(referenceHeight * options.renderScale)));
  inputs.reserve(originals.size());
  for (size_t index = 0; index < originals.size(); ++index) {
    InputImage input;
    input.path = options.inputPaths[index];
    input.pixels = resizeImage(originals[index], dimensions[index].first,
                               dimensions[index].second, width, height);
    inputs.push_back(std::move(input));
  }
  return true;
}

uint64_t configurationFingerprint(const Options &options,
                                  std::string_view backend, int width,
                                  int height,
                                  const std::vector<InputImage> &inputs) {
  uint64_t hash = 1469598103934665603ULL;
  const auto addByte = [&](uint8_t value) {
    hash ^= value;
    hash *= 1099511628211ULL;
  };
  const auto addText = [&](std::string_view text) {
    for (const unsigned char value : text)
      addByte(value);
    addByte(0xffu);
  };
  addText("glic-search-config-v6");
  addText(backend);
  addText("metal");
  addText(std::to_string(glic::kRealtimeCertificationWidth));
  addText(std::to_string(glic::kRealtimeCertificationHeight));
  addText(std::to_string(glic::kRealtimeCertificationWarmupFrames));
  addText(std::to_string(glic::kRealtimeCertificationMeasuredFrames));
  addText(std::to_string(glic::kRealtimeCertificationTargetFps));
  addText("ceil-n-minus-one-percentile-v1");
  addText("synchronous-buffer-roundtrip-v1");
  addText(std::to_string(options.seed));
  addText(std::to_string(options.renderScale));
  addText(std::to_string(width));
  addText(std::to_string(height));
  for (const uint32_t seed : kEvaluationSeeds)
    addText(std::to_string(seed));
  for (const auto &input : inputs) {
    addText(std::to_string(input.pixels.size()));
    for (const glic::Color color : input.pixels) {
      addByte(glic::getR(color));
      addByte(glic::getG(color));
      addByte(glic::getB(color));
      addByte(glic::getA(color));
    }
  }
  return hash;
}

std::string runConfigurationJson(const Options &options,
                                 std::string_view backend, int width,
                                 int height, uint64_t fingerprint) {
  std::ostringstream output;
  output << std::fixed << std::setprecision(8)
         << "{\n  \"schema\": \"glic-realtime-search-run-config-v6\",\n"
         << "  \"algorithm\": \"map-elites-family-stratified-v6\",\n"
         << "  \"recipe_schema\": \"glic-realtime-recipe-v2\",\n"
         << "  \"fingerprint\": \"" << hexHash(fingerprint) << "\",\n"
         << "  \"backend\": \"" << jsonEscape(backend) << "\",\n"
         << "  \"seed\": " << options.seed << ",\n"
         << "  \"render_scale\": " << options.renderScale << ",\n"
         << "  \"width\": " << width << ",\n"
         << "  \"height\": " << height << ",\n"
         << "  \"realtime_gate\": {\"backend\":\"metal\",\"width\":"
         << glic::kRealtimeCertificationWidth << ",\"height\":"
         << glic::kRealtimeCertificationHeight << ",\"target_fps\":"
         << glic::kRealtimeCertificationTargetFps
         << ",\"frame_budget_ms\":"
         << glic::kRealtimeCertificationFrameBudgetMilliseconds
         << ",\"warmup_frames\":"
         << glic::kRealtimeCertificationWarmupFrames
         << ",\"measured_frames\":"
         << glic::kRealtimeCertificationMeasuredFrames
         << ",\"percentile\":\"ceil-n-minus-one-v1\","
            "\"measurement_path\":\"synchronous-buffer-roundtrip-v1\"}\n}\n";
  return output.str();
}

bool sparseEffectFamily(glic::RealtimeEffectFamily family) {
  return family == glic::RealtimeEffectFamily::LINE_TEAR ||
         family == glic::RealtimeEffectFamily::CHANNEL_SHEAR ||
         family == glic::RealtimeEffectFamily::ANALOG_SYNC ||
         family == glic::RealtimeEffectFamily::EDGE_ECHO;
}

bool denseTonalEffectFamily(glic::RealtimeEffectFamily family) {
  return family == glic::RealtimeEffectFamily::BITPLANE_DITHER ||
         family == glic::RealtimeEffectFamily::POSTER_SOLAR;
}

std::optional<std::string>
hardRejectReason(const Metrics &metrics, glic::RealtimeEffectFamily family) {
  if (!std::isfinite(metrics.mae) || !std::isfinite(metrics.entropy) ||
      !std::isfinite(metrics.structure) ||
      !std::isfinite(metrics.temporalResidualDelta) ||
      !std::isfinite(metrics.meanProcessMilliseconds))
    return "non_finite_metrics";
  if (metrics.meanProcessMilliseconds > 1000.0 / 30.0)
    return "below_30_fps_lowres_prefilter";
  const bool sparse = sparseEffectFamily(family);
  const double minimumMae = sparse ? 1.5 : 8.0;
  const double minimumChanged = sparse ? 0.02 : 0.20;
  const double minimumInputChanged = sparse ? 0.01 : 0.15;
  if (metrics.mae < minimumMae || metrics.changedRatio < minimumChanged ||
      metrics.minimumInputChangedRatio < minimumInputChanged)
    return "no_op";
  const double maximumChanged = denseTonalEffectFamily(family) ? 0.995 : 0.95;
  if (metrics.mae > 75.0 || metrics.changedRatio > maximumChanged)
    return "excessive_change";
  if (metrics.entropy < 0.12 || metrics.outputStandardDeviation < 0.031)
    return "collapsed_output";
  if (metrics.clippingRatio > 0.25)
    return "excessive_clipping";
  if (std::abs(metrics.lumaCorrelation) < 0.10 || metrics.structure < 0.15 ||
      metrics.contentDependency < 0.15)
    return "input_independent_noise";
  return std::nullopt;
}

int fixedBin(double value, double firstThreshold, double secondThreshold) {
  if (value < firstThreshold)
    return 0;
  return value < secondThreshold ? 1 : 2;
}

std::string behaviorCell(const Metrics &metrics,
                         glic::RealtimeEffectFamily family) {
  const int change = fixedBin(metrics.mae, 35.0, 60.0);
  const int structure = fixedBin(metrics.structure, 0.40, 0.65);
  const int temporal =
      fixedBin(metrics.temporalResidualDelta, 0.12, 0.18);
  const int dependency = fixedBin(metrics.contentDependency, 0.75, 0.95);
  return "f-" + std::string(effectFamilyName(family)) + "-c" +
         std::to_string(change) + "-s" +
         std::to_string(structure) + "-t" + std::to_string(temporal) +
         "-d" + std::to_string(dependency);
}

double qualityScore(const Metrics &metrics) {
  const double change =
      std::exp(-std::abs(metrics.mae - 42.0) / 55.0);
  const double structure = 0.2 + 0.8 * metrics.structure;
  const double entropy =
      std::clamp(1.0 - std::abs(metrics.entropy - 0.78) / 0.78, 0.0, 1.0);
  const double dependency =
      std::clamp(metrics.contentDependency / 1.0, 0.0, 1.0);
  const double temporal = std::exp(
      -std::abs(metrics.temporalResidualDelta - 0.08) / 0.18);
  const double unclipped = 1.0 - metrics.clippingRatio;
  const double robustness = metrics.minimumInputChangedRatio;
  return std::clamp(0.20 * change + 0.20 * structure + 0.15 * entropy +
                        0.20 * dependency + 0.08 * temporal +
                        0.07 * unclipped + 0.10 * robustness,
                    0.0, 1.0);
}

class Archive {
public:
  struct Removal {
    std::string cell;
    uint64_t recipeHash = 0;

    bool operator==(const Removal &) const = default;
  };

  struct AdmissionPlan {
    bool admitted = false;
    uint64_t candidateRecipeHash = 0;
    std::vector<Removal> removals;
  };

  struct CommitResult {
    bool admitted = false;
    std::vector<std::string> removedPreviewPaths;
  };

  AdmissionPlan planAdmission(const Elite &elite) const {
    AdmissionPlan plan;
    plan.candidateRecipeHash = elite.recipeHash;

    for (const auto &[cellName, cell] : cells_) {
      (void)cellName;
      if (std::any_of(cell.begin(), cell.end(), [&](const Elite &existing) {
            return existing.recipeHash == elite.recipeHash;
          }))
        return plan;
    }

    if (elite.previewHash != 0) {
      for (const auto &[cellName, existingCell] : cells_) {
        const auto visualDuplicate = std::find_if(
            existingCell.begin(), existingCell.end(), [&](const Elite &existing) {
              return existing.previewHash == elite.previewHash;
            });
        if (visualDuplicate == existingCell.end())
          continue;
        if (visualDuplicate->quality >= elite.quality)
          return plan;
        plan.removals.push_back(
            {.cell = cellName, .recipeHash = visualDuplicate->recipeHash});
        break;
      }
    }

    const auto target = cells_.find(elite.cell);
    size_t effectiveSize = 0;
    const Elite *worst = nullptr;
    if (target != cells_.end()) {
      for (const auto &existing : target->second) {
        const bool alreadyRemoved =
            std::any_of(plan.removals.begin(), plan.removals.end(),
                        [&](const Removal &removal) {
                          return removal.cell == elite.cell &&
                                 removal.recipeHash == existing.recipeHash;
                        });
        if (alreadyRemoved)
          continue;
        ++effectiveSize;
        if (worst == nullptr || ranksBefore(*worst, existing))
          worst = &existing;
      }
    }

    if (effectiveSize > 4)
      return plan;
    if (effectiveSize == 4) {
      if (worst == nullptr || !ranksBefore(elite, *worst))
        return plan;
      plan.removals.push_back(
          {.cell = elite.cell, .recipeHash = worst->recipeHash});
    }

    plan.admitted = true;
    return plan;
  }

  CommitResult commitAdmission(Elite elite, const AdmissionPlan &plan) {
    CommitResult result;
    if (!plan.admitted || plan.candidateRecipeHash != elite.recipeHash ||
        !elite.realtimeGate.performed || !elite.realtimeGate.processPassed ||
        !elite.realtimeGate.passed)
      return result;

    const AdmissionPlan currentPlan = planAdmission(elite);
    if (!currentPlan.admitted || currentPlan.removals != plan.removals)
      return result;

    auto nextCells = cells_;
    for (const auto &removal : plan.removals) {
      auto cell = nextCells.find(removal.cell);
      if (cell == nextCells.end())
        return result;
      auto existing = std::find_if(
          cell->second.begin(), cell->second.end(), [&](const Elite &candidate) {
            return candidate.recipeHash == removal.recipeHash;
          });
      if (existing == cell->second.end())
        return result;
      if (!existing->previewPath.empty())
        result.removedPreviewPaths.push_back(existing->previewPath);
      cell->second.erase(existing);
      if (cell->second.empty())
        nextCells.erase(cell);
    }

    auto &target = nextCells[elite.cell];
    target.push_back(std::move(elite));
    std::sort(target.begin(), target.end(), ranksBefore);
    if (target.size() > 4)
      return {};

    cells_.swap(nextCells);
    result.admitted = true;
    return result;
  }

  size_t cellCount() const { return cells_.size(); }

  size_t eliteCount() const {
    size_t count = 0;
    for (const auto &[name, cell] : cells_) {
      (void)name;
      count += cell.size();
    }
    return count;
  }

  const std::map<std::string, std::vector<Elite>> &cells() const {
    return cells_;
  }

  const Elite *selectParentForFamily(glic::RealtimeEffectFamily family,
                                     uint64_t selector) const {
    size_t eligibleCount = 0;
    for (const auto &[name, cell] : cells_) {
      (void)name;
      eligibleCount += static_cast<size_t>(
          std::count_if(cell.begin(), cell.end(), [&](const Elite &elite) {
            return elite.recipe.effect.family == family;
          }));
    }
    if (eligibleCount == 0)
      return nullptr;

    size_t selected = static_cast<size_t>(selector % eligibleCount);
    for (const auto &[name, cell] : cells_) {
      (void)name;
      for (const auto &elite : cell) {
        if (elite.recipe.effect.family != family)
          continue;
        if (selected-- == 0)
          return &elite;
      }
    }
    return nullptr;
  }

  bool referencesPreview(std::string_view path) const {
    for (const auto &[name, cell] : cells_) {
      (void)name;
      for (const auto &elite : cell) {
        if (elite.previewPath == path)
          return true;
      }
    }
    return false;
  }

private:
  static bool ranksBefore(const Elite &left, const Elite &right) {
    if (left.quality != right.quality)
      return left.quality > right.quality;
    return left.recipeHash < right.recipeHash;
  }

  std::map<std::string, std::vector<Elite>> cells_;
};

std::string metricsJson(const Metrics &metrics) {
  std::ostringstream output;
  output << std::fixed << std::setprecision(8)
         << "{\"mae\":" << metrics.mae
         << ",\"changed_ratio\":" << metrics.changedRatio
         << ",\"luma_correlation\":" << metrics.lumaCorrelation
         << ",\"structure\":" << metrics.structure
         << ",\"clipping_ratio\":" << metrics.clippingRatio
         << ",\"entropy\":" << metrics.entropy
         << ",\"temporal_residual_delta\":"
         << metrics.temporalResidualDelta << ",\"content_dependency\":"
         << metrics.contentDependency << ",\"output_stddev\":"
         << metrics.outputStandardDeviation
         << ",\"min_input_changed_ratio\":"
         << metrics.minimumInputChangedRatio << ",\"mean_process_ms\":"
         << metrics.meanProcessMilliseconds << '}';
  return output.str();
}

std::string realtimeGateJson(
    const glic::RealtimeCertificationResult &result) {
  std::ostringstream output;
  output << std::fixed << std::setprecision(8)
         << "{\"performed\":" << (result.performed ? "true" : "false")
         << ",\"process_passed\":"
         << (result.processPassed ? "true" : "false")
         << ",\"passed\":" << (result.passed ? "true" : "false")
         << ",\"backend\":\"metal\""
         << ",\"width\":" << result.width << ",\"height\":" << result.height
         << ",\"target_fps\":" << result.targetFps
         << ",\"frame_budget_ms\":" << result.frameBudgetMilliseconds
         << ",\"warmup_frames\":" << result.warmupFrames
         << ",\"measured_frames\":" << result.measuredFrames
         << ",\"completed_frames\":" << result.completedFrames
         << ",\"mean_wall_ms\":" << result.meanWallMilliseconds
         << ",\"median_wall_ms\":" << result.medianWallMilliseconds
         << ",\"p95_wall_ms\":" << result.p95WallMilliseconds
         << ",\"p99_wall_ms\":" << result.p99WallMilliseconds
         << ",\"max_wall_ms\":" << result.maxWallMilliseconds
         << ",\"mean_gpu_ms\":" << result.meanGpuMilliseconds
         << ",\"p95_gpu_ms\":" << result.p95GpuMilliseconds
         << ",\"error\":\"" << jsonEscape(result.error) << "\"}";
  return output.str();
}

std::string candidateJson(uint64_t candidateId, uint64_t hash,
                          std::string_view previewHash,
                          std::string_view evaluationHash,
                          std::string_view canonical, const Recipe &recipe,
                          const Metrics &metrics,
                          const glic::RealtimeCertificationResult &realtimeGate,
                          bool accepted,
                          bool admitted, std::string_view rejectReason,
                          std::string_view cell, double quality,
                          std::string_view generation,
                          std::string_view parentHash) {
  std::ostringstream output;
  output << std::fixed << std::setprecision(8)
         << "{\"schema\":\"glic-realtime-search-candidate-v2\""
         << ",\"timestamp\":\"" << utcTimestamp() << "\""
         << ",\"candidate_id\":" << candidateId
         << ",\"recipe_hash\":\"" << hexHash(hash) << "\""
         << ",\"preview_hash\":\"" << jsonEscape(previewHash) << "\""
         << ",\"evaluation_hash\":\"" << jsonEscape(evaluationHash)
         << "\""
         << ",\"generation\":\"" << jsonEscape(generation) << "\""
         << ",\"parent_hash\":\"" << jsonEscape(parentHash) << "\""
         << ",\"canonical\":\"" << jsonEscape(canonical) << "\""
         << ",\"accepted\":" << (accepted ? "true" : "false")
         << ",\"admitted\":" << (admitted ? "true" : "false")
         << ",\"reject_reason\":\"" << jsonEscape(rejectReason) << "\""
         << ",\"cell\":\"" << jsonEscape(cell) << "\""
         << ",\"quality\":" << quality << ",\"metrics\":"
         << metricsJson(metrics) << ",\"realtime_gate\":"
         << realtimeGateJson(realtimeGate) << ",\"recipe\":"
         << recipeJson(recipe)
         << '}';
  return output.str();
}

std::optional<std::string> extractString(std::string_view json,
                                         std::string_view key) {
  const std::string marker = "\"" + std::string(key) + "\"";
  size_t position = json.find(marker);
  if (position == std::string_view::npos)
    return std::nullopt;
  position += marker.size();
  while (position < json.size() &&
         std::isspace(static_cast<unsigned char>(json[position])))
    ++position;
  if (position >= json.size() || json[position] != ':')
    return std::nullopt;
  ++position;
  while (position < json.size() &&
         std::isspace(static_cast<unsigned char>(json[position])))
    ++position;
  if (position >= json.size() || json[position] != '"')
    return std::nullopt;
  ++position;
  std::string value;
  bool escaped = false;
  for (; position < json.size(); ++position) {
    const char character = json[position];
    if (escaped) {
      switch (character) {
      case 'n':
        value += '\n';
        break;
      case 'r':
        value += '\r';
        break;
      case 't':
        value += '\t';
        break;
      default:
        value += character;
        break;
      }
      escaped = false;
    } else if (character == '\\') {
      escaped = true;
    } else if (character == '"') {
      return value;
    } else {
      value += character;
    }
  }
  return std::nullopt;
}

std::optional<double> extractNumber(std::string_view json,
                                    std::string_view key) {
  const std::string marker = "\"" + std::string(key) + "\":";
  size_t position = json.find(marker);
  if (position == std::string_view::npos)
    return std::nullopt;
  position += marker.size();
  size_t end = position;
  while (end < json.size() &&
         (std::isdigit(static_cast<unsigned char>(json[end])) ||
          json[end] == '-' || json[end] == '+' || json[end] == '.' ||
          json[end] == 'e' || json[end] == 'E'))
    ++end;
  double value = 0.0;
  if (end == position || !parseDouble(json.substr(position, end - position), value))
    return std::nullopt;
  return value;
}

std::optional<bool> extractBoolean(std::string_view json,
                                   std::string_view key) {
  const std::string marker = "\"" + std::string(key) + "\":";
  const size_t position = json.find(marker);
  if (position == std::string_view::npos)
    return std::nullopt;
  const size_t valuePosition = position + marker.size();
  if (json.substr(valuePosition, 4) == "true")
    return true;
  if (json.substr(valuePosition, 5) == "false")
    return false;
  return std::nullopt;
}

bool parseMetrics(std::string_view json, Metrics &metrics) {
  auto take = [&](std::string_view key, double &destination) {
    const auto value = extractNumber(json, key);
    if (!value)
      return false;
    destination = *value;
    return true;
  };
  return take("mae", metrics.mae) &&
         take("changed_ratio", metrics.changedRatio) &&
         take("luma_correlation", metrics.lumaCorrelation) &&
         take("structure", metrics.structure) &&
         take("clipping_ratio", metrics.clippingRatio) &&
         take("entropy", metrics.entropy) &&
         take("temporal_residual_delta", metrics.temporalResidualDelta) &&
         take("content_dependency", metrics.contentDependency) &&
         take("output_stddev", metrics.outputStandardDeviation) &&
         take("min_input_changed_ratio", metrics.minimumInputChangedRatio) &&
         take("mean_process_ms", metrics.meanProcessMilliseconds);
}

bool parsePassingRealtimeGate(
    std::string_view json, glic::RealtimeCertificationResult &result) {
  const auto performed = extractBoolean(json, "performed");
  const auto processPassed = extractBoolean(json, "process_passed");
  const auto passed = extractBoolean(json, "passed");
  const auto backend = extractString(json, "backend");
  const auto error = extractString(json, "error");
  if (!performed || !processPassed || !passed || !backend || !error ||
      !*performed || !*processPassed || !*passed || *backend != "metal")
    return false;

  auto take = [&](std::string_view key, double &destination) {
    const auto value = extractNumber(json, key);
    if (!value || !std::isfinite(*value))
      return false;
    destination = *value;
    return true;
  };
  double width = 0.0;
  double height = 0.0;
  double warmupFrames = 0.0;
  double measuredFrames = 0.0;
  double completedFrames = 0.0;
  if (!take("width", width) || !take("height", height) ||
      !take("target_fps", result.targetFps) ||
      !take("frame_budget_ms", result.frameBudgetMilliseconds) ||
      !take("warmup_frames", warmupFrames) ||
      !take("measured_frames", measuredFrames) ||
      !take("completed_frames", completedFrames) ||
      !take("mean_wall_ms", result.meanWallMilliseconds) ||
      !take("median_wall_ms", result.medianWallMilliseconds) ||
      !take("p95_wall_ms", result.p95WallMilliseconds) ||
      !take("p99_wall_ms", result.p99WallMilliseconds) ||
      !take("max_wall_ms", result.maxWallMilliseconds) ||
      !take("mean_gpu_ms", result.meanGpuMilliseconds) ||
      !take("p95_gpu_ms", result.p95GpuMilliseconds))
    return false;

  if (width != glic::kRealtimeCertificationWidth ||
      height != glic::kRealtimeCertificationHeight ||
      warmupFrames != glic::kRealtimeCertificationWarmupFrames ||
      measuredFrames != glic::kRealtimeCertificationMeasuredFrames ||
      completedFrames != glic::kRealtimeCertificationMeasuredFrames ||
      std::abs(result.targetFps - glic::kRealtimeCertificationTargetFps) >
          1.0e-8 ||
      std::abs(result.frameBudgetMilliseconds -
               glic::kRealtimeCertificationFrameBudgetMilliseconds) >
          1.0e-6 ||
      result.meanWallMilliseconds < 0.0 ||
      result.p95WallMilliseconds < 0.0 ||
      result.meanWallMilliseconds >
          glic::kRealtimeCertificationFrameBudgetMilliseconds ||
      result.p95WallMilliseconds >
          glic::kRealtimeCertificationFrameBudgetMilliseconds)
    return false;

  result.performed = true;
  result.processPassed = true;
  result.passed = true;
  result.width = static_cast<int>(width);
  result.height = static_cast<int>(height);
  result.warmupFrames = static_cast<uint32_t>(warmupFrames);
  result.measuredFrames = static_cast<uint32_t>(measuredFrames);
  result.completedFrames = static_cast<uint32_t>(completedFrames);
  result.error = *error;
  return true;
}

struct ResumeState {
  uint64_t nextCandidateId = 0;
  SearchCounters counters;
  Archive archive;
  std::unordered_set<uint64_t> hashes;
  std::unordered_set<uint64_t> visualHashes;
};

bool replayCandidateLog(const fs::path &path, ResumeState &state,
                        std::string &error) {
  if (!fs::exists(path))
    return true;
  std::ifstream input(path);
  if (!input) {
    error = "failed to open candidate log for replay: " + path.string();
    return false;
  }
  std::string line;
  uint64_t lineNumber = 0;
  while (std::getline(input, line)) {
    ++lineNumber;
    const auto schema = extractString(line, "schema");
    const auto idNumber = extractNumber(line, "candidate_id");
    const auto hashText = extractString(line, "recipe_hash");
    const auto previewHashText = extractString(line, "preview_hash");
    const auto evaluationHashText = extractString(line, "evaluation_hash");
    const auto canonical = extractString(line, "canonical");
    const auto accepted = extractBoolean(line, "accepted");
    const auto admitted = extractBoolean(line, "admitted");
    const auto rejectReason = extractString(line, "reject_reason");
    if (!schema || *schema != "glic-realtime-search-candidate-v2" ||
        !idNumber || !hashText || !canonical || !accepted || !admitted ||
        !rejectReason) {
      error = "incompatible or malformed candidate record at line " +
              std::to_string(lineNumber);
      return false;
    }
    const uint64_t candidateId = static_cast<uint64_t>(*idNumber);
    uint64_t hash = 0;
    try {
      hash = std::stoull(*hashText, nullptr, 16);
    } catch (...) {
      std::cerr << "Ignoring invalid recipe hash at line " << lineNumber
                << '\n';
      continue;
    }
    state.nextCandidateId = std::max(state.nextCandidateId, candidateId + 1);
    ++state.counters.attempted;
    state.hashes.insert(hash);
    uint64_t previewHash = 0;
    if (previewHashText && !previewHashText->empty()) {
      try {
        previewHash = std::stoull(*previewHashText, nullptr, 16);
      } catch (...) {
        error = "invalid preview hash at line " + std::to_string(lineNumber);
        return false;
      }
    }
    uint64_t evaluationHash = 0;
    if (evaluationHashText && !evaluationHashText->empty()) {
      try {
        evaluationHash = std::stoull(*evaluationHashText, nullptr, 16);
        state.visualHashes.insert(evaluationHash);
      } catch (...) {
        error = "invalid evaluation hash at line " +
                std::to_string(lineNumber);
        return false;
      }
    }
    if (*rejectReason == "duplicate_recipe" ||
        *rejectReason == "duplicate_visual") {
      ++state.counters.duplicates;
      if (*rejectReason == "duplicate_recipe")
        continue;
    }
    if (*rejectReason == "duplicate_visual") {
      ++state.counters.evaluated;
      ++state.counters.rejected;
      continue;
    }
    ++state.counters.evaluated;
    if (!*accepted) {
      if (*admitted) {
        error = "rejected candidate is marked admitted at line " +
                std::to_string(lineNumber);
        return false;
      }
      ++state.counters.rejected;
      continue;
    }

    const auto cell = extractString(line, "cell");
    const auto quality = extractNumber(line, "quality");
    Recipe recipe;
    Metrics metrics;
    glic::RealtimeCertificationResult realtimeGate;
    if (!*admitted || !cell || !quality ||
        !decodeCanonical(*canonical, recipe) || !parseMetrics(line, metrics) ||
        !parsePassingRealtimeGate(line, realtimeGate)) {
      error = "accepted candidate lacks a passing 960x540 Metal gate at line " +
              std::to_string(lineNumber);
      return false;
    }
    ++state.counters.accepted;
    Elite elite;
    elite.candidateId = candidateId;
    elite.recipeHash = hash;
    elite.previewHash = previewHash;
    elite.evaluationHash = evaluationHash;
    elite.canonical = *canonical;
    elite.recipe = recipe;
    elite.metrics = metrics;
    elite.realtimeGate = realtimeGate;
    elite.cell = *cell;
    elite.quality = *quality;
    elite.previewPath = "elites/" + *hashText + ".png";
    const auto plan = state.archive.planAdmission(elite);
    const auto result = state.archive.commitAdmission(std::move(elite), plan);
    if (!result.admitted) {
      error = "accepted candidate cannot be transactionally replayed at line " +
              std::to_string(lineNumber);
      return false;
    }
  }
  return true;
}

std::string archiveJson(const Options &options, std::string_view backend,
                        int width, int height, const SearchCounters &counters,
                        const Archive &archive, bool running,
                        std::string_view stopReason) {
  std::ostringstream output;
  output << std::fixed << std::setprecision(8)
         << "{\n  \"schema\": \"glic-realtime-search-archive-v2\",\n"
         << "  \"updated_at\": \"" << utcTimestamp() << "\",\n"
         << "  \"running\": " << (running ? "true" : "false") << ",\n"
         << "  \"stop_reason\": \"" << jsonEscape(stopReason) << "\",\n"
         << "  \"seed\": " << options.seed << ",\n"
         << "  \"algorithm\": \"map-elites-family-stratified-v6\",\n"
         << "  \"recipe_schema\": \"glic-realtime-recipe-v2\",\n"
         << "  \"backend\": \"" << jsonEscape(backend) << "\",\n"
         << "  \"render_scale\": " << options.renderScale << ",\n"
         << "  \"width\": " << width << ",\n"
         << "  \"height\": " << height << ",\n"
         << "  \"realtime_gate\": {\"backend\":\"metal\",\"width\":"
         << glic::kRealtimeCertificationWidth << ",\"height\":"
         << glic::kRealtimeCertificationHeight << ",\"target_fps\":"
         << glic::kRealtimeCertificationTargetFps
         << ",\"frame_budget_ms\":"
         << glic::kRealtimeCertificationFrameBudgetMilliseconds
         << ",\"warmup_frames\":"
         << glic::kRealtimeCertificationWarmupFrames
         << ",\"measured_frames\":"
         << glic::kRealtimeCertificationMeasuredFrames << "},\n"
         << "  \"frame_phases\": [";
  for (size_t index = 0; index < kFramePhases.size(); ++index) {
    if (index != 0)
      output << ',';
    output << kFramePhases[index];
  }
  output << "],\n  \"evaluation_seeds\": [";
  for (size_t index = 0; index < kEvaluationSeeds.size(); ++index) {
    if (index != 0)
      output << ',';
    output << kEvaluationSeeds[index];
  }
  output << "],\n  \"inputs\": [";
  for (size_t index = 0; index < options.inputPaths.size(); ++index) {
    if (index != 0)
      output << ',';
    output << "\"" << jsonEscape(options.inputPaths[index].string()) << "\"";
  }
  output << "],\n  \"counters\": {\"attempted\":" << counters.attempted
         << ",\"evaluated\":" << counters.evaluated
         << ",\"accepted\":" << counters.accepted
         << ",\"rejected\":" << counters.rejected
         << ",\"duplicates\":" << counters.duplicates << "},\n"
         << "  \"cell_count\": " << archive.cellCount() << ",\n"
         << "  \"elite_count\": " << archive.eliteCount() << ",\n"
         << "  \"cells\": {\n";
  size_t cellIndex = 0;
  for (const auto &[cellName, elites] : archive.cells()) {
    output << "    \"" << jsonEscape(cellName) << "\": [\n";
    for (size_t index = 0; index < elites.size(); ++index) {
      const auto &elite = elites[index];
      output << "      {\"candidate_id\":" << elite.candidateId
             << ",\"recipe_hash\":\"" << hexHash(elite.recipeHash)
             << "\",\"preview_hash\":\"" << hexHash(elite.previewHash)
             << "\",\"evaluation_hash\":\""
             << hexHash(elite.evaluationHash)
             << "\",\"quality\":" << elite.quality
             << ",\"preview\":\"" << jsonEscape(elite.previewPath)
             << "\",\"metrics\":" << metricsJson(elite.metrics)
             << ",\"realtime_gate\":"
             << realtimeGateJson(elite.realtimeGate)
             << ",\"recipe\":" << recipeJson(elite.recipe) << '}';
      if (index + 1 != elites.size())
        output << ',';
      output << '\n';
    }
    output << "    ]";
    if (++cellIndex != archive.cells().size())
      output << ',';
    output << '\n';
  }
  output << "  }\n}\n";
  return output.str();
}

std::string statusJson(const Options &options, std::string_view backend,
                       int width, int height, const SearchCounters &counters,
                       const Archive &archive, uint64_t nextCandidateId,
                       double elapsedSeconds, bool running,
                       std::string_view reason) {
  std::ostringstream output;
  output << std::fixed << std::setprecision(3)
         << "{\n  \"schema\": \"glic-realtime-search-status-v2\",\n"
         << "  \"heartbeat_at\": \"" << utcTimestamp() << "\",\n"
         << "  \"running\": " << (running ? "true" : "false") << ",\n"
         << "  \"reason\": \"" << jsonEscape(reason) << "\",\n"
         << "  \"backend\": \"" << jsonEscape(backend) << "\",\n"
         << "  \"width\": " << width << ", \"height\": " << height
         << ",\n  \"required_realtime\": {\"backend\":\"metal\","
            "\"width\":"
         << glic::kRealtimeCertificationWidth << ",\"height\":"
         << glic::kRealtimeCertificationHeight << ",\"target_fps\":"
         << glic::kRealtimeCertificationTargetFps << "}"
         << ",\n  \"seed\": " << options.seed
         << ",\n  \"elapsed_seconds\": " << elapsedSeconds
         << ",\n  \"duration_seconds\": " << options.durationSeconds
         << ",\n  \"next_candidate_id\": " << nextCandidateId
         << ",\n  \"attempted\": " << counters.attempted
         << ", \"evaluated\": " << counters.evaluated
         << ", \"accepted\": " << counters.accepted
         << ", \"rejected\": " << counters.rejected
         << ", \"duplicates\": " << counters.duplicates
         << ",\n  \"archive_cells\": " << archive.cellCount()
         << ", \"archive_elites\": " << archive.eliteCount() << "\n}\n";
  return output.str();
}

bool checkpoint(const Options &options, std::string_view backend, int width,
                int height, const SearchCounters &counters,
                const Archive &archive, uint64_t nextCandidateId,
                double elapsedSeconds, bool running, std::string_view reason,
                std::string &error) {
  const std::string archiveContents =
      archiveJson(options, backend, width, height, counters, archive, running,
                  reason);
  if (!atomicWrite(options.outputDirectory / "archive.json", archiveContents,
                   true, error))
    return false;
  const std::string statusContents =
      statusJson(options, backend, width, height, counters, archive,
                 nextCandidateId, elapsedSeconds, running, reason);
  return atomicWrite(options.outputDirectory / "status.json", statusContents,
                     false, error);
}

bool safeRemovePreview(const Options &options, const Archive &archive,
                       std::string_view relativePath) {
  if (relativePath.empty() || archive.referencesPreview(relativePath))
    return true;
  const fs::path relative(relativePath);
  if (relative.is_absolute() || relative.parent_path() != "elites" ||
      relative.extension() != ".png" || relative.stem().string().size() != 16)
    return false;
  std::error_code error;
  fs::remove(options.outputDirectory / relative, error);
  return !error;
}

void cleanupOrphanPreviews(const Options &options, const Archive &archive) {
  const fs::path directory = options.outputDirectory / "elites";
  if (!fs::exists(directory))
    return;
  for (const auto &entry : fs::directory_iterator(directory)) {
    if (!entry.is_regular_file() || entry.path().extension() != ".png" ||
        entry.path().stem().string().size() != 16)
      continue;
    const std::string relative = "elites/" + entry.path().filename().string();
    if (!archive.referencesPreview(relative)) {
      std::error_code error;
      fs::remove(entry.path(), error);
    }
  }
}

} // namespace

int main(int argc, char **argv) {
  Options options;
  if (!parseOptions(argc, argv, options)) {
    printUsage(argv[0]);
    return 2;
  }

  std::error_code filesystemError;
  fs::create_directories(options.outputDirectory / "elites", filesystemError);
  if (filesystemError) {
    std::cerr << "Failed to create output directory: "
              << filesystemError.message() << '\n';
    return 3;
  }
  const fs::path candidateLog =
      options.outputDirectory / "candidates.ndjson";
  if (!options.resume && fs::exists(candidateLog) &&
      fs::file_size(candidateLog) != 0) {
    std::cerr << "Output already contains candidates.ndjson; use --resume or "
                 "choose a new output directory\n";
    return 3;
  }

  std::string error;
  if (!recoverPartialLastLine(candidateLog, error)) {
    std::cerr << error << '\n';
    return 3;
  }

  std::vector<InputImage> inputs;
  int width = 0;
  int height = 0;
  if (!loadInputs(options, inputs, width, height, error)) {
    std::cerr << error << '\n';
    return 3;
  }

  auto backend = glic::createRealtimeBackend(
      glic::realtimeBackendKindFromName(options.backend), error);
  if (!backend) {
    std::cerr << "Failed to create realtime backend: " << error << '\n';
    return 4;
  }
  const std::string backendName = backend->name();
  if (backendName != "metal" || !backend->isHardwareAccelerated()) {
    std::cerr << "Realtime search certification requires the resolved Metal "
                 "backend; got "
              << backendName << '\n';
    return 4;
  }
  auto certificationBackend = glic::createRealtimeBackend(
      glic::RealtimeBackendKind::METAL, error);
  if (!certificationBackend ||
      std::string_view(certificationBackend->name()) != "metal" ||
      !certificationBackend->isHardwareAccelerated()) {
    std::cerr << "Failed to create dedicated Metal certification backend: "
              << error << '\n';
    return 4;
  }
  const uint64_t fingerprint =
      configurationFingerprint(options, backendName, width, height, inputs);
  const fs::path runConfigPath = options.outputDirectory / "run-config.json";
  if (options.resume) {
    std::ifstream configInput(runConfigPath);
    const std::string configContents(
        (std::istreambuf_iterator<char>(configInput)),
        std::istreambuf_iterator<char>());
    const auto previousFingerprint =
        extractString(configContents, "fingerprint");
    if (!configInput || !previousFingerprint ||
        *previousFingerprint != hexHash(fingerprint)) {
      std::cerr << "Resume refused: run configuration or input content does "
                   "not match run-config.json (stored="
                << (previousFingerprint ? *previousFingerprint : "missing")
                << ", current=" << hexHash(fingerprint) << ")\n";
      return 3;
    }
  } else if (!atomicWrite(
                 runConfigPath,
                 runConfigurationJson(options, backendName, width, height,
                                      fingerprint),
                 false, error)) {
    std::cerr << "Failed to write run configuration: " << error << '\n';
    return 3;
  }

  ResumeState state;
  if (options.resume && !replayCandidateLog(candidateLog, state, error)) {
    std::cerr << "Resume failed: " << error << '\n';
    return 3;
  }
  cleanupOrphanPreviews(options, state.archive);

  const size_t pixelCount = static_cast<size_t>(width) * height;
  std::vector<glic::Color> output(pixelCount);
  std::vector<int16_t> previousResidual(pixelCount);
  std::vector<std::vector<glic::Color>> representatives(
      inputs.size(), std::vector<glic::Color>(pixelCount));
  MetricAccumulator accumulator(inputs.size());
  const std::vector<glic::Color> certificationInput = resizeImage(
      inputs.front().pixels, width, height,
      glic::kRealtimeCertificationWidth,
      glic::kRealtimeCertificationHeight);
  std::vector<glic::Color> certificationOutput(
      static_cast<size_t>(glic::kRealtimeCertificationWidth) *
      static_cast<size_t>(glic::kRealtimeCertificationHeight));

  std::signal(SIGTERM, handleSignal);
  std::signal(SIGINT, handleSignal);
  const Clock::time_point started = Clock::now();
  const Clock::time_point deadline =
      started + std::chrono::seconds(options.durationSeconds);
  Clock::time_point lastStatusPrint = started;
  std::string stopReason = "duration_reached";

  std::cout << "glic realtime search: backend=" << backendName
            << " resolution=" << width << 'x' << height
            << " required_realtime=" << glic::kRealtimeCertificationWidth
            << 'x' << glic::kRealtimeCertificationHeight << '@'
            << glic::kRealtimeCertificationTargetFps << "fps"
            << " inputs=" << inputs.size()
            << " resume_at=" << state.nextCandidateId
            << " duration_seconds=" << options.durationSeconds << '\n';

  if (!checkpoint(options, backendName, width, height, state.counters,
                  state.archive, state.nextCandidateId, 0.0, true, "running",
                  error)) {
    std::cerr << "Initial checkpoint failed: " << error << '\n';
    return 5;
  }

  while (true) {
    if (gStopRequested != 0) {
      stopReason = "signal";
      break;
    }
    if (Clock::now() >= deadline) {
      stopReason = "duration_reached";
      break;
    }
    if (options.maxCandidates != 0 &&
        state.nextCandidateId >= options.maxCandidates) {
      stopReason = "max_candidates_reached";
      break;
    }

    const uint64_t candidateId = state.nextCandidateId++;
    ++state.counters.attempted;
    // Search in nine-candidate family sweeps. One sweep in three is a global
    // restart, so every restart epoch visits all nine families exactly once;
    // the other two sweeps mutate archive elites.
    constexpr uint64_t familyCount =
        static_cast<uint64_t>(glic::RealtimeEffectFamily::COUNT);
    const auto desiredFamily = static_cast<glic::RealtimeEffectFamily>(
        candidateId % familyCount);
    const bool useMutation =
        candidateId >= 128 && ((candidateId / familyCount) % 3) != 0;
    const Elite *parent =
        useMutation
            ? state.archive.selectParentForFamily(
                  desiredFamily,
                  splitMix64(options.seed ^ candidateId ^
                             0x243f6a8885a308d3ULL))
            : nullptr;
    Recipe recipe =
        parent != nullptr
            ? mutateRecipe(options.seed, candidateId, parent->recipe,
                           desiredFamily)
            : generateRecipe(options.seed, candidateId);
    const std::string generation = parent != nullptr ? "mutation" : "random";
    const std::string parentHash =
        parent != nullptr ? hexHash(parent->recipeHash) : std::string{};
    const std::string canonical = canonicalRecipe(recipe);
    const uint64_t hash = recipeHash(canonical);
    std::string previewHashText;
    std::string evaluationHashText;
    Metrics metrics;
    glic::RealtimeCertificationResult realtimeGate;
    bool accepted = false;
    bool admitted = false;
    std::string rejectReason;
    std::string cell;
    double quality = 0.0;
    std::vector<std::string> removedPreviews;

    if (!state.hashes.insert(hash).second) {
      rejectReason = "duplicate_recipe";
      ++state.counters.duplicates;
    } else {
      accumulator.reset();
      bool processSucceeded = true;
      uint64_t evaluationHash = 1469598103934665603ULL;
      double totalProcessMilliseconds = 0.0;
      uint64_t processedFrames = 0;
      for (size_t inputIndex = 0;
           inputIndex < inputs.size() && processSucceeded; ++inputIndex) {
        for (size_t seedIndex = 0;
             seedIndex < kEvaluationSeeds.size() && processSucceeded;
             ++seedIndex) {
          const glic::RealtimePrepareOptions prepareOptions{
              .width = width,
              .height = height,
              .config = recipe.config,
              .seed = kEvaluationSeeds[seedIndex],
              .effectStrength = recipe.strength,
              .effect = recipe.effect};
          if (!backend->prepare(prepareOptions, error)) {
            rejectReason = "prepare_failed: " + error;
            processSucceeded = false;
            break;
          }
          bool hasPreviousResidual = false;
          for (const uint64_t framePhase : kFramePhases) {
            const Clock::time_point processStart = Clock::now();
            if (!backend->process(inputs[inputIndex].pixels, output, framePhase,
                                  error)) {
              rejectReason = "process_failed: " + error;
              processSucceeded = false;
              break;
            }
            totalProcessMilliseconds +=
                std::chrono::duration<double, std::milli>(Clock::now() -
                                                          processStart)
                    .count();
            ++processedFrames;
            appendHashValue(evaluationHash, inputIndex);
            appendHashValue(evaluationHash, seedIndex);
            appendHashValue(evaluationHash, framePhase);
            appendPixelsHash(evaluationHash, output);
            accumulator.addFrame(inputIndex, inputs[inputIndex].pixels, output,
                                 width, height, previousResidual,
                                 hasPreviousResidual);
            hasPreviousResidual = true;
          }
          if (processSucceeded && seedIndex == 0)
            std::copy(output.begin(), output.end(),
                      representatives[inputIndex].begin());
        }
      }

      ++state.counters.evaluated;
      if (processSucceeded) {
        metrics = accumulator.finalize(inputs, representatives,
                                       totalProcessMilliseconds,
                                       processedFrames);
        const uint64_t previewHash = hashPixels(representatives.front());
        previewHashText = hexHash(previewHash);
        evaluationHashText = hexHash(evaluationHash);
        if (!state.visualHashes.insert(evaluationHash).second) {
          rejectReason = "duplicate_visual";
          ++state.counters.duplicates;
        } else {
          const auto hardReject =
              hardRejectReason(metrics, recipe.effect.family);
          if (hardReject) {
            rejectReason = *hardReject;
          } else {
            cell = behaviorCell(metrics, recipe.effect.family);
            quality = qualityScore(metrics);
            Elite elite;
            elite.candidateId = candidateId;
            elite.recipeHash = hash;
            elite.previewHash = previewHash;
            elite.evaluationHash = evaluationHash;
            elite.canonical = canonical;
            elite.recipe = recipe;
            elite.metrics = metrics;
            elite.cell = cell;
            elite.quality = quality;
            elite.previewPath = "elites/" + hexHash(hash) + ".png";
            const auto admissionPlan = state.archive.planAdmission(elite);
            if (!admissionPlan.admitted) {
              rejectReason = "not_archive_competitive";
            } else {
              const glic::RealtimeCertificationRequest certificationRequest{
                  .config = recipe.config,
                  .seed = kEvaluationSeeds.front(),
                  .effectStrength = recipe.strength,
                  .effect = recipe.effect,
                  .frameIndexBase =
                      candidateId *
                      (static_cast<uint64_t>(
                           glic::kRealtimeCertificationWarmupFrames) +
                       static_cast<uint64_t>(
                           glic::kRealtimeCertificationMeasuredFrames) +
                       1u)};
              realtimeGate = glic::certifyRealtimePreset(
                  *certificationBackend, certificationInput,
                  certificationOutput, certificationRequest);
              if (!realtimeGate.passed) {
                rejectReason = realtimeGate.error.empty()
                                   ? "realtime_gate_failed"
                                   : realtimeGate.error;
              } else {
                elite.realtimeGate = realtimeGate;
                auto result = state.archive.commitAdmission(
                    std::move(elite), admissionPlan);
                if (!result.admitted) {
                  std::cerr << "Transactional archive admission changed "
                               "between planning and commit for candidate "
                            << candidateId << '\n';
                  return 5;
                }
                admitted = true;
                accepted = true;
                ++state.counters.accepted;
                removedPreviews = std::move(result.removedPreviewPaths);
                const fs::path preview = options.outputDirectory / "elites" /
                                         (hexHash(hash) + ".png");
                if (!glic::saveImage(preview.string(),
                                     representatives.front(), width, height)) {
                  std::cerr << "Failed to save admitted elite preview: "
                            << preview << '\n';
                  return 5;
                }
              }
            }
          }
        }
      }
      if (!accepted)
        ++state.counters.rejected;
    }

    const std::string record = candidateJson(
        candidateId, hash, previewHashText, evaluationHashText, canonical,
        recipe, metrics, realtimeGate, accepted, admitted, rejectReason, cell,
        quality, generation, parentHash);
    if (!appendDurableLine(candidateLog, record, error)) {
      std::cerr << "Candidate log checkpoint failed: " << error << '\n';
      return 5;
    }
    for (const auto &preview : removedPreviews) {
      if (!safeRemovePreview(options, state.archive, preview))
        std::cerr << "Refused to remove unsafe elite preview path: " << preview
                  << '\n';
    }

    const double elapsed =
        std::chrono::duration<double>(Clock::now() - started).count();
    if (!checkpoint(options, backendName, width, height, state.counters,
                    state.archive, state.nextCandidateId, elapsed, true,
                    "running", error)) {
      std::cerr << "Archive checkpoint failed: " << error << '\n';
      return 5;
    }

    if (std::chrono::duration<double>(Clock::now() - lastStatusPrint).count() >=
        options.statusIntervalSeconds) {
      std::cout << "heartbeat elapsed=" << std::fixed << std::setprecision(1)
                << elapsed << "s attempted=" << state.counters.attempted
                << " accepted=" << state.counters.accepted
                << " cells=" << state.archive.cellCount()
                << " elites=" << state.archive.eliteCount() << '\n';
      lastStatusPrint = Clock::now();
    }
  }

  const double elapsed =
      std::chrono::duration<double>(Clock::now() - started).count();
  if (!checkpoint(options, backendName, width, height, state.counters,
                  state.archive, state.nextCandidateId, elapsed, false,
                  stopReason, error)) {
    std::cerr << "Final checkpoint failed: " << error << '\n';
    return 5;
  }
  std::cout << "search stopped: reason=" << stopReason
            << " elapsed=" << std::fixed << std::setprecision(1) << elapsed
            << "s attempted=" << state.counters.attempted
            << " accepted=" << state.counters.accepted
            << " cells=" << state.archive.cellCount()
            << " elites=" << state.archive.eliteCount() << '\n';
  return 0;
}
