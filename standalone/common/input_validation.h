// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#ifndef BLINK_STANDALONE_COMMON_INPUT_VALIDATION_H_
#define BLINK_STANDALONE_COMMON_INPUT_VALIDATION_H_

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <span>

#include "standalone/include/blink_engine.h"

namespace blink_standalone {

// Keep coordinates representable by both Blink LayoutUnit and native floats.
// Negative positions are useful while dragging or leaving the viewport.
constexpr double kMaxInputCoordinate = 1048576.0;

inline bool ValidInputCoordinate(double value) {
  return std::isfinite(value) && std::fabs(value) <= kMaxInputCoordinate;
}

// Strict Unicode scalar decoding: reject overlong encodings, surrogates,
// isolated continuations, truncated sequences and values beyond U+10FFFF.
// The returned length excludes the mandatory in-bounds NUL terminator.
template <size_t N>
inline bool BoundedInputUtf8(const char (&text)[N], size_t* length = nullptr) {
  const std::span<const char, N> bytes(text);
  size_t size = 0;
  while (size != bytes.size() && bytes[size])
    ++size;
  if (size == bytes.size())
    return false;
  for (size_t i = 0; i < size;) {
    const auto lead = static_cast<uint8_t>(bytes[i++]);
    uint32_t codepoint = lead;
    uint32_t minimum = 0;
    size_t remaining = 0;
    if (lead <= 0x7f) {
      continue;
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
    if (remaining > size - i)
      return false;
    for (size_t j = 0; j < remaining; ++j) {
      const auto next = static_cast<uint8_t>(bytes[i++]);
      if ((next & 0xc0) != 0x80)
        return false;
      codepoint = (codepoint << 6) | (next & 0x3f);
    }
    if (codepoint < minimum || codepoint > 0x10ffff ||
        (codepoint >= 0xd800 && codepoint <= 0xdfff))
      return false;
  }
  if (length)
    *length = size;
  return true;
}

inline uint32_t InputButtonMask(uint32_t button) {
  switch (button) {
    case BE_BUTTON_LEFT:
      return BE_BUTTONS_LEFT;
    case BE_BUTTON_MIDDLE:
      return BE_BUTTONS_MIDDLE;
    case BE_BUTTON_RIGHT:
      return BE_BUTTONS_RIGHT;
    case BE_BUTTON_BACK:
      return BE_BUTTONS_BACK;
    case BE_BUTTON_FORWARD:
      return BE_BUTTONS_FORWARD;
    default:
      return 0;
  }
}

inline be_status ValidateInputEvent(const be_input_event& event) {
  if (event.struct_size < sizeof(be_input_event))
    return BE_INVALID_ARGUMENT;
  if (event.abi_version != BE_ABI_VERSION)
    return BE_ABI_MISMATCH;
  constexpr uint32_t modifiers =
      BE_MOD_SHIFT | BE_MOD_CTRL | BE_MOD_ALT | BE_MOD_META | BE_MOD_CAPS_LOCK |
      BE_MOD_NUM_LOCK | BE_MOD_AUTO_REPEAT | BE_MOD_KEY_PAD;
  if (event.type > BE_INPUT_TEXT || (event.modifiers & ~modifiers))
    return BE_INVALID_ARGUMENT;
  if (event.type <= BE_INPUT_WHEEL) {
    constexpr uint32_t buttons = BE_BUTTONS_LEFT | BE_BUTTONS_MIDDLE |
                                 BE_BUTTONS_RIGHT | BE_BUTTONS_BACK |
                                 BE_BUTTONS_FORWARD;
    if (!ValidInputCoordinate(event.x) || !ValidInputCoordinate(event.y) ||
        event.button > BE_BUTTON_FORWARD || (event.buttons & ~buttons) ||
        (event.modifiers & (BE_MOD_AUTO_REPEAT | BE_MOD_KEY_PAD)))
      return BE_INVALID_ARGUMENT;
    if (event.type == BE_INPUT_MOUSE_DOWN || event.type == BE_INPUT_MOUSE_UP) {
      const auto mask = InputButtonMask(event.button);
      if (!mask || event.click_count < 1 || event.click_count > 3 ||
          ((event.buttons & mask) != 0) != (event.type == BE_INPUT_MOUSE_DOWN))
        return BE_INVALID_ARGUMENT;
    } else if (event.button != BE_BUTTON_NONE || event.click_count != 0) {
      return BE_INVALID_ARGUMENT;
    }
    if (event.type == BE_INPUT_WHEEL && (!ValidInputCoordinate(event.delta_x) ||
                                         !ValidInputCoordinate(event.delta_y)))
      return BE_INVALID_ARGUMENT;
    return BE_OK;
  }
  if (event.type == BE_INPUT_KEY_DOWN || event.type == BE_INPUT_KEY_UP) {
    size_t code_size = 0;
    size_t key_size = 0;
    if (event.windows_key_code > 255 ||
        event.native_key_code > uint32_t(std::numeric_limits<int>::max()) ||
        !BoundedInputUtf8(event.dom_code, &code_size) ||
        !BoundedInputUtf8(event.dom_key, &key_size) || !key_size ||
        event.text_utf8[0])
      return BE_INVALID_ARGUMENT;
    for (const char c : std::span(event.dom_code).first(code_size)) {
      if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
            (c >= '0' && c <= '9')))
        return BE_INVALID_ARGUMENT;
    }
    return BE_OK;
  }
  size_t text_size = 0;
  return BoundedInputUtf8(event.text_utf8, &text_size) && text_size
             ? BE_OK
             : BE_INVALID_ARGUMENT;
}

}  // namespace blink_standalone
#endif
