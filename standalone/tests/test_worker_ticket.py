from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class WorkerTicketTests(unittest.TestCase):
    def test_cross_thread_delivery_termination_and_late_response(self):
        compiler = shutil.which('c++')
        if not compiler:
            self.skipTest('C++ compiler unavailable')
        source = r'''
#include "standalone/platform/worker_fetch_ticket.h"
#include <cassert>
#include <string>
#include <thread>
using blink_standalone::WorkerFetchTicket;
using namespace std::chrono_literals;
int main() {
  const auto deadline = [] { return std::chrono::steady_clock::now() + 2s; };
  WorkerFetchTicket<std::string> delivered;
  std::thread owner([&] { assert(delivered.Complete("copied response")); });
  assert(delivered.Wait(deadline(), [] { return false; }));
  owner.join();
  assert(delivered.Take() == "copied response");
  assert(!delivered.Take()); // one consumer, no duplicate callbacks

  WorkerFetchTicket<std::string> terminated;
  std::atomic<bool> terminate_event{false};
  std::thread worker([&] {
    assert(!terminated.Wait(deadline(), [&] {
      return terminate_event.load(std::memory_order_acquire);
    }));
  });
  terminate_event.store(true, std::memory_order_release);
  worker.join();
  assert(terminated.cancelled());
  assert(!terminated.Complete("late host response"));
  assert(!terminated.Take());

  WorkerFetchTicket<std::string> pending;
  std::thread waiter([&] {
    assert(!pending.Wait(deadline(), [] { return false; }));
  });
  pending.Cancel(); // destroying an async URLLoader cancels a queued request
  waiter.join();
  assert(!pending.Complete("must not notify a destroyed URLLoader"));

  WorkerFetchTicket<std::string> timed_out;
  assert(!timed_out.Wait(std::chrono::steady_clock::now() + 1ms,
                        [] { return false; }));
  assert(timed_out.cancelled());
  assert(!timed_out.Complete("too late"));

  WorkerFetchTicket<std::string> arrived_then_cancelled;
  assert(arrived_then_cancelled.Complete("queued worker notification"));
  arrived_then_cancelled.Cancel();
  assert(!arrived_then_cancelled.Take());
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / 'ticket.cc'
            binary_path = Path(directory) / 'ticket'
            source_path.write_text(source)
            result = subprocess.run(
                [compiler, '-std=c++20', '-pthread', '-Wall', '-Wextra', '-Werror',
                 '-I', str(ROOT), str(source_path), '-o', str(binary_path)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(binary_path)], capture_output=True,
                           check=True, timeout=5)


if __name__ == '__main__':
    unittest.main()
