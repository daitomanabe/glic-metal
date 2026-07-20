#include "preset_loader.hpp"
#include "realtime.hpp"

#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <new>
#include <span>
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
#endif
  return 0;
}
