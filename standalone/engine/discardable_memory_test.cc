// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
// Native provider contract tests. These use the production base/Skia objects;
// they neither initialize Blink nor prove that the engine renders images.
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <thread>
#include <vector>

#include "base/memory/discardable_memory.h"
#include "base/memory/discardable_memory_allocator.h"
#include "base/trace_event/memory_allocator_dump.h"
#include "base/trace_event/process_memory_dump.h"
#include "standalone/platform/discardable_memory.h"
#include "third_party/skia/include/private/chromium/SkDiscardableMemory.h"

namespace {
void Require(bool condition, const char* message) {
  if (!condition) {
    std::fprintf(stderr, "FAIL: %s\n", message);
    std::exit(1);
  }
}

uint64_t DumpSize(base::DiscardableMemory& memory) {
  base::trace_event::ProcessMemoryDump dump(
      {base::trace_event::MemoryDumpLevelOfDetail::kDetailed,
       base::trace_event::MemoryDumpDeterminism::kNone, 1});
  auto* allocation = memory.CreateMemoryAllocatorDump("test/image", &dump);
  Require(allocation == dump.GetAllocatorDump("test/image"),
          "the process dump owns the returned entry");
  return allocation->GetSizeInternal();
}

void LockAndDiscardContract() {
  auto allocator = blink_standalone::CreateDiscardableMemoryAllocator(8192);
  Require(!allocator->AllocateLockedDiscardableMemory(0),
          "zero bytes are rejected");
  auto memory = allocator->AllocateLockedDiscardableMemory(4096);
  Require(memory && memory->data(), "allocations begin locked");
  std::memset(memory->data(), 0xa5, 4096);
  Require(allocator->GetBytesAllocated() == 4096,
          "allocated bytes include locked memory");
  Require(DumpSize(*memory) == 4096, "a locked dump reports resident bytes");
  allocator->ReleaseFreeMemory();
  Require(static_cast<unsigned char*>(memory->data())[4095] == 0xa5,
          "pressure preserves locked bytes");
  memory->Unlock();
  Require(DumpSize(*memory) == 4096, "an unlocked resident dump keeps bytes");
  Require(memory->Lock(), "dumping does not leave the object locked");
  Require(static_cast<unsigned char*>(memory->data())[0] == 0xa5,
          "relocking preserves the original allocation");
  memory->Unlock();
  memory->DiscardForTesting();
  Require(allocator->GetBytesAllocated() == 0,
          "discard removes resident byte accounting");
  Require(!memory->Lock(), "a purged allocation cannot lock again");
  Require(DumpSize(*memory) == 0, "a discarded dump reports zero bytes");
  memory.reset();
}

void BudgetAndLruContract() {
  auto allocator = blink_standalone::CreateDiscardableMemoryAllocator(8192);
  auto first = allocator->AllocateLockedDiscardableMemory(4096);
  auto second = allocator->AllocateLockedDiscardableMemory(4096);
  Require(first && second, "cache entries allocate");
  first->Unlock();
  second->Unlock();
  Require(first->Lock(), "a cache hit updates recency");
  first->Unlock();
  auto third = allocator->AllocateLockedDiscardableMemory(4096);
  Require(third && allocator->GetBytesAllocated() == 8192,
          "allocation evicts to the resident budget");
  Require(!second->Lock(), "the least recently used unlocked entry is evicted");
  Require(first->Lock(), "the recently used entry survives eviction");
  allocator->ReleaseFreeMemory();
  Require(allocator->GetBytesAllocated() == 8192,
          "pressure preserves all currently locked entries");
  first.reset();
  third.reset();
  Require(allocator->GetBytesAllocated() == 0,
          "locked destruction releases memory safely");

  auto bounded = blink_standalone::CreateDiscardableMemoryAllocator(4096);
  auto a = bounded->AllocateLockedDiscardableMemory(4096);
  auto b = bounded->AllocateLockedDiscardableMemory(4096);
  Require(a && b && bounded->GetBytesAllocated() == 8192,
          "locked live allocations may temporarily exceed the cache budget");
  a->Unlock();
  Require(!a->Lock() && bounded->GetBytesAllocated() == 4096,
          "unlocking enforces the budget without discarding locked peers");
  b->Unlock();
  bounded->ReleaseFreeMemory();
  Require(!b->Lock() && bounded->GetBytesAllocated() == 0,
          "pressure frees the remaining unlocked cache");
}

void LifetimeAndZeroBudgetContract() {
  auto allocator = blink_standalone::CreateDiscardableMemoryAllocator(0);
  auto memory = allocator->AllocateLockedDiscardableMemory(4096);
  Require(memory && allocator->GetBytesAllocated() == 4096,
          "zero cache budget still permits live image memory");
  memory->Unlock();
  Require(!memory->Lock() && allocator->GetBytesAllocated() == 0,
          "zero budget immediately discards an unlocked entry");
  allocator = blink_standalone::CreateDiscardableMemoryAllocator(4096);
  memory = allocator->AllocateLockedDiscardableMemory(4096);
  Require(memory != nullptr, "lifetime fixture allocates");
  std::memset(memory->data(), 0x5a, 4096);
  allocator.reset();
  memory->Unlock();
  Require(memory->Lock() &&
              static_cast<unsigned char*>(memory->data())[4095] == 0x5a,
          "allocation handles retain the pool after allocator destruction");
}

void ConcurrentPoolContract() {
  auto allocator = blink_standalone::CreateDiscardableMemoryAllocator(32768);
  std::atomic<bool> run{true};
  std::atomic<bool> healthy{true};
  std::thread pressure([&] {
    while (run.load()) {
      allocator->ReleaseFreeMemory();
      (void)allocator->GetBytesAllocated();
      std::this_thread::yield();
    }
  });
  std::vector<std::thread> threads;
  for (int worker = 1; worker <= 8; ++worker) {
    threads.emplace_back([&, worker] {
      for (int iteration = 0; iteration < 200; ++iteration) {
        auto memory = allocator->AllocateLockedDiscardableMemory(4096);
        if (!memory) {
          healthy.store(false);
          return;
        }
        std::memset(memory->data(), worker, 4096);
        std::this_thread::yield();
        if (static_cast<unsigned char*>(memory->data())[4095] != worker)
          healthy.store(false);
        memory->Unlock();
        if (memory->Lock() &&
            static_cast<unsigned char*>(memory->data())[0] != worker)
          healthy.store(false);
      }
    });
  }
  for (auto& thread : threads)
    thread.join();
  run.store(false);
  pressure.join();
  Require(healthy.load(), "concurrent allocations and pressure preserve locks");
  Require(allocator->GetBytesAllocated() == 0,
          "concurrent handle destruction leaves no resident bytes");
}

void SkiaBridgeAndRegistrationContract() {
  Require(!base::DiscardableMemoryAllocator::HasInstance(),
          "this isolated native process has no initial allocator");
  blink_standalone::InitializeDiscardableMemory();
  auto* registered = base::DiscardableMemoryAllocator::GetInstance();
  blink_standalone::InitializeDiscardableMemory();
  Require(registered == base::DiscardableMemoryAllocator::GetInstance(),
          "initialization preserves the existing singleton");
  std::unique_ptr<SkDiscardableMemory> image(SkDiscardableMemory::Create(4096));
  Require(image && registered->GetBytesAllocated() == 4096,
          "the production Skia image bridge reaches the host provider");
  std::memset(image->data(), 0x7f, 4096);
  image->unlock();
  registered->ReleaseFreeMemory();
  Require(!image->lock(), "the Skia bridge observes host cache eviction");
  image.reset();
  Require(registered->GetBytesAllocated() == 0,
          "the image bridge releases its backing allocation");
}
}  // namespace

int main() {
  LockAndDiscardContract();
  BudgetAndLruContract();
  LifetimeAndZeroBudgetContract();
  ConcurrentPoolContract();
  SkiaBridgeAndRegistrationContract();
  std::puts(
      "discardable memory: 5 native production-provider contracts passed");
}
