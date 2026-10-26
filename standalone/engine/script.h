// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#ifndef BLINK_STANDALONE_ENGINE_SCRIPT_H_
#define BLINK_STANDALONE_ENGINE_SCRIPT_H_

#include <string>
#include <string_view>

#include "standalone/include/blink_engine.h"

namespace blink {
class LocalFrame;
}

namespace blink_standalone {

// Owns copied UTF-8 bytes only. No V8 handles or page-realm objects escape the
// evaluation scope. BE_OK holds JSON, except undefined's literal "undefined".
struct ScriptResult {
  be_status status = BE_SCRIPT_ERROR;
  std::string utf8_result;
  bool exception = false;
};

// Execute synchronous classic script in this frame's existing main world.
// The caller enforces owning-thread access, prevents API reentry and keeps the
// frame alive throughout the call. A source URL is diagnostic metadata, never
// a navigation, fetch or change of the page's security origin. Empty selects
// the current document URL; nonempty URLs must be absolute HTTP(S)/about:blank.
//
// Blink's production runner performs normal script policy and microtask work.
// Promises are not awaited; function/symbol/Promise result values are rejected.
// JSON serialization may execute user toJSON/getters and can throw. Source and
// copied result bytes are limited to 64 MiB; this is not a JS heap/time limit.
// The helper provides no timeout or isolate-wide termination/recovery policy.
ScriptResult EvaluateScript(blink::LocalFrame& frame,
                            std::string_view source,
                            std::string_view source_url);

}  // namespace blink_standalone

#endif
