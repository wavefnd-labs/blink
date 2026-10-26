// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#ifndef BLINK_STANDALONE_PLATFORM_HOST_PLATFORM_H_
#define BLINK_STANDALONE_PLATFORM_HOST_PLATFORM_H_
#include <condition_variable>
#include <memory>
#include <mutex>
#include <string>
#include "base/memory/raw_ref.h"
#include "base/message_loop/message_pump.h"
#include "base/threading/thread.h"
#include "standalone/include/blink_engine.h"
#include "third_party/blink/public/platform/platform.h"

namespace blink_standalone {
// Install Linux libc/PartitionAlloc ownership routing before host services run.
bool InitializeHostAllocator();
class StorageHost;
class WorkerRuntime;
class HostServices {
 public:
  explicit HostServices(const be_engine_config& config);
  void Wake(uint64_t delay_ms) const;
  void Disable();
  const be_engine_config& config() const { return config_; }

 private:
  be_engine_config config_;
  mutable std::mutex callback_mutex_;
  mutable std::condition_variable callbacks_finished_;
  mutable unsigned callbacks_in_flight_ = 0;
  bool enabled_ = true;
};

// A non-blocking message pump. The embedder owns the outer event loop.
class HostMessagePump final : public base::MessagePump {
 public:
  explicit HostMessagePump(HostServices& services);
  void SetBudget(uint32_t budget_ms);
  void Run(Delegate* delegate) override;
  void Quit() override;
  void ScheduleWork() override;
  void ScheduleDelayedWork(const Delegate::NextWorkInfo& next) override;

 private:
  const base::raw_ref<HostServices> services_;
  bool quit_ = false;
  uint32_t budget_ms_ = 8;
};

class HostPlatform final : public blink::Platform {
 public:
  explicit HostPlatform(const be_engine_config& config);
  ~HostPlatform() override;
  bool StartIO();
  StorageHost& storage() const;
  void StartWorkers(HostServices& services);
  WorkerRuntime& workers() const;
  std::unique_ptr<blink::WebDedicatedWorkerHostFactoryClient>
  CreateDedicatedWorkerHostFactoryClient(
      blink::WebDedicatedWorker*,
      const blink::BrowserInterfaceBrokerProxy&) override;
  blink::ThreadSafeBrowserInterfaceBrokerProxy* GetBrowserInterfaceBroker()
      override;
  bool HasDataResource(int resource_id) const override;
  blink::WebData GetDataResource(
      int resource_id,
      ui::ResourceScaleFactor scale_factor = ui::kScaleFactorNone) override;
  std::string GetDataResourceString(int resource_id) override;
  scoped_refptr<base::RefCountedMemory> GetDataResourceBytes(
      int resource_id) override;
  blink::WebString DefaultLocale() override;
  blink::WebString UserAgent() override;
  scoped_refptr<base::SingleThreadTaskRunner> GetIOTaskRunner() const override;

 private:
  std::string locale_;
  std::unique_ptr<base::Thread> io_thread_;
  std::unique_ptr<StorageHost> storage_;
  std::unique_ptr<WorkerRuntime> workers_;
};
}  // namespace blink_standalone
#endif
