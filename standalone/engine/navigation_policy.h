// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#ifndef BLINK_STANDALONE_ENGINE_NAVIGATION_POLICY_H_
#define BLINK_STANDALONE_ENGINE_NAVIGATION_POLICY_H_

#include <memory>
#include <string>
#include <utility>
#include <vector>

class GURL;
namespace url {
class Origin;
}
namespace blink {
struct WebPolicyContainer;
}

namespace blink_standalone {

// Parse the supported HTTP document policies with Blink's production parsers.
// A null result rejects an invalid URL or invalid UTF-8 policy header. The
// caller must fail the navigation rather than substitute an empty container.
// The input headers must already pass the response header syntax/size checks.
// This supplies CSP (enforce and report-only), CSP sandbox flags and referrer
// policy. It does not implement other browser-side response policy decisions.
std::unique_ptr<blink::WebPolicyContainer> MakeNavigationPolicy(
    const GURL& url,
    const std::vector<std::pair<std::string, std::string>>& headers);

// Browser-independent response-side embedding checks for every ancestor.
bool AllowsFrameEmbedding(
    const GURL& url,
    const std::vector<std::pair<std::string, std::string>>& headers,
    const std::vector<url::Origin>& ancestors);

}  // namespace blink_standalone
#endif  // BLINK_STANDALONE_ENGINE_NAVIGATION_POLICY_H_
