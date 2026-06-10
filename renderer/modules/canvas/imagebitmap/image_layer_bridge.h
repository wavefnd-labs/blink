// Copyright 2017 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef THIRD_PARTY_BLINK_RENDERER_MODULES_CANVAS_IMAGEBITMAP_IMAGE_LAYER_BRIDGE_H_
#define THIRD_PARTY_BLINK_RENDERER_MODULES_CANVAS_IMAGEBITMAP_IMAGE_LAYER_BRIDGE_H_

#include "cc/layers/texture_layer_client.h"
#include "components/viz/common/resources/shared_image_format.h"
#include "third_party/blink/renderer/modules/modules_export.h"
#include "third_party/blink/renderer/platform/graphics/opacity_mode.h"
#include "third_party/blink/renderer/platform/graphics/static_bitmap_image.h"
#include "third_party/blink/renderer/platform/heap/garbage_collected.h"
#include "ui/gfx/geometry/point_f.h"

namespace blink {

class MODULES_EXPORT ImageLayerBridge
    : public GarbageCollected<ImageLayerBridge> {
 public:
  ImageLayerBridge(OpacityMode);
  ImageLayerBridge(const ImageLayerBridge&) = delete;
  ImageLayerBridge& operator=(const ImageLayerBridge&) = delete;
  ~ImageLayerBridge();

  void Dispose();

  scoped_refptr<StaticBitmapImage> GetImage() { return image_; }


  void Trace(Visitor* visitor) const {}

  scoped_refptr<StaticBitmapImage> image_;

  bool disposed_ = false;
  bool has_presented_since_last_set_image_ = false;
  bool is_opaque_ = false;
};

}  // namespace blink

#endif  // THIRD_PARTY_BLINK_RENDERER_MODULES_CANVAS_IMAGEBITMAP_IMAGE_LAYER_BRIDGE_H_
