// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#include "standalone/platform/storage_host.h"

#include <atomic>
#include <condition_variable>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "base/check_op.h"
#include "base/functional/bind.h"
#include "base/location.h"
#include "base/memory/raw_ref.h"
#include "base/memory/ref_counted.h"
#include "base/memory/ref_counted_delete_on_sequence.h"
#include "base/task/single_thread_task_runner.h"
#include "mojo/public/cpp/bindings/generic_pending_receiver.h"
#include "mojo/public/cpp/bindings/receiver_set.h"
#include "mojo/public/cpp/bindings/remote_set.h"
#include "standalone/common/storage.h"
#include "third_party/blink/public/common/storage_key/storage_key.h"
#include "third_party/blink/public/common/thread_safe_browser_interface_broker_proxy.h"
#include "third_party/blink/public/mojom/dom_storage/dom_storage.mojom-blink.h"
#include "third_party/blink/public/mojom/dom_storage/session_storage_namespace.mojom-blink.h"
#include "third_party/blink/public/mojom/dom_storage/storage_area.mojom-blink.h"
#include "third_party/blink/renderer/core/execution_context/execution_context_lifecycle_observer.h"
#include "third_party/blink/renderer/core/frame/local_dom_window.h"
#include "third_party/blink/renderer/core/frame/local_frame.h"
#include "third_party/blink/renderer/core/frame/settings.h"
#include "third_party/blink/renderer/core/page/page.h"
#include "third_party/blink/renderer/modules/storage/cached_storage_area.h"
#include "third_party/blink/renderer/modules/storage/storage_controller.h"
#include "third_party/blink/renderer/modules/storage/storage_namespace.h"
#include "third_party/blink/renderer/platform/storage/blink_storage_key.h"
#include "third_party/blink/renderer/platform/supplementable.h"
#include "third_party/blink/renderer/platform/weborigin/security_origin.h"
#include "third_party/blink/renderer/platform/wtf/text/wtf_string.h"

namespace blink_standalone {
namespace {
namespace dom = blink::mojom::blink;
static_assert(kStorageAreaQuota == dom::StorageArea::kPerStorageAreaQuota);

StorageBytes Bytes(const blink::Vector<uint8_t>& value) {
  return StorageBytes(value.begin(), value.end());
}
blink::Vector<uint8_t> BlinkBytes(const StorageBytes& value) {
  blink::Vector<uint8_t> result;
  result.append_range(value);
  return result;
}

struct RegisteredFrame {
  std::string namespace_id;
  std::string key;
};
class StorageBackend;

class NativeStorageArea final : public dom::StorageArea {
 public:
  NativeStorageArea(StorageBackend& owner,
                    std::string key,
                    bool session,
                    bool persistent);
  void Bind(mojo::PendingReceiver<dom::StorageArea> receiver);
  void AddObserver(
      mojo::PendingRemote<dom::StorageAreaObserver> observer) override;
  void Put(const blink::Vector<uint8_t>& key,
           const blink::Vector<uint8_t>& value,
           const std::optional<blink::Vector<uint8_t>>& client_old_value,
           dom::StorageAreaSourcePtr source,
           PutCallback callback) override;
  void Delete(const blink::Vector<uint8_t>& key,
              const std::optional<blink::Vector<uint8_t>>& client_old_value,
              dom::StorageAreaSourcePtr source,
              DeleteCallback callback) override;
  void DeleteAll(dom::StorageAreaSourcePtr source,
                 mojo::PendingRemote<dom::StorageAreaObserver> new_observer,
                 DeleteAllCallback callback) override;
  void GetAll(mojo::PendingRemote<dom::StorageAreaObserver> new_observer,
              GetAllCallback callback) override;
  StorageData data;

 private:
  bool Commit(StorageData candidate);
  void FailedKey(const blink::Vector<uint8_t>& key,
                 const dom::StorageAreaSourcePtr& source);
  base::raw_ref<StorageBackend> owner_;
  const std::string key_;
  const bool session_;
  const bool persistent_;
  bool readable_ = true;
  mojo::ReceiverSet<dom::StorageArea> receivers_;
  mojo::RemoteSet<dom::StorageAreaObserver> observers_;
};

class NativeSessionNamespace final : public dom::SessionStorageNamespace {
 public:
  NativeSessionNamespace(StorageBackend& owner, std::string id)
      : owner_(owner), id_(std::move(id)) {}
  void Bind(mojo::PendingReceiver<dom::SessionStorageNamespace> receiver) {
    receivers_.Add(this, std::move(receiver));
  }
  void Clone(const blink::String& target) override;

 private:
  base::raw_ref<StorageBackend> owner_;
  const std::string id_;
  mojo::ReceiverSet<dom::SessionStorageNamespace> receivers_;
};

// Every member and Mojo endpoint below belongs to the IO runner. No browser
// storage implementation, global JS replacement or filesystem fallback is used.
class StorageBackend final : public dom::DomStorageProvider,
                             public dom::DomStorage {
 public:
  explicit StorageBackend(std::atomic<be_status>& status) : status_(status) {}
  be_status Configure(const be_storage_client* client) {
    if (used_)
      return BE_BUSY;
    client_ = client ? *client : be_storage_client{};
    client_.struct_size = sizeof(client_);
    client_.abi_version = BE_ABI_VERSION;
    status_->store(BE_OK);
    return BE_OK;
  }
  void Error(const std::string& key, be_status status) {
    if (status < BE_INVALID_ARGUMENT || status > BE_SCRIPT_ERROR)
      status = BE_RESOURCE_ERROR;
    auto before = BE_OK;
    status_->compare_exchange_strong(before, status);
    if (client_.error)
      client_.error(client_.user_data, key.c_str(), status);
  }
  bool Load(const std::string& key, StorageData& output) {
    be_storage_snapshot response{};
    response.struct_size = sizeof(response);
    response.abi_version = BE_ABI_VERSION;
    const auto status = client_.load(client_.user_data, key.c_str(), &response);
    if (status != BE_OK) {
      Error(key, status);
      return false;
    }
    struct Release {
      explicit Release(be_storage_snapshot& value) : response(value) {}
      base::raw_ref<be_storage_snapshot> response;
      ~Release() {
        if (response->release)
          response->release(response->release_user_data);
      }
    } release(response);
    if (response.struct_size < sizeof(response) ||
        response.abi_version != BE_ABI_VERSION ||
        response.size > kStorageSnapshotLimit ||
        (response.size && !response.data) ||
        !DecodeStorageSnapshot(std::span(response.data, response.size),
                               output)) {
      Error(key, BE_RESOURCE_ERROR);
      return false;
    }
    return true;
  }
  bool Save(const std::string& key, const StorageData& data) {
    auto bytes = EncodeStorageSnapshot(data);
    if (!bytes) {
      Error(key, BE_RESOURCE_ERROR);
      return false;
    }
    const auto status = client_.save(client_.user_data, key.c_str(),
                                     bytes->data(), bytes->size());
    if (status == BE_OK)
      return true;
    Error(key, status);
    return false;
  }
  void ResetLocalClients() {
    for (const auto& client : clients_)
      client->ResetLocalStorageConnections();
  }
  void CreateNamespace(const std::string& id) {
    used_ = true;
    namespaces_.try_emplace(
        id, std::make_unique<NativeSessionNamespace>(*this, id));
  }
  void RemoveNamespace(const std::string& id) {
    for (auto it = areas_.begin(); it != areas_.end();) {
      if (it->first.first == id)
        it = areas_.erase(it);
      else
        ++it;
    }
    namespaces_.erase(id);
    for (auto it = frames_.begin(); it != frames_.end();) {
      if (it->second.namespace_id == id)
        it = frames_.erase(it);
      else
        ++it;
    }
  }
  void RegisterFrame(std::string token, std::string id, std::string key) {
    if (namespaces_.contains(id))
      frames_[std::move(token)] = {std::move(id), std::move(key)};
  }
  void RemoveFrame(const std::string& token) { frames_.erase(token); }
  void CloneNamespace(const std::string& id, const std::string& target) {
    if (target.empty() || target.size() > 256 || namespaces_.contains(target)) {
      Error({}, BE_INVALID_ARGUMENT);
      return;
    }
    CreateNamespace(target);
    // Snapshot the source before insertion, so map insertions cannot change
    // the iteration range. Source pipes are synchronized by Blink's CloneTo.
    std::vector<std::pair<std::string, StorageData>> cloned;
    for (const auto& [name, area] : areas_)
      if (name.first == id)
        cloned.emplace_back(name.second, area->data);
    for (auto& [key, data] : cloned) {
      auto area = std::make_unique<NativeStorageArea>(*this, key, true, false);
      area->data = std::move(data);
      areas_.emplace(std::make_pair(target, key), std::move(area));
    }
  }
  void BindProvider(mojo::PendingReceiver<dom::DomStorageProvider> receiver) {
    providers_.Add(this, std::move(receiver));
  }
  void BindDomStorage(
      mojo::PendingReceiver<dom::DomStorage> receiver,
      mojo::PendingRemote<dom::DomStorageClient> client) override {
    controllers_.Add(this, std::move(receiver));
    if (client)
      clients_.Add(std::move(client));
  }
  void OpenLocalStorage(
      const blink::BlinkStorageKey& key,
      const blink::LocalFrameToken& token,
      mojo::PendingReceiver<dom::StorageArea> receiver) override {
    BindArea(key, token, {}, false, std::move(receiver));
  }
  void BindSessionStorageNamespace(
      const blink::String& id,
      mojo::PendingReceiver<dom::SessionStorageNamespace> receiver) override {
    const auto found = namespaces_.find(id.Utf8());
    if (found != namespaces_.end())
      found->second->Bind(std::move(receiver));
  }
  void BindSessionStorageArea(
      const blink::BlinkStorageKey& key,
      const blink::LocalFrameToken& token,
      const blink::String& id,
      mojo::PendingReceiver<dom::StorageArea> receiver) override {
    BindArea(key, token, id.Utf8(), true, std::move(receiver));
  }

 private:
  void BindArea(const blink::BlinkStorageKey& key,
                const blink::LocalFrameToken& token,
                const std::string& id,
                bool session,
                mojo::PendingReceiver<dom::StorageArea> receiver) {
    used_ = true;
    if (client_.mode == BE_STORAGE_DISABLED ||
        key.GetSecurityOrigin()->IsOpaque())
      return;
    const blink::StorageKey native_key = key;
    const std::string serialized = native_key.Serialize();
    const auto frame = frames_.find(token.ToString());
    if (frame == frames_.end() || frame->second.key != serialized ||
        (session &&
         (frame->second.namespace_id != id || !namespaces_.contains(id)))) {
      Error(serialized, BE_INVALID_STATE);
      return;
    }
    const auto name = std::make_pair(session ? id : std::string(), serialized);
    auto& area = areas_[name];
    if (!area) {
      // Nonced storage keys are ephemeral even when host persistence is
      // enabled.
      area = std::make_unique<NativeStorageArea>(
          *this, serialized, session,
          !session && !native_key.nonce() &&
              client_.mode == BE_STORAGE_HOST_PERSISTENT);
    }
    area->Bind(std::move(receiver));
  }
  base::raw_ref<std::atomic<be_status>> status_;
  be_storage_client client_{};
  bool used_ = false;
  std::map<std::string, RegisteredFrame> frames_;
  std::map<std::string, std::unique_ptr<NativeSessionNamespace>> namespaces_;
  std::map<std::pair<std::string, std::string>,
           std::unique_ptr<NativeStorageArea>>
      areas_;
  mojo::ReceiverSet<dom::DomStorageProvider> providers_;
  mojo::ReceiverSet<dom::DomStorage> controllers_;
  mojo::RemoteSet<dom::DomStorageClient> clients_;
};

NativeStorageArea::NativeStorageArea(StorageBackend& owner,
                                     std::string key,
                                     bool session,
                                     bool persistent)
    : data(session),
      owner_(owner),
      key_(std::move(key)),
      session_(session),
      persistent_(persistent) {
  if (persistent_)
    readable_ = owner_->Load(key_, data);
}
void NativeStorageArea::Bind(mojo::PendingReceiver<dom::StorageArea> receiver) {
  receivers_.Add(this, std::move(receiver));
}
void NativeStorageArea::AddObserver(
    mojo::PendingRemote<dom::StorageAreaObserver> observer) {
  if (!observer)
    return;
  mojo::Remote<dom::StorageAreaObserver> remote(std::move(observer));
  if (!session_)
    remote->ShouldSendOldValueOnMutations(false);
  observers_.Add(std::move(remote));
}
bool NativeStorageArea::Commit(StorageData candidate) {
  if (!readable_)
    return false;
  if (persistent_ && !owner_->Save(key_, candidate))
    return false;
  data = std::move(candidate);
  return true;
}
void NativeStorageArea::FailedKey(const blink::Vector<uint8_t>& key,
                                  const dom::StorageAreaSourcePtr& source) {
  if (!session_)
    for (const auto& observer : observers_)
      observer->KeyChangeFailed(key, source.Clone());
}
void NativeStorageArea::Put(const blink::Vector<uint8_t>& key,
                            const blink::Vector<uint8_t>& value,
                            const std::optional<blink::Vector<uint8_t>>&,
                            dom::StorageAreaSourcePtr source,
                            PutCallback callback) {
  const auto encoded_key = Bytes(key);
  const auto old = data.Get(encoded_key);
  StorageData candidate = data;
  if (!candidate.Put(encoded_key, Bytes(value))) {
    owner_->Error(key_, BE_RESOURCE_ERROR);
    FailedKey(key, source);
    std::move(callback).Run(false);
    return;
  }
  if (!Commit(std::move(candidate))) {
    FailedKey(key, source);
    std::move(callback).Run(false);
    return;
  }
  if (!session_) {
    std::optional<blink::Vector<uint8_t>> previous;
    if (old)
      previous = BlinkBytes(*old);
    // An unchanged value still needs acknowledgement to clear Blink's pending
    // mutation, even though Blink suppresses the corresponding DOM event.
    for (const auto& observer : observers_)
      observer->KeyChanged(key, value, previous, source.Clone());
  }
  std::move(callback).Run(true);
}
void NativeStorageArea::Delete(const blink::Vector<uint8_t>& key,
                               const std::optional<blink::Vector<uint8_t>>&,
                               dom::StorageAreaSourcePtr source,
                               DeleteCallback callback) {
  const auto encoded = Bytes(key);
  const auto old = data.Get(encoded);
  StorageData candidate = data;
  candidate.Delete(encoded);
  if (!Commit(std::move(candidate))) {
    FailedKey(key, source);
  } else if (!session_) {
    std::optional<blink::Vector<uint8_t>> previous;
    if (old)
      previous = BlinkBytes(*old);
    for (const auto& observer : observers_)
      observer->KeyDeleted(key, previous, source.Clone());
  }
  std::move(callback).Run();
}
void NativeStorageArea::DeleteAll(
    dom::StorageAreaSourcePtr source,
    mojo::PendingRemote<dom::StorageAreaObserver> new_observer,
    DeleteAllCallback callback) {
  AddObserver(std::move(new_observer));
  const bool was_nonempty = !data.empty();
  StorageData candidate = data;
  candidate.Clear();
  if (!Commit(std::move(candidate))) {
    // The StorageArea protocol has no failed-clear notification. Resetting the
    // native local caches reloads the unchanged backend snapshot instead.
    owner_->ResetLocalClients();
  } else if (!session_) {
    for (const auto& observer : observers_)
      observer->AllDeleted(was_nonempty, source.Clone());
  }
  std::move(callback).Run();
}
void NativeStorageArea::GetAll(
    mojo::PendingRemote<dom::StorageAreaObserver> new_observer,
    GetAllCallback callback) {
  blink::Vector<dom::KeyValuePtr> snapshot;
  if (readable_)
    for (const auto& [key, value] : data.entries())
      snapshot.push_back(
          dom::KeyValue::New(BlinkBytes(key), BlinkBytes(value)));
  AddObserver(std::move(new_observer));
  std::move(callback).Run(std::move(snapshot));
}
void NativeSessionNamespace::Clone(const blink::String& target) {
  owner_->CloneNamespace(id_, target.Utf8());
}

class StorageRouting final
    : public base::RefCountedDeleteOnSequence<StorageRouting> {
 public:
  explicit StorageRouting(scoped_refptr<base::SingleThreadTaskRunner> runner)
      : base::RefCountedDeleteOnSequence<StorageRouting>(runner),
        runner(std::move(runner)) {}
  const scoped_refptr<base::SingleThreadTaskRunner> runner;
  std::atomic<be_status> status{BE_OK};
  std::atomic<bool> active{true};
  std::atomic<bool> enabled{true};
  std::unique_ptr<StorageBackend> backend;  // IO sequence only.

 private:
  friend class base::RefCountedDeleteOnSequence<StorageRouting>;
  friend class base::DeleteHelper<StorageRouting>;
  ~StorageRouting() = default;
};

template <typename Function>
be_status OnStorageThread(const scoped_refptr<StorageRouting>& route,
                          Function function) {
  if (route->runner->RunsTasksInCurrentSequence())
    return function(*route);
  std::mutex mutex;
  std::condition_variable condition;
  bool complete = false;
  be_status result = BE_INVALID_STATE;
  if (!route->runner->PostTask(
          FROM_HERE,
          base::BindOnce(
              [](scoped_refptr<StorageRouting> route, Function function,
                 be_status* output, std::mutex* mutex,
                 std::condition_variable* condition, bool* complete) {
                *output = function(*route);
                {
                  std::lock_guard<std::mutex> lock(*mutex);
                  *complete = true;
                  condition->notify_one();
                }
              },
              route, std::move(function), &result, &mutex, &condition,
              &complete)))
    return BE_INVALID_STATE;
  std::unique_lock<std::mutex> lock(mutex);
  condition.wait(lock, [&complete] { return complete; });
  return result;
}

class StorageBroker final
    : public blink::ThreadSafeBrowserInterfaceBrokerProxy {
 public:
  explicit StorageBroker(scoped_refptr<StorageRouting> route)
      : route_(std::move(route)) {}

 private:
  ~StorageBroker() override = default;
  void GetInterfaceImpl(mojo::GenericPendingReceiver receiver) override {
    if (!route_->active.load() ||
        receiver.interface_name() != dom::DomStorageProvider::Name_)
      return;  // Other browser services have no implicit fallback.
    route_->runner->PostTask(
        FROM_HERE, base::BindOnce(
                       [](scoped_refptr<StorageRouting> route,
                          mojo::GenericPendingReceiver receiver) {
                         if (route->active.load() && route->backend)
                           route->backend->BindProvider(
                               receiver.As<dom::DomStorageProvider>());
                       },
                       route_, std::move(receiver)));
  }
  const scoped_refptr<StorageRouting> route_;
};

be_status FlushDocument(blink::LocalDOMWindow& window,
                        const scoped_refptr<StorageRouting>& route) {
  if (!route->active.load())
    return BE_INVALID_STATE;
  auto* frame = window.GetFrame();
  if (!route->enabled.load() || !frame)
    return route->status.load();
  bool succeeded = true;
  // Query the real renderer caches' Mojo pipes. A synchronous GetAll ordered
  // after Put/Delete/DeleteAll ensures those mutations and host saves completed
  // on IO. A task-runner barrier alone cannot order unread Mojo messages.
  blink::Vector<dom::KeyValuePtr> ignored;
  if (window.GetSecurityOrigin()->CanAccessLocalStorage() &&
      frame->GetSettings()->GetLocalStorageEnabled()) {
    auto area =
        blink::StorageController::GetInstance()->GetLocalStorageArea(&window);
    succeeded &= area->RemoteArea()->GetAll({}, &ignored);
  }
  if (window.GetSecurityOrigin()->CanAccessSessionStorage()) {
    if (auto* ns = blink::StorageNamespace::From(frame->GetPage())) {
      ignored.clear();
      auto area = ns->GetCachedArea(&window);
      succeeded &= area->RemoteArea()->GetAll({}, &ignored);
    }
  }
  if (!succeeded) {
    const blink::StorageKey key = window.GetStorageKey();
    const std::string serialized =
        key.origin().opaque() ? std::string() : key.Serialize();
    OnStorageThread(route, [&serialized](StorageRouting& value) {
      if (value.backend)
        value.backend->Error(serialized, BE_RESOURCE_ERROR);
      return BE_RESOURCE_ERROR;
    });
  }
  return route->status.load();
}

class StorageContextObserver final
    : public blink::GarbageCollected<StorageContextObserver>,
      public blink::Supplement<blink::LocalDOMWindow>,
      public blink::ExecutionContextLifecycleObserver {
 public:
  static const char kSupplementName[];
  StorageContextObserver(blink::LocalDOMWindow& window,
                         scoped_refptr<StorageRouting> route)
      : blink::Supplement<blink::LocalDOMWindow>(window),
        blink::ExecutionContextLifecycleObserver(&window),
        route_(std::move(route)) {}
  void ContextDestroyed() override {
    // LocalDOMWindow::FrameDestroyed calls observers after unload/abort script
    // but before DisconnectFromFrame. No JS is executed by the sync pipe drain.
    FlushDocument(*GetSupplementable(), route_);
  }
  void Trace(blink::Visitor* visitor) const override {
    blink::Supplement<blink::LocalDOMWindow>::Trace(visitor);
    blink::ExecutionContextLifecycleObserver::Trace(visitor);
  }

 private:
  const scoped_refptr<StorageRouting> route_;
};
const char StorageContextObserver::kSupplementName[] =
    "StandaloneStorageLifetime";
}  // namespace

class StorageHost::Impl {
 public:
  explicit Impl(scoped_refptr<base::SingleThreadTaskRunner> runner)
      : route(base::MakeRefCounted<StorageRouting>(std::move(runner))),
        broker(base::MakeRefCounted<StorageBroker>(route)) {
    CHECK_EQ(OnStorageThread(route,
                             [](StorageRouting& value) {
                               value.backend = std::make_unique<StorageBackend>(
                                   value.status);
                               return BE_OK;
                             }),
             BE_OK);
  }
  scoped_refptr<StorageRouting> route;
  scoped_refptr<StorageBroker> broker;
  std::map<std::string, std::string> frame_namespaces;
  uint64_t next_namespace = 0;
};

StorageHost::StorageHost(scoped_refptr<base::SingleThreadTaskRunner> runner)
    : impl_(std::make_unique<Impl>(std::move(runner))) {}
StorageHost::~StorageHost() {
  Shutdown();
}
blink::ThreadSafeBrowserInterfaceBrokerProxy* StorageHost::Broker() const {
  return impl_->broker.get();
}
be_status StorageHost::Configure(const be_storage_client* client) {
  const auto status = ValidateStorageClient(client);
  if (status != BE_OK)
    return status;
  if (!impl_->route->active.load())
    return BE_INVALID_STATE;
  const auto copy =
      client ? std::optional<be_storage_client>(*client) : std::nullopt;
  return OnStorageThread(impl_->route, [copy](StorageRouting& route) {
    const auto status = route.backend->Configure(copy ? &*copy : nullptr);
    if (status == BE_OK)
      route.enabled.store(!copy || copy->mode != BE_STORAGE_DISABLED);
    return status;
  });
}
bool StorageHost::enabled() const {
  return impl_->route->enabled.load();
}
std::string StorageHost::CreateSessionNamespace(blink::Page& page) {
  const std::string id =
      "be-session-" + std::to_string(++impl_->next_namespace);
  if (!enabled())
    return id;
  const auto status =
      OnStorageThread(impl_->route, [&id](StorageRouting& route) {
        if (!route.backend)
          return BE_INVALID_STATE;
        route.backend->CreateNamespace(id);
        return BE_OK;
      });
  if (status == BE_OK)
    blink::StorageNamespace::ProvideSessionStorageNamespaceTo(page, id);
  return id;
}
void StorageHost::RegisterFrame(const blink::LocalFrameToken& token,
                                const std::string& namespace_id,
                                const blink::StorageKey& key) {
  const std::string serialized =
      key.origin().opaque() ? std::string() : key.Serialize();
  impl_->frame_namespaces[token.ToString()] = namespace_id;
  OnStorageThread(impl_->route, [&](StorageRouting& route) {
    if (route.backend)
      route.backend->RegisterFrame(token.ToString(), namespace_id, serialized);
    return BE_OK;
  });
}
void StorageHost::ObserveDocument(blink::LocalDOMWindow& window) {
  auto* frame = window.GetFrame();
  if (!frame || !impl_->route->active.load() || !enabled())
    return;
  const auto token = frame->GetLocalFrameToken();
  const auto found = impl_->frame_namespaces.find(token.ToString());
  if (found != impl_->frame_namespaces.end())
    RegisterFrame(token, found->second, window.GetStorageKey());
  if (!blink::Supplement<blink::LocalDOMWindow>::From<StorageContextObserver>(
          window))
    blink::Supplement<blink::LocalDOMWindow>::ProvideTo(
        window, blink::MakeGarbageCollected<StorageContextObserver>(
                    window, impl_->route));
}
void StorageHost::RemoveFrame(const blink::LocalFrameToken& token) {
  OnStorageThread(impl_->route, [&](StorageRouting& route) {
    if (route.backend)
      route.backend->RemoveFrame(token.ToString());
    return BE_OK;
  });
  impl_->frame_namespaces.erase(token.ToString());
}
void StorageHost::RemoveSessionNamespace(const std::string& id) {
  OnStorageThread(impl_->route, [&](StorageRouting& route) {
    if (route.backend)
      route.backend->RemoveNamespace(id);
    return BE_OK;
  });
}
be_status StorageHost::FlushFrame(blink::LocalFrame& frame) {
  return frame.DomWindow() ? FlushDocument(*frame.DomWindow(), impl_->route)
                           : last_status();
}
be_status StorageHost::last_status() const {
  return impl_->route->status.load();
}
void StorageHost::Shutdown() {
  if (!impl_->route->active.exchange(false))
    return;
  OnStorageThread(impl_->route, [](StorageRouting& route) {
    route.backend.reset();
    return BE_OK;
  });
}

}  // namespace blink_standalone
