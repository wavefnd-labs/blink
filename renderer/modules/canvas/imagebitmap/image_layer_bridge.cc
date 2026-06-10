// Copyright 2017 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "third_party/blink/renderer/modules/canvas/imagebitmap/image_layer_bridge.h"

#include "cc/layers/texture_layer.h"
#include "components/viz/common/resources/shared_image_format_utils.h"
#include "components/viz/common/resources/transferable_resource.h"
#include "gpu/command_buffer/client/gles2_interface.h"
#include "gpu/command_buffer/client/shared_image_interface.h"
#include "gpu/command_buffer/common/shared_image_usage.h"
#include "third_party/blink/public/common/features.h"
#include "third_party/blink/public/platform/platform.h"
#include "third_party/blink/public/platform/web_graphics_context_3d_provider.h"
#include "third_party/blink/public/platform/web_graphics_shared_image_interface_provider.h"
#include "third_party/blink/renderer/modules/canvas/imagebitmap/image_bitmap_rendering_context.h"
#include "third_party/blink/renderer/platform/graphics/accelerated_static_bitmap_image.h"
#include "third_party/blink/renderer/platform/graphics/canvas_resource_provider.h"
#include "third_party/blink/renderer/platform/graphics/color_behavior.h"
#include "third_party/blink/renderer/platform/graphics/gpu/shared_gpu_context.h"
#include "third_party/blink/renderer/platform/graphics/image_orientation.h"
#include "third_party/blink/renderer/platform/graphics/skia/skia_utils.h"
#include "third_party/blink/renderer/platform/wtf/functional.h"
#include "ui/gfx/geometry/size.h"

namespace blink {

ImageLayerBridge::ImageLayerBridge(OpacityMode opacity_mode)
    : is_opaque_(opacity_mode == kOpaque) {}

ImageLayerBridge::~ImageLayerBridge() {
  if (!disposed_) {
    Dispose();
  }
}


void ImageLayerBridge::Dispose() {
  image_ = nullptr;
  disposed_ = true;
}

ImageLayerBridge::SoftwareResource
ImageLayerBridge::CreateOrRecycleSoftwareResource(
    const gfx::Size& size,
    const gfx::ColorSpace& color_space) {
  // Must call SharedImageInterfaceProvider() first so all base::WeakPtr
  // restored in |resource.sii_provider| is updated.
  auto* sii_provider = SharedGpuContext::SharedImageInterfaceProvider();
  DCHECK(sii_provider);
  auto it = std::remove_if(
      recycled_software_resources_.begin(), recycled_software_resources_.end(),
      [&size, &color_space](const SoftwareResource& resource) {
        return resource.shared_image->size() != size ||
               resource.shared_image->color_space() != color_space ||
               !resource.sii_provider;
      });

  recycled_software_resources_.Shrink(
      static_cast<wtf_size_t>(it - recycled_software_resources_.begin()));

  if (!recycled_software_resources_.empty()) {
    SoftwareResource resource = std::move(recycled_software_resources_.back());
    recycled_software_resources_.pop_back();
    return resource;
  }

  // There are no resources to recycle so allocate a new one.
  SoftwareResource resource;
  auto* shared_image_interface = sii_provider->SharedImageInterface();
  if (!shared_image_interface) {
    return resource;
  }
  resource.shared_image =
      shared_image_interface->CreateSharedImageForSoftwareCompositor(
          {viz::SinglePlaneFormat::kBGRA_8888, size, color_space,
           gpu::SHARED_IMAGE_USAGE_CPU_WRITE_ONLY, "ImageLayerBridgeBitmap"});

  resource.sii_provider = sii_provider->GetWeakPtr();
  resource.sync_token = shared_image_interface->GenVerifiedSyncToken();

  return resource;
}

void ImageLayerBridge::ResourceReleasedSoftware(
    SoftwareResource resource,
    const gpu::SyncToken& sync_token,
    bool lost_resource) {
  if (!disposed_ && !lost_resource) {
    recycled_software_resources_.push_back(std::move(resource));
  }
}


ImageLayerBridge::SoftwareResource::SoftwareResource() = default;
ImageLayerBridge::SoftwareResource::SoftwareResource(SoftwareResource&& other) =
    default;
ImageLayerBridge::SoftwareResource&
ImageLayerBridge::SoftwareResource::operator=(SoftwareResource&& other) =
    default;

}  // namespace blink
