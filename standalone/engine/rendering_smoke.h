// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#ifndef BLINK_STANDALONE_ENGINE_RENDERING_SMOKE_H_
#define BLINK_STANDALONE_ENGINE_RENDERING_SMOKE_H_

#include "standalone/include/blink_engine.h"

namespace blink_standalone::smoke {
// Returns true only for this harness's private resource URLs. Responses point
// to immutable, process-lifetime fixtures and have no response lease to
// release.
bool FetchRenderingFixture(const char* url, be_resource_response* response);
bool ImageRenderingSmoke(be_engine* engine, be_page* page);
bool FontRenderingSmoke(be_engine* engine, be_page* page);
}  // namespace blink_standalone::smoke

#endif  // BLINK_STANDALONE_ENGINE_RENDERING_SMOKE_H_
