// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#include "standalone/platform/host_platform.h"
#include <algorithm>
#include "build/build_config.h"
#include "partition_alloc/buildflags.h"
#if BUILDFLAG(IS_LINUX) && PA_BUILDFLAG(USE_PARTITION_ALLOC_AS_MALLOC)
#include <dlfcn.h>
#include "partition_alloc/partition_address_space.h"
#include "partition_alloc/shim/allocator_dispatch.h"
#include "partition_alloc/shim/allocator_shim.h"
#endif
#include "base/containers/span.h"
#include "base/memory/ref_counted_memory.h"
#include "base/task/single_thread_task_runner.h"
#include "base/time/time.h"
#include "standalone/platform/storage_host.h"
#include "standalone/platform/worker.h"
#include "third_party/blink/public/platform/web_data.h"
#include "third_party/blink/public/platform/web_dedicated_worker_host_factory_client.h"
#include "third_party/blink/public/platform/web_string.h"
#include "ui/base/resource/resource_bundle.h"

namespace blink_standalone {
#if BUILDFLAG(IS_LINUX) && PA_BUILDFLAG(USE_PARTITION_ALLOC_AS_MALLOC)
namespace {
allocator_shim::AllocatorDispatch host_allocator{};
void (*libc_free)(void*) = nullptr;
void* (*libc_realloc)(void*, size_t) = nullptr;
size_t (*libc_allocation_size)(void*) = nullptr;

bool EngineAllocation(void* address) {
  return partition_alloc::IsManagedByPartitionAlloc(
      reinterpret_cast<uintptr_t>(address));
}
void HostFree(void* address, void* context) {
  if (!EngineAllocation(address))
    return libc_free(address);
  host_allocator.next->free_function(address, context);
}
void HostSizedFree(void* address, size_t size, void* context) {
  if (!EngineAllocation(address))
    return libc_free(address);
  host_allocator.next->free_with_size_function(address, size, context);
}
void HostAlignedFree(void* address, size_t alignment, void* context) {
  if (!EngineAllocation(address))
    return libc_free(address);
  host_allocator.next->free_with_alignment_function(address, alignment, context);
}
void HostSizedAlignedFree(void* address, size_t size, size_t alignment,
                          void* context) {
  if (!EngineAllocation(address))
    return libc_free(address);
  host_allocator.next->free_with_size_and_alignment_function(
      address, size, alignment, context);
}
void* HostRealloc(void* address, size_t size,
                  allocator_shim::AllocToken token, void* context) {
  if (address && !EngineAllocation(address))
    return libc_realloc(address, size);
  return host_allocator.next->realloc_function(address, size, token, context);
}
void* HostUncheckedRealloc(void* address, size_t size,
                           allocator_shim::AllocToken token, void* context) {
  if (address && !EngineAllocation(address))
    return libc_realloc(address, size);
  return host_allocator.next->realloc_unchecked_function(address, size, token,
                                                         context);
}
size_t HostAllocationSize(void* address, void* context) {
  if (!EngineAllocation(address))
    return libc_allocation_size(address);
  return host_allocator.next->get_size_estimate_function(address, context);
}
}  // namespace
#endif

bool InitializeHostAllocator() {
#if BUILDFLAG(IS_LINUX) && PA_BUILDFLAG(USE_PARTITION_ALLOC_AS_MALLOC)
  static const bool initialized = [] {
    // libc helpers such as realpath()/strdup() can return libc-owned memory
    // even when called by a DSO with private malloc symbols. Fontconfig then
    // passes those buffers to free(). Keep those allocations in libc's heap,
    // while preserving PartitionAlloc for every engine-owned allocation.
    void* libc = dlopen("libc.so.6", RTLD_NOW | RTLD_LOCAL | RTLD_NOLOAD);
    if (!libc)
      return false;
    libc_free = reinterpret_cast<decltype(libc_free)>(dlsym(libc, "__libc_free"));
    libc_realloc = reinterpret_cast<decltype(libc_realloc)>(
        dlsym(libc, "__libc_realloc"));
    libc_allocation_size = reinterpret_cast<decltype(libc_allocation_size)>(
        dlsym(libc, "malloc_usable_size"));
    if (!libc_free || !libc_realloc || !libc_allocation_size)
      return false;
    host_allocator.free_function = HostFree;
    host_allocator.free_with_size_function = HostSizedFree;
    host_allocator.free_with_alignment_function = HostAlignedFree;
    host_allocator.free_with_size_and_alignment_function = HostSizedAlignedFree;
    host_allocator.realloc_function = HostRealloc;
    host_allocator.realloc_unchecked_function = HostUncheckedRealloc;
    host_allocator.get_size_estimate_function = HostAllocationSize;
    allocator_shim::InsertAllocatorDispatch(&host_allocator);
    return true;
  }();
  return initialized;
#else
  return true;
#endif
}

HostServices::HostServices(const be_engine_config& config) : config_(config) {}
void HostServices::Wake(uint64_t delay_ms) const {
  {
    std::lock_guard<std::mutex> lock(callback_mutex_);
    if (!enabled_ || !config_.wake_up)
      return;
    ++callbacks_in_flight_;
  }
  config_.wake_up(config_.user_data, delay_ms);
  {
    std::lock_guard<std::mutex> lock(callback_mutex_);
    --callbacks_in_flight_;
    if (!callbacks_in_flight_)
      callbacks_finished_.notify_all();
  }
}
void HostServices::Disable() {
  std::unique_lock<std::mutex> lock(callback_mutex_);
  enabled_ = false;
  callbacks_finished_.wait(lock, [&] { return callbacks_in_flight_ == 0; });
}

HostMessagePump::HostMessagePump(HostServices& services)
    : services_(services) {}
void HostMessagePump::SetBudget(uint32_t budget_ms) {
  budget_ms_ = budget_ms;
}
void HostMessagePump::Run(Delegate* delegate) {
  const bool outer_quit = quit_;
  quit_ = false;
  const auto deadline = base::TimeTicks::Now() + base::Milliseconds(budget_ms_);
  while (!quit_) {
    auto next = delegate->DoWork();
    if (quit_)
      break;
    if (next.is_immediate()) {
      if (base::TimeTicks::Now() >= deadline) {
        services_->Wake(0);
        break;
      }
      continue;
    }
    delegate->DoIdleWork();
    if (!next.delayed_run_time.is_max() && !quit_)
      ScheduleDelayedWork(next);
    break;
  }
  quit_ = outer_quit;
}
void HostMessagePump::Quit() {
  quit_ = true;
}
void HostMessagePump::ScheduleWork() {
  services_->Wake(0);
}
void HostMessagePump::ScheduleDelayedWork(const Delegate::NextWorkInfo& next) {
  if (next.is_immediate()) {
    services_->Wake(0);
  } else if (!next.delayed_run_time.is_max()) {
    const auto delay = next.delayed_run_time - base::TimeTicks::Now();
    services_->Wake(static_cast<uint64_t>(
        std::max<int64_t>(0, delay.InMillisecondsRoundedUp())));
  }
}

HostPlatform::HostPlatform(const be_engine_config& config)
    : locale_(config.locale ? config.locale : "en-US"),
      io_thread_(std::make_unique<base::Thread>("Blink standalone IO")) {}
HostPlatform::~HostPlatform() = default;
bool HostPlatform::StartIO() {
  base::Thread::Options options;
  options.message_pump_type = base::MessagePumpType::IO;
  if (!io_thread_->StartWithOptions(std::move(options)))
    return false;
  storage_ = std::make_unique<StorageHost>(io_thread_->task_runner());
  return true;
}
StorageHost& HostPlatform::storage() const {
  return *storage_;
}
void HostPlatform::StartWorkers(HostServices& services) {
  workers_ = std::make_unique<WorkerRuntime>(
      services, base::SingleThreadTaskRunner::GetCurrentDefault());
}
WorkerRuntime& HostPlatform::workers() const {
  return *workers_;
}
std::unique_ptr<blink::WebDedicatedWorkerHostFactoryClient>
HostPlatform::CreateDedicatedWorkerHostFactoryClient(
    blink::WebDedicatedWorker* worker,
    const blink::BrowserInterfaceBrokerProxy&) {
  return workers_->CreateFactory(worker);
}
blink::ThreadSafeBrowserInterfaceBrokerProxy*
HostPlatform::GetBrowserInterfaceBroker() {
  return storage_ ? storage_->Broker()
                  : blink::Platform::GetBrowserInterfaceBroker();
}
bool HostPlatform::HasDataResource(int resource_id) const {
  return ui::ResourceBundle::GetSharedInstance().HasDataResource(resource_id);
}
blink::WebData HostPlatform::GetDataResource(
    int resource_id,
    ui::ResourceScaleFactor scale_factor) {
  auto bytes =
      ui::ResourceBundle::GetSharedInstance().LoadDataResourceBytesForScale(
          resource_id, scale_factor);
  return bytes ? blink::WebData(base::span<const uint8_t>(*bytes))
               : blink::WebData();
}
std::string HostPlatform::GetDataResourceString(int resource_id) {
  return ui::ResourceBundle::GetSharedInstance().LoadDataResourceString(
      resource_id);
}
scoped_refptr<base::RefCountedMemory> HostPlatform::GetDataResourceBytes(
    int resource_id) {
  return ui::ResourceBundle::GetSharedInstance().LoadDataResourceBytes(
      resource_id);
}
blink::WebString HostPlatform::DefaultLocale() {
  return blink::WebString::FromUtf8(locale_);
}
blink::WebString HostPlatform::UserAgent() {
  return blink::WebString::FromUtf8("BlinkStandalone/1");
}
scoped_refptr<base::SingleThreadTaskRunner> HostPlatform::GetIOTaskRunner()
    const {
  return io_thread_->task_runner();
}
}  // namespace blink_standalone
