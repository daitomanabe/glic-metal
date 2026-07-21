#pragma once

#include <cstdint>

namespace glic {

inline constexpr std::uint64_t kSegmentationTraceFnvOffset =
    14695981039346656037ULL;
inline constexpr std::uint64_t kSegmentationTraceFnvPrime = 1099511628211ULL;

inline void appendSegmentationTraceWord(std::uint64_t &hash,
                                        std::uint32_t value) noexcept {
  for (unsigned shift = 0; shift < 32; shift += 8) {
    hash ^= (value >> shift) & 0xffu;
    hash *= kSegmentationTraceFnvPrime;
  }
}

inline void appendSegmentationTraceLeaf(std::uint64_t &hash, int channel,
                                        int x, int y, int size) noexcept {
  appendSegmentationTraceWord(hash, static_cast<std::uint32_t>(channel));
  appendSegmentationTraceWord(hash, static_cast<std::uint32_t>(x));
  appendSegmentationTraceWord(hash, static_cast<std::uint32_t>(y));
  appendSegmentationTraceWord(hash, static_cast<std::uint32_t>(size));
}

} // namespace glic
