#pragma once

#include <bit>
#include <cstdint>

namespace glic {

// java.util.Random as used by Processing's random(float). GLIC consumes one
// evolving Processing RNG stream while building channel 0, 1, then 2. Keeping
// that stream on the CPU preserves the original DFS sampling order while the
// much heavier reconstruction remains channel-parallel on Metal.
class ProcessingRandom {
public:
  explicit ProcessingRandom(std::int64_t seed = 0) noexcept { setSeed(seed); }

  void setSeed(std::int64_t seed) noexcept {
    state_ = (static_cast<std::uint64_t>(seed) ^ kMultiplier) & kMask;
  }

  float nextFloat() noexcept {
    return static_cast<float>(nextBits(24)) * (1.0f / 16777216.0f);
  }

  int nextPosition(int size) noexcept {
    if (size <= 0)
      return 0;
    const auto unsignedSize = static_cast<std::uint32_t>(size);
    if (unsignedSize <= (1u << 24u) &&
        (unsignedSize & (unsignedSize - 1u)) == 0) {
      const unsigned magnitude = std::countr_zero(unsignedSize);
      return static_cast<int>(nextBits(24) >> (24u - magnitude));
    }
    const float high = static_cast<float>(size);
    float value = 0.0f;
    do {
      value = nextFloat() * high;
    } while (value == high);
    return static_cast<int>(value);
  }

  // Advance by calls to nextFloat() without materializing unused samples.
  // This is used where GLIC computes a standard deviation even though min/max
  // block bounds force the quadtree decision. Affine exponentiation preserves
  // the exact 48-bit state in O(log count).
  void discardNextFloats(std::uint64_t count) noexcept {
    std::uint64_t accumulatedMultiplier = 1;
    std::uint64_t accumulatedAddend = 0;
    std::uint64_t currentMultiplier = kMultiplier;
    std::uint64_t currentAddend = kAddend;
    while (count != 0) {
      if ((count & 1u) != 0) {
        accumulatedMultiplier =
            (accumulatedMultiplier * currentMultiplier) & kMask;
        accumulatedAddend =
            (accumulatedAddend * currentMultiplier + currentAddend) & kMask;
      }
      currentAddend =
          ((currentMultiplier + 1u) * currentAddend) & kMask;
      currentMultiplier = (currentMultiplier * currentMultiplier) & kMask;
      count >>= 1u;
    }
    state_ = (state_ * accumulatedMultiplier + accumulatedAddend) & kMask;
  }

  [[nodiscard]] std::uint64_t state() const noexcept { return state_; }

private:
  std::uint32_t nextBits(int bits) noexcept {
    state_ = (state_ * kMultiplier + kAddend) & kMask;
    return static_cast<std::uint32_t>(state_ >> (48 - bits));
  }

  static constexpr std::uint64_t kMultiplier = 0x5deece66dULL;
  static constexpr std::uint64_t kAddend = 0xbULL;
  static constexpr std::uint64_t kMask = (1ULL << 48u) - 1u;
  std::uint64_t state_ = 0;
};

} // namespace glic
