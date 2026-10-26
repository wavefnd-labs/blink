// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#include "standalone/engine/navigation_policy.h"

#include <tuple>
#include <utility>

#include "base/memory/scoped_refptr.h"
#include "base/strings/string_util.h"
#include "mojo/public/cpp/bindings/associated_remote.h"
#include "net/http/http_response_headers.h"
#include "net/http/http_util.h"
#include "services/network/public/cpp/content_security_policy/content_security_policy.h"
#include "services/network/public/cpp/content_security_policy/csp_context.h"
#include "services/network/public/cpp/web_sandbox_flags.h"
#include "services/network/public/cpp/x_frame_options_parser.h"
#include "services/network/public/mojom/content_security_policy.mojom-blink.h"
#include "services/network/public/mojom/web_sandbox_flags.mojom-shared.h"
#include "services/network/public/mojom/x_frame_options.mojom.h"
#include "third_party/blink/public/mojom/frame/policy_container.mojom-blink.h"
#include "third_party/blink/public/platform/web_policy_container.h"
#include "third_party/blink/renderer/platform/loader/fetch/policy_container_utils.h"
#include "third_party/blink/renderer/platform/network/http_parsers.h"
#include "third_party/blink/renderer/platform/weborigin/kurl.h"
#include "third_party/blink/renderer/platform/weborigin/security_policy.h"
#include "third_party/blink/renderer/platform/wtf/text/wtf_string.h"
#include "url/gurl.h"
#include "url/origin.h"

namespace blink_standalone {

std::unique_ptr<blink::WebPolicyContainer> MakeNavigationPolicy(
    const GURL& url,
    const std::vector<std::pair<std::string, std::string>>& headers) {
  if (!url.is_valid())
    return nullptr;
  auto container = std::make_unique<blink::WebPolicyContainer>();
  const blink::KURL policy_url(url);
  std::string referrer_headers;
  for (const auto& [name, value] : headers) {
    const bool enforce =
        base::EqualsCaseInsensitiveASCII(name, "content-security-policy");
    const bool report_only = base::EqualsCaseInsensitiveASCII(
        name, "content-security-policy-report-only");
    if (enforce || report_only) {
      const auto text = blink::String::FromUtf8(value);
      if (text.IsNull())
        return nullptr;
      const auto type =
          enforce ? network::mojom::blink::ContentSecurityPolicyType::kEnforce
                  : network::mojom::blink::ContentSecurityPolicyType::kReport;
      auto policies = blink::ParseContentSecurityPolicies(
          text, type, network::mojom::blink::ContentSecurityPolicySource::kHTTP,
          policy_url);
      for (const auto& policy : policies) {
        // The document origin is established from these flags before the CSP
        // is bound to its execution context. Preserve enforced sandboxing at
        // that earlier stage as well as in the parsed policy itself.
        if (enforce)
          container->policies.sandbox_flags |= policy->sandbox;
        container->policies.content_security_policies.push_back(
            blink::ToWebContentSecurityPolicy(*policy));
      }
    } else if (base::EqualsCaseInsensitiveASCII(name, "referrer-policy")) {
      if (!referrer_headers.empty())
        referrer_headers += ',';
      referrer_headers += value;
    }
  }
  if (!referrer_headers.empty()) {
    const auto text = blink::String::FromUtf8(referrer_headers);
    if (text.IsNull())
      return nullptr;
    auto policy = container->policies.referrer_policy;
    if (blink::SecurityPolicy::ReferrerPolicyFromHeaderValue(
            text, blink::kDoNotSupportReferrerPolicyLegacyKeywords, &policy))
      container->policies.referrer_policy = policy;
  }

  // Meta CSP/referrer tags update Blink's local container, then notify its
  // associated remote. Keep a bound endpoint even though there is no browser
  // policy store to notify, following PolicyContainer::CreateEmpty exactly.
  // A merely unbound WebPolicyContainer remote would CHECK on those updates.
  mojo::AssociatedRemote<blink::mojom::blink::PolicyContainerHost> local_host;
  std::ignore = local_host.BindNewEndpointAndPassDedicatedReceiver();
  container->remote = local_host.Unbind();
  return container;
}

bool AllowsFrameEmbedding(
    const GURL& url,
    const std::vector<std::pair<std::string, std::string>>& headers,
    const std::vector<url::Origin>& ancestors) {
  std::vector<network::mojom::ContentSecurityPolicyPtr> policies;
  bool has_ancestors = false;
  std::string raw = "HTTP/1.1 200 OK\r\n";
  for (const auto& [name, value] : headers) {
    raw += name + ": " + value + "\r\n";
    if (base::EqualsCaseInsensitiveASCII(name, "content-security-policy")) {
      auto parsed = network::ParseContentSecurityPolicies(
          value, network::mojom::ContentSecurityPolicyType::kEnforce,
          network::mojom::ContentSecurityPolicySource::kHTTP, url);
      for (auto& policy : parsed) {
        has_ancestors |= policy->directives.contains(
            network::mojom::CSPDirectiveName::FrameAncestors);
        policies.push_back(std::move(policy));
      }
    }
  }
  network::CSPContext context;
  for (const auto& origin : ancestors) {
    if (has_ancestors &&
        (origin.opaque() ||
         !context
              .IsAllowedByCsp(policies,
                              network::mojom::CSPDirectiveName::FrameAncestors,
                              origin.GetURL(), origin.GetURL(), false, nullptr,
                              network::CSPContext::CHECK_ENFORCED_CSP, false)
              .IsAllowed()))
      return false;
  }
  // An enforced frame-ancestors directive takes precedence over XFO.
  if (has_ancestors)
    return true;
  raw += "\r\n";
  auto response = base::MakeRefCounted<net::HttpResponseHeaders>(
      net::HttpUtil::AssembleRawHeaders(raw));
  const auto xfo = network::ParseXFrameOptions(*response);
  if (xfo == network::mojom::XFrameOptionsValue::kDeny ||
      xfo == network::mojom::XFrameOptionsValue::kConflict)
    return false;
  if (xfo == network::mojom::XFrameOptionsValue::kSameOrigin) {
    const auto self = url::Origin::Create(url);
    for (const auto& ancestor : ancestors)
      if (!self.IsSameOriginWith(ancestor))
        return false;
  }
  return true;
}

}  // namespace blink_standalone
