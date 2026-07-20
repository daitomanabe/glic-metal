#include "preset_loader.hpp"
#include "realtime.hpp"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <new>
#include <span>
#include <set>
#include <string>
#include <vector>

namespace {
std::atomic<bool> gCountAllocations{false};
std::atomic<uint64_t> gAllocationCount{0};
} // namespace

void *operator new(std::size_t size) {
  if (gCountAllocations.load(std::memory_order_relaxed)) {
    gAllocationCount.fetch_add(1, std::memory_order_relaxed);
  }
  if (void *pointer = std::malloc(size))
    return pointer;
  throw std::bad_alloc();
}

void operator delete(void *pointer) noexcept { std::free(pointer); }
void operator delete(void *pointer, std::size_t) noexcept {
  std::free(pointer);
}

namespace {

std::vector<glic::Color> makeFixture(int width, int height) {
  std::vector<glic::Color> pixels(static_cast<size_t>(width) *
                                  static_cast<size_t>(height));
  for (int y = 0; y < height; ++y) {
    for (int x = 0; x < width; ++x) {
      pixels[static_cast<size_t>(y) * static_cast<size_t>(width) +
             static_cast<size_t>(x)] =
          glic::makeColor(static_cast<uint8_t>((x * 7 + y * 3) & 0xff),
                          static_cast<uint8_t>((x * 2 + y * 11) & 0xff),
                          static_cast<uint8_t>((x * 13 + y * 5) & 0xff));
    }
  }
  return pixels;
}

bool isVisibilityPreset(const std::string &preset) {
  return preset == "bi0g4n1c" || preset == "d1ffu510n" || preset == "gif2";
}

double meanAbsoluteRgbDifference(const std::vector<glic::Color> &input,
                                 const std::vector<glic::Color> &output) {
  uint64_t difference = 0;
  for (size_t index = 0; index < input.size(); ++index) {
    difference += static_cast<uint64_t>(
        std::abs(static_cast<int>(glic::getR(input[index])) -
                 static_cast<int>(glic::getR(output[index]))));
    difference += static_cast<uint64_t>(
        std::abs(static_cast<int>(glic::getG(input[index])) -
                 static_cast<int>(glic::getG(output[index]))));
    difference += static_cast<uint64_t>(
        std::abs(static_cast<int>(glic::getB(input[index])) -
                 static_cast<int>(glic::getB(output[index]))));
  }
  return static_cast<double>(difference) /
         static_cast<double>(input.size() * 3);
}

double changedPixelRatio(const std::vector<glic::Color> &input,
                         const std::vector<glic::Color> &output,
                         int threshold) {
  size_t changed = 0;
  for (size_t index = 0; index < input.size(); ++index) {
    const int maximum =
        std::max({std::abs(static_cast<int>(glic::getR(input[index])) -
                           static_cast<int>(glic::getR(output[index]))),
                  std::abs(static_cast<int>(glic::getG(input[index])) -
                           static_cast<int>(glic::getG(output[index]))),
                  std::abs(static_cast<int>(glic::getB(input[index])) -
                           static_cast<int>(glic::getB(output[index])))});
    if (maximum >= threshold)
      ++changed;
  }
  return static_cast<double>(changed) / static_cast<double>(input.size());
}

uint64_t hashPixels(const std::vector<glic::Color> &pixels) {
  uint64_t hash = 1469598103934665603ULL;
  for (const glic::Color pixel : pixels) {
    hash ^= pixel;
    hash *= 1099511628211ULL;
  }
  return hash;
}

bool runEffectFamilies(glic::RealtimeBackend &backend,
                       const std::vector<glic::Color> &input, int width,
                       int height) {
  constexpr std::array families = {
      glic::RealtimeEffectFamily::LINE_TEAR,
      glic::RealtimeEffectFamily::CHANNEL_SHEAR,
      glic::RealtimeEffectFamily::ANALOG_SYNC,
      glic::RealtimeEffectFamily::MIRROR_FOLD,
      glic::RealtimeEffectFamily::EDGE_ECHO,
      glic::RealtimeEffectFamily::BITPLANE_DITHER,
      glic::RealtimeEffectFamily::WAVE_WARP,
      glic::RealtimeEffectFamily::POSTER_SOLAR,
  };

  glic::CodecConfig config;
  // Explicit effect families are RGB mechanisms even when a legacy preset
  // carries another colour space. Exercise that contract on each backend.
  config.colorSpace = glic::ColorSpace::HWB;
  std::vector<glic::Color> first(input.size());
  std::vector<glic::Color> second(input.size());
  std::set<uint64_t> outputHashes;
  std::string error;

  for (const auto family : families) {
    glic::RealtimePrepareOptions options{
        .width = width,
        .height = height,
        .config = config,
        .seed = 0x12345678u,
        .effectStrength = 1.0f,
        .effect = {.family = family,
                   .amount = 0.78f,
                   .scale = 0.43f,
                   .rate = 1.0f},
    };
    if (!backend.prepare(options, error) ||
        !backend.process(input, first, 0, error) ||
        !backend.process(input, second, 0, error)) {
      std::cerr << backend.name() << " family processing failed for "
                << static_cast<uint32_t>(family) << ": " << error << '\n';
      return false;
    }
    if (first != second) {
      std::cerr << backend.name() << " family output is not deterministic for "
                << static_cast<uint32_t>(family) << '\n';
      return false;
    }
    if (changedPixelRatio(input, first, 1) < 0.01) {
      std::cerr << backend.name() << " family is effectively unchanged for "
                << static_cast<uint32_t>(family) << '\n';
      return false;
    }
    if (!outputHashes.insert(hashPixels(first)).second) {
      std::cerr << backend.name() << " family topology hash is duplicated for "
                << static_cast<uint32_t>(family) << '\n';
      return false;
    }
    if (!backend.process(input, second, 17, error) || first == second) {
      std::cerr << backend.name() << " family has no rate-driven variation for "
                << static_cast<uint32_t>(family) << '\n';
      return false;
    }

    options.effectStrength = 0.0f;
    if (!backend.prepare(options, error) ||
        !backend.process(input, second, 17, error) || second != input) {
      std::cerr << backend.name()
                << " family strength-zero path is not exact passthrough for "
                << static_cast<uint32_t>(family) << '\n';
      return false;
    }
  }
  return true;
}

bool runEffectFamilyParity(const std::vector<glic::Color> &input, int width,
                           int height) {
  std::string error;
  auto cpu =
      glic::createRealtimeBackend(glic::RealtimeBackendKind::CPU, error);
  auto metal =
      glic::createRealtimeBackend(glic::RealtimeBackendKind::METAL, error);
  if (!cpu || !metal) {
    std::cout << "SKIP family parity: " << error << '\n';
    return true;
  }

  constexpr std::array families = {
      glic::RealtimeEffectFamily::LINE_TEAR,
      glic::RealtimeEffectFamily::CHANNEL_SHEAR,
      glic::RealtimeEffectFamily::ANALOG_SYNC,
      glic::RealtimeEffectFamily::MIRROR_FOLD,
      glic::RealtimeEffectFamily::EDGE_ECHO,
      glic::RealtimeEffectFamily::BITPLANE_DITHER,
      glic::RealtimeEffectFamily::WAVE_WARP,
      glic::RealtimeEffectFamily::POSTER_SOLAR,
  };
  glic::CodecConfig config;
  // A grayscale preset would expose any accidental colour-space conversion in
  // either backend; direct-RGB family outputs must still match byte-for-byte.
  config.colorSpace = glic::ColorSpace::GS;
  struct ParityControls {
    float amount;
    float scale;
    float rate;
    float strength;
  };
  constexpr std::array<ParityControls, 3> controls = {{
      {.amount = 0.78f, .scale = 0.43f, .rate = 1.0f, .strength = 1.0f},
      {.amount = 0.50f, .scale = 0.50f, .rate = 0.0f, .strength = 1.7f},
      {.amount = 1.0f, .scale = 1.0f, .rate = 1.0f, .strength = 2.0f},
  }};
  std::vector<glic::Color> cpuOutput(input.size());
  std::vector<glic::Color> metalOutput(input.size());
  for (const auto family : families) {
    for (size_t controlIndex = 0; controlIndex < controls.size();
         ++controlIndex) {
      const auto &control = controls[controlIndex];
      glic::RealtimePrepareOptions options{
          .width = width,
          .height = height,
          .config = config,
          .seed = 0x12345678u,
          .effectStrength = control.strength,
          .effect = {.family = family,
                     .amount = control.amount,
                     .scale = control.scale,
                     .rate = control.rate},
      };
      if (!cpu->prepare(options, error) || !metal->prepare(options, error)) {
        std::cerr << "Family parity prepare failed for "
                  << static_cast<uint32_t>(family) << " controls "
                  << controlIndex << ": " << error << '\n';
        return false;
      }
      for (const uint64_t frame : {0u, 17u, 63u}) {
        if (!cpu->process(input, cpuOutput, frame, error) ||
            !metal->process(input, metalOutput, frame, error)) {
          std::cerr << "Family parity process failed for "
                    << static_cast<uint32_t>(family) << " controls "
                    << controlIndex << ": " << error << '\n';
          return false;
        }
        for (size_t index = 0; index < input.size(); ++index) {
          const int maximumDifference = std::max(
              {std::abs(static_cast<int>(glic::getR(cpuOutput[index])) -
                        static_cast<int>(glic::getR(metalOutput[index]))),
               std::abs(static_cast<int>(glic::getG(cpuOutput[index])) -
                        static_cast<int>(glic::getG(metalOutput[index]))),
               std::abs(static_cast<int>(glic::getB(cpuOutput[index])) -
                        static_cast<int>(glic::getB(metalOutput[index])))});
          if (maximumDifference > 1 ||
              glic::getA(cpuOutput[index]) != glic::getA(metalOutput[index])) {
            std::cerr << "CPU/Metal family parity exceeded one byte for family "
                      << static_cast<uint32_t>(family) << " controls "
                      << controlIndex << " frame " << frame << " pixel "
                      << index << " difference " << maximumDifference << '\n';
            return false;
          }
        }
      }
    }
  }
  std::cout << "PASS CPU/Metal effect family parity\n";
  return true;
}

bool runBackend(glic::RealtimeBackendKind kind,
                const std::vector<std::string> &presets,
                const std::vector<glic::Color> &input, int width, int height,
                bool required) {
  std::string error;
  auto backend = glic::createRealtimeBackend(kind, error);
  if (!backend) {
    if (required)
      std::cerr << "Backend creation failed: " << error << '\n';
    else
      std::cout << "SKIP backend: " << error << '\n';
    return !required;
  }

  std::vector<glic::Color> first(input.size());
  std::vector<glic::Color> second(input.size());
  for (size_t presetIndex = 0; presetIndex < presets.size(); ++presetIndex) {
    const auto &preset = presets[presetIndex];
    glic::CodecConfig config;
    if (!glic::PresetLoader::loadPresetByName(GLIC_TEST_PRESETS_DIR, preset,
                                              config)) {
      std::cerr << "Failed to load preset: " << preset << '\n';
      return false;
    }
    glic::RealtimePrepareOptions options{
        .width = width, .height = height, .config = config, .seed = 12345};
    if (!backend->prepare(options, error)) {
      std::cerr << backend->name() << " prepare failed for " << preset << ": "
                << error << '\n';
      return false;
    }
    if (!backend->process(input, first, 7, error) ||
        !backend->process(input, second, 7, error)) {
      std::cerr << backend->name() << " process failed for " << preset << ": "
                << error << '\n';
      return false;
    }
    if (first != second) {
      std::cerr << backend->name() << " output is not deterministic for "
                << preset << '\n';
      return false;
    }
    for (const auto pixel : first) {
      if (glic::getA(pixel) != 255) {
        std::cerr << backend->name() << " produced invalid alpha for " << preset
                  << '\n';
        return false;
      }
    }

    if (isVisibilityPreset(preset)) {
      const double meanDifference = meanAbsoluteRgbDifference(input, first);
      const double changedRatio = changedPixelRatio(input, first, 10);
      if (meanDifference < 6.0 || changedRatio < 0.20) {
        std::cerr << backend->name() << " effect is not visible for " << preset
                  << ": mean_rgb_difference=" << meanDifference
                  << " changed_ratio=" << changedRatio << '\n';
        return false;
      }
      if (!backend->process(input, second, 31, error) || first == second) {
        std::cerr << backend->name()
                  << " effect has no held temporal variation for " << preset
                  << '\n';
        return false;
      }
    }

    if (kind == glic::RealtimeBackendKind::CPU && presetIndex == 0) {
      gAllocationCount.store(0, std::memory_order_relaxed);
      gCountAllocations.store(true, std::memory_order_release);
      const bool allocationFreePass = backend->process(input, second, 7, error);
      gCountAllocations.store(false, std::memory_order_release);
      if (!allocationFreePass ||
          gAllocationCount.load(std::memory_order_relaxed) != 0) {
        std::cerr << "CPU hot path allocated " << gAllocationCount.load()
                  << " object(s)\n";
        return false;
      }
    }
  }

  // Search candidates are canonicalized, but the public API must still treat
  // an inverted block-size range consistently across CPU and Metal.
  glic::CodecConfig invertedConfig;
  for (auto &channel : invertedConfig.channels) {
    channel.minBlockSize = 128;
    channel.maxBlockSize = 4;
  }
  glic::RealtimePrepareOptions invertedOptions{
      .width = width,
      .height = height,
      .config = invertedConfig,
      .seed = 12345,
  };
  if (!backend->prepare(invertedOptions, error) ||
      !backend->process(input, first, 13, error)) {
    std::cerr << backend->name()
              << " inverted block range test failed: " << error << '\n';
    return false;
  }
  for (auto &channel : invertedOptions.config.channels)
    std::swap(channel.minBlockSize, channel.maxBlockSize);
  if (!backend->prepare(invertedOptions, error) ||
      !backend->process(input, second, 13, error) || first != second) {
    std::cerr << backend->name()
              << " block range normalization is inconsistent\n";
    return false;
  }

  glic::CodecConfig offConfig;
  if (!glic::PresetLoader::loadPresetByName(GLIC_TEST_PRESETS_DIR,
                                            presets.front(), offConfig)) {
    std::cerr << "Failed to load strength-zero test preset\n";
    return false;
  }
  glic::RealtimePrepareOptions offOptions{.width = width,
                                          .height = height,
                                          .config = offConfig,
                                          .seed = 12345,
                                          .effectStrength = 0.0f};
  if (!backend->prepare(offOptions, error) ||
      !backend->process(input, first, 7, error) || first != input) {
    std::cerr << backend->name() << " strength zero is not exact passthrough\n";
    return false;
  }
  if (!runEffectFamilies(*backend, input, width, height))
    return false;
  std::cout << "PASS backend=" << backend->name()
            << " presets=" << presets.size() << '\n';
  return true;
}

} // namespace

int main() {
  constexpr int width = 64;
  constexpr int height = 48;
  const auto input = makeFixture(width, height);
  const auto presets = glic::PresetLoader::listPresets(GLIC_TEST_PRESETS_DIR);
  if (presets.size() != 144) {
    std::cerr << "Expected 144 presets, got " << presets.size() << '\n';
    return 1;
  }
  if (!runBackend(glic::RealtimeBackendKind::CPU, presets, input, width, height,
                  true))
    return 1;
#if defined(__APPLE__)
  if (!runBackend(glic::RealtimeBackendKind::METAL, presets, input, width,
                  height, false))
    return 1;
  constexpr int parityWidth = 65;
  constexpr int parityHeight = 49;
  const auto parityInput = makeFixture(parityWidth, parityHeight);
  if (!runEffectFamilyParity(parityInput, parityWidth, parityHeight))
    return 1;
#endif
  return 0;
}
