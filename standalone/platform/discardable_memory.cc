// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#include "standalone/platform/discardable_memory.h"

#include <memory>
#include <mutex>
#include <utility>

#include "base/check.h"
#include "base/memory/discardable_memory.h"
#include "base/memory/discardable_memory_allocator.h"
#include "base/no_destructor.h"
#include "base/trace_event/memory_allocator_dump.h"
#include "base/trace_event/process_memory_dump.h"
#include "third_party/skia/src/lazy/SkDiscardableMemoryPool.h"

namespace blink_standalone {
namespace {
constexpr size_t kCacheBudget = 128u * 1024u * 1024u;

struct PoolState {
  explicit PoolState(size_t cache_budget)
      : pool(SkDiscardableMemoryPool::Make(cache_budget)) {}

  // Skia serializes mutations internally, but getRAMUsed() does not acquire
  // its internal lock. Serialize every operation through this private pool.
  mutable std::mutex mutex;
  sk_sp<SkDiscardableMemoryPool> pool;
};

class HostDiscardableMemory final : public base::DiscardableMemory {
 public:
  HostDiscardableMemory(std::shared_ptr<PoolState> state,
                        std::unique_ptr<SkDiscardableMemory> memory,
                        size_t size)
      : state_(std::move(state)), memory_(std::move(memory)), size_(size) {}

  ~HostDiscardableMemory() override {
    std::lock_guard<std::mutex> lock(state_->mutex);
    if (locked_)
      memory_->unlock();
    // SkDiscardableMemory requires destruction in the unlocked state.
    memory_.reset();
  }

  bool Lock() override {
    std::lock_guard<std::mutex> lock(state_->mutex);
    DCHECK(!locked_);
    locked_ = memory_->lock();
    return locked_;
  }

  void Unlock() override {
    std::lock_guard<std::mutex> lock(state_->mutex);
    DCHECK(locked_);
    memory_->unlock();
    locked_ = false;
  }

  void* data() const override {
    std::lock_guard<std::mutex> lock(state_->mutex);
    DCHECK(locked_);
    return memory_->data();
  }

  void DiscardForTesting() override {
    std::lock_guard<std::mutex> lock(state_->mutex);
    DCHECK(!locked_);
    // Evicting other unlocked cache entries is also allowed under pressure.
    state_->pool->dumpPool();
  }

  base::trace_event::MemoryAllocatorDump* CreateMemoryAllocatorDump(
      const char* name,
      base::trace_event::ProcessMemoryDump* process_dump) const override {
    std::lock_guard<std::mutex> lock(state_->mutex);
    // An unlocked block may already be purged. Probe residency while preserving
    // the externally visible lock state; a discarded block is never recreated.
    const bool resident = locked_ || memory_->lock();
    if (resident && !locked_)
      memory_->unlock();
    auto* dump = process_dump->CreateAllocatorDump(name);
    dump->AddScalar(base::trace_event::MemoryAllocatorDump::kNameSize,
                    base::trace_event::MemoryAllocatorDump::kUnitsBytes,
                    resident ? size_ : 0u);
    return dump;
  }

 private:
  std::shared_ptr<PoolState> state_;
  std::unique_ptr<SkDiscardableMemory> memory_;
  size_t size_;
  bool locked_ = true;
};

class HostDiscardableMemoryAllocator final
    : public base::DiscardableMemoryAllocator {
 public:
  explicit HostDiscardableMemoryAllocator(size_t cache_budget)
      : state_(std::make_shared<PoolState>(cache_budget)) {}

  std::unique_ptr<base::DiscardableMemory> AllocateLockedDiscardableMemory(
      size_t size) override {
    if (!size)
      return nullptr;
    std::lock_guard<std::mutex> lock(state_->mutex);
    // The concrete pool avoids SkDiscardableMemory::Create(), which calls this
    // allocator through Chromium's Skia bridge and would otherwise recurse.
    std::unique_ptr<SkDiscardableMemory> memory(state_->pool->create(size));
    if (!memory)
      return nullptr;
    return std::make_unique<HostDiscardableMemory>(state_, std::move(memory),
                                                   size);
  }

  size_t GetBytesAllocated() const override {
    std::lock_guard<std::mutex> lock(state_->mutex);
    return state_->pool->getRAMUsed();
  }

  void ReleaseFreeMemory() override {
    std::lock_guard<std::mutex> lock(state_->mutex);
    state_->pool->dumpPool();
  }

 private:
  std::shared_ptr<PoolState> state_;
};
}  // namespace

std::unique_ptr<base::DiscardableMemoryAllocator>
CreateDiscardableMemoryAllocator(size_t cache_budget) {
  return std::make_unique<HostDiscardableMemoryAllocator>(cache_budget);
}

void InitializeDiscardableMemory() {
  if (base::DiscardableMemoryAllocator::HasInstance())
    return;
  static base::NoDestructor<HostDiscardableMemoryAllocator> allocator(
      kCacheBudget);
  base::DiscardableMemoryAllocator::SetInstance(allocator.get());
}
}  // namespace blink_standalone
