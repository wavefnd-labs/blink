# Copyright 2014 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

{
  'includes': [
    '../bindings.gypi',
    'v8/generated.gypi',
  ],

  'variables': {
    'bindings_modules_output_dir': '<(bindings_output_dir)/modules',
  },
}
