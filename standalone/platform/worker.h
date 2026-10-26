// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#ifndef BLINK_STANDALONE_PLATFORM_WORKER_H_
#define BLINK_STANDALONE_PLATFORM_WORKER_H_

#include <memory>

#include "base/memory/scoped_refptr.h"

namespace base {
class SingleThreadTaskRunner;
}
namespace blink {
class WebDedicatedWorker;
class WebDedicatedWorkerHostFactoryClient;
class WebWorkerFetchContext;
}  // namespace blink

namespace blink_standalone {
class HostServices;

// Host integration for Blink's real DedicatedWorker/WorkerThread machinery.
// Only classic, same-origin HTTP(S) workers created by a document are enabled.
// Module/nested/shared/service workers, blob/data URLs and worker browser
// services are unavailable. postMessage, structured cloning and terminate are
// provided by upstream Blink; this class does not replace those JS objects.
class WorkerRuntime {
 public:
  WorkerRuntime(HostServices& services,
                scoped_refptr<base::SingleThreadTaskRunner> owner_task_runner);
  ~WorkerRuntime();
  WorkerRuntime(const WorkerRuntime&) = delete;
  WorkerRuntime& operator=(const WorkerRuntime&) = delete;

  // Platform::CreateDedicatedWorkerHostFactoryClient delegates here. It can
  // be invoked on a worker parent thread too, where nested starts are denied.
  std::unique_ptr<blink::WebDedicatedWorkerHostFactoryClient> CreateFactory(
      blink::WebDedicatedWorker* worker);

  // LocalFrameClient::CreateWorkerFetchContext delegates here. The argument
  // must be a factory returned by this runtime's CreateFactory().
  scoped_refptr<blink::WebWorkerFetchContext> CreateFetchContext(
      blink::WebDedicatedWorkerHostFactoryClient* factory);

  // Check before shutting down the owner task runner or host callbacks. After
  // document destruction or Worker.terminate(), pump until this becomes zero;
  // return BE_BUSY from engine shutdown while threads remain, never fake join.
  unsigned ActiveThreads() const;

 private:
  class State;
  std::unique_ptr<State> state_;
};
}  // namespace blink_standalone

#endif
