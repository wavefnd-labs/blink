// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#ifndef BLINK_STANDALONE_COMMON_IME_VALIDATION_H_
#define BLINK_STANDALONE_COMMON_IME_VALIDATION_H_

#include <cstddef>
#include <cstdint>
#include <span>
#include <string_view>
#include <vector>

#include "standalone/include/blink_ime.h"

namespace blink_standalone {
inline constexpr size_t kMaxImeTextBytes = 1024u * 1024u;
inline constexpr size_t kMaxImeSpans = 1024;

// The C API borrows text_size readable bytes from the host for this call.
// Call only after checking the byte limit and pointer/count consistency.
inline std::string_view ImeTextView(const be_ime_composition& value) {
#if defined(__clang__)
#pragma clang unsafe_buffer_usage begin
#endif
  const std::string_view text(value.text_utf8 ? value.text_utf8 : "",
                              value.text_size);
#if defined(__clang__)
#pragma clang unsafe_buffer_usage end
#endif
  return text;
}

// Call only after checking the ABI's span count limit and non-null pointer for
// a nonzero count. The host must keep that many be_ime_span elements readable
// for the duration of the C API call; their allocation extent cannot be
// recovered from the raw ABI pointer. Confine that boundary assumption to the
// span constructor, then use bounded iteration in validation and dispatch.
inline std::span<const be_ime_span> ImeSpanView(
    const be_ime_composition& value) {
#if defined(__clang__)
#pragma clang unsafe_buffer_usage begin
#endif
  const std::span<const be_ime_span> spans(value.spans, value.span_count);
#if defined(__clang__)
#pragma clang unsafe_buffer_usage end
#endif
  return spans;
}

// A byte per UTF-16 offset permits one-pass validation of all span boundaries
// without quadratic rescanning. The middle of a surrogate pair stays zero.
inline bool DecodeImeUtf8(std::string_view text,
                          std::vector<uint8_t>& boundaries) {
  boundaries.clear();
  if (text.size() > kMaxImeTextBytes)
    return false;
  boundaries.reserve(text.size() + 1);
  boundaries.push_back(1);
  for (size_t i = 0; i < text.size();) {
    const auto lead = static_cast<uint8_t>(text[i++]);
    uint32_t codepoint = lead;
    uint32_t minimum = 0;
    size_t remaining = 0;
    if (lead <= 0x7f) {
      // Single-byte scalar, including embedded U+0000.
    } else if (lead >= 0xc2 && lead <= 0xdf) {
      codepoint &= 0x1f;
      minimum = 0x80;
      remaining = 1;
    } else if (lead >= 0xe0 && lead <= 0xef) {
      codepoint &= 0x0f;
      minimum = 0x800;
      remaining = 2;
    } else if (lead >= 0xf0 && lead <= 0xf4) {
      codepoint &= 0x07;
      minimum = 0x10000;
      remaining = 3;
    } else {
      return false;
    }
    if (remaining > text.size() - i)
      return false;
    for (size_t j = 0; j < remaining; ++j) {
      const auto next = static_cast<uint8_t>(text[i++]);
      if ((next & 0xc0) != 0x80)
        return false;
      codepoint = (codepoint << 6) | (next & 0x3f);
    }
    if (codepoint < minimum || codepoint > 0x10ffff ||
        (codepoint >= 0xd800 && codepoint <= 0xdfff))
      return false;
    if (codepoint > 0xffff)
      boundaries.push_back(0);
    boundaries.push_back(1);
  }
  return true;
}

inline be_status ValidateImeComposition(const be_ime_composition& value) {
  if (value.struct_size < sizeof(value))
    return BE_INVALID_ARGUMENT;
  if (value.abi_version != BE_ABI_VERSION)
    return BE_ABI_MISMATCH;
  if ((!value.text_utf8 && value.text_size) ||
      value.text_size > kMaxImeTextBytes || value.span_count > kMaxImeSpans ||
      (!value.spans && value.span_count))
    return BE_INVALID_ARGUMENT;
  std::vector<uint8_t> boundaries;
  if (!DecodeImeUtf8(ImeTextView(value), boundaries))
    return BE_INVALID_ARGUMENT;
  const auto valid = [&boundaries](uint32_t offset) {
    return offset < boundaries.size() && boundaries[offset];
  };
  if (value.selection_start_utf16 > value.selection_end_utf16 ||
      !valid(value.selection_start_utf16) || !valid(value.selection_end_utf16))
    return BE_INVALID_ARGUMENT;
  uint32_t previous_end = 0;
  for (const auto& span : ImeSpanView(value)) {
    if (span.start_utf16 >= span.end_utf16 || span.start_utf16 < previous_end ||
        !valid(span.start_utf16) || !valid(span.end_utf16) ||
        span.thickness > BE_IME_THICKNESS_THICK ||
        span.underline_style > BE_IME_UNDERLINE_SQUIGGLE)
      return BE_INVALID_ARGUMENT;
    previous_end = span.end_utf16;
  }
  return BE_OK;
}

inline be_status ValidateImeCommit(std::string_view text, int32_t relative) {
  std::vector<uint8_t> boundaries;
  if (!DecodeImeUtf8(text, boundaries) || relative > 0)
    return BE_INVALID_ARGUMENT;
  const int64_t caret = static_cast<int64_t>(boundaries.size() - 1) + relative;
  return caret >= 0 && boundaries[static_cast<size_t>(caret)]
             ? BE_OK
             : BE_INVALID_ARGUMENT;
}
}  // namespace blink_standalone
#endif
