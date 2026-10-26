// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#include "standalone/engine/script.h"

#include <cstddef>
#include <string>
#include <utility>

#include "standalone/common/limits.h"
#include "third_party/blink/public/platform/web_string.h"
#include "third_party/blink/public/platform/web_url.h"
#include "third_party/blink/public/web/web_script_source.h"
#include "third_party/blink/renderer/bindings/core/v8/sanitize_script_errors.h"
#include "third_party/blink/renderer/bindings/core/v8/script_evaluation_result.h"
#include "third_party/blink/renderer/bindings/core/v8/v8_binding_for_core.h"
#include "third_party/blink/renderer/bindings/core/v8/v8_microtasks_scope.h"
#include "third_party/blink/renderer/bindings/core/v8/v8_script_runner.h"
#include "third_party/blink/renderer/core/dom/document.h"
#include "third_party/blink/renderer/core/frame/local_dom_window.h"
#include "third_party/blink/renderer/core/frame/local_frame.h"
#include "third_party/blink/renderer/core/script/classic_script.h"
#include "third_party/blink/renderer/platform/bindings/script_forbidden_scope.h"
#include "third_party/blink/renderer/platform/bindings/script_state.h"
#include "third_party/blink/renderer/platform/weborigin/kurl.h"
#include "third_party/blink/renderer/platform/wtf/text/wtf_string.h"
#include "v8/include/v8.h"

namespace blink_standalone {
namespace {

ScriptResult Error(be_status status,
                   const char* message,
                   bool exception = false) {
  return {status, message, exception};
}

// Check lengths before allocating the native copy. WriteUtf8 with replacement
// also gives valid UTF-8 for exception strings containing lone surrogates.
ScriptResult CopyString(v8::Isolate* isolate,
                        v8::Local<v8::String> value,
                        be_status status,
                        bool exception) {
  if (value.IsEmpty())
    return Error(BE_SCRIPT_ERROR, "JavaScript produced an empty string handle",
                 exception);
  if (static_cast<size_t>(value->Length()) > kMaxResourceBytes)
    return Error(BE_SCRIPT_ERROR, "JavaScript result exceeds 64 MiB",
                 exception);
  const size_t size = value->Utf8Length(isolate);
  if (size > kMaxResourceBytes)
    return Error(BE_SCRIPT_ERROR, "JavaScript result exceeds 64 MiB",
                 exception);
  std::string copy(size, '\0');
  if (size &&
      value->WriteUtf8(isolate, copy.data(), copy.size(),
                       v8::String::WriteFlags::kReplaceInvalidUtf8) != size)
    return Error(BE_SCRIPT_ERROR, "Could not copy JavaScript result",
                 exception);
  return {status, std::move(copy), exception};
}

ScriptResult CaptureException(v8::Isolate* isolate, v8::TryCatch& caught) {
  if (caught.HasTerminated() || !caught.CanContinue())
    return Error(BE_SCRIPT_ERROR, "JavaScript execution was terminated");
  if (!caught.HasCaught())
    return Error(BE_SCRIPT_ERROR,
                 "JavaScript execution failed without an exception");
  // Read V8's captured diagnostic instead of invoking a thrown object's
  // arbitrary toString(), which could throw or execute another unbounded
  // script.
  const auto message = caught.Message();
  if (message.IsEmpty())
    return Error(BE_SCRIPT_EXCEPTION, "JavaScript exception", true);
  return CopyString(isolate, message->Get(), BE_SCRIPT_EXCEPTION, true);
}

}  // namespace

ScriptResult EvaluateScript(blink::LocalFrame& frame,
                            std::string_view source,
                            std::string_view source_url) {
  if (source.size() > kMaxResourceBytes ||
      source_url.size() > kMaxResourceBytes)
    return Error(BE_INVALID_ARGUMENT, "JavaScript input exceeds 64 MiB");
  auto* window = frame.DomWindow();
  auto* document = frame.GetDocument();
  if (!window || !document || frame.IsDetached())
    return Error(BE_INVALID_STATE, "The page has no live script document");
  if (blink::ScriptForbiddenScope::IsScriptForbidden())
    return Error(BE_SCRIPT_ERROR,
                 "Script execution is forbidden in this lifecycle phase");
  if (!window->CanExecuteScripts(blink::kAboutToExecuteScript))
    return Error(BE_NOT_SUPPORTED,
                 "Script execution is disabled for this document");

  // A default-constructed string_view has a null pointer even though it is a
  // valid empty input; Blink distinguishes that from a non-null empty string.
  const auto text =
      blink::String::FromUtf8(source.empty() ? std::string_view("") : source);
  const auto url_text = blink::String::FromUtf8(
      source_url.empty() ? std::string_view("") : source_url);
  if (text.IsNull() || url_text.IsNull())
    return Error(BE_INVALID_ARGUMENT,
                 "Script source and URL must be valid UTF-8");
  const blink::KURL url =
      source_url.empty() ? document->Url() : blink::KURL(url_text);
  if (!url.IsValid() || (!url.ProtocolIs("http") && !url.ProtocolIs("https") &&
                         !url.IsAboutBlankUrl()))
    return Error(BE_INVALID_ARGUMENT,
                 "Script source URL must be HTTP(S) or about:blank");

  auto* isolate = window->GetIsolate();
  v8::HandleScope handles(isolate);
  auto* script_state = blink::ToScriptStateForMainWorld(&frame);
  if (!script_state || !script_state->ContextIsValid())
    return Error(BE_INVALID_STATE,
                 "The document's main-world context is unavailable");
  blink::ScriptState::Scope scope(script_state);
  v8::TryCatch caught(isolate);
  caught.SetVerbose(false);
  caught.SetCaptureMessage(true);

  const blink::WebScriptSource script_source{blink::WebString(text),
                                             blink::WebURL(url)};
  // Caller-supplied host script uses the page's existing realm and origin.
  // It is not a cross-origin resource, so preserving its diagnostic exception
  // for the owning host is intentional; do not use this path for network
  // scripts.
  auto* script = blink::ClassicScript::CreateUnspecifiedScript(
      script_source, blink::SanitizeScriptErrors::kDoNotSanitize);
  auto result = script->RunScriptOnScriptStateAndReturnValue(
      script_state,
      blink::ExecuteScriptPolicy::kDoNotExecuteScriptWhenScriptsDisabled,
      blink::V8ScriptRunner::RethrowErrorsOption::Rethrow(blink::String()));
  if (caught.HasCaught() || caught.HasTerminated())
    return CaptureException(isolate, caught);
  using ResultType = blink::ScriptEvaluationResult::ResultType;
  if (result.GetResultType() == ResultType::kNotRun)
    return Error(BE_NOT_SUPPORTED, "Blink declined to execute this script");
  if (result.GetResultType() != ResultType::kSuccess)
    return Error(BE_SCRIPT_ERROR, "Blink aborted script execution");

  const auto value = result.GetSuccessValue();
  if (value.IsEmpty())
    return Error(BE_SCRIPT_ERROR, "JavaScript did not produce a result value");
  if (value->IsUndefined())
    return {BE_OK, "undefined", false};
  if (value->IsFunction() || value->IsSymbol() || value->IsPromise())
    return Error(
        BE_NOT_SUPPORTED,
        "Function, symbol and Promise results cannot be returned as JSON");

  v8::Local<v8::String> json;
  bool serialized = false;
  {
    // JSON.stringify can run user-defined getters/toJSON. Use the same Blink
    // microtask policy as its script/function runner, not a raw V8 context.
    blink::V8RunMicrotasksScope microtasks(script_state);
    serialized =
        v8::JSON::Stringify(script_state->GetContext(), value).ToLocal(&json);
  }
  if (caught.HasCaught() || caught.HasTerminated())
    return CaptureException(isolate, caught);
  if (!serialized)
    return Error(BE_NOT_SUPPORTED,
                 "JavaScript result has no JSON representation");
  return CopyString(isolate, json, BE_OK, false);
}

}  // namespace blink_standalone
