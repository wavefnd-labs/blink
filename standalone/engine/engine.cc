// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
// Frame setup follows upstream initialization ordering; see adjacent NOTICE.
#include "standalone/include/blink_engine.h"

#include <atomic>
#include <cstring>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <thread>
#include <tuple>
#include <unordered_set>
#include <vector>
#include "base/at_exit.h"
#include "base/command_line.h"
#include "base/compiler_specific.h"
#include "base/containers/span.h"
#include "base/feature_list.h"
#include "base/files/file.h"
#include "base/files/file_path.h"
#include "base/files/file_util.h"
#include "base/files/memory_mapped_file.h"
#include "base/i18n/icu_util.h"
#include "base/memory/raw_ptr.h"
#include "base/memory/raw_ref.h"
#include "base/run_loop.h"
#include "base/task/thread_pool/thread_pool_instance.h"
#include "base/unguessable_token.h"
#include "cc/paint/skia_paint_canvas.h"
#include "gin/v8_initializer.h"
#include "mojo/core/embedder/embedder.h"
#include "mojo/public/cpp/bindings/associated_remote.h"
#include "mojo/public/cpp/bindings/binder_map.h"
#include "mojo/public/cpp/bindings/pending_remote.h"
#include "net/base/schemeful_site.h"
#include "services/metrics/public/cpp/ukm_source_id.h"
#include "services/network/public/cpp/web_sandbox_flags.h"
#include "standalone/common/limits.h"
#include "standalone/common/navigation.h"
#include "standalone/engine/ime.h"
#include "standalone/engine/input.h"
#include "standalone/engine/navigation_policy.h"
#include "standalone/engine/script.h"
#include "standalone/include/blink_storage.h"
#include "standalone/platform/discardable_memory.h"
#include "standalone/platform/host_platform.h"
#include "standalone/platform/host_url_loader.h"
#include "standalone/platform/storage_host.h"
#include "standalone/platform/worker.h"
#include "third_party/blink/public/common/metrics/document_update_reason.h"
#include "third_party/blink/public/common/page/color_provider_color_maps.h"
#include "third_party/blink/public/common/storage_key/storage_key.h"
#include "third_party/blink/public/common/tokens/tokens.h"
#include "third_party/blink/public/mojom/commit_result/commit_result.mojom-shared.h"
#include "third_party/blink/public/mojom/frame/policy_container.mojom-blink.h"
#include "third_party/blink/public/platform/scheduler/web_agent_group_scheduler.h"
#include "third_party/blink/public/platform/scheduler/web_thread_scheduler.h"
#include "third_party/blink/public/platform/web_string.h"
#include "third_party/blink/public/platform/web_worker_fetch_context.h"
#include "third_party/blink/public/resources/grit/blink_resources.h"
#include "third_party/blink/public/web/blink.h"
#include "third_party/blink/public/web/web_navigation_params.h"
#include "third_party/blink/renderer/core/core_initializer.h"
#include "third_party/blink/renderer/core/dom/document.h"
#include "third_party/blink/renderer/core/execution_context/security_context.h"
#include "third_party/blink/renderer/core/frame/local_dom_window.h"
#include "third_party/blink/renderer/core/frame/local_frame.h"
#include "third_party/blink/renderer/core/frame/local_frame_view.h"
#include "third_party/blink/renderer/core/frame/policy_container.h"
#include "third_party/blink/renderer/core/frame/settings.h"
#include "third_party/blink/renderer/core/frame/visual_viewport.h"
#include "third_party/blink/renderer/core/html/html_frame_owner_element.h"
#include "third_party/blink/renderer/core/html/html_iframe_element.h"
#include "third_party/blink/renderer/core/html_names.h"
#include "third_party/blink/renderer/core/loader/document_loader.h"
#include "third_party/blink/renderer/core/loader/empty_clients.h"
#include "third_party/blink/renderer/core/loader/frame_load_request.h"
#include "third_party/blink/renderer/core/loader/frame_loader.h"
#include "third_party/blink/renderer/core/loader/navigation_policy.h"
#include "third_party/blink/renderer/core/navigation_api/navigation_api.h"
#include "third_party/blink/renderer/core/page/focus_controller.h"
#include "third_party/blink/renderer/core/page/frame_tree.h"
#include "third_party/blink/renderer/core/paint/paint_flags.h"
#include "third_party/blink/renderer/platform/exported/wrapped_resource_request.h"
#include "third_party/blink/renderer/platform/fonts/font_cache.h"
#include "third_party/blink/renderer/platform/graphics/color.h"
#include "third_party/blink/renderer/platform/graphics/paint/cull_rect.h"
#include "third_party/blink/renderer/platform/graphics/paint/paint_record_builder.h"
#include "third_party/blink/renderer/platform/heap/garbage_collected.h"
#include "third_party/blink/renderer/platform/heap/persistent.h"
#include "third_party/blink/renderer/platform/loader/fetch/policy_container_utils.h"
#include "third_party/blink/renderer/platform/loader/fetch/resource_request.h"
#include "third_party/blink/renderer/platform/network/encoded_form_data.h"
#include "third_party/blink/renderer/platform/network/http_header_map.h"
#include "third_party/blink/renderer/platform/scheduler/public/agent_group_scheduler.h"
#include "third_party/blink/renderer/platform/weborigin/security_origin.h"
#include "third_party/blink/renderer/platform/weborigin/security_policy.h"
#include "third_party/blink/renderer/platform/wtf/text/atomic_string.h"
#include "third_party/blink/renderer/platform/wtf/vector.h"
#include "third_party/skia/include/core/SkBitmap.h"
#include "third_party/skia/include/core/SkColor.h"
#include "third_party/skia/include/core/SkImageInfo.h"
#include "tools/v8_context_snapshot/buildflags.h"
#include "ui/base/resource/data_pack.h"
#include "ui/base/resource/resource_bundle.h"
#include "ui/gfx/geometry/rect.h"
#include "ui/gfx/geometry/size.h"
#include "url/gurl.h"
#include "url/origin.h"

using blink_standalone::HostMessagePump;
using blink_standalone::HostPlatform;
using blink_standalone::HostServices;

struct be_engine {
  explicit be_engine(const be_engine_config& config)
      : owner(std::this_thread::get_id()), services(config), platform(config) {}
  const std::thread::id owner;
  HostServices services;
  HostPlatform platform;
  std::unique_ptr<base::AtExitManager> at_exit;
  std::unique_ptr<blink::scheduler::WebThreadScheduler> scheduler;
  base::raw_ptr<HostMessagePump> pump = nullptr;
  mojo::BinderMap binders;
  base::File icu_file;
  std::unordered_set<base::raw_ptr<be_page>> pages;
  bool active = false;
  bool in_call = false;
};

namespace {
std::atomic<be_engine*> g_engine{nullptr};
std::mutex g_initialization;

class ApiCall {
 public:
  explicit ApiCall(be_engine* engine) : engine_(engine) {
    if (!engine || g_engine.load(std::memory_order_acquire) != engine)
      status_ = BE_INVALID_ARGUMENT;
    else if (engine->owner != std::this_thread::get_id())
      status_ = BE_WRONG_THREAD;
    else if (!engine->active)
      status_ = BE_INVALID_STATE;
    else if (engine->in_call)
      status_ = BE_BUSY;
    else {
      engine->in_call = true;
      entered_ = true;
    }
  }
  ~ApiCall() {
    if (entered_)
      engine_->in_call = false;
  }
  be_status status() const { return status_; }

 private:
  base::raw_ptr<be_engine> engine_;
  be_status status_ = BE_OK;
  bool entered_ = false;
};

template <typename T>
be_status CheckConfig(const T* config) {
  if (!config || config->struct_size < sizeof(T))
    return BE_INVALID_ARGUMENT;
  return config->abi_version == BE_ABI_VERSION ? BE_OK : BE_ABI_MISMATCH;
}
bool ValidFile(const char* path) {
  if (!path)
    return false;
  const base::FilePath file(path);
  const auto size = base::GetFileSize(file);
  return file.IsAbsolute() && size.has_value() && *size > 0 &&
         !base::DirectoryExists(file);
}
}  // namespace

struct StaticDocument {
  std::string body;
  std::string mime = "text/html";
  std::string charset = "UTF-8";
  std::vector<std::pair<std::string, std::string>> headers;
  uint32_t status = 200;
};
struct NavigationRequest {
  std::string url;
  std::string method = "GET";
  std::string headers;
  std::string body;
  std::string referrer;
  blink::WebSecurityOrigin requestor_origin;
  blink::WebSecurityOrigin committed_origin;
  blink::WebURL fallback_base_url;
  std::optional<blink::WebPolicyContainerPolicies> inherited_policy;
  network::mojom::ReferrerPolicy referrer_policy =
      network::mojom::ReferrerPolicy::kDefault;
  blink::WebFrameLoadType load_type = blink::WebFrameLoadType::kStandard;
  blink::WebHistoryItem item;
  std::shared_ptr<const StaticDocument> supplied;
  int history_target = -1;
  uint64_t id = 0;
};

struct FrameState;
struct be_page {
  base::raw_ptr<be_engine> engine = nullptr;
  uint32_t width = 0;
  uint32_t height = 0;
  bool alive = false;
  be_navigation_client navigation_client{};
  std::string storage_namespace;
  std::unique_ptr<blink::scheduler::WebAgentGroupScheduler> agent_group;
  blink::Persistent<blink::Page> page;
  blink::Persistent<FrameState> main;
  std::vector<blink::Persistent<FrameState>> frames;
};

// One lifecycle/navigation state for each real LocalFrame. A detached frame's
// state is kept alive only by Blink callbacks already on the stack, not by an
// accumulating list of native tombstone handles.
struct FrameState final : public blink::GarbageCollected<FrameState> {
  explicit FrameState(be_page& page) : owner(page) {}
  void Trace(blink::Visitor* visitor) const { visitor->Trace(frame); }
  const base::raw_ref<be_page> owner;
  blink::Member<blink::LocalFrame> frame;
  bool alive = true;
  bool main = false;
  blink_standalone::SessionHistory<NavigationRequest> history;
  std::optional<NavigationRequest> pending;
  std::optional<NavigationRequest> current;
  std::string script_result;
  std::string state_url;
  blink::Vector<blink::String> retired_history_keys;
  uint64_t next_navigation_id = 0;
  be_status navigation_status = BE_OK;
  bool loading = false;
  bool suppress_navigation = false;
};

namespace {
void Notify(FrameState& page,
            uint32_t type,
            be_status status,
            const std::string& url,
            uint64_t id) {
  page.navigation_status = status;
  if (!page.main)
    return;
  const auto& client = page.owner->navigation_client;
  if (client.event) {
    const be_navigation_event event{sizeof(event), BE_ABI_VERSION, id,
                                    type,          status,         url.c_str()};
    client.event(client.user_data, &*page.owner, &event);
  }
}
bool NavigationUrl(const GURL& url) {
  return url.is_valid() && !url.has_username() && !url.has_password() &&
         (url.SchemeIsHTTPOrHTTPS() || url.spec() == "about:blank");
}
void QueueNavigation(FrameState& page, NavigationRequest request) {
  if (page.suppress_navigation || !page.alive || !page.owner->alive)
    return;
  if (page.pending)
    Notify(page, BE_NAV_CANCELLED, BE_OK, page.pending->url, page.pending->id);
  request.id = ++page.next_navigation_id;
  page.pending = std::move(request);
  Notify(page, BE_NAV_QUEUED, BE_OK, page.pending->url, page.pending->id);
  page.owner->engine->services.Wake(0);
}
bool QueueHistory(FrameState& page, int32_t offset) {
  const int target = page.history.Target(offset);
  if (target < 0)
    return false;
  auto request = *page.history.At(target);
  request.history_target = target;
  request.load_type = offset == 0 ? blink::WebFrameLoadType::kReload
                                  : blink::WebFrameLoadType::kBackForward;
  QueueNavigation(page, std::move(request));
  return true;
}
void RecordCommit(FrameState& page,
                  blink::HistoryItem* item,
                  blink::WebHistoryCommitType type,
                  bool same_document) {
  if (!page.alive || page.suppress_navigation || !item)
    return;
  NavigationRequest entry;
  if (same_document && page.history.Current())
    entry = *page.history.Current();
  else if (page.current)
    entry = *page.current;
  entry.url = page.frame->GetDocument()->Url().GetString().Utf8();
  entry.item = item;
  entry.committed_origin =
      blink::WebSecurityOrigin(scoped_refptr<const blink::SecurityOrigin>(
          page.frame->DomWindow()->GetSecurityOrigin()));
  if (same_document) {
    entry.id = type == blink::kWebBackForwardCommit && page.current
                   ? page.current->id
                   : ++page.next_navigation_id;
    auto* loader = page.frame->Loader().GetDocumentLoader();
    if (loader) {
      entry.method = loader->HttpMethod().Utf8();
      if (entry.method == "GET") {
        entry.body.clear();
        entry.headers.clear();
      }
    }
  }
  const int target = page.current && type == blink::kWebBackForwardCommit
                         ? page.current->history_target
                         : -1;
  if (type == blink::kWebStandardCommit &&
      page.history.size() == page.history.kCapacity &&
      page.history.index() == static_cast<int>(page.history.size()) - 1)
    page.retired_history_keys.push_back(
        page.history.At(0)->item.GetNavigationApiKey());
  page.history.Commit(std::move(entry), type != blink::kWebStandardCommit,
                      target);
  if (same_document) {
    page.current = *page.history.Current();
    Notify(page, BE_NAV_SAME_DOCUMENT, BE_OK, page.history.Current()->url,
           page.current->id);
  } else {
    page.script_result.clear();
    Notify(page, BE_NAV_COMMITTED, BE_OK, page.history.Current()->url,
           page.current ? page.current->id : 0);
  }
}
void RejectNavigation(FrameState& page,
                      be_status status,
                      const std::string& url) {
  const auto id = ++page.next_navigation_id;
  page.frame->DomWindow()->navigation()->InformAboutCanceledNavigation();
  Notify(page, BE_NAV_FAILED, status, url, id);
}

bool SupportedFrameOwner(blink::FrameOwner* owner) {
  auto* element = blink::DynamicTo<blink::HTMLFrameOwnerElement>(owner);
  return !element || (!element->Credentialless() &&
                      !element->FastHasAttribute(blink::html_names::kCspAttr) &&
                      !element->FastHasAttribute(
                          blink::html_names::kConnectionallowlistAttr));
}

class HostFrameClient final : public blink::EmptyLocalFrameClient {
 public:
  HostFrameClient(FrameState& page, HostServices& services)
      : page_(&page), services_(services) {}
  std::unique_ptr<blink::URLLoader> CreateURLLoaderForTesting() override {
    // This production injection point precedes the network IPC factory.
    return blink_standalone::MakeHostURLLoader(*services_);
  }
  bool IsDomStorageDisabled() const override {
    return !page_->owner->engine->platform.storage().enabled();
  }
  bool AreDedicatedWorkersDisabled() const override { return false; }
  scoped_refptr<blink::WebWorkerFetchContext> CreateWorkerFetchContext(
      blink::WebDedicatedWorkerHostFactoryClient* factory) override {
    return page_->owner->engine->platform.workers().CreateFetchContext(factory);
  }
  void Trace(blink::Visitor* visitor) const override {
    visitor->Trace(page_);
    blink::EmptyLocalFrameClient::Trace(visitor);
  }
  void DispatchDidClearWindowObjectInMainWorld(
      blink::LocalDOMWindow* window) override {
    if (window)
      page_->owner->engine->platform.storage().ObserveDocument(*window);
  }
  void WillBeDetached() override {
    if (page_->frame) {
      auto& storage = page_->owner->engine->platform.storage();
      storage.RemoveFrame(page_->frame->GetLocalFrameToken());
    }
  }
  void Detached(blink::FrameDetachType) override {
    page_->alive = false;
    page_->pending.reset();
    page_->current.reset();
    page_->history.Clear();
    page_->retired_history_keys.clear();
    page_->script_result.clear();
    page_->loading = false;
    if (page_->frame && page_->frame->Parent())
      page_->frame->Parent()->RemoveChild(page_->frame);
    page_->frame = nullptr;
  }
  blink::LocalFrame* CreateFrame(const blink::AtomicString& name,
                                 blink::HTMLFrameOwnerElement* owner) override;
  unsigned BackForwardLength() override {
    return static_cast<unsigned>(page_->history.size());
  }
  bool NavigateBackForward(
      int offset,
      base::TimeTicks,
      std::optional<blink::scheduler::TaskAttributionId>) const override {
    return QueueHistory(*page_, offset);
  }
  void DispatchDidCommitLoad(
      blink::HistoryItem* item,
      blink::WebHistoryCommitType type,
      bool,
      const network::ParsedPermissionsPolicy&,
      const blink::DocumentPolicyFeatureState&) override {
    RecordCommit(*page_, item, type, false);
  }
  void DidFinishSameDocumentNavigation(
      blink::WebHistoryCommitType type,
      bool,
      blink::mojom::blink::SameDocumentNavigationType,
      bool,
      bool,
      bool,
      base::UnguessableToken,
      bool) override {
    auto* loader = page_->frame->Loader().GetDocumentLoader();
    if (loader)
      RecordCommit(*page_, loader->GetHistoryItem(), type, true);
  }
  void DidUpdateCurrentHistoryItem() override {
    auto* loader = page_->frame->Loader().GetDocumentLoader();
    if (page_->alive && loader && page_->history.Current()) {
      auto entry = *page_->history.Current();
      entry.item = loader->GetHistoryItem();
      page_->history.Commit(std::move(entry), true);
    }
  }
  void DispatchDidFinishLoad() override {
    if (!page_->alive || page_->suppress_navigation || !page_->current ||
        !page_->loading)
      return;
    page_->loading = false;
    Notify(*page_, BE_NAV_FINISHED, BE_OK,
           page_->frame->GetDocument()->Url().GetString().Utf8(),
           page_->current->id);
  }
  void DispatchDidFailLoad(const blink::ResourceError& error,
                           blink::WebHistoryCommitType) override {
    if (!page_->alive || page_->suppress_navigation || !page_->current ||
        !page_->loading)
      return;
    page_->loading = false;
    Notify(*page_, error.IsCancellation() ? BE_NAV_CANCELLED : BE_NAV_FAILED,
           BE_RESOURCE_ERROR, page_->current->url, page_->current->id);
  }
  void BeginNavigation(
      const blink::ResourceRequest& request,
      const blink::KURL& requestor_base_url,
      blink::mojom::RequestContextFrameType,
      blink::LocalDOMWindow*,
      blink::DocumentLoader*,
      blink::WebNavigationType,
      blink::NavigationPolicy policy,
      blink::WebFrameLoadType load_type,
      blink::mojom::blink::ForceHistoryPush,
      bool,
      bool,
      blink::mojom::blink::TriggeringEventInfo,
      blink::HTMLFormElement*,
      network::mojom::CSPDisposition,
      mojo::PendingRemote<blink::mojom::blink::BlobURLToken>,
      base::TimeTicks,
      base::TimeTicks,
      const blink::String&,
      const blink::LocalFrameToken* initiator_frame_token,
      const blink::InitiatorStateToken&,
      const blink::DocumentToken&,
      blink::SourceLocation*,
      mojo::PendingRemote<blink::mojom::blink::NavigationStateKeepAliveHandle>,
      bool,
      bool,
      mojo::PendingReceiver<
          blink::mojom::blink::NavigationResumeDeferredCommitListener>,
      std::optional<blink::ToolInvocationToken>,
      const blink::String&) override {
    if (!page_->alive || page_->suppress_navigation || !page_->owner->alive)
      return;
    if (!SupportedFrameOwner(page_->frame->Owner())) {
      RejectNavigation(*page_, BE_NOT_SUPPORTED,
                       request.Url().GetString().Utf8());
      return;
    }
    NavigationRequest pending;
    pending.url = request.Url().GetString().Utf8();
    pending.method = request.HttpMethod().Utf8();
    pending.referrer = request.ReferrerString().Utf8();
    pending.referrer_policy = request.GetReferrerPolicy();
    pending.fallback_base_url = blink::WebURL(requestor_base_url);
    if (request.RequestorOrigin())
      pending.requestor_origin =
          blink::WebSecurityOrigin(request.RequestorOrigin());
    pending.load_type = load_type;
    if (pending.url == "about:blank" || pending.url == "about:srcdoc") {
      auto* initiator =
          initiator_frame_token
              ? blink::LocalFrame::FromFrameToken(*initiator_frame_token)
              : nullptr;
      if (!page_->main && pending.url == "about:srcdoc")
        initiator = blink::DynamicTo<blink::LocalFrame>(page_->frame->Parent());
      if (initiator && initiator->DomWindow() &&
          initiator->DomWindow()->GetPolicyContainer())
        pending.inherited_policy = blink::ToWebPolicyContainerPolicies(
            initiator->DomWindow()->GetPolicyContainer()->GetPolicies());
    }
    if (policy != blink::kNavigationPolicyCurrentTab ||
        !(NavigationUrl(GURL(pending.url)) ||
          (!page_->main && pending.url == "about:srcdoc")) ||
        !blink_standalone::HeaderToken(pending.method)) {
      RejectNavigation(*page_, BE_NOT_SUPPORTED, pending.url);
      return;
    }
    for (const auto& field : request.HttpHeaderFields()) {
      pending.headers += field.key.Utf8() + ": " + field.value.Utf8() + "\r\n";
      if (pending.headers.size() > blink_standalone::kMaxNavigationHeaders) {
        RejectNavigation(*page_, BE_RESOURCE_ERROR, pending.url);
        return;
      }
    }
    if (request.HttpBody()) {
      // Never silently omit file/blob/stream parts from a form submission.
      for (const auto& element : request.HttpBody()->Elements()) {
        if (element.type_ != blink::FormDataElement::kData ||
            element.data_.size() >
                blink_standalone::kMaxResourceBytes - pending.body.size()) {
          RejectNavigation(*page_, BE_NOT_SUPPORTED, pending.url);
          return;
        }
        pending.body.append(element.data_.data(), element.data_.size());
      }
    }
    if (!page_->main && pending.url == "about:srcdoc") {
      auto* iframe = blink::DynamicTo<blink::HTMLIFrameElement>(
          blink::DynamicTo<blink::HTMLFrameOwnerElement>(
              page_->frame->Owner()));
      if (!iframe) {
        RejectNavigation(*page_, BE_NOT_SUPPORTED, pending.url);
        return;
      }
      auto document = std::make_shared<StaticDocument>();
      document->body = iframe->srcdoc().Utf8();
      if (document->body.size() > blink_standalone::kMaxResourceBytes) {
        RejectNavigation(*page_, BE_RESOURCE_ERROR, pending.url);
        return;
      }
      pending.supplied = std::move(document);
    }
    blink::WebNavigationInfo info;
    info.url_request.CopyFrom(blink::WrappedResourceRequest(request));
    info.frame_load_type = load_type;
    const uint64_t generation = page_->next_navigation_id;
    if (!page_->frame->Loader().WillStartNavigation(info) || !page_->alive ||
        page_->next_navigation_id != generation)
      return;
    QueueNavigation(*page_, std::move(pending));
  }

 private:
  const blink::Member<FrameState> page_;
  const base::raw_ref<HostServices> services_;
};

blink::LocalFrame* HostFrameClient::CreateFrame(
    const blink::AtomicString& name,
    blink::HTMLFrameOwnerElement* owner) {
  auto* parent = owner ? owner->GetDocument().GetFrame() : nullptr;
  if (!page_->alive || !parent || !parent->GetPage() ||
      !SupportedFrameOwner(owner) || parent != page_->frame ||
      page_->owner->frames.size() >= 256)
    return nullptr;
  auto* state = blink::MakeGarbageCollected<FrameState>(*page_->owner);
  auto* client =
      blink::MakeGarbageCollected<HostFrameClient>(*state, *services_);
  auto* child = blink::MakeGarbageCollected<blink::LocalFrame>(
      client, *parent->GetPage(), owner, parent, parent->LastChild(),
      blink::FrameInsertType::kInsertInConstructor, blink::LocalFrameToken(),
      &parent->window_agent_factory(), nullptr, mojo::NullRemote());
  state->frame = child;
  page_->owner->frames.emplace_back(state);
  child->Tree().SetName(name);
  child->CreateView(gfx::Size(), blink::Color::kTransparent);
  auto policies =
      parent->DomWindow()->GetPolicyContainer()->GetPolicies().Clone();
  policies->sandbox_flags |= owner->GetFramePolicy().sandbox_flags;
  std::unique_ptr<base::UnguessableToken> sandbox_token;
  if ((policies->sandbox_flags & network::mojom::WebSandboxFlags::kOrigin) !=
      network::mojom::WebSandboxFlags::kNone)
    sandbox_token = std::make_unique<base::UnguessableToken>(
        base::UnguessableToken::Create());
  mojo::AssociatedRemote<blink::mojom::blink::PolicyContainerHost> remote;
  std::ignore = remote.BindNewEndpointAndPassDedicatedReceiver();
  auto container = std::make_unique<blink::PolicyContainer>(
      remote.Unbind(), std::move(policies));
  child->Init(nullptr, blink::DocumentToken(), blink::InitiatorStateToken(),
              std::move(container), parent->DomWindow()->GetStorageKey(),
              ukm::kInvalidSourceId, parent->GetDocument()->BaseURL(),
              std::move(sandbox_token));
  if (state->alive)
    page_->owner->engine->platform.storage().RegisterFrame(
        child->GetLocalFrameToken(), page_->owner->storage_namespace,
        child->DomWindow()->GetStorageKey());
  return state->alive ? child : nullptr;
}

be_status CheckPage(be_page* page, be_engine* engine) {
  // The owning-thread check is performed before touching the registry.
  if (!page || !engine->pages.contains(page))
    return BE_INVALID_ARGUMENT;
  return page->alive ? BE_OK : BE_INVALID_STATE;
}
be_status Layout(be_page* page) {
  auto* view = page->main->frame->View();
  if (!view || !view->UpdateAllLifecyclePhases(
                   blink::DocumentUpdateReason::kBeginMainFrame))
    return BE_RENDER_ERROR;
  return BE_OK;
}
bool CopyText(const char* value, size_t maximum, std::string& out) {
  if (!value) {
    out.clear();
    return true;
  }
  // The host ABI supplies a NUL-terminated string valid for this call. Keep
  // strnlen's bounded scan: a span of maximum + 1 would claim storage beyond
  // a shorter host string, even when its terminator occurs much earlier.
  const size_t size = UNSAFE_BUFFERS(strnlen(value, maximum + 1));
  if (size > maximum)
    return false;
  out.assign(value, size);
  return true;
}
struct NavigationResponseRelease {
  explicit NavigationResponseRelease(be_navigation_response& value)
      : response(value) {}
  const base::raw_ref<be_navigation_response> response;
  ~NavigationResponseRelease() {
    if (response->release)
      response->release(response->release_user_data);
  }
};
struct ResourceResponseRelease {
  explicit ResourceResponseRelease(be_resource_response& value)
      : response(value) {}
  const base::raw_ref<be_resource_response> response;
  ~ResourceResponseRelease() {
    if (response->release)
      response->release(response->release_user_data);
  }
};
be_status FetchDocument(FrameState& page,
                        NavigationRequest& request,
                        StaticDocument& document,
                        std::string& redirect) {
  if (!NavigationUrl(GURL(request.url)))
    return BE_NOT_SUPPORTED;
  if (request.url == "about:blank")
    return BE_OK;
  const std::string transport_url = GURL(request.url).GetWithoutRef().spec();
  std::string transport_headers;
  std::vector<std::pair<std::string, std::string>> fields;
  if (!blink_standalone::ParseNavigationHeaders(request.headers, fields))
    return BE_RESOURCE_ERROR;
  for (const auto& [name, value] : fields)
    if (blink_standalone::NavigationHeaderName(name) != "referer")
      transport_headers += name + ": " + value + "\r\n";
  if (!request.referrer.empty())
    transport_headers += "Referer: " + request.referrer + "\r\n";
  if (transport_headers.size() > blink_standalone::kMaxNavigationHeaders)
    return BE_RESOURCE_ERROR;
  const auto& client = page.owner->navigation_client;
  if (client.fetch) {
    const be_navigation_request input{
        sizeof(input),
        BE_ABI_VERSION,
        transport_url.c_str(),
        request.method.c_str(),
        transport_headers.c_str(),
        reinterpret_cast<const uint8_t*>(request.body.data()),
        request.body.size()};
    be_navigation_response response{};
    response.struct_size = sizeof(response);
    response.abi_version = BE_ABI_VERSION;
    response.http_status = 200;
    const auto status = client.fetch(client.user_data, &input, &response);
    if (status != BE_OK)
      return BE_RESOURCE_ERROR;
    NavigationResponseRelease release{response};
    if (response.struct_size < sizeof(response) ||
        response.abi_version != BE_ABI_VERSION || response.http_status < 200 ||
        response.http_status > 599 ||
        response.body_size > blink_standalone::kMaxResourceBytes ||
        (response.body_size && !response.body))
      return BE_RESOURCE_ERROR;
    document.status = response.http_status;
    if (!CopyText(response.redirect_url_utf8, 65536, redirect))
      return BE_RESOURCE_ERROR;
    std::string headers;
    if (!CopyText(response.headers_utf8,
                  blink_standalone::kMaxNavigationHeaders, headers) ||
        !blink_standalone::ParseNavigationHeaders(headers, document.headers))
      return BE_RESOURCE_ERROR;
    if (blink_standalone::RedirectStatus(document.status))
      return redirect.empty() ? BE_RESOURCE_ERROR : BE_OK;
    if (document.status == 204 || document.status == 205)
      return BE_OK;
    if (!response.mime_type ||
        !CopyText(response.mime_type, 1024, document.mime) ||
        document.mime.empty() ||
        !CopyText(response.charset, 1024, document.charset))
      return BE_RESOURCE_ERROR;
    for (const auto& field : document.headers)
      if (blink_standalone::UnsupportedNavigationPolicy(field.first))
        return BE_NOT_SUPPORTED;
    if (response.body_size)
      document.body.assign(reinterpret_cast<const char*>(response.body),
                           response.body_size);
    return BE_OK;
  }
  const auto& config = page.owner->engine->services.config();
  if (!config.fetch_resource || request.method != "GET")
    return BE_RESOURCE_ERROR;
  be_resource_response response{};
  response.struct_size = sizeof(response);
  response.abi_version = BE_ABI_VERSION;
  if (config.fetch_resource(config.user_data, transport_url.c_str(),
                            &response) != BE_OK)
    return BE_RESOURCE_ERROR;
  ResourceResponseRelease release{response};
  if (response.struct_size < sizeof(response) ||
      response.abi_version != BE_ABI_VERSION ||
      response.size > blink_standalone::kMaxResourceBytes ||
      (response.size && !response.data) || !response.mime_type ||
      !CopyText(response.mime_type, 1024, document.mime) ||
      document.mime.empty() ||
      !CopyText(response.charset, 1024, document.charset))
    return BE_RESOURCE_ERROR;
  if (response.size)
    document.body.assign(reinterpret_cast<const char*>(response.data),
                         response.size);
  return BE_OK;
}
void StopNavigation(FrameState& page) {
  if (page.pending) {
    Notify(page, BE_NAV_CANCELLED, BE_OK, page.pending->url, page.pending->id);
    page.pending.reset();
  }
  // Cancel parsing/subresources too; clients may not queue another navigation
  // from unload/abort handlers while the caller is explicitly stopping.
  page.suppress_navigation = true;
  page.frame->Loader().StopAllLoaders(true);
  page.suppress_navigation = false;
  if (page.loading && page.current)
    Notify(page, BE_NAV_CANCELLED, BE_OK, page.current->url, page.current->id);
  page.loading = false;
}
void CommitDocument(
    FrameState& page,
    NavigationRequest request,
    const StaticDocument& document,
    std::vector<blink::WebNavigationParams::RedirectInfo> redirects = {},
    blink::WebNavigationTimings timings = {}) {
  page.current = std::move(request);
  page.loading = true;
  auto policy = blink_standalone::MakeNavigationPolicy(GURL(page.current->url),
                                                       document.headers);
  if (!policy) {
    page.loading = false;
    page.frame->Loader().CancelClientNavigation();
    Notify(page, BE_NAV_FAILED, BE_RESOURCE_ERROR, page.current->url,
           page.current->id);
    return;
  }
  if (page.current->inherited_policy)
    policy->policies = *page.current->inherited_policy;
  else if ((page.current->url == "about:blank" ||
            page.current->url == "about:srcdoc") &&
           page.frame->DomWindow()->GetPolicyContainer())
    policy->policies = blink::ToWebPolicyContainerPolicies(
        page.frame->DomWindow()->GetPolicyContainer()->GetPolicies());
  if (page.frame->Owner()) {
    policy->policies.sandbox_flags |=
        page.frame->Owner()->GetFramePolicy().sandbox_flags;
    if (auto* parent = page.frame->Parent())
      policy->policies.sandbox_flags |=
          parent->GetSecurityContext()->GetSandboxFlags();
  }
  auto params = std::make_unique<blink::WebNavigationParams>();
  // The host has already fetched the document before committing it. Blink
  // requires non-null navigation/fetch times when it replays redirects.
  if (timings.navigation_start.is_null())
    timings.navigation_start = base::TimeTicks::Now();
  if (timings.fetch_start.is_null())
    timings.fetch_start = timings.navigation_start;
  params->navigation_timings = timings;
  if (page.frame->Owner())
    params->frame_policy = page.frame->Owner()->GetFramePolicy();
  params->url = blink::WebURL(blink::KURL(GURL(page.current->url)));
  params->http_method = blink::WebString::FromUtf8(page.current->method);
  params->requestor_origin = page.current->requestor_origin;
  params->fallback_base_url = page.current->fallback_base_url;
  params->referrer = blink::WebString::FromUtf8(page.current->referrer);
  params->referrer_policy = page.current->referrer_policy;
  params->policy_container = std::move(policy);
  auto origin = blink::SecurityOrigin::CreateWithReferenceOrigin(
      blink::KURL(GURL(page.current->url)),
      page.current->requestor_origin.Get());
  if (page.current->url == "about:srcdoc" && page.frame->Parent())
    origin = page.frame->Parent()
                 ->GetSecurityContext()
                 ->GetSecurityOrigin()
                 ->IsolatedCopy();
  if ((params->policy_container->policies.sandbox_flags &
       network::mojom::WebSandboxFlags::kOrigin) !=
      network::mojom::WebSandboxFlags::kNone)
    origin = origin->DeriveNewOpaqueOrigin();
  params->origin_to_commit = blink::WebSecurityOrigin(origin);
  if (page.main) {
    params->storage_key =
        blink::StorageKey::CreateFirstParty(origin->ToUrlOrigin());
  } else {
    const net::SchemefulSite top(page.owner->main->frame->DomWindow()
                                     ->GetSecurityOrigin()
                                     ->ToUrlOrigin());
    auto chain = blink::mojom::AncestorChainBit::kSameSite;
    for (auto* ancestor = page.frame->Parent(); ancestor;
         ancestor = ancestor->Parent()) {
      if (net::SchemefulSite(ancestor->GetSecurityContext()
                                 ->GetSecurityOrigin()
                                 ->ToUrlOrigin()) !=
          net::SchemefulSite(origin->ToUrlOrigin())) {
        chain = blink::mojom::AncestorChainBit::kCrossSite;
        break;
      }
    }
    params->storage_key =
        blink::StorageKey::Create(origin->ToUrlOrigin(), top, chain,
                                  /*third_party_partitioning_allowed=*/true);
  }
  params->frame_load_type = page.current->load_type;
  if (page.current->load_type == blink::WebFrameLoadType::kBackForward)
    params->history_item = page.current->item;
  std::vector<std::pair<std::string, std::string>> request_headers;
  if (blink_standalone::ParseNavigationHeaders(page.current->headers,
                                               request_headers)) {
    for (auto [name, value] : request_headers) {
      name = blink_standalone::NavigationHeaderName(name);
      if (name == "content-type")
        params->http_content_type = blink::WebString::FromUtf8(value);
    }
  }
  params->redirects = std::move(redirects);
  if (!page.current->body.empty()) {
    params->http_body.Initialize();
    params->http_body.AppendData(
        blink::WebData(base::as_byte_span(page.current->body)));
  }
  const bool standard =
      page.current->load_type == blink::WebFrameLoadType::kStandard;
  const int destination =
      page.current->history_target >= 0 && !standard &&
              page.current->load_type != blink::WebFrameLoadType::kReload
          ? page.current->history_target
          : page.history.index() + (standard ? 1 : 0);
  const int first =
      standard && destination >= static_cast<int>(page.history.kCapacity) ? 1
                                                                          : 0;
  auto same_origin = [&](const NavigationRequest& entry) {
    return !entry.committed_origin.IsNull() &&
           !entry.committed_origin.IsOpaque() &&
           params->origin_to_commit.IsSameOriginWith(entry.committed_origin);
  };
  // Navigation API entries expose only the contiguous same-origin portion;
  // host session history can still traverse across these origin boundaries.
  for (int i = destination - 1; i >= first; --i) {
    const auto* entry = page.history.At(i);
    if (!entry || !same_origin(*entry))
      break;
    params->navigation_api_back_entries.insert(
        params->navigation_api_back_entries.begin(), entry->item);
  }
  if (!standard) {
    for (int i = destination + 1; i < static_cast<int>(page.history.size());
         ++i) {
      const auto* entry = page.history.At(i);
      if (!entry || !same_origin(*entry))
        break;
      params->navigation_api_forward_entries.push_back(entry->item);
    }
  }
  if (page.history.Current() && same_origin(*page.history.Current()))
    params->navigation_api_previous_entry = page.history.Current()->item;
  blink::WebNavigationParams::FillStaticResponse(
      params.get(), blink::WebString::FromUtf8(document.mime),
      blink::WebString::FromUtf8(document.charset.empty() ? "UTF-8"
                                                          : document.charset),
      base::span(document.body));
  params->http_status_code = static_cast<int>(document.status);
  params->response.SetHttpStatusCode(static_cast<int>(document.status));
  for (const auto& [name, value] : document.headers)
    params->response.AddHttpHeaderField(blink::WebString::FromUtf8(name),
                                        blink::WebString::FromUtf8(value));
  auto& storage = page.owner->engine->platform.storage();
  storage.FlushFrame(*page.frame);
  storage.RegisterFrame(page.frame->GetLocalFrameToken(),
                        page.owner->storage_namespace, params->storage_key);
  page.frame->Loader().CommitNavigation(std::move(params), nullptr);
}
void ProcessNavigation(FrameState& page) {
  if (!page.alive || !page.owner->alive || !page.pending)
    return;
  auto request = std::move(*page.pending);
  page.pending.reset();
  if (request.load_type == blink::WebFrameLoadType::kBackForward) {
    const auto* target = page.history.At(request.history_target);
    if (!target || target->item.IsNull() || request.item.IsNull() ||
        target->item.ItemSequenceNumber() !=
            request.item.ItemSequenceNumber()) {
      page.frame->DomWindow()->navigation()->InformAboutCanceledNavigation();
      Notify(page, BE_NAV_CANCELLED, BE_INVALID_STATE, request.url, request.id);
      return;
    }
  }
  // History traversal within the live document must preserve its JS realm,
  // state object and popstate/hashchange behavior, without a network refetch.
  auto* loader = page.frame->Loader().GetDocumentLoader();
  if (request.load_type == blink::WebFrameLoadType::kBackForward &&
      !request.item.IsNull() && loader && loader->GetHistoryItem() &&
      request.item.DocumentSequenceNumber() ==
          loader->GetHistoryItem()->DocumentSequenceNumber()) {
    page.current = request;
    Notify(page, BE_NAV_STARTED, BE_OK, request.url, request.id);
    const auto result = loader->CommitSameDocumentNavigation(
        blink::KURL(GURL(request.url)), blink::WebFrameLoadType::kBackForward,
        request.item, blink::ClientRedirectPolicy::kNotClientRedirect, false,
        nullptr, false, nullptr,
        blink::mojom::blink::TriggeringEventInfo::kNotFromEvent, true, false,
        std::nullopt, false);
    if (result == blink::mojom::CommitResult::Ok)
      return;
    page.frame->DomWindow()->navigation()->InformAboutCanceledNavigation();
    Notify(page, BE_NAV_FAILED, BE_RESOURCE_ERROR, request.url, request.id);
    return;
  }
  if (page.loading && page.current)
    Notify(page, BE_NAV_CANCELLED, BE_OK, page.current->url, page.current->id);
  page.current = request;
  page.loading = true;
  Notify(page, BE_NAV_STARTED, BE_OK, request.url, request.id);
  StaticDocument document;
  std::vector<blink::WebNavigationParams::RedirectInfo> redirects;
  blink::WebNavigationTimings timings;
  timings.navigation_start = base::TimeTicks::Now();
  timings.fetch_start = timings.navigation_start;
  be_status status = BE_OK;
  if (request.supplied) {
    document = *request.supplied;
  } else {
    for (unsigned hop = 0;; ++hop) {
      document = StaticDocument{};
      std::string redirect;
      timings.fetch_start = base::TimeTicks::Now();
      status = FetchDocument(page, request, document, redirect);
      if (status != BE_OK || !blink_standalone::RedirectStatus(document.status))
        break;
      if (timings.redirect_start.is_null())
        timings.redirect_start = timings.fetch_start;
      timings.redirect_end = base::TimeTicks::Now();
      if (hop >= 19) {
        status = BE_RESOURCE_ERROR;
        break;
      }
      const GURL before(request.url);
      const GURL after = before.Resolve(redirect);
      if (!NavigationUrl(after)) {
        status = BE_NOT_SUPPORTED;
        break;
      }
      std::string referrer_policy_headers;
      for (const auto& [name, value] : document.headers) {
        if (blink_standalone::NavigationHeaderName(name) != "referrer-policy")
          continue;
        if (!referrer_policy_headers.empty())
          referrer_policy_headers += ',';
        referrer_policy_headers += value;
      }
      if (!referrer_policy_headers.empty()) {
        auto text = blink::String::FromUtf8(referrer_policy_headers);
        if (text.IsNull()) {
          status = BE_RESOURCE_ERROR;
          break;
        }
        blink::SecurityPolicy::ReferrerPolicyFromHeaderValue(
            text, blink::kDoNotSupportReferrerPolicyLegacyKeywords,
            &request.referrer_policy);
      }
      const auto referrer = blink::SecurityPolicy::GenerateReferrer(
          request.referrer_policy, blink::KURL(after),
          blink::String::FromUtf8(request.referrer));
      request.referrer = referrer.referrer.Utf8();
      request.referrer_policy = referrer.referrer_policy;
      const bool becomes_get =
          blink_standalone::RedirectBecomesGet(document.status, request.method);
      if (becomes_get) {
        request.method = "GET";
        request.body.clear();
      }
      const bool cross_origin =
          before.DeprecatedGetOriginAsURL() != after.DeprecatedGetOriginAsURL();
      std::string headers;
      if (!blink_standalone::RedirectRequestHeaders(
              request.headers, becomes_get, cross_origin, headers)) {
        status = BE_RESOURCE_ERROR;
        break;
      }
      request.headers = std::move(headers);
      blink::WebNavigationParams::RedirectInfo info{};
      info.new_url = blink::WebURL(blink::KURL(after));
      info.new_http_method = blink::WebString::FromUtf8(request.method);
      info.new_referrer = blink::WebString::FromUtf8(request.referrer);
      info.new_referrer_policy = request.referrer_policy;
      info.redirect_response =
          blink::WebURLResponse(blink::WebURL(blink::KURL(before)));
      info.redirect_response.SetHttpStatusCode(
          static_cast<int>(document.status));
      redirects.push_back(std::move(info));
      request.url = after.spec();
      page.current = request;
      Notify(page, BE_NAV_REDIRECTED, BE_OK, request.url, request.id);
    }
  }
  if (status == BE_OK && !page.main) {
    std::vector<url::Origin> ancestors;
    for (auto* ancestor = page.frame->Parent(); ancestor;
         ancestor = ancestor->Parent())
      ancestors.push_back(
          ancestor->GetSecurityContext()->GetSecurityOrigin()->ToUrlOrigin());
    if (!blink_standalone::AllowsFrameEmbedding(GURL(request.url),
                                                document.headers, ancestors))
      status = BE_RESOURCE_ERROR;
  }
  if (status != BE_OK || document.status == 304) {
    page.loading = false;
    page.frame->Loader().CancelClientNavigation();
    page.frame->Loader().DidFinishNavigation(
        blink::FrameLoader::NavigationFinishState::kFailure);
    Notify(page, BE_NAV_FAILED, status == BE_OK ? BE_RESOURCE_ERROR : status,
           request.url, request.id);
  } else if (document.status == 204 || document.status == 205) {
    page.loading = false;
    page.frame->Loader().CancelClientNavigation();
    page.frame->Loader().DidFinishNavigation(
        blink::FrameLoader::NavigationFinishState::kSuccess);
    Notify(page, BE_NAV_FINISHED, BE_OK, request.url, request.id);
  } else {
    CommitDocument(page, std::move(request), document, std::move(redirects),
                   timings);
  }
}
}  // namespace

extern "C" {
uint32_t be_get_abi_version(void) {
  return BE_ABI_VERSION;
}
uint64_t be_get_capabilities(void) {
  return BE_CAP_LOCAL_HTML | BE_CAP_SOFTWARE_RGBA | BE_CAP_HOST_RESOURCES |
         BE_CAP_TASK_PUMP | BE_CAP_JAVASCRIPT | BE_CAP_INPUT |
         BE_CAP_NAVIGATION | BE_CAP_IME | BE_CAP_CHILD_FRAMES |
         BE_CAP_DOM_STORAGE | BE_CAP_DEDICATED_WORKERS;
}
const char* be_status_string(be_status status) {
  switch (status) {
    case BE_OK:
      return "ok";
    case BE_INVALID_ARGUMENT:
      return "invalid argument";
    case BE_ABI_MISMATCH:
      return "ABI version mismatch";
    case BE_WRONG_THREAD:
      return "operation requires the owning thread";
    case BE_INVALID_STATE:
      return "engine or page is closed";
    case BE_BUSY:
      return "operation would reenter Blink or pages remain open";
    case BE_NOT_SUPPORTED:
      return "operation is not supported";
    case BE_RESOURCE_ERROR:
      return "missing or invalid runtime resources";
    case BE_BUFFER_TOO_SMALL:
      return "pixel buffer is too small";
    case BE_RENDER_ERROR:
      return "document lifecycle did not reach paint";
    case BE_SCRIPT_EXCEPTION:
      return "JavaScript exception";
    case BE_SCRIPT_ERROR:
      return "JavaScript evaluation failed";
  }
  return "unknown status";
}

be_status be_engine_create(const be_engine_config* config,
                           be_engine** engine_out) {
  if (!engine_out)
    return BE_INVALID_ARGUMENT;
  *engine_out = nullptr;
  const auto config_status = CheckConfig(config);
  if (config_status != BE_OK)
    return config_status;
  if (!blink_standalone::InitializeHostAllocator())
    return BE_NOT_SUPPORTED;
  if (!config->wake_up || !ValidFile(config->icu_data_path) ||
      !ValidFile(config->v8_snapshot_path) ||
      !ValidFile(config->blink_resources_path) ||
      (config->blink_scaled_resources_path &&
       !ValidFile(config->blink_scaled_resources_path)))
    return BE_RESOURCE_ERROR;
  // Check mapping/pack structure before initializing process-global services.
  // V8 must still validate that the nonempty snapshot matches this exact build.
  base::MemoryMappedFile snapshot_preflight;
  ui::DataPack resources_preflight(ui::kScaleFactorNone);
  if (!snapshot_preflight.Initialize(
          base::FilePath(config->v8_snapshot_path)) ||
      !resources_preflight.LoadFromPath(
          base::FilePath(config->blink_resources_path)) ||
      !resources_preflight.HasResource(IDR_UASTYLE_HTML_CSS))
    return BE_RESOURCE_ERROR;
  if (config->blink_scaled_resources_path) {
    ui::DataPack scaled_preflight(ui::k100Percent);
    if (!scaled_preflight.LoadFromPath(
            base::FilePath(config->blink_scaled_resources_path)))
      return BE_RESOURCE_ERROR;
  }

  if (g_engine.load(std::memory_order_acquire))
    return BE_INVALID_STATE;
  std::lock_guard<std::mutex> lock(g_initialization);
  if (g_engine.load(std::memory_order_acquire))
    return BE_INVALID_STATE;
  // A statically linked Chromium runtime already present in the caller cannot
  // be independently owned by this engine. Refuse this ambiguous environment.
  if (blink::Platform::Current() || ui::ResourceBundle::HasSharedInstance())
    return BE_INVALID_STATE;
  auto* engine = new be_engine(*config);
  engine->at_exit = std::make_unique<base::AtExitManager>();
  // Retain the process context even if an irreversible initialization stage
  // fails. A second initialization must never run over partially live Blink.
  g_engine.store(engine, std::memory_order_release);
  if (!base::CommandLine::InitializedForCurrentProcess())
    base::CommandLine::Init(0, nullptr);
  if (!base::FeatureList::GetInstance())
    base::FeatureList::SetInstance(std::make_unique<base::FeatureList>());
  if (!base::ThreadPoolInstance::Get())
    base::ThreadPoolInstance::CreateAndStartWithDefaultParams(
        "BlinkStandalone");
  engine->icu_file = base::File(base::FilePath(config->icu_data_path),
                                base::File::FLAG_OPEN | base::File::FLAG_READ);
#if ICU_UTIL_DATA_IMPL == ICU_UTIL_DATA_FILE
  if (!engine->icu_file.IsValid() ||
      !base::i18n::InitializeICUWithFileDescriptor(
          engine->icu_file.GetPlatformFile(),
          base::MemoryMappedFile::Region::kWholeFile))
    return BE_RESOURCE_ERROR;
#else
  if (!base::i18n::InitializeICU())
    return BE_RESOURCE_ERROR;
#endif
#if defined(V8_USE_EXTERNAL_STARTUP_DATA)
  base::File snapshot(base::FilePath(config->v8_snapshot_path),
                      base::File::FLAG_OPEN | base::File::FLAG_READ);
  if (!snapshot.IsValid())
    return BE_RESOURCE_ERROR;
  gin::V8Initializer::LoadV8SnapshotFromFile(
      std::move(snapshot), nullptr,
#if BUILDFLAG(USE_V8_CONTEXT_SNAPSHOT)
      gin::V8SnapshotFileType::kWithAdditionalContext);
#else
      gin::V8SnapshotFileType::kDefault);
#endif
#endif
  base::File resources(base::FilePath(config->blink_resources_path),
                       base::File::FLAG_OPEN | base::File::FLAG_READ);
  if (!resources.IsValid())
    return BE_RESOURCE_ERROR;
  ui::ResourceBundle::InitSharedInstanceWithPakFileRegion(
      std::move(resources), base::MemoryMappedFile::Region::kWholeFile);
  // The initial file-region pack is registered as locale data by
  // ResourceBundle. Blink's binary/string resource access reads its separate
  // data-pack list.
  ui::ResourceBundle::GetSharedInstance().AddDataPackFromPath(
      base::FilePath(config->blink_resources_path), ui::kScaleFactorNone);
  if (!engine->platform.HasDataResource(IDR_UASTYLE_HTML_CSS) ||
      engine->platform.GetDataResourceString(IDR_UASTYLE_HTML_CSS).empty())
    return BE_RESOURCE_ERROR;
  if (config->blink_scaled_resources_path)
    ui::ResourceBundle::GetSharedInstance().AddDataPackFromPath(
        base::FilePath(config->blink_scaled_resources_path), ui::k100Percent);
  blink_standalone::InitializeDiscardableMemory();
  mojo::core::Init();
  if (!engine->platform.StartIO())
    return BE_RESOURCE_ERROR;
  blink::Platform::InitializeBlink();
  auto pump = std::make_unique<HostMessagePump>(engine->services);
  engine->pump = pump.get();
  engine->scheduler =
      blink::scheduler::WebThreadScheduler::CreateInProcessMainThreadScheduler(
          std::move(pump));
  blink::Initialize(&engine->platform, &engine->binders,
                    engine->scheduler.get());
#if BUILDFLAG(IS_LINUX)
  // The pinned Skia fontconfig interface recognizes "sans", not the CSS
  // keyword "sans-serif". Set this process-global family before workers start.
  blink::FontCache::SetSystemFontFamily(blink::AtomicString("sans"));
#endif
  engine->platform.StartWorkers(engine->services);
  engine->active = true;
  *engine_out = engine;
  return BE_OK;
}

be_status be_engine_shutdown(be_engine* engine) {
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  for (auto page : engine->pages)
    if (page->alive)
      return BE_BUSY;
  base::RunLoop().RunUntilIdle();
  if (engine->platform.workers().ActiveThreads())
    return BE_BUSY;
  for (auto page : engine->pages)
    page->agent_group.reset();
  engine->platform.storage().Shutdown();
  engine->services.Disable();
  engine->scheduler->Shutdown();
  engine->active = false;
  return BE_OK;
}
be_status be_engine_pump(be_engine* engine, uint32_t budget_ms) {
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  if (!budget_ms || budget_ms > 1000)
    return BE_INVALID_ARGUMENT;
  engine->pump->SetBudget(budget_ms);
  // Each commit can detach children and create new frames. Hold a GC-rooted
  // snapshot so navigation callbacks cannot invalidate iteration.
  for (auto page : engine->pages) {
    if (!page->alive)
      continue;
    const auto frames = page->frames;
    for (const auto& state : frames)
      if (state->alive)
        ProcessNavigation(*state);
  }
  base::RunLoop().RunUntilIdle();
  const bool workers_finished = !engine->platform.workers().ActiveThreads();
  for (auto page : engine->pages) {
    if (!page->alive) {
      if (workers_finished)
        page->agent_group.reset();
      continue;
    }
    const auto frames = page->frames;
    for (const auto& state : frames) {
      if (!state->alive)
        continue;
      if (!state->retired_history_keys.empty()) {
        auto keys = std::move(state->retired_history_keys);
        state->retired_history_keys.clear();
        state->frame->DomWindow()
            ->navigation()
            ->DisposeEntriesForSessionHistoryRemoval(keys);
      }
      if (state->pending)
        engine->services.Wake(0);
    }
    std::erase_if(page->frames,
                  [](const auto& state) { return !state->alive; });
  }
  return BE_OK;
}
be_status be_engine_set_storage_client(be_engine* engine,
                                       const be_storage_client* client) {
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  // Retired page handles also count: storage policy is fixed for this engine
  // once a page has exposed a document, not merely while pages remain live.
  if (!engine->pages.empty())
    return BE_BUSY;
  return engine->platform.storage().Configure(client);
}
be_status be_engine_get_storage_status(be_engine* engine, be_status* result) {
  if (!result)
    return BE_INVALID_ARGUMENT;
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  *result = engine->platform.storage().last_status();
  return BE_OK;
}
be_status be_engine_flush_storage(be_engine* engine) {
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  for (auto page : engine->pages) {
    if (!page->alive)
      continue;
    const auto frames = page->frames;
    for (const auto& state : frames)
      if (state->alive)
        engine->platform.storage().FlushFrame(*state->frame);
  }
  return engine->platform.storage().last_status();
}

be_status be_page_create(be_engine* engine,
                         const be_page_config* config,
                         be_page** page_out) {
  if (!page_out)
    return BE_INVALID_ARGUMENT;
  *page_out = nullptr;
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  const auto config_status = CheckConfig(config);
  if (config_status != BE_OK)
    return config_status;
  if (!blink_standalone::ValidSize(config->width, config->height))
    return BE_INVALID_ARGUMENT;
  auto* handle = new be_page;
  handle->engine = engine;
  handle->width = config->width;
  handle->height = config->height;
  handle->agent_group = engine->scheduler->CreateWebAgentGroupScheduler();
  const auto colors = blink::ColorProviderColorMaps::CreateDefault();
  auto* chrome = blink::MakeGarbageCollected<blink::EmptyChromeClient>();
  handle->page = blink::Page::CreateNonOrdinary(
      *chrome, handle->agent_group->GetAgentGroupScheduler(), &colors);
  auto& settings = handle->page->GetSettings();
  settings.SetScriptEnabled(true);
  settings.SetLocalStorageEnabled(engine->platform.storage().enabled());
  settings.SetPluginsEnabled(false);
  settings.SetAcceleratedCompositingEnabled(false);
  settings.SetLoadsImagesAutomatically(true);
  settings.SetDefaultFontSize(16);
  settings.SetDefaultFixedFontSize(13);
  auto& fonts = settings.GetGenericFontFamilySettings();
  fonts.UpdateStandard(blink::AtomicString("sans"));
  fonts.UpdateSansSerif(blink::AtomicString("sans"));
  fonts.UpdateSerif(blink::AtomicString("serif"));
  fonts.UpdateFixed(blink::AtomicString("monospace"));
  handle->main = blink::MakeGarbageCollected<FrameState>(*handle);
  handle->main->main = true;
  handle->frames.push_back(handle->main);
  auto* client = blink::MakeGarbageCollected<HostFrameClient>(*handle->main,
                                                              engine->services);
  handle->main->frame = blink::MakeGarbageCollected<blink::LocalFrame>(
      client, *handle->page, nullptr, nullptr, nullptr,
      blink::FrameInsertType::kInsertInConstructor, blink::LocalFrameToken(),
      nullptr, nullptr, mojo::NullRemote());
  const gfx::Size size(static_cast<int>(handle->width),
                       static_cast<int>(handle->height));
  handle->main->frame->SetView(
      blink::MakeGarbageCollected<blink::LocalFrameView>(*handle->main->frame,
                                                         size));
  handle->page->GetVisualViewport().SetSize(size);
  handle->main->frame->Init(
      nullptr, blink::DocumentToken(), blink::InitiatorStateToken(), nullptr,
      blink::StorageKey(), ukm::kInvalidSourceId, blink::KURL());
  blink::CoreInitializer::GetInstance().ProvideModulesToPage(*handle->page, "");
  handle->storage_namespace =
      engine->platform.storage().CreateSessionNamespace(*handle->page);
  engine->platform.storage().RegisterFrame(
      handle->main->frame->GetLocalFrameToken(), handle->storage_namespace,
      handle->main->frame->DomWindow()->GetStorageKey());
  handle->alive = true;
  engine->pages.insert(handle);
  *page_out = handle;
  return BE_OK;
}
be_status be_page_destroy(be_page* page) {
  auto* engine = g_engine.load(std::memory_order_acquire);
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  const auto status = CheckPage(page, engine);
  if (status != BE_OK)
    return status;
  StopNavigation(*page->main);
  page->alive = false;
  page->main->suppress_navigation = true;
  page->page->WillBeDestroyed();
  engine->platform.storage().RemoveSessionNamespace(page->storage_namespace);
  page->frames.clear();
  page->main->frame = nullptr;
  page->page.Clear();
  // Retain the parent scheduler until worker termination acknowledgments drain.
  page->main->pending.reset();
  page->main->current.reset();
  page->main->history.Clear();
  page->main->retired_history_keys.clear();
  page->main->script_result.clear();
  page->navigation_client = {};
  return BE_OK;
}
be_status be_page_load_html(be_page* page,
                            const char* html_utf8,
                            size_t html_size,
                            const char* base_url_utf8) {
  auto* engine = g_engine.load(std::memory_order_acquire);
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  const auto status = CheckPage(page, engine);
  if (status != BE_OK)
    return status;
  if ((!html_utf8 && html_size) ||
      html_size > blink_standalone::kMaxResourceBytes || !base_url_utf8)
    return BE_INVALID_ARGUMENT;
  const GURL base_url(base_url_utf8);
  if (!base_url.is_valid() || base_url.has_username() ||
      base_url.has_password())
    return BE_INVALID_ARGUMENT;
  if (base_url.spec() != "about:blank" && !base_url.SchemeIsHTTPOrHTTPS())
    return BE_NOT_SUPPORTED;
  StopNavigation(*page->main);
  auto document = std::make_shared<StaticDocument>();
  if (html_size)
    document->body.assign(html_utf8, html_size);
  NavigationRequest request;
  request.url = base_url.spec();
  request.supplied = document;
  request.id = ++page->main->next_navigation_id;
  Notify(*page->main, BE_NAV_STARTED, BE_OK, request.url, request.id);
  CommitDocument(*page->main, std::move(request), *document);
  return BE_OK;
}
be_status be_page_resize(be_page* page, uint32_t width, uint32_t height) {
  auto* engine = g_engine.load(std::memory_order_acquire);
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  const auto status = CheckPage(page, engine);
  if (status != BE_OK)
    return status;
  if (!blink_standalone::ValidSize(width, height))
    return BE_INVALID_ARGUMENT;
  page->width = width;
  page->height = height;
  const gfx::Size size(static_cast<int>(width), static_cast<int>(height));
  auto* view = page->main->frame->View();
  // Fixed layout views track frame Resize() themselves. The explicit setter
  // is only valid when an embedder has selected an independent layout size.
  view->Resize(size);
  if (!view->LayoutSizeFixedToFrameSize())
    view->SetLayoutSize(size);
  page->page->GetVisualViewport().SetSize(size);
  return BE_OK;
}
be_status be_page_layout(be_page* page) {
  auto* engine = g_engine.load(std::memory_order_acquire);
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  const auto status = CheckPage(page, engine);
  return status == BE_OK ? Layout(page) : status;
}
be_status be_page_paint_rgba(be_page* page,
                             uint8_t* pixels,
                             size_t pixels_size,
                             size_t stride) {
  auto* engine = g_engine.load(std::memory_order_acquire);
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  const auto status = CheckPage(page, engine);
  if (status != BE_OK)
    return status;
  if (!pixels)
    return BE_INVALID_ARGUMENT;
  if (!blink_standalone::ValidPixels(page->width, page->height, pixels_size,
                                     stride))
    return BE_BUFFER_TOO_SMALL;
  const auto layout = Layout(page);
  if (layout != BE_OK)
    return layout;
  SkBitmap bitmap;
  const auto info = SkImageInfo::Make(
      static_cast<int>(page->width), static_cast<int>(page->height),
      kRGBA_8888_SkColorType, kPremul_SkAlphaType);
  if (!bitmap.installPixels(info, pixels, stride))
    return BE_INVALID_ARGUMENT;
  bitmap.eraseColor(SK_ColorTRANSPARENT);
  cc::SkiaPaintCanvas canvas(bitmap);
  blink::PaintRecordBuilder builder;
  page->main->frame->View()->PaintOutsideOfLifecycle(
      builder.Context(), blink::PaintFlag::kOmitCompositingInfo,
      blink::CullRect(gfx::Rect(static_cast<int>(page->width),
                                static_cast<int>(page->height))));
  builder.EndRecording(canvas);
  return BE_OK;
}
be_status be_page_evaluate_script(be_page* page,
                                  const char* source_utf8,
                                  size_t source_size,
                                  const char* source_url_utf8,
                                  be_script_result* result) {
  auto* engine = g_engine.load(std::memory_order_acquire);
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  auto status = CheckPage(page, engine);
  if (status != BE_OK)
    return status;
  status = CheckConfig(result);
  if (status != BE_OK)
    return status;
  result->utf8_data = nullptr;
  result->utf8_size = 0;
  result->is_exception = 0;
  if ((!source_utf8 && source_size) ||
      source_size > blink_standalone::kMaxResourceBytes)
    return BE_INVALID_ARGUMENT;
  const auto value = blink_standalone::EvaluateScript(
      *page->main->frame,
      std::string_view(source_utf8 ? source_utf8 : "", source_size),
      source_url_utf8 ? std::string_view(source_url_utf8) : std::string_view());
  page->main->script_result = value.utf8_result;
  result->utf8_data = page->main->script_result.c_str();
  result->utf8_size = page->main->script_result.size();
  result->is_exception = value.exception ? 1u : 0u;
  return value.status;
}
be_status be_page_dispatch_input(be_page* page,
                                 const be_input_event* event,
                                 uint32_t* handled_out) {
  if (!handled_out)
    return BE_INVALID_ARGUMENT;
  *handled_out = 0;
  auto* engine = g_engine.load(std::memory_order_acquire);
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  auto status = CheckPage(page, engine);
  if (status != BE_OK)
    return status;
  status = CheckConfig(event);
  if (status != BE_OK)
    return status;
  status = Layout(page);
  if (status != BE_OK)
    return status;
  bool handled = false;
  auto* target = page->main->frame.Get();
  if (event->type >= BE_INPUT_KEY_DOWN) {
    if (auto* focused = page->page->GetFocusController().FocusedFrame())
      target = focused;
  }
  status = blink_standalone::DispatchPageInput(*target, *event, &handled);
  *handled_out = handled ? 1u : 0u;
  return status;
}
be_status be_page_set_focus(be_page* page, uint32_t focused) {
  auto* engine = g_engine.load(std::memory_order_acquire);
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  const auto status = CheckPage(page, engine);
  if (status != BE_OK)
    return status;
  if (focused > 1)
    return BE_INVALID_ARGUMENT;
  auto* target = page->page->GetFocusController().FocusedFrame();
  return blink_standalone::SetPageInputFocus(
      target ? *target : *page->main->frame, focused != 0);
}
be_status be_page_ime_set_composition(be_page* page,
                                      const be_ime_composition* composition) {
  auto* engine = g_engine.load(std::memory_order_acquire);
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  auto status = CheckPage(page, engine);
  if (status != BE_OK)
    return status;
  status = CheckConfig(composition);
  if (status != BE_OK)
    return status;
  status = Layout(page);
  if (status != BE_OK)
    return status;
  auto* frame = page->page->GetFocusController().FocusedFrame();
  return frame ? blink_standalone::SetImeComposition(*frame, *composition)
               : BE_INVALID_STATE;
}
be_status be_page_ime_commit_text(be_page* page,
                                  const char* text,
                                  size_t length,
                                  int32_t relative_caret_utf16) {
  auto* engine = g_engine.load(std::memory_order_acquire);
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  auto status = CheckPage(page, engine);
  if (status != BE_OK)
    return status;
  if (!text && length)
    return BE_INVALID_ARGUMENT;
  status = Layout(page);
  if (status != BE_OK)
    return status;
  auto* frame = page->page->GetFocusController().FocusedFrame();
  return frame ? blink_standalone::CommitImeText(
                     *frame, std::string_view(text ? text : "", length),
                     relative_caret_utf16)
               : BE_INVALID_STATE;
}
be_status be_page_ime_finish_composition(be_page* page,
                                         uint32_t keep_selection) {
  auto* engine = g_engine.load(std::memory_order_acquire);
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  auto status = CheckPage(page, engine);
  if (status != BE_OK)
    return status;
  if (keep_selection > 1)
    return BE_INVALID_ARGUMENT;
  status = Layout(page);
  if (status != BE_OK)
    return status;
  auto* frame = page->page->GetFocusController().FocusedFrame();
  return frame ? blink_standalone::FinishImeComposition(*frame,
                                                        keep_selection != 0)
               : BE_INVALID_STATE;
}
be_status be_page_ime_cancel_composition(be_page* page) {
  auto* engine = g_engine.load(std::memory_order_acquire);
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  auto status = CheckPage(page, engine);
  if (status != BE_OK)
    return status;
  status = Layout(page);
  if (status != BE_OK)
    return status;
  auto* frame = page->page->GetFocusController().FocusedFrame();
  return frame ? blink_standalone::CancelImeComposition(*frame)
               : BE_INVALID_STATE;
}
be_status be_page_ime_get_state(be_page* page,
                                be_ime_state* state,
                                char* text,
                                size_t capacity,
                                size_t* required) {
  if (!required)
    return BE_INVALID_ARGUMENT;
  *required = 0;
  auto* engine = g_engine.load(std::memory_order_acquire);
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  auto status = CheckPage(page, engine);
  if (status != BE_OK)
    return status;
  status = CheckConfig(state);
  if (status != BE_OK)
    return status;
  status = Layout(page);
  if (status != BE_OK)
    return status;
  auto* frame = page->page->GetFocusController().FocusedFrame();
  return blink_standalone::QueryImeState(frame ? *frame : *page->main->frame,
                                         state, text, capacity, required);
}

be_status be_page_set_navigation_client(be_page* page,
                                        const be_navigation_client* client) {
  auto* engine = g_engine.load(std::memory_order_acquire);
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  auto status = CheckPage(page, engine);
  if (status != BE_OK)
    return status;
  for (const auto& state : page->frames)
    if (state->alive && (state->pending || state->loading))
      return BE_BUSY;
  if (client) {
    status = CheckConfig(client);
    if (status != BE_OK)
      return status;
    page->navigation_client = *client;
  } else {
    page->navigation_client = {};
  }
  return BE_OK;
}
be_status be_page_navigate(be_page* page, const char* url_utf8) {
  auto* engine = g_engine.load(std::memory_order_acquire);
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  const auto status = CheckPage(page, engine);
  if (status != BE_OK)
    return status;
  if (!url_utf8)
    return BE_INVALID_ARGUMENT;
  const GURL url(url_utf8);
  if (!url.is_valid() || url.has_username() || url.has_password())
    return BE_INVALID_ARGUMENT;
  if (!NavigationUrl(url))
    return BE_NOT_SUPPORTED;
  blink::ResourceRequest request{blink::KURL(url)};
  blink::FrameLoadRequest load(page->main->frame->DomWindow(), request);
  page->main->frame->Navigate(load, blink::WebFrameLoadType::kStandard);
  return BE_OK;
}
be_status be_page_reload(be_page* page) {
  return be_page_go_back_forward(page, 0);
}
be_status be_page_go_back_forward(be_page* page, int32_t offset) {
  auto* engine = g_engine.load(std::memory_order_acquire);
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  const auto status = CheckPage(page, engine);
  if (status != BE_OK)
    return status;
  return QueueHistory(*page->main, offset) ? BE_OK : BE_INVALID_ARGUMENT;
}
be_status be_page_stop(be_page* page) {
  auto* engine = g_engine.load(std::memory_order_acquire);
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  const auto status = CheckPage(page, engine);
  if (status != BE_OK)
    return status;
  const auto frames = page->frames;
  for (const auto& state : frames)
    if (state->alive)
      StopNavigation(*state);
  return BE_OK;
}
be_status be_page_get_navigation_state(be_page* page,
                                       be_navigation_state* state) {
  auto* engine = g_engine.load(std::memory_order_acquire);
  ApiCall call(engine);
  if (call.status() != BE_OK)
    return call.status();
  auto status = CheckPage(page, engine);
  if (status != BE_OK)
    return status;
  status = CheckConfig(state);
  if (status != BE_OK)
    return status;
  page->main->state_url =
      page->main->frame->GetDocument()->Url().GetString().Utf8();
  state->url_utf8 = page->main->state_url.c_str();
  state->navigation_id = page->main->pending   ? page->main->pending->id
                         : page->main->current ? page->main->current->id
                                               : 0;
  state->loading = page->main->loading ? 1u : 0u;
  state->pending = page->main->pending ? 1u : 0u;
  state->history_index = page->main->history.index();
  state->history_length = static_cast<uint32_t>(page->main->history.size());
  state->last_status = page->main->navigation_status;
  return BE_OK;
}

}  // extern "C"
