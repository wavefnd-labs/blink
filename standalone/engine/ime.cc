// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#include "standalone/engine/ime.h"

#include <algorithm>
#include <span>
#include <string>

#include "base/compiler_specific.h"
#include "standalone/common/ime_validation.h"
#include "third_party/blink/public/common/metrics/document_update_reason.h"
#include "third_party/blink/public/mojom/frame/user_activation_notification_type.mojom-shared.h"
#include "third_party/blink/public/platform/web_text_input_info.h"
#include "third_party/blink/renderer/core/dom/document.h"
#include "third_party/blink/renderer/core/editing/editing_utilities.h"
#include "third_party/blink/renderer/core/editing/editor.h"
#include "third_party/blink/renderer/core/editing/ephemeral_range.h"
#include "third_party/blink/renderer/core/editing/frame_selection.h"
#include "third_party/blink/renderer/core/editing/ime/ime_text_span.h"
#include "third_party/blink/renderer/core/editing/ime/input_method_controller.h"
#include "third_party/blink/renderer/core/frame/local_frame.h"
#include "third_party/blink/renderer/core/frame/local_frame_view.h"
#include "third_party/blink/renderer/core/page/focus_controller.h"
#include "third_party/blink/renderer/core/page/page.h"
#include "third_party/blink/renderer/platform/heap/persistent.h"
#include "third_party/blink/renderer/platform/wtf/text/wtf_string.h"
#include "ui/base/ime/mojom/ime_types.mojom-shared.h"
#include "ui/gfx/geometry/rect.h"

namespace blink_standalone {
namespace {

bool Focused(blink::LocalFrame& frame) {
  return frame.IsAttached() && frame.GetPage() && frame.View() &&
         frame.GetDocument() && frame.GetDocument()->IsActive() &&
         frame.GetPage()->GetFocusController().IsFocused() &&
         frame.GetPage()->GetFocusController().FocusedFrame() == &frame;
}

uint32_t InputType(blink::WebTextInputType type) {
  switch (type) {
    case blink::kWebTextInputTypeText:
      return BE_IME_TYPE_TEXT;
    case blink::kWebTextInputTypePassword:
      return BE_IME_TYPE_PASSWORD;
    case blink::kWebTextInputTypeSearch:
      return BE_IME_TYPE_SEARCH;
    case blink::kWebTextInputTypeEmail:
      return BE_IME_TYPE_EMAIL;
    case blink::kWebTextInputTypeNumber:
      return BE_IME_TYPE_NUMBER;
    case blink::kWebTextInputTypeTelephone:
      return BE_IME_TYPE_TELEPHONE;
    case blink::kWebTextInputTypeURL:
      return BE_IME_TYPE_URL;
    case blink::kWebTextInputTypeTextArea:
      return BE_IME_TYPE_TEXTAREA;
    case blink::kWebTextInputTypeContentEditable:
      return BE_IME_TYPE_CONTENTEDITABLE;
    default:
      return BE_IME_TYPE_NONE;
  }
}

be_status Ready(blink::LocalFrame& frame, bool editable) {
  if (!Focused(frame))
    return BE_INVALID_STATE;
  frame.GetDocument()->UpdateStyleAndLayout(
      blink::DocumentUpdateReason::kInput);
  if (!Focused(frame))
    return BE_INVALID_STATE;
  auto& controller = frame.GetInputMethodController();
  if (controller.GetActiveEditContext())
    return BE_NOT_SUPPORTED;
  if (editable && (!frame.GetEditor().CanEdit() ||
                   InputType(controller.TextInputType()) == BE_IME_TYPE_NONE))
    return BE_NOT_SUPPORTED;
  // A script may remove/disable the old composing node between IME messages.
  // Match the production WebInputMethodController's parent editability guard.
  const auto range = controller.CompositionEphemeralRange();
  if (range.IsNotNull()) {
    auto* node = range.StartPosition().ComputeContainerNode();
    if (!node || !blink::IsEditable(*node))
      return BE_INVALID_STATE;
  }
  return BE_OK;
}

blink::Vector<blink::ImeTextSpan> Spans(const be_ime_composition& value) {
  blink::Vector<blink::ImeTextSpan> result;
  result.ReserveInitialCapacity(
      static_cast<blink::wtf_size_t>(value.span_count));
  for (const auto& span : ImeSpanView(value)) {
    // The public enum values deliberately match these pinned Mojo enums;
    // validation bounds every cast before it reaches Blink.
    result.emplace_back(
        blink::ImeTextSpan::Type::kComposition, span.start_utf16,
        span.end_utf16, blink::Color::kBlack,
        static_cast<ui::mojom::ImeTextSpanThickness>(span.thickness),
        static_cast<ui::mojom::ImeTextSpanUnderlineStyle>(span.underline_style),
        blink::Color::kTransparent, blink::Color::kTransparent);
  }
  return result;
}

be_ime_rect Rect(const gfx::Rect& rect) {
  return {rect.x(), rect.y(), rect.width(), rect.height()};
}

}  // namespace

be_status SetImeComposition(blink::LocalFrame& frame,
                            const be_ime_composition& value) {
  const auto validation = ValidateImeComposition(value);
  if (validation != BE_OK)
    return validation;
  blink::Persistent<blink::LocalFrame> keep_alive(&frame);
  const auto status = Ready(frame, true);
  if (status != BE_OK)
    return status;
  const auto text = blink::String::FromUtf8(ImeTextView(value));
  if (text.empty()) {
    // An empty update replaces the active composition and queues its events.
    // Without this guard Blink would delete an ordinary selected range too.
    auto& controller = frame.GetInputMethodController();
    if (controller.HasComposition())
      controller.SetComposition(blink::String::FromUtf8(""), {}, 0, 0);
    return Focused(frame) ? BE_OK : BE_INVALID_STATE;
  }
  blink::LocalFrame::NotifyUserActivation(
      &frame,
      blink::mojom::blink::UserActivationNotificationType::kInteraction);
  frame.GetInputMethodController().SetComposition(
      text, Spans(value), static_cast<int>(value.selection_start_utf16),
      static_cast<int>(value.selection_end_utf16));
  if (!Focused(frame))
    return BE_INVALID_STATE;
  return frame.GetInputMethodController().HasComposition() ? BE_OK
                                                           : BE_INVALID_STATE;
}

be_status CommitImeText(blink::LocalFrame& frame,
                        std::string_view text_utf8,
                        int32_t relative_caret_utf16) {
  const auto validation = ValidateImeCommit(text_utf8, relative_caret_utf16);
  if (validation != BE_OK)
    return validation;
  blink::Persistent<blink::LocalFrame> keep_alive(&frame);
  const auto status = Ready(frame, true);
  if (status != BE_OK)
    return status;
  const auto text = blink::String::FromUtf8(
      text_utf8.empty() ? std::string_view("") : text_utf8);
  if (!text.empty())
    blink::LocalFrame::NotifyUserActivation(
        &frame,
        blink::mojom::blink::UserActivationNotificationType::kInteraction);
  const bool committed = frame.GetInputMethodController().CommitText(
      text, {}, relative_caret_utf16);
  return committed && Focused(frame) ? BE_OK : BE_INVALID_STATE;
}

be_status FinishImeComposition(blink::LocalFrame& frame, bool keep_selection) {
  blink::Persistent<blink::LocalFrame> keep_alive(&frame);
  const auto status = Ready(frame, true);
  if (status != BE_OK)
    return status;
  const bool finished = frame.GetInputMethodController().FinishComposingText(
      keep_selection ? blink::InputMethodController::kKeepSelection
                     : blink::InputMethodController::kDoNotKeepSelection);
  return finished && Focused(frame) ? BE_OK : BE_INVALID_STATE;
}

be_status CancelImeComposition(blink::LocalFrame& frame) {
  blink::Persistent<blink::LocalFrame> keep_alive(&frame);
  const auto status = Ready(frame, true);
  if (status != BE_OK)
    return status;
  auto& controller = frame.GetInputMethodController();
  if (controller.HasComposition())
    controller.SetComposition(blink::String::FromUtf8(""), {}, 0, 0);
  return Focused(frame) ? BE_OK : BE_INVALID_STATE;
}

be_status QueryImeState(blink::LocalFrame& frame,
                        be_ime_state* state,
                        char* text_utf8,
                        size_t capacity,
                        size_t* required) {
  if (!state || !required || (capacity && !text_utf8))
    return BE_INVALID_ARGUMENT;
  *required = 0;
  if (state->struct_size < sizeof(*state))
    return BE_INVALID_ARGUMENT;
  if (state->abi_version != BE_ABI_VERSION)
    return BE_ABI_MISMATCH;
  blink::Persistent<blink::LocalFrame> keep_alive(&frame);
  const auto status = Ready(frame, false);
  if (status != BE_OK)
    return status;
  auto& controller = frame.GetInputMethodController();
  const auto info = controller.TextInputInfo();
  if (info.value.length() > kMaxImeTextBytes)
    return BE_NOT_SUPPORTED;
  const auto text = info.value.Utf8(
      blink::WebString::Utf8ConversionMode::kStrictReplacingErrors);
  if (text.size() > kMaxImeTextBytes)
    return BE_NOT_SUPPORTED;
  const auto length = static_cast<int>(info.value.length());
  if (info.selection_start < 0 || info.selection_end < info.selection_start ||
      info.selection_end > length ||
      ((info.composition_start != -1 || info.composition_end != -1) &&
       (info.composition_start < 0 ||
        info.composition_end < info.composition_start ||
        info.composition_end > length)))
    return BE_INVALID_STATE;
  be_ime_state output{};
  output.struct_size = state->struct_size;
  output.abi_version = BE_ABI_VERSION;
  output.input_type = InputType(info.type);
  output.text_utf16_length = static_cast<uint32_t>(length);
  output.selection_start_utf16 = static_cast<uint32_t>(info.selection_start);
  output.selection_end_utf16 = static_cast<uint32_t>(info.selection_end);
  output.composition_start_utf16 = info.composition_start;
  output.composition_end_utf16 = info.composition_end;
  if (output.input_type != BE_IME_TYPE_NONE &&
      frame.Selection().IsAvailable()) {
    // FrameToViewport includes ConvertToRootFrame, then the visual viewport
    // transform. GetLayoutBounds already returns BoundsInWidget in that same
    // root viewport space; converting it again would double-offset subframes.
    output.caret_bounds = Rect(
        frame.View()->FrameToViewport(frame.Selection().AbsoluteCaretBounds()));
    gfx::Rect control_bounds, unused_selection_bounds;
    controller.GetLayoutBounds(&control_bounds, &unused_selection_bounds);
    output.control_bounds = Rect(control_bounds);
  }
  if (!Focused(frame))
    return BE_INVALID_STATE;
  *state = output;
  *required = text.size() + 1;
  if (capacity < *required)
    return BE_BUFFER_TOO_SMALL;
  // SAFETY: The host promises capacity writable bytes for this C ABI call.
  // The null check above and capacity >= required establish this prefix;
  // required includes one terminator and is bounded by kMaxImeTextBytes + 1.
  const auto destination =
      UNSAFE_BUFFERS(std::span<char>(text_utf8, *required));
  std::ranges::copy(text, destination.first(text.size()).begin());
  destination.back() = '\0';
  return BE_OK;
}

}  // namespace blink_standalone
