// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#ifndef BLINK_STANDALONE_COMMON_NAVIGATION_H_
#define BLINK_STANDALONE_COMMON_NAVIGATION_H_
#include <algorithm>
#include <cstdint>
#include <string>
#include <string_view>
#include <utility>
#include <vector>
namespace blink_standalone {
inline constexpr size_t kMaxNavigationHeaders = 64 * 1024;
inline bool HeaderToken(std::string_view value) {
  if (value.empty())
    return false;
  for (char character : value) {
    const auto c = static_cast<unsigned char>(character);
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') ||
          std::string_view("!#$%&'*+-.^_`|~").find(c) !=
              std::string_view::npos))
      return false;
  }
  return true;
}
inline bool ParseNavigationHeaders(
    std::string_view text,
    std::vector<std::pair<std::string, std::string>>& headers) {
  headers.clear();
  if (text.size() > kMaxNavigationHeaders)
    return false;
  while (!text.empty()) {
    auto end = text.find("\r\n");
    auto line = text.substr(0, end);
    if (line.empty())
      return false;
    auto colon = line.find(':');
    if (colon == std::string_view::npos || !HeaderToken(line.substr(0, colon)))
      return false;
    auto value = line.substr(colon + 1);
    while (!value.empty() && (value.front() == ' ' || value.front() == '\t'))
      value.remove_prefix(1);
    for (char character : value) {
      const auto c = static_cast<unsigned char>(character);
      if ((c < 32 && c != '\t') || c == 127)
        return false;
    }
    headers.emplace_back(line.substr(0, colon), value);
    if (end == std::string_view::npos)
      break;
    text.remove_prefix(end + 2);
  }
  return true;
}
inline bool RedirectStatus(uint32_t code) {
  return code == 301 || code == 302 || code == 303 || code == 307 ||
         code == 308;
}
inline bool RedirectBecomesGet(uint32_t code, std::string_view method) {
  return (code == 303 && method != "HEAD") ||
         ((code == 301 || code == 302) && method == "POST");
}
inline std::string NavigationHeaderName(std::string name) {
  for (char& c : name)
    if (c >= 'A' && c <= 'Z')
      c = static_cast<char>(c + ('a' - 'A'));
  return name;
}
inline bool UnsupportedNavigationPolicy(const std::string& name) {
  const auto lower = NavigationHeaderName(name);
  return lower == "permissions-policy" || lower == "document-policy" ||
         lower.starts_with("cross-origin-opener-policy") ||
         lower.starts_with("cross-origin-embedder-policy") ||
         lower.starts_with("document-isolation-policy") ||
         lower.starts_with("integrity-policy") ||
         lower == "origin-agent-cluster";
}
inline bool RedirectRequestHeaders(std::string_view original,
                                   bool becomes_get,
                                   bool cross_origin,
                                   std::string& result) {
  std::vector<std::pair<std::string, std::string>> headers;
  if (!ParseNavigationHeaders(original, headers))
    return false;
  result.clear();
  for (const auto& [name, value] : headers) {
    const auto lower = NavigationHeaderName(name);
    if (becomes_get &&
        (lower.starts_with("content-") || lower == "transfer-encoding"))
      continue;
    if (cross_origin && (lower == "authorization" ||
                         lower == "proxy-authorization" || lower == "cookie"))
      continue;
    result += name + ": " + value + "\r\n";
  }
  return true;
}
template <typename Entry>
class SessionHistory {
 public:
  static constexpr size_t kCapacity = 64;
  int index() const { return index_; }
  size_t size() const { return entries_.size(); }
  int Target(int32_t offset) const {
    const auto target = int64_t(index_) + offset;
    return index_ >= 0 && target >= 0 && target < int64_t(entries_.size())
               ? static_cast<int>(target)
               : -1;
  }
  const Entry* Current() const { return At(index_); }
  const Entry* At(int index) const {
    return index >= 0 && size_t(index) < entries_.size()
               ? &entries_[size_t(index)]
               : nullptr;
  }
  void Commit(Entry entry, bool replace, int traversal = -1) {
    if (traversal >= 0 && At(traversal)) {
      index_ = traversal;
      entries_[size_t(index_)] = std::move(entry);
    } else if (replace && Current()) {
      entries_[size_t(index_)] = std::move(entry);
    } else {
      entries_.resize(size_t(index_ + 1));
      entries_.push_back(std::move(entry));
      if (entries_.size() > kCapacity)
        entries_.erase(entries_.begin());
      index_ = static_cast<int>(entries_.size()) - 1;
    }
  }
  void Clear() {
    entries_.clear();
    index_ = -1;
  }

 private:
  std::vector<Entry> entries_;
  int index_ = -1;
};
}  // namespace blink_standalone
#endif
