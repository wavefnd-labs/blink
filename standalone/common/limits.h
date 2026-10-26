// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#ifndef BLINK_STANDALONE_COMMON_LIMITS_H_
#define BLINK_STANDALONE_COMMON_LIMITS_H_
#include <cstddef>
#include <cstdint>
#include <limits>
namespace blink_standalone {
constexpr uint32_t kMaxDimension = 16384;
constexpr size_t kMaxResourceBytes = 64u * 1024u * 1024u;
inline bool ValidSize(uint32_t width, uint32_t height) {
  return width != 0 && height != 0 && width <= kMaxDimension &&
         height <= kMaxDimension;
}
inline bool ValidPixels(uint32_t width,
                        uint32_t height,
                        size_t bytes,
                        size_t stride) {
  return ValidSize(width, height) && stride >= size_t(width) * 4 &&
         stride <= std::numeric_limits<size_t>::max() / height &&
         bytes >= stride * height;
}
}  // namespace blink_standalone
#endif
