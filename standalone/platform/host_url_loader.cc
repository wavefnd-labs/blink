// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#include "standalone/platform/host_url_loader.h"
#include <optional>
#include <string>
#include "base/compiler_specific.h"
#include "base/containers/span.h"
#include "base/functional/bind.h"
#include "base/location.h"
#include "base/memory/raw_ptr.h"
#include "base/memory/raw_ref.h"
#include "base/memory/weak_ptr.h"
#include "base/task/single_thread_task_runner.h"
#include "net/base/net_errors.h"
#include "services/network/public/cpp/resource_request.h"
#include "standalone/common/limits.h"
#include "standalone/platform/host_platform.h"
#include "third_party/blink/public/platform/resource_load_info_notifier_wrapper.h"
#include "third_party/blink/public/platform/web_security_origin.h"
#include "third_party/blink/public/platform/web_url_error.h"
#include "third_party/blink/public/platform/web_url_response.h"
#include "third_party/blink/renderer/platform/blob/blob_data.h"
#include "third_party/blink/renderer/platform/loader/fetch/url_loader/url_loader_client.h"
#include "third_party/blink/renderer/platform/weborigin/kurl.h"
#include "third_party/blink/renderer/platform/weborigin/security_origin.h"
#include "third_party/blink/renderer/platform/wtf/shared_buffer.h"

namespace blink_standalone {
namespace {
class HostURLLoader final : public blink::URLLoader {
 public:
  explicit HostURLLoader(HostServices& services)
      : services_(services),
        task_runner_(base::SingleThreadTaskRunner::GetCurrentDefault()) {}
  ~HostURLLoader() override = default;

  void LoadSynchronously(
      std::unique_ptr<network::ResourceRequest> request,
      scoped_refptr<const blink::SecurityOrigin> top_frame_origin,
      bool download_to_blob,
      bool no_mime_sniffing,
      base::TimeDelta timeout_interval,
      blink::URLLoaderClient* client,
      blink::WebURLResponse& response,
      std::optional<blink::WebURLError>& error,
      scoped_refptr<blink::SharedBuffer>& data,
      int64_t& encoded_data_length,
      uint64_t& encoded_body_length,
      scoped_refptr<blink::BlobDataHandle>& downloaded_blob,
      std::unique_ptr<blink::ResourceLoadInfoNotifierWrapper> notifier)
      override {
    encoded_data_length = 0;
    encoded_body_length = 0;
    downloaded_blob.reset();
    data.reset();
    if (download_to_blob || !Fetch(*request, response, data)) {
      error.emplace(net::ERR_ACCESS_DENIED,
                    blink::WebURL(blink::KURL(request->url)));
      return;
    }
    error.reset();
    encoded_data_length = static_cast<int64_t>(data->size());
    encoded_body_length = data->size();
  }

  void LoadAsynchronously(
      std::unique_ptr<network::ResourceRequest> request,
      scoped_refptr<const blink::SecurityOrigin> top_frame_origin,
      bool no_mime_sniffing,
      std::unique_ptr<blink::ResourceLoadInfoNotifierWrapper> notifier,
      blink::CodeCacheHost* code_cache_host,
      blink::URLLoaderClient* client) override {
    request_ = std::move(request);
    client_ = client;
    task_runner_->PostTask(
        FROM_HERE,
        base::BindOnce(&HostURLLoader::Serve, weak_factory_.GetWeakPtr()));
  }
  void Freeze(blink::LoaderFreezeMode mode) override {
    frozen_ = mode != blink::LoaderFreezeMode::kNone;
    if (!frozen_ && request_)
      task_runner_->PostTask(
          FROM_HERE,
          base::BindOnce(&HostURLLoader::Serve, weak_factory_.GetWeakPtr()));
  }
  void DidChangePriority(blink::WebURLRequest::Priority, int) override {}
  scoped_refptr<base::SingleThreadTaskRunner> GetTaskRunnerForBodyLoader()
      override {
    return task_runner_;
  }

 private:
  bool Fetch(const network::ResourceRequest& request,
             blink::WebURLResponse& response,
             scoped_refptr<blink::SharedBuffer>& data) {
    const auto& config = services_->config();
    if (!config.fetch_resource || request.method != "GET" ||
        !request.url.SchemeIsHTTPOrHTTPS() || request.url.has_username() ||
        request.url.has_password())
      return false;
    be_resource_response result{};
    result.struct_size = sizeof(result);
    result.abi_version = BE_ABI_VERSION;
    const std::string transport_url = request.url.GetWithoutRef().spec();
    const auto status =
        config.fetch_resource(config.user_data, transport_url.c_str(), &result);
    if (status != BE_OK)
      return false;
    const bool valid = result.abi_version == BE_ABI_VERSION &&
                       result.struct_size >= sizeof(result) &&
                       result.size <= kMaxResourceBytes &&
                       (result.size == 0 || result.data) && result.mime_type;
    if (valid) {
      // The callback ABI guarantees size readable bytes until release(). The
      // size cap and nonempty/null relationship were checked immediately above.
      const auto bytes = UNSAFE_BUFFERS(base::span(result.data, result.size));
      data = blink::SharedBuffer::Create(bytes);
      response = blink::WebURLResponse(blink::WebURL(blink::KURL(request.url)));
      response.SetHttpStatusCode(200);
      response.SetMimeType(blink::WebString::FromUtf8(result.mime_type));
      // importScripts() and other strict script loaders inspect the HTTP
      // Content-Type header, independently of ResourceResponse's MIME field.
      response.AddHttpHeaderField(blink::WebString::FromUtf8("Content-Type"),
                                  blink::WebString::FromUtf8(result.mime_type));
      response.SetTextEncodingName(blink::WebString::FromUtf8(
          result.charset ? result.charset : "UTF-8"));
      response.SetExpectedContentLength(static_cast<int64_t>(result.size));
    }
    if (result.release)
      result.release(result.release_user_data);
    return valid;
  }
  void Serve() {
    if (frozen_ || !request_ || !client_)
      return;
    auto request = std::move(request_);
    blink::WebURLResponse response;
    scoped_refptr<blink::SharedBuffer> data;
    auto self = weak_factory_.GetWeakPtr();
    if (!Fetch(*request, response, data)) {
      client_->DidFail(
          blink::WebURLError(net::ERR_ACCESS_DENIED,
                             blink::WebURL(blink::KURL(request->url))),
          base::TimeTicks::Now(), 0, 0, 0);
      return;
    }
    // Fetch/XHR require a real body consumer, including for an empty body.
    // SegmentedBuffer's optimized background-response path bypasses that
    // notification and is not suitable for a generic host URL loader.
    auto body = MakeHostBodyPipe(*data);
    if (!body) {
      client_->DidFail(blink::WebURLError(net::ERR_INSUFFICIENT_RESOURCES,
                         blink::WebURL(blink::KURL(request->url))),
                       base::TimeTicks::Now(), 0, 0, 0);
      return;
    }
    const auto size = static_cast<int64_t>(data->size());
    client_->DidReceiveResponse(response, std::move(body), std::nullopt);
    if (self)
      client_->DidFinishLoading(base::TimeTicks::Now(), size,
                                static_cast<uint64_t>(size), size);
  }
  const base::raw_ref<HostServices> services_;
  scoped_refptr<base::SingleThreadTaskRunner> task_runner_;
  std::unique_ptr<network::ResourceRequest> request_;
  base::raw_ptr<blink::URLLoaderClient> client_ = nullptr;
  bool frozen_ = false;
  base::WeakPtrFactory<HostURLLoader> weak_factory_{this};
};
}  // namespace
mojo::ScopedDataPipeConsumerHandle MakeHostBodyPipe(
    const blink::SegmentedBuffer& body) {
  if (body.size() > kMaxResourceBytes)
    return {};
  MojoCreateDataPipeOptions options{};
  options.struct_size = sizeof(options);
  options.element_num_bytes = 1;
  options.capacity_num_bytes =
      static_cast<uint32_t>(body.size() ? body.size() : 1);
  mojo::ScopedDataPipeProducerHandle producer;
  mojo::ScopedDataPipeConsumerHandle consumer;
  if (mojo::CreateDataPipe(&options, producer, consumer) != MOJO_RESULT_OK)
    return {};
  for (const auto segment : body) {
    if (producer->WriteAllData(base::as_byte_span(segment)) != MOJO_RESULT_OK)
      return {};
  }
  // Closing the producer publishes EOF after the bounded immutable response.
  return consumer;
}
std::unique_ptr<blink::URLLoader> MakeHostURLLoader(HostServices& services) {
  return std::make_unique<HostURLLoader>(services);
}
}  // namespace blink_standalone
