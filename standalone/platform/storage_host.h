// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#ifndef BLINK_STANDALONE_PLATFORM_STORAGE_HOST_H_
#define BLINK_STANDALONE_PLATFORM_STORAGE_HOST_H_

#include <memory>
#include <string>

#include "base/memory/scoped_refptr.h"
#include "standalone/include/blink_storage.h"
#include "third_party/blink/public/common/tokens/tokens.h"

namespace base {
class SingleThreadTaskRunner;
}
namespace blink {
class LocalFrame;
class LocalDOMWindow;
class Page;
class StorageKey;
class ThreadSafeBrowserInterfaceBrokerProxy;
}  // namespace blink

namespace blink_standalone {

// Native Blink DOM storage, backed by our own in-process Mojo service on the
// IO runner. Sync renderer reads must never dispatch to that same thread.
class StorageHost {
 public:
  explicit StorageHost(scoped_refptr<base::SingleThreadTaskRunner> io_runner);
  ~StorageHost();
  StorageHost(const StorageHost&) = delete;
  StorageHost& operator=(const StorageHost&) = delete;
  blink::ThreadSafeBrowserInterfaceBrokerProxy* Broker() const;
  be_status Configure(const be_storage_client* client);
  bool enabled() const;
  std::string CreateSessionNamespace(blink::Page& page);
  // Register initial frames and update before every cross-document commit,
  // using the exact StorageKey passed to WebNavigationParams::storage_key.
  void RegisterFrame(const blink::LocalFrameToken& token,
                     const std::string& namespace_id,
                     const blink::StorageKey& key);
  // Call from DispatchDidClearWindowObjectInMainWorld, before document script.
  // A real lifecycle observer flushes unload mutations before the window is
  // disconnected; WillBeDetached is too late to access the window's frame.
  void ObserveDocument(blink::LocalDOMWindow& window);
  void RemoveFrame(const blink::LocalFrameToken& token);
  void RemoveSessionNamespace(const std::string& namespace_id);
  be_status FlushFrame(blink::LocalFrame& frame);
  be_status last_status() const;
  void Shutdown();

 private:
  class Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace blink_standalone
#endif
