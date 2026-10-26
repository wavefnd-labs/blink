// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#ifndef BLINK_STANDALONE_PLATFORM_HOST_URL_LOADER_H_
#define BLINK_STANDALONE_PLATFORM_HOST_URL_LOADER_H_
#include <memory>
#include "mojo/public/cpp/system/data_pipe.h"
#include "third_party/blink/renderer/platform/loader/fetch/url_loader/url_loader.h"
namespace blink { class SegmentedBuffer; }
namespace blink_standalone {
class HostServices;
mojo::ScopedDataPipeConsumerHandle MakeHostBodyPipe(
    const blink::SegmentedBuffer& body);
std::unique_ptr<blink::URLLoader> MakeHostURLLoader(HostServices& services);
}  // namespace blink_standalone
#endif
