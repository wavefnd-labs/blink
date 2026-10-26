// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#ifndef BLINK_STANDALONE_ENGINE_INPUT_H_
#define BLINK_STANDALONE_ENGINE_INPUT_H_

#include "standalone/include/blink_engine.h"

namespace blink {
class LocalFrame;
}

namespace blink_standalone {

// Caller owns the engine API guard and updates layout/lifecycle before entry.
// BE_OK means delivery was valid. handled reports Blink consumption/default
// action, not whether a JavaScript observer happened to receive the event.
be_status DispatchPageInput(blink::LocalFrame& frame,
                            const be_input_event& event,
                            bool* handled);
be_status SetPageInputFocus(blink::LocalFrame& frame, bool focused);

}  // namespace blink_standalone
#endif
