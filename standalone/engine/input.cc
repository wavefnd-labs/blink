// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#include "standalone/engine/input.h"

#include <cmath>
#include <string_view>
#include <utility>

#include "base/time/time.h"
#include "cc/trees/scroll_source_type.h"
#include "standalone/common/input_validation.h"
#include "third_party/blink/public/common/input/web_keyboard_event.h"
#include "third_party/blink/public/common/input/web_mouse_event.h"
#include "third_party/blink/public/common/input/web_mouse_wheel_event.h"
#include "third_party/blink/public/platform/web_input_event_result.h"
#include "third_party/blink/renderer/core/dom/document.h"
#include "third_party/blink/renderer/core/dom/element.h"
#include "third_party/blink/renderer/core/frame/local_frame.h"
#include "third_party/blink/renderer/core/frame/local_frame_view.h"
#include "third_party/blink/renderer/core/input/event_handler.h"
#include "third_party/blink/renderer/core/input/scroll_manager.h"
#include "third_party/blink/renderer/core/layout/hit_test_location.h"
#include "third_party/blink/renderer/core/layout/hit_test_result.h"
#include "third_party/blink/renderer/core/page/focus_controller.h"
#include "third_party/blink/renderer/core/page/page.h"
#include "third_party/blink/renderer/core/scroll/scrollable_area.h"
#include "third_party/blink/renderer/platform/heap/persistent.h"
#include "third_party/blink/renderer/platform/wtf/functional.h"
#include "third_party/blink/renderer/platform/wtf/text/wtf_string.h"
#include "ui/events/keycodes/dom/keycode_converter.h"
#include "ui/events/types/scroll_types.h"
#include "ui/gfx/geometry/point_f.h"

namespace blink_standalone {
namespace {
using Event = blink::WebInputEvent;
using Mouse = blink::WebMouseEvent;
using Result = blink::WebInputEventResult;

bool FrameReady(blink::LocalFrame& frame) {
  return frame.IsAttached() && frame.GetPage() && frame.View() &&
         frame.GetDocument() && frame.GetDocument()->IsActive();
}

int Modifiers(const be_input_event& event) {
  int result = 0;
  if (event.modifiers & BE_MOD_SHIFT)
    result |= Event::kShiftKey;
  if (event.modifiers & BE_MOD_CTRL)
    result |= Event::kControlKey;
  if (event.modifiers & BE_MOD_ALT)
    result |= Event::kAltKey;
  if (event.modifiers & BE_MOD_META)
    result |= Event::kMetaKey;
  if (event.modifiers & BE_MOD_CAPS_LOCK)
    result |= Event::kCapsLockOn;
  if (event.modifiers & BE_MOD_NUM_LOCK)
    result |= Event::kNumLockOn;
  if (event.modifiers & BE_MOD_AUTO_REPEAT)
    result |= Event::kIsAutoRepeat;
  if (event.modifiers & BE_MOD_KEY_PAD)
    result |= Event::kIsKeyPad;
  if (event.buttons & BE_BUTTONS_LEFT)
    result |= Event::kLeftButtonDown;
  if (event.buttons & BE_BUTTONS_MIDDLE)
    result |= Event::kMiddleButtonDown;
  if (event.buttons & BE_BUTTONS_RIGHT)
    result |= Event::kRightButtonDown;
  if (event.buttons & BE_BUTTONS_BACK)
    result |= Event::kBackButtonDown;
  if (event.buttons & BE_BUTTONS_FORWARD)
    result |= Event::kForwardButtonDown;
  return result;
}

Mouse::Button Button(uint32_t button) {
  switch (button) {
    case BE_BUTTON_LEFT:
      return Mouse::Button::kLeft;
    case BE_BUTTON_MIDDLE:
      return Mouse::Button::kMiddle;
    case BE_BUTTON_RIGHT:
      return Mouse::Button::kRight;
    case BE_BUTTON_BACK:
      return Mouse::Button::kBack;
    case BE_BUTTON_FORWARD:
      return Mouse::Button::kForward;
    default:
      return Mouse::Button::kNoButton;
  }
}

Event::Type MouseType(uint32_t type) {
  switch (type) {
    case BE_INPUT_MOUSE_DOWN:
      return Event::Type::kMouseDown;
    case BE_INPUT_MOUSE_UP:
      return Event::Type::kMouseUp;
    case BE_INPUT_MOUSE_LEAVE:
      return Event::Type::kMouseLeave;
    default:
      return Event::Type::kMouseMove;
  }
}

// The software profile has no cc input handler to perform the wheel default
// action. Use Blink's existing user-scroll implementation and enclosing-area
// traversal after the cancellable production wheel event has been dispatched.
// This keeps DOM event cancellation, scroll consumption and per-axis CSS
// overscroll containment, without a browser/compositor runtime or DOM setters.
bool ScrollWheel(blink::LocalFrame& frame,
                 const be_input_event& event,
                 blink::WebMouseWheelEvent::EventAction action) {
  auto point = frame.View()->ConvertFromRootFrame(
      gfx::PointF(static_cast<float>(event.x), static_cast<float>(event.y)));
  auto hit = frame.GetEventHandler().HitTestResultAtLocation(
      blink::HitTestLocation(point), blink::HitTestRequest::kReadOnly);
  blink::Node* target = hit.InnerNode();
  if (!target && hit.GetScrollbar())
    target = frame.GetDocument()->documentElement();
  if (!target)
    return false;

  float dx = static_cast<float>(event.delta_x);
  float dy = static_cast<float>(event.delta_y);
  if (action == blink::WebMouseWheelEvent::EventAction::kScrollHorizontal &&
      dx == 0 && (event.modifiers & BE_MOD_SHIFT)) {
    dx = dy;
    dy = 0;
  }
  bool consumed = false;
  for (blink::ScrollableArea& area : blink::ScrollableAreaTraversal(target)) {
    if (dx == 0 && dy == 0)
      break;
    auto finished = blink::BindOnce(
        [](blink::WeakPersistent<blink::ScrollableArea> weak,
           blink::ScrollableArea::ScrollCompletionMode mode) {
          if (weak)
            weak->OnScrollFinished(
                mode == blink::ScrollableArea::ScrollCompletionMode::kFinished);
        },
        blink::WrapWeakPersistent(&area));
    auto result = area.UserScroll(ui::ScrollGranularity::kScrollByPrecisePixel,
                                  blink::ScrollOffset(dx, dy),
                                  cc::ScrollSourceType::kRelativeScroll,
                                  std::move(finished));
    consumed |= result.DidScroll();
    dx = result.unused_scroll_delta_x;
    dy = result.unused_scroll_delta_y;
    if (auto* box = area.GetLayoutBox()) {
      if (!blink::ScrollManager::CanPropagate(
              box, blink::ScrollPropagationDirection::kHorizontal)) {
        consumed |= dx != 0;
        dx = 0;
      }
      if (!blink::ScrollManager::CanPropagate(
              box, blink::ScrollPropagationDirection::kVertical)) {
        consumed |= dy != 0;
        dy = 0;
      }
    }
  }
  return consumed;
}

}  // namespace

be_status SetPageInputFocus(blink::LocalFrame& frame, bool focused) {
  if (!FrameReady(frame))
    return BE_INVALID_STATE;
  auto& controller = frame.GetPage()->GetFocusController();
  if (focused) {
    controller.SetActive(true);
    controller.SetFocusedFrame(&frame, /*notify_embedder=*/false);
    controller.SetFocused(true);
  } else {
    controller.SetFocused(false);
    controller.SetActive(false);
  }
  return BE_OK;
}

be_status DispatchPageInput(blink::LocalFrame& frame,
                            const be_input_event& event,
                            bool* handled) {
  if (!handled)
    return BE_INVALID_ARGUMENT;
  *handled = false;
  const auto validation = ValidateInputEvent(event);
  if (validation != BE_OK)
    return validation;
  if (!FrameReady(frame))
    return BE_INVALID_STATE;
  auto& handler = frame.GetEventHandler();

  if (event.type <= BE_INPUT_MOUSE_LEAVE) {
    const gfx::PointF position(static_cast<float>(event.x),
                               static_cast<float>(event.y));
    Mouse native(MouseType(event.type), position, position,
                 Button(event.button), static_cast<int>(event.click_count),
                 Modifiers(event), base::TimeTicks::Now());
    native.UpdateEventModifiersToMatchButton();
    if (event.type == BE_INPUT_MOUSE_MOVE) {
      if (!handler.IsMousePositionUnknown()) {
        auto previous = handler.LastKnownMousePositionInRootFrame();
        native.movement_x =
            static_cast<int>(std::round(event.x - previous.x()));
        native.movement_y =
            static_cast<int>(std::round(event.y - previous.y()));
      }
      *handled =
          handler.HandleMouseMoveEvent(native, {}, {}) != Result::kNotHandled;
    } else if (event.type == BE_INPUT_MOUSE_DOWN) {
      *handled = handler.HandleMousePressEvent(native) != Result::kNotHandled;
    } else if (event.type == BE_INPUT_MOUSE_UP) {
      *handled = handler.HandleMouseReleaseEvent(native) != Result::kNotHandled;
    } else {
      handler.HandleMouseLeaveEvent(native);
    }
    return BE_OK;
  }
  if (event.type == BE_INPUT_WHEEL) {
    // Host page zoom is a separate, currently unavailable service.
    if (event.modifiers & (BE_MOD_CTRL | BE_MOD_META))
      return BE_NOT_SUPPORTED;
    blink::WebMouseWheelEvent native(Event::Type::kMouseWheel, Modifiers(event),
                                     base::TimeTicks::Now());
    native.SetPositionInWidget(static_cast<float>(event.x),
                               static_cast<float>(event.y));
    native.SetPositionInScreen(static_cast<float>(event.x),
                               static_cast<float>(event.y));
    // WebMouseWheelEvent uses the opposite sign to CSS/DOM wheel deltas.
    native.delta_x = static_cast<float>(-event.delta_x);
    native.delta_y = static_cast<float>(-event.delta_y);
    native.delta_units = ui::ScrollGranularity::kScrollByPrecisePixel;
    native.event_action =
        blink::WebMouseWheelEvent::GetPlatformSpecificDefaultEventAction(
            native);
    *handled = handler.HandleWheelEvent(native) != Result::kNotHandled;
    if (!*handled && FrameReady(frame))
      *handled = ScrollWheel(frame, event, native.event_action);
    return BE_OK;
  }
  if (!frame.GetPage()->GetFocusController().IsFocused())
    return BE_INVALID_STATE;
  if (event.type == BE_INPUT_TEXT) {
    const blink::String text =
        blink::String::FromUtf8(std::string_view(event.text_utf8));
    if (text.IsNull())
      return BE_INVALID_ARGUMENT;
    *handled = handler.HandleTextInputEvent(text);
    return BE_OK;
  }

  auto code = ui::KeycodeConverter::CodeStringToDomCode(event.dom_code);
  auto key = ui::KeycodeConverter::KeyStringToDomKey(event.dom_key);
  if ((code == ui::DomCode::NONE && event.dom_code[0]) || !key.IsValid())
    return BE_INVALID_ARGUMENT;
  int modifiers = Modifiers(event);
  switch (ui::KeycodeConverter::DomCodeToLocation(code)) {
    case ui::DomKeyLocation::LEFT:
      modifiers |= Event::kIsLeft;
      break;
    case ui::DomKeyLocation::RIGHT:
      modifiers |= Event::kIsRight;
      break;
    case ui::DomKeyLocation::NUMPAD:
      modifiers |= Event::kIsKeyPad;
      break;
    case ui::DomKeyLocation::STANDARD:
      break;
  }
  blink::WebKeyboardEvent native(event.type == BE_INPUT_KEY_DOWN
                                     ? Event::Type::kRawKeyDown
                                     : Event::Type::kKeyUp,
                                 modifiers, base::TimeTicks::Now());
  native.windows_key_code = static_cast<int>(event.windows_key_code);
  native.native_key_code = static_cast<int>(event.native_key_code);
  native.dom_code = static_cast<int>(code);
  native.dom_key = static_cast<uint32_t>(key);
  *handled = handler.KeyEvent(native) != Result::kNotHandled;
  return BE_OK;
}

}  // namespace blink_standalone
