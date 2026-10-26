// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#ifndef BLINK_STANDALONE_PLATFORM_WORKER_FETCH_TICKET_H_
#define BLINK_STANDALONE_PLATFORM_WORKER_FETCH_TICKET_H_

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <mutex>
#include <optional>
#include <utility>

namespace blink_standalone {

// Plain-data handoff between the owner transport and a worker. Cancellation
// never destroys a running host callback: its reply is discarded instead.
// Polling the Blink termination event keeps synchronous importScripts from
// preventing Worker.terminate() while the owner has not serviced the request.
template <class Reply>
class WorkerFetchTicket {
 public:
  bool cancelled() const { return cancelled_.load(std::memory_order_acquire); }

  void Cancel() {
    cancelled_.store(true, std::memory_order_release);
    changed_.notify_all();
  }

  bool Complete(Reply reply) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (cancelled())
      return false;
    reply_.emplace(std::move(reply));
    changed_.notify_all();
    return true;
  }

  template <class ShouldCancel>
  bool Wait(std::chrono::steady_clock::time_point deadline,
            ShouldCancel should_cancel) {
    std::unique_lock<std::mutex> lock(mutex_);
    for (;;) {
      if (cancelled() || should_cancel()) {
        Cancel();
        return false;
      }
      if (reply_)
        return true;
      const auto now = std::chrono::steady_clock::now();
      if (now >= deadline) {
        Cancel();
        return false;
      }
      changed_.wait_until(
          lock, std::min(deadline, now + std::chrono::milliseconds(5)));
    }
  }

  std::optional<Reply> Take() {
    std::lock_guard<std::mutex> lock(mutex_);
    if (cancelled())
      return std::nullopt;
    return std::exchange(reply_, std::nullopt);
  }

 private:
  std::atomic<bool> cancelled_{false};
  std::mutex mutex_;
  std::condition_variable changed_;
  std::optional<Reply> reply_;
};

}  // namespace blink_standalone

#endif
