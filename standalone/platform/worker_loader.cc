// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#include "standalone/platform/worker_loader.h"

#include <chrono>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "base/containers/span.h"
#include "base/functional/bind.h"
#include "base/location.h"
#include "base/memory/raw_ptr.h"
#include "base/memory/raw_ref.h"
#include "base/memory/weak_ptr.h"
#include "base/synchronization/waitable_event.h"
#include "base/task/single_thread_task_runner.h"
#include "base/time/time.h"
#include "net/base/net_errors.h"
#include "net/cookies/site_for_cookies.h"
#include "services/network/public/cpp/resource_request.h"
#include "services/network/public/mojom/fetch_api.mojom-shared.h"
#include "standalone/common/limits.h"
#include "standalone/platform/host_platform.h"
#include "standalone/platform/host_url_loader.h"
#include "standalone/platform/worker_fetch_ticket.h"
#include "third_party/blink/public/platform/web_http_header_visitor.h"
#include "third_party/blink/public/platform/web_url_error.h"
#include "third_party/blink/public/platform/web_url_request.h"
#include "third_party/blink/public/platform/web_url_response.h"
#include "third_party/blink/renderer/platform/blob/blob_data.h"
#include "third_party/blink/renderer/platform/loader/fetch/resource_response.h"
#include "third_party/blink/renderer/platform/loader/fetch/url_loader/url_loader.h"
#include "third_party/blink/renderer/platform/loader/fetch/url_loader/url_loader_client.h"
#include "third_party/blink/renderer/platform/loader/fetch/url_loader/url_loader_factory.h"
#include "third_party/blink/renderer/platform/weborigin/kurl.h"
#include "third_party/blink/renderer/platform/weborigin/security_origin.h"
#include "third_party/blink/renderer/platform/wtf/shared_buffer.h"
#include "url/gurl.h"
#include "url/origin.h"

namespace blink_standalone {
namespace {

// No ResourceResponse, AtomicString, SharedBuffer, SecurityOrigin or other
// Blink heap object crosses threads. The worker rebuilds its own response.
struct FetchReply {
  int error = net::ERR_ACCESS_DENIED;
  std::string url;
  std::string mime;
  std::string charset;
  int status = 0;
  std::string status_text;
  std::vector<std::pair<std::string, std::string>> headers;
  std::vector<char> bytes;
  network::mojom::FetchResponseType type =
      network::mojom::FetchResponseType::kDefault;
  bool timing_allow_passed = false;
  bool include_credentials = false;
};
using FetchTicket = WorkerFetchTicket<FetchReply>;

class HeaderSnapshot final : public blink::WebHTTPHeaderVisitor {
 public:
  explicit HeaderSnapshot(FetchReply& reply) : reply_(reply) {}
  void VisitHeader(const blink::WebString& name,
                   const blink::WebString& value) override {
    std::string key = name.Utf8();
    std::string val = value.Utf8();
    if (key.size() > kMaxResourceBytes - size_ ||
        val.size() > kMaxResourceBytes - size_ - key.size()) {
      valid_ = false;
      return;
    }
    size_ += key.size() + val.size();
    reply_->headers.emplace_back(std::move(key), std::move(val));
  }
  bool valid() const { return valid_; }

 private:
  const base::raw_ref<FetchReply> reply_;
  size_t size_ = 0;
  bool valid_ = true;
};

bool AllowedURL(const GURL& url, const url::Origin& origin) {
  return url.SchemeIsHTTPOrHTTPS() && !url.has_username() &&
         !url.has_password() &&
         origin.IsSameOriginWith(url::Origin::Create(url));
}

// Only executes on the engine owner thread. Both the legacy resource callback
// and any installed HTTP transport therefore retain their C ABI threading
// contract, including release callbacks. This invokes the same transport used
// by document subresources, rather than introducing a browser/network service.
void FetchOnOwner(HostServices* services,
                  url::Origin origin,
                  url::Origin top_origin,
                  std::unique_ptr<network::ResourceRequest> request,
                  std::shared_ptr<FetchTicket> ticket,
                  scoped_refptr<base::SingleThreadTaskRunner> worker_runner,
                  base::OnceClosure notify) {
  if (ticket->cancelled())
    return;
  FetchReply reply;
  reply.url = request->url.spec();
  // This host deliberately supports a narrower worker transport than the web
  // API's full CORS profile. Preserve that restriction during redirects too.
  request->mode = network::mojom::RequestMode::kSameOrigin;
  auto loader = MakeHostURLLoader(*services);
  blink::WebURLResponse response;
  std::optional<blink::WebURLError> error;
  scoped_refptr<blink::SharedBuffer> body;
  scoped_refptr<blink::BlobDataHandle> blob;
  int64_t encoded_length = 0;
  uint64_t encoded_body_length = 0;
  loader->LoadSynchronously(
      std::move(request),
      blink::SecurityOrigin::CreateFromUrlOrigin(top_origin), false, true,
      base::TimeDelta(), nullptr, response, error, body, encoded_length,
      encoded_body_length, blob, nullptr);
  if (error) {
    reply.error = error->reason();
  } else if (!response.IsNull() && body && body->size() <= kMaxResourceBytes &&
             AllowedURL(GURL(response.ResponseUrl().GetString().Utf8()),
                        origin) &&
             !response.WasFetchedViaServiceWorker() &&
             response.GetType() != network::mojom::FetchResponseType::kOpaque &&
             response.GetType() != network::mojom::FetchResponseType::kError &&
             response.GetType() !=
                 network::mojom::FetchResponseType::kOpaqueRedirect) {
    reply.url = response.CurrentRequestUrl().GetString().Utf8();
    reply.mime = response.MimeType().Utf8();
    reply.charset =
        blink::WebString(response.ToResourceResponse().TextEncodingName())
            .Utf8();
    reply.status = response.HttpStatusCode();
    reply.status_text = response.HttpStatusText().Utf8();
    reply.type = response.GetType();
    reply.timing_allow_passed = response.TimingAllowPassed();
    reply.include_credentials = response.RequestIncludeCredentials();
    HeaderSnapshot headers(reply);
    response.VisitHttpHeaderFields(&headers);
    if (headers.valid()) {
      reply.bytes.reserve(body->size());
      for (const auto segment : *body)
        reply.bytes.insert(reply.bytes.end(), segment.begin(), segment.end());
      reply.error = net::OK;
    }
  }
  if (ticket->Complete(std::move(reply)) && notify)
    worker_runner->PostTask(FROM_HERE, std::move(notify));
}

blink::WebURLResponse RestoreResponse(const FetchReply& reply) {
  blink::WebURLResponse response(blink::WebURL(blink::KURL(GURL(reply.url))));
  response.SetHttpStatusCode(reply.status);
  response.SetHttpStatusText(blink::WebString::FromUtf8(reply.status_text));
  response.SetMimeType(blink::WebString::FromUtf8(reply.mime));
  response.SetTextEncodingName(blink::WebString::FromUtf8(reply.charset));
  response.SetExpectedContentLength(static_cast<int64_t>(reply.bytes.size()));
  response.SetType(reply.type);
  response.SetTimingAllowPassed(reply.timing_allow_passed);
  response.SetRequestIncludeCredentials(reply.include_credentials);
  for (const auto& [name, value] : reply.headers)
    response.AddHttpHeaderField(blink::WebString::FromUtf8(name),
                                blink::WebString::FromUtf8(value));
  return response;
}

class WorkerURLLoader final : public blink::URLLoader {
 public:
  WorkerURLLoader(HostServices& services,
                  scoped_refptr<base::SingleThreadTaskRunner> owner_runner,
                  scoped_refptr<base::SingleThreadTaskRunner> worker_runner,
                  const url::Origin& origin,
                  const url::Origin& top_origin,
                  base::WaitableEvent* terminate_event,
                  bool enabled)
      : services_(services),
        owner_runner_(std::move(owner_runner)),
        worker_runner_(std::move(worker_runner)),
        origin_(origin),
        top_origin_(top_origin),
        terminate_event_(terminate_event),
        enabled_(enabled) {}
  ~WorkerURLLoader() override {
    if (ticket_)
      ticket_->Cancel();
  }

  void LoadSynchronously(
      std::unique_ptr<network::ResourceRequest> request,
      scoped_refptr<const blink::SecurityOrigin>,
      bool download_to_blob,
      bool,
      base::TimeDelta timeout,
      blink::URLLoaderClient*,
      blink::WebURLResponse& response,
      std::optional<blink::WebURLError>& error,
      scoped_refptr<blink::SharedBuffer>& data,
      int64_t& encoded_length,
      uint64_t& encoded_body_length,
      scoped_refptr<blink::BlobDataHandle>& downloaded_blob,
      std::unique_ptr<blink::ResourceLoadInfoNotifierWrapper>) override {
    const GURL url = request->url;
    data.reset();
    downloaded_blob.reset();
    encoded_length = 0;
    encoded_body_length = 0;
    if (download_to_blob || !Allowed(*request)) {
      error.emplace(net::ERR_ACCESS_DENIED, blink::WebURL(blink::KURL(url)));
      return;
    }
    ticket_ = std::make_shared<FetchTicket>();
    Post(std::move(request), base::OnceClosure());
    const auto now = std::chrono::steady_clock::now();
    auto deadline = std::chrono::steady_clock::time_point::max();
    if (timeout.is_positive() && !timeout.is_max()) {
      const auto duration = std::chrono::microseconds(timeout.InMicroseconds());
      const auto available =
          std::chrono::duration_cast<std::chrono::microseconds>(deadline - now);
      if (duration < available)
        deadline = now + duration;
    }
    if (!ticket_->Wait(deadline, [this] {
          return terminate_event_ && terminate_event_->IsSignaled();
        })) {
      error.emplace(net::ERR_ABORTED, blink::WebURL(blink::KURL(url)));
      return;
    }
    auto reply = ticket_->Take();
    ticket_.reset();
    if (!reply || reply->error != net::OK) {
      error.emplace(reply ? reply->error : net::ERR_ABORTED,
                    blink::WebURL(blink::KURL(url)));
      return;
    }
    response = RestoreResponse(*reply);
    data = blink::SharedBuffer::Create(base::span(reply->bytes));
    encoded_length = static_cast<int64_t>(reply->bytes.size());
    encoded_body_length = reply->bytes.size();
    error.reset();
  }

  void LoadAsynchronously(
      std::unique_ptr<network::ResourceRequest> request,
      scoped_refptr<const blink::SecurityOrigin>,
      bool,
      std::unique_ptr<blink::ResourceLoadInfoNotifierWrapper>,
      blink::CodeCacheHost*,
      blink::URLLoaderClient* client) override {
    client_ = client;
    url_ = request->url;
    ticket_ = std::make_shared<FetchTicket>();
    auto notify =
        base::BindOnce(&WorkerURLLoader::Ready, weak_factory_.GetWeakPtr());
    if (!Allowed(*request)) {
      FetchReply denied;
      denied.url = url_.spec();
      ticket_->Complete(std::move(denied));
      worker_runner_->PostTask(FROM_HERE, std::move(notify));
      return;
    }
    Post(std::move(request), std::move(notify));
  }

  void Freeze(blink::LoaderFreezeMode mode) override {
    frozen_ = mode != blink::LoaderFreezeMode::kNone;
    if (!frozen_ && ticket_)
      worker_runner_->PostTask(
          FROM_HERE,
          base::BindOnce(&WorkerURLLoader::Ready, weak_factory_.GetWeakPtr()));
  }
  void DidChangePriority(blink::WebURLRequest::Priority, int) override {}
  scoped_refptr<base::SingleThreadTaskRunner> GetTaskRunnerForBodyLoader()
      override {
    return worker_runner_;
  }

 private:
  bool Allowed(const network::ResourceRequest& request) const {
    return enabled_ && request.method == "GET" &&
           AllowedURL(request.url, origin_) &&
           (!terminate_event_ || !terminate_event_->IsSignaled());
  }
  void Post(std::unique_ptr<network::ResourceRequest> request,
            base::OnceClosure notify) {
    const bool posted = owner_runner_->PostTask(
        FROM_HERE, base::BindOnce(&FetchOnOwner, base::Unretained(&*services_),
                                  origin_, top_origin_, std::move(request),
                                  ticket_, worker_runner_, std::move(notify)));
    if (!posted) {
      FetchReply failed;
      failed.error = net::ERR_ABORTED;
      ticket_->Complete(std::move(failed));
      worker_runner_->PostTask(
          FROM_HERE,
          base::BindOnce(&WorkerURLLoader::Ready, weak_factory_.GetWeakPtr()));
    }
  }
  void Ready() {
    if (frozen_ || !ticket_ || !client_)
      return;
    auto reply = ticket_->Take();
    if (!reply)
      return;
    ticket_.reset();
    if (reply->error != net::OK) {
      client_->DidFail(
          blink::WebURLError(reply->error, blink::WebURL(blink::KURL(url_))),
          base::TimeTicks::Now(), 0, 0, 0);
      return;
    }
    auto self = weak_factory_.GetWeakPtr();
    blink::SegmentedBuffer body;
    body.Append(base::span(reply->bytes));
    auto pipe = MakeHostBodyPipe(body);
    if (!pipe) {
      client_->DidFail(blink::WebURLError(net::ERR_INSUFFICIENT_RESOURCES,
                          blink::WebURL(blink::KURL(url_))),
                       base::TimeTicks::Now(), 0, 0, 0);
      return;
    }
    const int64_t size = static_cast<int64_t>(reply->bytes.size());
    client_->DidReceiveResponse(RestoreResponse(*reply), std::move(pipe),
                                std::nullopt);
    if (self)
      client_->DidFinishLoading(base::TimeTicks::Now(), size,
                                static_cast<uint64_t>(size), size);
  }

  const base::raw_ref<HostServices> services_;
  const scoped_refptr<base::SingleThreadTaskRunner> owner_runner_;
  const scoped_refptr<base::SingleThreadTaskRunner> worker_runner_;
  const url::Origin origin_;
  const url::Origin top_origin_;
  const base::raw_ptr<base::WaitableEvent> terminate_event_;
  const bool enabled_;
  base::raw_ptr<blink::URLLoaderClient> client_ = nullptr;
  GURL url_;
  std::shared_ptr<FetchTicket> ticket_;
  bool frozen_ = false;
  base::WeakPtrFactory<WorkerURLLoader> weak_factory_{this};
};

class WorkerURLLoaderFactory final : public blink::URLLoaderFactory {
 public:
  WorkerURLLoaderFactory(HostServices& services,
                         scoped_refptr<base::SingleThreadTaskRunner> owner,
                         const url::Origin& origin,
                         const url::Origin& top_origin,
                         bool enabled)
      : services_(services),
        owner_(std::move(owner)),
        origin_(origin),
        top_origin_(top_origin),
        enabled_(enabled) {}
  void SetTerminateEvent(base::WaitableEvent* event) {
    terminate_sync_load_event_ = event;
  }
  std::unique_ptr<blink::URLLoader> CreateURLLoader(
      const network::ResourceRequest&,
      scoped_refptr<base::SingleThreadTaskRunner> freezable,
      scoped_refptr<base::SingleThreadTaskRunner>,
      mojo::PendingRemote<blink::mojom::blink::KeepAliveHandle> keep_alive,
      blink::BackForwardCacheLoaderHelper*,
      blink::Vector<std::unique_ptr<blink::URLLoaderThrottle>> throttles)
      override {
    return std::make_unique<WorkerURLLoader>(
        *services_, owner_, std::move(freezable), origin_, top_origin_,
        terminate_sync_load_event_,
        enabled_ && !keep_alive.is_valid() && throttles.empty());
  }

 private:
  const base::raw_ref<HostServices> services_;
  const scoped_refptr<base::SingleThreadTaskRunner> owner_;
  const url::Origin origin_;
  const url::Origin top_origin_;
  const bool enabled_;
};

class HostWorkerFetchContext final : public blink::WebWorkerFetchContext {
 public:
  HostWorkerFetchContext(HostServices& services,
                         scoped_refptr<base::SingleThreadTaskRunner> owner,
                         const url::Origin& origin,
                         const url::Origin& top_origin)
      : WebWorkerFetchContext(blink::RendererPreferences()),
        services_(services),
        owner_(std::move(owner)),
        origin_(origin),
        top_origin_(top_origin) {}
  void SetTerminateSyncLoadEvent(base::WaitableEvent* event) override {
    terminate_event_ = event;
  }
  void InitializeOnWorkerThread(blink::AcceptLanguagesWatcher*) override {
    factory_ = std::make_unique<WorkerURLLoaderFactory>(
        *services_, owner_, origin_, top_origin_, true);
    factory_->SetTerminateEvent(terminate_event_);
  }
  blink::URLLoaderFactory* GetURLLoaderFactory() override {
    return factory_.get();
  }
  // Dedicated workers use the normal factory for scripts as well. The
  // separate script factory is reserved for service workers; retain the
  // interface's null default for this dedicated-worker host.
  std::unique_ptr<blink::URLLoaderFactory> WrapURLLoaderFactory(
      blink::CrossVariantMojoRemote<
          network::mojom::URLLoaderFactoryInterfaceBase>) override {
    // Alternate factories (service workers, bundles, etc.) cannot bypass this
    // host's transport policy. Return an explicit failing factory, not nullptr.
    auto denied = std::make_unique<WorkerURLLoaderFactory>(
        *services_, owner_, origin_, top_origin_, false);
    denied->SetTerminateEvent(terminate_event_);
    return denied;
  }
  void FinalizeRequest(blink::WebURLRequest&) override {}
  std::vector<std::unique_ptr<blink::URLLoaderThrottle>> CreateThrottles(
      const network::ResourceRequest&) override {
    return {};
  }
  blink::mojom::ControllerServiceWorkerMode GetControllerServiceWorkerMode()
      const override {
    return blink::mojom::ControllerServiceWorkerMode::kNoController;
  }
  void SetIsOnSubframe(bool value) override { is_on_subframe_ = value; }
  bool IsOnSubframe() const override { return is_on_subframe_; }
  net::SiteForCookies SiteForCookies() const override {
    return net::SiteForCookies::FromOrigin(top_origin_);
  }
  std::optional<blink::WebSecurityOrigin> TopFrameOrigin() const override {
    return blink::WebSecurityOrigin(top_origin_);
  }
  blink::WebString GetAcceptLanguages() const override {
    return blink::WebString::FromUtf8("en-US,en");
  }
  bool IsDedicatedWorkerOrSharedWorkerFetchContext() const override {
    return true;
  }

 private:
  ~HostWorkerFetchContext() override;

  const base::raw_ref<HostServices> services_;
  const scoped_refptr<base::SingleThreadTaskRunner> owner_;
  const url::Origin origin_;
  const url::Origin top_origin_;
  base::raw_ptr<base::WaitableEvent> terminate_event_ = nullptr;
  std::unique_ptr<WorkerURLLoaderFactory> factory_;
  bool is_on_subframe_ = false;
};

HostWorkerFetchContext::~HostWorkerFetchContext() = default;

}  // namespace

scoped_refptr<blink::WebWorkerFetchContext> MakeWorkerFetchContext(
    HostServices& services,
    scoped_refptr<base::SingleThreadTaskRunner> owner_task_runner,
    const url::Origin& worker_origin,
    const url::Origin& top_frame_origin) {
  return base::AdoptRef(new HostWorkerFetchContext(
      services, std::move(owner_task_runner), worker_origin, top_frame_origin));
}

}  // namespace blink_standalone
