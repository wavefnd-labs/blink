// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#ifndef BLINK_STANDALONE_PLATFORM_WORKER_LOADER_H_
#define BLINK_STANDALONE_PLATFORM_WORKER_LOADER_H_

#include <memory>

#include "base/memory/scoped_refptr.h"
#include "third_party/blink/public/platform/web_worker_fetch_context.h"

namespace base {
class SingleThreadTaskRunner;
}
namespace url {
class Origin;
}

namespace blink_standalone {
class HostServices;

// Construct on the parent context thread, then transfer to Blink's worker
// thread. Every host resource callback is marshalled to owner_task_runner;
// only copied plain data crosses back to the worker's heap and task runner.
scoped_refptr<blink::WebWorkerFetchContext> MakeWorkerFetchContext(
    HostServices& services,
    scoped_refptr<base::SingleThreadTaskRunner> owner_task_runner,
    const url::Origin& worker_origin,
    const url::Origin& top_frame_origin);

}  // namespace blink_standalone

#endif
