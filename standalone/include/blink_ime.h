/* Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE. */
#ifndef BLINK_STANDALONE_INCLUDE_BLINK_IME_H_
#define BLINK_STANDALONE_INCLUDE_BLINK_IME_H_

#include "blink_engine.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Additive ABI 1 interface. All offsets count UTF-16 code units, never UTF-8
 * bytes. Composition offsets are relative to its text. Operations require
 * the focused editable frame; readonly/disabled controls cannot be edited.
 * OS input-method/candidate windows belong to the host application. */
enum {
  BE_IME_THICKNESS_NONE = 0,
  BE_IME_THICKNESS_THIN = 1,
  BE_IME_THICKNESS_THICK = 2
};
enum {
  BE_IME_UNDERLINE_NONE = 0,
  BE_IME_UNDERLINE_SOLID = 1,
  BE_IME_UNDERLINE_DOT = 2,
  BE_IME_UNDERLINE_DASH = 3,
  BE_IME_UNDERLINE_SQUIGGLE = 4
};
typedef struct be_ime_span {
  uint32_t start_utf16;
  uint32_t end_utf16;
  uint32_t thickness;
  uint32_t underline_style;
} be_ime_span;
typedef struct be_ime_composition {
  uint32_t struct_size;
  uint32_t abi_version;
  const char* text_utf8;
  size_t text_size;
  /* Ordered, nonoverlapping nonempty segments, limited to 1024. Offsets and
   * selection must be Unicode scalar boundaries, not split surrogate pairs.
   * Empty text cancels the existing composition. No pointer is retained. */
  const be_ime_span* spans;
  size_t span_count;
  uint32_t selection_start_utf16;
  uint32_t selection_end_utf16;
} be_ime_composition;
typedef enum be_ime_input_type {
  BE_IME_TYPE_NONE = 0,
  BE_IME_TYPE_TEXT = 1,
  BE_IME_TYPE_PASSWORD = 2,
  BE_IME_TYPE_SEARCH = 3,
  BE_IME_TYPE_EMAIL = 4,
  BE_IME_TYPE_NUMBER = 5,
  BE_IME_TYPE_TELEPHONE = 6,
  BE_IME_TYPE_URL = 7,
  BE_IME_TYPE_TEXTAREA = 8,
  BE_IME_TYPE_CONTENTEDITABLE = 9
} be_ime_input_type;
typedef struct be_ime_rect {
  int32_t x, y, width, height;
} be_ime_rect;
typedef struct be_ime_state {
  uint32_t struct_size;
  uint32_t abi_version;
  uint32_t input_type;
  uint32_t text_utf16_length;
  uint32_t selection_start_utf16;
  uint32_t selection_end_utf16;
  int32_t composition_start_utf16; /* -1 when there is no composition. */
  int32_t composition_end_utf16;
  /* Root viewport CSS pixels; no host screen/window offset is included. */
  be_ime_rect caret_bounds;
  be_ime_rect control_bounds;
} be_ime_state;

BE_EXPORT be_status
be_page_ime_set_composition(be_page* page,
                            const be_ime_composition* composition);
/* Replaces the active composition, or inserts at the current selection.
 * relative_caret_utf16 is relative to the end of inserted text (0=end).
 * It must be <= 0, stay within inserted text, and land at a valid Unicode
 * scalar boundary. */
BE_EXPORT be_status be_page_ime_commit_text(be_page* page,
                                            const char* text_utf8,
                                            size_t text_size,
                                            int32_t relative_caret_utf16);
BE_EXPORT be_status be_page_ime_finish_composition(be_page* page,
                                                   uint32_t keep_selection);
BE_EXPORT be_status be_page_ime_cancel_composition(be_page* page);
/* Copies surrounding editable text as valid UTF-8 including a final NUL.
 * required includes that NUL; size-query with NULL/0 returns
 * BE_BUFFER_TOO_SMALL. State is populated even when the text buffer is too
 * small. Text inputs/queries are bounded to 1 MiB of UTF-8. No pointer is
 * retained; password text must be treated as sensitive by the owning host. */
BE_EXPORT be_status be_page_ime_get_state(be_page* page,
                                          be_ime_state* state,
                                          char* text_utf8,
                                          size_t capacity,
                                          size_t* required);

#ifdef __cplusplus
} /* extern "C" */
#endif
#endif
