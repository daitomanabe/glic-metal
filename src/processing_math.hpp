#pragma once

#include <cmath>
#include <cstdint>
#include <limits>

namespace glic {

// Processing's round(float) delegates to Java Math.round: floor(x + 0.5).
// std::round is not equivalent for negative half-integers because it rounds
// those away from zero. Keep this helper at every upstream GLIC round site.
inline int processingRound(float value) noexcept {
  if (std::isnan(value))
    return 0;
  constexpr int minimum = std::numeric_limits<int>::min();
  constexpr int maximum = std::numeric_limits<int>::max();
  if (value <= static_cast<float>(minimum))
    return minimum;
  if (value >= static_cast<float>(maximum))
    return maximum;
  return static_cast<int>(std::floor(value + 0.5f));
}

// Planes.toPixels in upstream GLIC packs signed Java ints directly with
// shifts/OR before fromColorspace(). Values are deliberately not clamped or
// masked first: overflow from one plane can bleed into an adjacent byte and is
// part of the glitch aesthetic. Unsigned arithmetic reproduces Java's 32-bit
// bit pattern without invoking C++ signed-shift undefined behavior.
inline std::uint32_t processingPackPlanes(int channel0, int channel1,
                                          int channel2,
                                          std::uint32_t alphaBits) noexcept {
  return static_cast<std::uint32_t>(channel2) |
         (static_cast<std::uint32_t>(channel1) << 8u) |
         (static_cast<std::uint32_t>(channel0) << 16u) |
         (alphaBits & 0xff000000u);
}

} // namespace glic
