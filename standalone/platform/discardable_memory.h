// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#ifndef BLINK_STANDALONE_PLATFORM_DISCARDABLE_MEMORY_H_
#define BLINK_STANDALONE_PLATFORM_DISCARDABLE_MEMORY_H_

#include <stddef.h>

#include <memory>

namespace base {
class DiscardableMemoryAllocator;
}

namespace blink_standalone {

// Creates a private production Skia pool behind base's allocator API. The
// resident cache budget may be exceeded while allocations are locked. Unlocked
// allocations are evicted least recently used first. Allocation handles retain
// their pool and may outlive the allocator object.
std::unique_ptr<base::DiscardableMemoryAllocator>
CreateDiscardableMemoryAllocator(size_t cache_budget);

// Installs the process-lived engine allocator, preserving an existing provider.
// Call under engine initialization serialization before Blink or worker
// startup. Registration and the backing pool survive engine shutdown, like
// Blink itself.
void InitializeDiscardableMemory();

}  // namespace blink_standalone
#endif  // BLINK_STANDALONE_PLATFORM_DISCARDABLE_MEMORY_H_
