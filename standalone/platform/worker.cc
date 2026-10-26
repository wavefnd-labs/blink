// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#include "standalone/platform/worker.h"

#include <utility>

#include "base/functional/bind.h"
#include "base/location.h"
#include "base/memory/raw_ref.h"
#include "base/memory/weak_ptr.h"
#include "base/task/single_thread_task_runner.h"
#include "mojo/public/cpp/bindings/generic_pending_receiver.h"
#include "mojo/public/cpp/bindings/pending_receiver.h"
#include "mojo/public/cpp/bindings/pending_remote.h"
#include "mojo/public/cpp/bindings/self_owned_receiver.h"
#include "standalone/platform/host_platform.h"
#include "standalone/platform/worker_loader.h"
#include "third_party/blink/public/mojom/browser_interface_broker.mojom-blink.h"
#include "third_party/blink/public/mojom/worker/dedicated_worker_host.mojom-blink.h"
#include "third_party/blink/public/platform/web_dedicated_worker.h"
#include "third_party/blink/public/platform/web_dedicated_worker_host_factory_client.h"
#include "third_party/blink/public/platform/web_fetch_client_settings_object.h"
#include "third_party/blink/public/platform/web_security_origin.h"
#include "third_party/blink/public/platform/web_url.h"
#include "third_party/blink/renderer/core/execution_context/execution_context.h"
#include "third_party/blink/renderer/core/frame/local_dom_window.h"
#include "third_party/blink/renderer/core/frame/local_frame.h"
#include "third_party/blink/renderer/core/workers/dedicated_worker.h"
#include "third_party/blink/renderer/core/workers/worker_thread.h"
#include "third_party/blink/renderer/platform/heap/persistent.h"
#include "third_party/blink/renderer/platform/scheduler/public/thread.h"
#include "third_party/blink/renderer/platform/weborigin/security_origin.h"
#include "third_party/blink/renderer/platform/wtf/casting.h"
#include "url/gurl.h"
#include "url/origin.h"

namespace blink_standalone {
namespace {

// The pinned Mojo interface intentionally has no methods. A real live endpoint
// supplies the lifetime token required by DedicatedWorkerThread; browser
// services are not reachable through it.
class DedicatedWorkerHost final
    : public blink::mojom::blink::DedicatedWorkerHost {};

// Keep a bound transport even for unavailable interfaces: several upstream
// diagnostic paths assume a bound broker before requesting an optional host.
// Closing the received endpoint communicates service unavailability without
// impersonating storage, permissions, sockets, GPU or other browser services.
class WorkerBroker final : public blink::mojom::blink::BrowserInterfaceBroker {
 public:
  void GetInterface(mojo::GenericPendingReceiver) override {}
};

class HostWorkerFactory final
    : public blink::WebDedicatedWorkerHostFactoryClient {
 public:
  HostWorkerFactory(HostServices& services,
                    scoped_refptr<base::SingleThreadTaskRunner> owner_runner,
                    blink::DedicatedWorker& worker)
      : services_(services),
        owner_runner_(std::move(owner_runner)),
        worker_(&worker),
        parent_runner_(worker.GetExecutionContext()->GetTaskRunner(
            blink::TaskType::kInternalDefault)),
        worker_origin_(
            worker.GetExecutionContext()->GetSecurityOrigin()->ToUrlOrigin()),
        top_origin_(worker_origin_) {
    auto* window =
        blink::DynamicTo<blink::LocalDOMWindow>(worker.GetExecutionContext());
    supported_ = window && worker.UsesClassicScriptForEmbedding() &&
                 !worker_origin_.opaque();
    if (window && window->GetFrame())
      top_origin_ = window->GetFrame()
                        ->Top()
                        ->GetSecurityContext()
                        ->GetSecurityOrigin()
                        ->ToUrlOrigin();
  }

  void CreateWorkerHost(
      const blink::DedicatedWorkerToken&,
      const blink::WebURL& script_url,
      network::mojom::CredentialsMode,
      const blink::WebFetchClientSettingsObject& settings,
      blink::CrossVariantMojoRemote<blink::mojom::BlobURLTokenInterfaceBase>)
      override {
    const GURL url(script_url.GetString().Utf8());
    // Worker-src/CSP checks already ran in DedicatedWorker::Start(). Enforce
    // this host's smaller transport/type profile as well; no blob resolver,
    // nested worker host or cross-origin-isolated worker service is invented.
    const bool allow =
        supported_ && url.SchemeIsHTTPOrHTTPS() && !url.has_username() &&
        !url.has_password() &&
        worker_origin_.IsSameOriginWith(url::Origin::Create(url)) &&
        settings.policy_container_policies.cross_origin_embedder_policy ==
            network::mojom::CrossOriginEmbedderPolicyValue::kNone;
    parent_runner_->PostTask(FROM_HERE,
                             base::BindOnce(&HostWorkerFactory::Start,
                                            weak_factory_.GetWeakPtr(), allow));
  }

  scoped_refptr<blink::WebWorkerFetchContext> CloneWorkerFetchContext(
      blink::WebWorkerFetchContext*,
      scoped_refptr<base::SingleThreadTaskRunner>) override {
    // Unsupported nested workers fail in CreateWorkerHost before this hook.
    // Returning a usable restricted context also avoids a null dereference if
    // upstream asks for one during a future failure-path change.
    return CreateFetchContext();
  }

  scoped_refptr<blink::WebWorkerFetchContext> CreateFetchContext() {
    return MakeWorkerFetchContext(*services_, owner_runner_, worker_origin_,
                                  top_origin_);
  }

 private:
  void Start(bool allow) {
    auto* worker = worker_.Get();
    if (!worker || !worker->GetExecutionContext() ||
        worker->GetExecutionContext()->IsContextDestroyed())
      return;
    if (!allow) {
      // This callback can delete this factory. Do not touch members afterward.
      worker->OnScriptLoadStartFailed();
      return;
    }
    mojo::PendingRemote<blink::mojom::blink::DedicatedWorkerHost> host;
    mojo::MakeSelfOwnedReceiver(std::make_unique<DedicatedWorkerHost>(),
                                host.InitWithNewPipeAndPassReceiver());
    mojo::PendingRemote<blink::mojom::blink::BrowserInterfaceBroker> broker;
    mojo::MakeSelfOwnedReceiver(std::make_unique<WorkerBroker>(),
                                broker.InitWithNewPipeAndPassReceiver());
    worker->OnWorkerHostCreated(std::move(broker), std::move(host),
                                blink::WebSecurityOrigin(worker_origin_));
    // A null preloaded-main-script packet selects Blink's retained production
    // WorkerClassicScriptLoader/ResourceFetcher path on the actual worker
    // thread. PolicyContainer::CreateEmpty already supplied a safe local host
    // in WorkerOrWorkletGlobalScope; response CSP comes from the script load.
    worker->OnScriptLoadStarted(nullptr, mojo::NullRemote(), nullptr,
                                mojo::NullReceiver(), mojo::NullReceiver());
  }

  const base::raw_ref<HostServices> services_;
  const scoped_refptr<base::SingleThreadTaskRunner> owner_runner_;
  blink::WeakPersistent<blink::DedicatedWorker> worker_;
  const scoped_refptr<base::SingleThreadTaskRunner> parent_runner_;
  const url::Origin worker_origin_;
  url::Origin top_origin_;
  bool supported_ = false;
  base::WeakPtrFactory<HostWorkerFactory> weak_factory_{this};
};

}  // namespace

class WorkerRuntime::State {
 public:
  State(HostServices& host, scoped_refptr<base::SingleThreadTaskRunner> runner)
      : services(host), owner_runner(std::move(runner)) {}
  const base::raw_ref<HostServices> services;
  const scoped_refptr<base::SingleThreadTaskRunner> owner_runner;
};

WorkerRuntime::WorkerRuntime(
    HostServices& services,
    scoped_refptr<base::SingleThreadTaskRunner> owner_task_runner)
    : state_(std::make_unique<State>(services, std::move(owner_task_runner))) {}
WorkerRuntime::~WorkerRuntime() = default;

std::unique_ptr<blink::WebDedicatedWorkerHostFactoryClient>
WorkerRuntime::CreateFactory(blink::WebDedicatedWorker* worker) {
  // This is the concrete implementation of WebDedicatedWorker at this pinned
  // Blink revision. The host never supplies an alternative worker JS object.
  return std::make_unique<HostWorkerFactory>(
      *state_->services, state_->owner_runner,
      *static_cast<blink::DedicatedWorker*>(worker));
}

scoped_refptr<blink::WebWorkerFetchContext> WorkerRuntime::CreateFetchContext(
    blink::WebDedicatedWorkerHostFactoryClient* factory) {
  return static_cast<HostWorkerFactory*>(factory)->CreateFetchContext();
}

unsigned WorkerRuntime::ActiveThreads() const {
  return blink::WorkerThread::WorkerThreadCount();
}
}  // namespace blink_standalone
