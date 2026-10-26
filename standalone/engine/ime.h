// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#ifndef BLINK_STANDALONE_ENGINE_IME_H_
#define BLINK_STANDALONE_ENGINE_IME_H_

#include <string_view>

#include "standalone/include/blink_ime.h"

namespace blink {
class LocalFrame;
}
namespace blink_standalone {

// The caller holds the engine API guard and owns the frame throughout the
// call. These helpers recheck document/focus/editability after layout work.
// Frames with an active EditContext are explicitly unsupported by this
// initial DOM InputMethodController integration.
be_status SetImeComposition(blink::LocalFrame&, const be_ime_composition&);
be_status CommitImeText(blink::LocalFrame&,
                        std::string_view,
                        int32_t relative_caret_utf16);
be_status FinishImeComposition(blink::LocalFrame&, bool keep_selection);
be_status CancelImeComposition(blink::LocalFrame&);
be_status QueryImeState(blink::LocalFrame&,
                        be_ime_state*,
                        char* text_utf8,
                        size_t capacity,
                        size_t* required);

}  // namespace blink_standalone
#endif
