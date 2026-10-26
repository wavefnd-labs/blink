// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <span>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <vector>
#include "standalone/include/blink_engine.h"

// Keep raw pointer/count adaptation at the two C ABI boundaries below. This
// harness otherwise uses standard containers and has no Chromium dependency.
#if defined(__clang__)
#define SMOKE_UNSAFE_BUFFERS(...)            \
  _Pragma("clang unsafe_buffer_usage begin") \
  __VA_ARGS__                               \
  _Pragma("clang unsafe_buffer_usage end")
#else
#define SMOKE_UNSAFE_BUFFERS(...) __VA_ARGS__
#endif

namespace {
std::atomic<unsigned> wakes{0};
unsigned fetches = 0;
std::thread::id owner_thread;
void Wake(void*, uint64_t) {
  ++wakes;
}
be_status Fetch(void*, const char* url, be_resource_response* response) {
  if (std::this_thread::get_id() != owner_thread)
    return BE_WRONG_THREAD;
  if (!url || !response)
    return BE_INVALID_ARGUMENT;
  const std::string_view resource_url(url);
  static const char css[] =
      "html,body{margin:0;background:white}"
      "#sample{position:absolute;left:0;top:0;width:24px;height:24px;"
      "background:rgb(255,0,0)}";
  static const char script[] =
      "window.externalRuns=(window.externalRuns||0)+1;";
  static const char worker[] =
      "importScripts('/worker-import.js');"
      "onmessage=async e=>{const data=await "
      "fetch('/worker-data').then(r=>r.text());"
      "postMessage({answer:e.data.value+workerOffset,data,"
      "worker:typeof document==='undefined'})};";
  static const char worker_import[] = "self.workerOffset=2;";
  static const char worker_data[] = "engine";
  if (resource_url == "https://engine.invalid/worker-data") {
    ++fetches;
    response->data = reinterpret_cast<const uint8_t*>(worker_data);
    response->size = sizeof(worker_data) - 1;
    response->mime_type = "text/plain";
    response->charset = "UTF-8";
    return BE_OK;
  }
  static const char storage_frame[] =
      "<!doctype "
      "html><script>parent.postMessage({partition:localStorage.getItem('shared'"
      ")},'*')</script>";
  if (resource_url == "https://engine.invalid/storage-frame") {
    ++fetches;
    response->data = reinterpret_cast<const uint8_t*>(storage_frame);
    response->size = sizeof(storage_frame) - 1;
    response->mime_type = "text/html";
    response->charset = "UTF-8";
    return BE_OK;
  }
  if (resource_url == "https://engine.invalid/worker.js" ||
      resource_url == "https://engine.invalid/worker-import.js") {
    const bool main = resource_url == "https://engine.invalid/worker.js";
    ++fetches;
    response->data =
        reinterpret_cast<const uint8_t*>(main ? worker : worker_import);
    response->size = main ? sizeof(worker) - 1 : sizeof(worker_import) - 1;
    response->mime_type = "text/javascript";
    response->charset = "UTF-8";
    return BE_OK;
  }
  const bool stylesheet = resource_url == "https://engine.invalid/theme.css";
  const bool javascript = resource_url == "https://engine.invalid/bootstrap.js";
  if (!stylesheet && !javascript)
    return BE_RESOURCE_ERROR;
  ++fetches;
  response->data = reinterpret_cast<const uint8_t*>(stylesheet ? css : script);
  response->size = stylesheet ? sizeof(css) - 1 : sizeof(script) - 1;
  response->mime_type = stylesheet ? "text/css" : "text/javascript";
  response->charset = "UTF-8";
  return BE_OK;
}
bool Check(be_status status, const char* action) {
  if (status == BE_OK)
    return true;
  std::cerr << action << ": " << be_status_string(status) << '\n';
  return false;
}
std::string Argument(std::span<char* const> arguments, const char* key) {
  const std::string prefix = std::string("--") + key + "=";
  if (arguments.empty())
    return {};
  for (const char* argument : arguments.subspan(1)) {
    const std::string_view value(argument);
    if (value.starts_with(prefix))
      return std::string(value.substr(prefix.size()));
  }
  return {};
}

template <size_t Capacity, size_t Size>
void SetLiteral(char (&destination)[Capacity], const char (&literal)[Size]) {
  static_assert(Size <= Capacity);
  std::ranges::copy(literal, std::span(destination).begin());
}
bool Pixel(const std::vector<uint8_t>& pixels,
           unsigned width,
           unsigned x,
           unsigned y,
           uint8_t red,
           uint8_t green,
           uint8_t blue) {
  const auto offset = (size_t(y) * width + x) * 4;
  return pixels[offset] == red && pixels[offset + 1] == green &&
         pixels[offset + 2] == blue && pixels[offset + 3] == 255;
}

bool Assert(bool condition, const char* action) {
  if (!condition)
    std::cerr << "Assertion failed: " << action << '\n';
  return condition;
}

bool Pump(be_engine* engine, unsigned turns = 20) {
  for (unsigned turn = 0; turn < turns; ++turn)
    if (!Check(be_engine_pump(engine, 8), "task pump"))
      return false;
  return true;
}

bool Evaluate(be_page* page,
              std::string_view source,
              std::string_view expected,
              const char* action) {
  be_script_result result{};
  result.struct_size = sizeof(result);
  result.abi_version = BE_ABI_VERSION;
  if (!Check(
          be_page_evaluate_script(page, source.data(), source.size(),
                                  "https://engine.invalid/smoke.js", &result),
          action))
    return false;
  if (!Assert(result.utf8_data || !result.utf8_size, "script result storage"))
    return false;
  const std::string_view value(result.utf8_data ? result.utf8_data : "",
                               result.utf8_size);
  if (result.is_exception || value != expected) {
    std::cerr << action << ": expected " << expected << "; received " << value
              << '\n';
    return false;
  }
  return true;
}

bool JavaScriptSmoke(be_page* page) {
  if (!Evaluate(page, "window.inlineRuns===1 && window.externalRuns===1",
                "true", "inline and external document scripts") ||
      !Evaluate(page, "({answer:6*7,text:'engine'})",
                "{\"answer\":42,\"text\":\"engine\"}", "JSON evaluation") ||
      !Evaluate(page, "void 0", "undefined", "undefined evaluation") ||
      !Evaluate(page,
                "window.microtaskRan=false;Promise.resolve().then(()=>{"
                "window.microtaskRan=true});void 0",
                "undefined", "microtask scheduling") ||
      !Evaluate(page, "window.microtaskRan", "true", "microtask checkpoint"))
    return false;
  be_script_result result{};
  result.struct_size = sizeof(result);
  result.abi_version = BE_ABI_VERSION;
  static const char throws[] = "throw new Error('engine-smoke-exception')";
  const auto status =
      be_page_evaluate_script(page, throws, sizeof(throws) - 1,
                              "https://engine.invalid/exception.js", &result);
  if (!Assert(result.utf8_data || !result.utf8_size,
              "exception result storage"))
    return false;
  const std::string_view message(result.utf8_data ? result.utf8_data : "",
                                 result.utf8_size);
  if (!Assert(
          status == BE_SCRIPT_EXCEPTION && result.is_exception &&
              message.find("engine-smoke-exception") != std::string_view::npos,
          "JavaScript exception is captured"))
    return false;
  if (!Evaluate(page, "21*2", "42", "evaluation after caught exception") ||
      !Evaluate(page, "sample.style.background='rgb(0,128,0)';true", "true",
                "JavaScript style mutation"))
    return false;
  std::vector<uint8_t> pixels(80 * 80 * 4, 0);
  return Check(be_page_paint_rgba(page, pixels.data(), pixels.size(), 80 * 4),
               "paint after JavaScript mutation") &&
         Assert(Pixel(pixels, 80, 8, 8, 0, 128, 0) &&
                    Pixel(pixels, 80, 72, 72, 255, 255, 255),
                "JavaScript changes actual rendered pixels");
}

be_input_event InputEvent(be_input_type type, double x = 0, double y = 0) {
  be_input_event event{};
  event.struct_size = sizeof(event);
  event.abi_version = BE_ABI_VERSION;
  event.type = type;
  event.x = x;
  event.y = y;
  return event;
}

bool Dispatch(be_page* page,
              const be_input_event& event,
              const char* action,
              bool require_handled = false) {
  uint32_t handled = 0;
  return Check(be_page_dispatch_input(page, &event, &handled), action) &&
         (!require_handled || Assert(handled != 0, action));
}

bool Click(be_page* page, double x, double y) {
  auto event = InputEvent(BE_INPUT_MOUSE_MOVE, x, y);
  if (!Dispatch(page, event, "mouse move"))
    return false;
  event.type = BE_INPUT_MOUSE_DOWN;
  event.button = BE_BUTTON_LEFT;
  event.buttons = BE_BUTTONS_LEFT;
  event.click_count = 1;
  if (!Dispatch(page, event, "mouse down"))
    return false;
  event.type = BE_INPUT_MOUSE_UP;
  event.buttons = 0;
  return Dispatch(page, event, "mouse up");
}

bool InputSmoke(be_engine* engine, be_page* page) {
  static const char setup[] =
      "document.body.innerHTML='<button id=button style=\"position:absolute;"
      "left:0;top:0;width:64px;height:24px\">click</button>' +"
      "'<input id=editor style=\"position:absolute;left:0;top:28px;"
      "width:64px;height:22px;box-sizing:border-box\">' +"
      "'<div id=scroller style=\"position:absolute;left:0;top:56px;"
      "width:70px;height:20px;overflow:auto\"><div style=\"height:300px\">'"
      "+'scroll</div></div>';"
      "window.clicks=0;window.keyDowns=0;window.keyUps=0;window.wheels=0;"
      "window.wheelPrevented=false;"
      "button.addEventListener('click',()=>{window.clicks++});"
      "editor.addEventListener('keydown',e=>{if(e.code==='KeyA'&&e.key==='a')"
      "window.keyDowns++});"
      "editor.addEventListener('keyup',e=>{if(e.code==='KeyA'&&e.key==='a')"
      "window.keyUps++});"
      "window.cancelWheel=e=>{window.wheels++;e.preventDefault();"
      "window.wheelPrevented=e.defaultPrevented};"
      "scroller.addEventListener('wheel',window.cancelWheel,{passive:false});"
      "true";
  if (!Evaluate(page, setup, "true", "input fixture installation") ||
      !Check(be_page_layout(page), "input fixture layout") ||
      !Check(be_page_set_focus(page, 1), "page focus") ||
      !Click(page, 10, 10) ||
      !Evaluate(page, "window.clicks===1", "true", "DOM click delivery") ||
      !Click(page, 10, 36) ||
      !Evaluate(page, "document.activeElement===editor", "true",
                "input element focus"))
    return false;
  auto event = InputEvent(BE_INPUT_KEY_DOWN);
  event.windows_key_code = 65;
  SetLiteral(event.dom_code, "KeyA");
  SetLiteral(event.dom_key, "a");
  if (!Dispatch(page, event, "key down"))
    return false;
  event.type = BE_INPUT_KEY_UP;
  if (!Dispatch(page, event, "key up"))
    return false;
  event = InputEvent(BE_INPUT_TEXT);
  SetLiteral(event.text_utf8, "한A");
  if (!Dispatch(page, event, "UTF-8 text insertion") ||
      !Evaluate(
          page,
          "window.keyDowns===1 && window.keyUps===1 && editor.value==='한A'",
          "true", "keyboard and UTF-8 input values"))
    return false;
  event = InputEvent(BE_INPUT_WHEEL, 8, 64);
  event.delta_y = 40;
  if (!Dispatch(page, event, "cancelled wheel event", true) ||
      !Evaluate(page,
                "window.wheels===1 && window.wheelPrevented && "
                "scroller.scrollTop===0",
                "true", "preventDefault blocks native wheel scrolling") ||
      !Evaluate(page,
                "scroller.removeEventListener('wheel',window.cancelWheel);true",
                "true", "remove wheel cancellation") ||
      !Dispatch(page, event, "native wheel scrolling", true) || !Pump(engine) ||
      !Evaluate(page, "scroller.scrollTop>0", "true", "native scroll offset"))
    return false;
  return true;
}

struct NavigationRecord {
  std::string url;
  std::string method;
  std::string headers;
  std::string body;
};
struct NavigationContext {
  std::vector<NavigationRecord> requests;
  std::vector<uint32_t> events;
  unsigned successful_fetches = 0;
  unsigned releases = 0;
};

void ReleaseNavigation(void* data) {
  ++static_cast<NavigationContext*>(data)->releases;
}
be_status FetchNavigation(void* data,
                          const be_navigation_request* request,
                          be_navigation_response* response) {
  auto& context = *static_cast<NavigationContext*>(data);
  if (!request || request->struct_size < sizeof(*request) ||
      request->abi_version != BE_ABI_VERSION || !request->url_utf8 ||
      !request->method_utf8 || (!request->body && request->body_size))
    return BE_INVALID_ARGUMENT;
  context.requests.push_back(
      {request->url_utf8, request->method_utf8,
       request->headers_utf8 ? request->headers_utf8 : "",
       request->body_size
           ? std::string(reinterpret_cast<const char*>(request->body),
                         request->body_size)
           : std::string()});
  const std::string_view url(request->url_utf8);
  if (url == "https://engine.invalid/denied")
    return BE_RESOURCE_ERROR;
  static const char one[] =
      "<!doctype html><meta charset=utf-8><style>html,body{margin:0}"
      "#navlink{position:absolute;left:0;top:0;width:64px;height:24px;"
      "display:block}</style><a id=navlink href=/two>two</a>"
      "<script>window.pageId='one'</script>";
  static const char two[] =
      "<!doctype html><meta charset=utf-8><style>html,body{margin:0}</style>"
      "<form id=postform method=post action=/submitted>"
      "<input type=hidden name=field value=engine>"
      "<button id=submitter style='position:absolute;left:0;top:0;"
      "width:64px;height:24px'>submit</button></form>"
      "<script>window.pageId='two'</script>";
  static const char posted[] =
      "<!doctype html><meta "
      "charset=utf-8><script>window.pageId='posted'</script>";
  static const char csp[] =
      "<!doctype html><meta charset=utf-8><div id=policy-marker>CSP</div>"
      "<script>window.cspRan=true</script>";
  const char* body = nullptr;
  size_t body_size = 0;
  if (url == "https://engine.invalid/one" ||
      url == "https://engine.invalid/stop" ||
      url == "https://engine.invalid/frame-deny" ||
      url == "https://engine.invalid/frame-ancestors" ||
      url == "https://engine.invalid/sandbox") {
    body = one;
    body_size = sizeof(one) - 1;
  } else if (url == "https://engine.invalid/two" ||
             url == "https://other.invalid/foreign") {
    body = two;
    body_size = sizeof(two) - 1;
  } else if (url == "https://engine.invalid/submitted") {
    if (std::string_view(request->method_utf8) != "POST")
      return BE_RESOURCE_ERROR;
    body = posted;
    body_size = sizeof(posted) - 1;
  } else if (url == "https://engine.invalid/csp") {
    body = csp;
    body_size = sizeof(csp) - 1;
  } else if (url != "https://engine.invalid/redirect") {
    return BE_RESOURCE_ERROR;
  }
  ++context.successful_fetches;
  response->http_status = url == "https://engine.invalid/redirect" ? 302 : 200;
  response->mime_type = "text/html";
  response->charset = "UTF-8";
  response->body = reinterpret_cast<const uint8_t*>(body);
  response->body_size = body_size;
  response->headers_utf8 =
      url == "https://engine.invalid/frame-deny" ? "X-Frame-Options: DENY\r\n"
      : url == "https://engine.invalid/frame-ancestors"
          ? "Content-Security-Policy: frame-ancestors 'none'\r\n"
      : url == "https://engine.invalid/sandbox"
          ? "Content-Security-Policy: sandbox allow-scripts\r\n"
      : url == "https://engine.invalid/csp"
          ? "Content-Security-Policy: script-src "
            "'none'\r\nX-Engine-Smoke: policy\r\n"
          : "X-Engine-Smoke: main-document\r\n";
  response->redirect_url_utf8 = response->http_status == 302 ? "/two" : nullptr;
  response->release_user_data = &context;
  response->release = ReleaseNavigation;
  return BE_OK;
}
void NavigationEvent(void* data, be_page*, const be_navigation_event* event) {
  auto& context = *static_cast<NavigationContext*>(data);
  if (event && event->struct_size >= sizeof(*event) &&
      event->abi_version == BE_ABI_VERSION)
    context.events.push_back(event->type);
}
unsigned EventCount(const NavigationContext& context, uint32_t type) {
  unsigned count = 0;
  for (auto event : context.events)
    if (event == type)
      ++count;
  return count;
}
bool NavigationState(be_page* page, be_navigation_state& state) {
  state = {};
  state.struct_size = sizeof(state);
  state.abi_version = BE_ABI_VERSION;
  return Check(be_page_get_navigation_state(page, &state), "navigation state");
}
bool Settled(be_engine* engine, be_page* page, be_status expected = BE_OK) {
  for (unsigned turn = 0; turn < 200; ++turn) {
    if (!Pump(engine, 1))
      return false;
    be_navigation_state state{};
    if (!NavigationState(page, state))
      return false;
    if (!state.pending && !state.loading)
      return Assert(state.last_status == expected,
                    "completed navigation status");
  }
  return Assert(false, "navigation did not finish within bounded task turns");
}
bool AtDocument(be_page* page, const char* url, const char* id) {
  be_navigation_state state{};
  if (!NavigationState(page, state) ||
      !Assert(state.url_utf8 && std::string_view(state.url_utf8) == url,
              "committed navigation URL"))
    return false;
  const std::string expression =
      std::string("window.pageId===") + "'" + id + "'";
  return Evaluate(page, expression.c_str(), "true",
                  "committed document script");
}

bool NavigationSmoke(be_engine* engine,
                     be_page* page,
                     NavigationContext& context) {
  be_navigation_client client{};
  client.struct_size = sizeof(client);
  client.abi_version = BE_ABI_VERSION;
  client.user_data = &context;
  client.fetch = FetchNavigation;
  client.event = NavigationEvent;
  if (!Assert(be_page_navigate(page, "file:///standalone-not-supported") ==
                  BE_NOT_SUPPORTED,
              "unsupported navigation scheme") ||
      !Check(be_page_set_navigation_client(page, &client),
             "navigation host installation") ||
      !Check(be_page_navigate(page, "https://engine.invalid/one"),
             "host GET navigation"))
    return false;
  be_navigation_state first{};
  if (!NavigationState(page, first) ||
      !Assert(first.pending != 0, "navigation is queued before pump") ||
      !Settled(engine, page) ||
      !AtDocument(page, "https://engine.invalid/one", "one"))
    return false;
  if (!Assert(
          !context.requests.empty() && context.requests.back().method == "GET",
          "main document GET transport") ||
      !Check(be_page_navigate(page, "https://engine.invalid/redirect"),
             "redirect navigation") ||
      !Settled(engine, page) ||
      !AtDocument(page, "https://engine.invalid/two", "two") ||
      !Evaluate(page, "navigation.canGoBack && !navigation.canGoForward",
                "true", "Navigation API reports last history entry") ||
      !Assert(EventCount(context, BE_NAV_REDIRECTED) >= 1,
              "redirect event reaches host"))
    return false;
  be_navigation_state second{};
  if (!NavigationState(page, second))
    return false;
  const auto second_index = second.history_index;
  const auto second_length = second.history_length;
  if (!Check(be_page_go_back_forward(page, -1),
             "cross-document history back") ||
      !Settled(engine, page) ||
      !AtDocument(page, "https://engine.invalid/one", "one") ||
      !Evaluate(page, "navigation.canGoForward", "true",
                "Navigation API reports a forward entry after back") ||
      !Check(be_page_go_back_forward(page, 1),
             "cross-document history forward") ||
      !Settled(engine, page) ||
      !AtDocument(page, "https://engine.invalid/two", "two"))
    return false;
  const auto requests_before_reload = context.requests.size();
  if (!Check(be_page_reload(page), "reload") || !Settled(engine, page) ||
      !AtDocument(page, "https://engine.invalid/two", "two") ||
      !Assert(context.requests.size() > requests_before_reload,
              "reload invokes host transport"))
    return false;
  be_navigation_state state{};
  if (!NavigationState(page, state) ||
      !Assert(state.history_index == second_index &&
                  state.history_length == second_length,
              "reload preserves history position") ||
      !Check(be_page_navigate(page, "https://engine.invalid/denied"),
             "denied navigation submission") ||
      !Settled(engine, page, BE_RESOURCE_ERROR) ||
      !AtDocument(page, "https://engine.invalid/two", "two") ||
      !NavigationState(page, state) ||
      !Assert(state.history_index == second_index &&
                  state.history_length == second_length &&
                  EventCount(context, BE_NAV_FAILED) >= 1,
              "denied navigation preserves document and history"))
    return false;
  const auto requests_before_stop = context.requests.size();
  if (!Check(be_page_navigate(page, "https://engine.invalid/stop"),
             "stoppable navigation submission") ||
      !Check(be_page_stop(page), "stop pending navigation") || !Pump(engine) ||
      !NavigationState(page, state) ||
      !Assert(!state.pending && !state.loading && state.last_status == BE_OK &&
                  context.requests.size() == requests_before_stop &&
                  EventCount(context, BE_NAV_CANCELLED) >= 1,
              "stop cancels before transport") ||
      !AtDocument(page, "https://engine.invalid/two", "two"))
    return false;
  if (!Evaluate(page, "location.href='/one';true", "true",
                "script location navigation") ||
      !Settled(engine, page) ||
      !AtDocument(page, "https://engine.invalid/one", "one") ||
      !Check(be_page_layout(page), "link layout") || !Click(page, 10, 10) ||
      !Settled(engine, page) ||
      !AtDocument(page, "https://engine.invalid/two", "two") ||
      !Evaluate(page, "postform.requestSubmit();true", "true",
                "form submission") ||
      !Settled(engine, page) ||
      !AtDocument(page, "https://engine.invalid/submitted", "posted"))
    return false;
  const auto& post = context.requests.back();
  if (!Assert(post.method == "POST" && post.body == "field=engine" &&
                  post.headers.find("application/x-www-form-urlencoded") !=
                      std::string::npos,
              "form POST body and content type reach host"))
    return false;
  if (!NavigationState(page, state))
    return false;
  const auto posted_index = state.history_index;
  const auto posted_length = state.history_length;
  const auto requests_before_same_document = context.requests.size();
  if (!Evaluate(page, "history.pushState({marker:7},'', '#same');true", "true",
                "same-document pushState") ||
      !Settled(engine, page) ||
      !AtDocument(page, "https://engine.invalid/submitted#same", "posted") ||
      !NavigationState(page, state) ||
      !Assert(state.history_index == posted_index + 1 &&
                  state.history_length == posted_length + 1,
              "pushState adds a history entry") ||
      !Evaluate(page, "history.back();true", "true",
                "script same-document history back") ||
      !Settled(engine, page) ||
      !AtDocument(page, "https://engine.invalid/submitted", "posted") ||
      !Evaluate(page, "history.state===null", "true",
                "restored original history state") ||
      !Evaluate(page, "history.forward();true", "true",
                "script same-document history forward") ||
      !Settled(engine, page) ||
      !AtDocument(page, "https://engine.invalid/submitted#same", "posted") ||
      !Evaluate(page, "history.state.marker===7", "true",
                "restored pushed history state") ||
      !Assert(context.requests.size() == requests_before_same_document &&
                  EventCount(context, BE_NAV_SAME_DOCUMENT) >= 3,
              "same-document traversal uses no transport") ||
      !Check(be_page_navigate(page, "https://engine.invalid/csp"),
             "response CSP navigation") ||
      !Settled(engine, page) ||
      !Evaluate(page,
                "Boolean(document.getElementById('policy-marker')) && "
                "typeof window.cspRan==='undefined'",
                "true", "response CSP blocks document inline script"))
    return false;
  const auto requests_before_api_navigation = context.requests.size();
  if (!Evaluate(page,
                "window.navApiCommitted=false;window.navApiFinished=false;"
                "window.navApiRejected=false;"
                "const apiResult=navigation.navigate('#navigation-api');"
                "apiResult.committed.then(entry=>{window.navApiCommitted="
                "entry.url.endsWith('#navigation-api')}).catch(()=>{"
                "window.navApiRejected=true});"
                "apiResult.finished.then(entry=>{window.navApiFinished="
                "entry.url.endsWith('#navigation-api')}).catch(()=>{"
                "window.navApiRejected=true});void 0",
                "undefined", "Navigation API same-document submission") ||
      !Settled(engine, page) || !Pump(engine) ||
      !Evaluate(
          page,
          "window.navApiCommitted && window.navApiFinished && "
          "!window.navApiRejected && location.hash==='#navigation-api' && "
          "navigation.currentEntry.url===location.href && "
          "navigation.canGoBack && !navigation.canGoForward",
          "true", "Navigation API promises and current entry") ||
      !Assert(context.requests.size() == requests_before_api_navigation,
              "Navigation API fragment change uses no transport") ||
      !Assert(context.releases == context.successful_fetches,
              "main-document responses released exactly once"))
    return false;
  if (!Check(be_page_navigate(page, "https://other.invalid/foreign"),
             "cross-origin navigation") ||
      !Settled(engine, page) ||
      !Evaluate(page,
                "navigation.entries().every(e=>new "
                "URL(e.url).origin===location.origin) && !navigation.canGoBack",
                "true", "Navigation API hides cross-origin history") ||
      !Check(be_page_go_back_forward(page, -1),
             "host cross-origin history back") ||
      !Settled(engine, page) ||
      !Evaluate(page,
                "location.origin==='https://engine.invalid' && "
                "!navigation.canGoForward",
                "true",
                "host traversal preserves Navigation API origin boundary") ||
      !Evaluate(page,
                "for(let i=0;i<70;++i)history.pushState({i},'', "
                "'#capacity-'+i);void 0",
                "undefined", "bounded session history") ||
      !Pump(engine) ||
      !Evaluate(page, "history.length===64 && navigation.entries().length===64",
                "true", "Navigation API retires evicted history entries") ||
      !Assert(context.releases == context.successful_fetches,
              "cross-origin response ownership") ||
      !Check(be_page_navigate(page, "https://engine.invalid/sandbox"),
             "sandbox policy navigation") ||
      !Settled(engine, page) ||
      !Evaluate(page,
                "window.origin==='null' && window.pageId==='one' && "
                "navigation.entries().length===0",
                "true", "CSP sandbox commits an opaque origin") ||
      !Assert(context.releases == context.successful_fetches,
              "sandbox response ownership"))
    return false;
  return true;
}
struct StorageContext {
  std::mutex mutex;
  std::unordered_map<std::string, std::vector<uint8_t>> snapshots;
  std::atomic<unsigned> loads{0}, saves{0}, errors{0};
  std::atomic<bool> wrong_thread{false}, fail_save{false};
};
void ReleaseStorage(void* data) {
  delete static_cast<std::vector<uint8_t>*>(data);
}
be_status LoadStorage(void* data,
                      const char* key,
                      be_storage_snapshot* response) {
  auto& context = *static_cast<StorageContext*>(data);
  if (std::this_thread::get_id() == owner_thread)
    context.wrong_thread = true;
  ++context.loads;
  auto* bytes = new std::vector<uint8_t>;
  {
    std::lock_guard<std::mutex> lock(context.mutex);
    const auto found = context.snapshots.find(key);
    if (found != context.snapshots.end())
      *bytes = found->second;
  }
  response->data = bytes->data();
  response->size = bytes->size();
  response->release_user_data = bytes;
  response->release = ReleaseStorage;
  return BE_OK;
}
be_status SaveStorage(void* data,
                      const char* key,
                      const uint8_t* bytes,
                      size_t size) {
  auto& context = *static_cast<StorageContext*>(data);
  if (std::this_thread::get_id() == owner_thread)
    context.wrong_thread = true;
  if (context.fail_save.exchange(false))
    return BE_RESOURCE_ERROR;
  if (!bytes && size)
    return BE_INVALID_ARGUMENT;
  ++context.saves;
  std::lock_guard<std::mutex> lock(context.mutex);
  auto& snapshot = context.snapshots[key];
  if (!size) {
    snapshot.clear();
    return BE_OK;
  }
  // The synchronous storage save ABI keeps exactly `size` readable bytes
  // alive for this callback. Null is rejected above for nonempty snapshots.
  SMOKE_UNSAFE_BUFFERS(const std::span<const uint8_t> data_bytes(bytes, size));
  snapshot.assign(data_bytes.begin(), data_bytes.end());
  return BE_OK;
}
void StorageError(void* data, const char*, be_status) {
  ++static_cast<StorageContext*>(data)->errors;
}
bool StorageSmoke(be_engine* engine, be_page* page, StorageContext& context) {
  static const char html[] = "<!doctype html><div>DOM storage</div>";
  if (!Check(be_page_load_html(page, html, sizeof(html) - 1,
                               "https://engine.invalid/storage"),
             "storage document") ||
      !Pump(engine) ||
      !Evaluate(
          page,
          "localStorage.clear();sessionStorage.clear();localStorage.setItem('"
          "shared','한😀');sessionStorage.setItem('session','primary');true",
          "true", "native DOM storage writes") ||
      !Check(be_engine_flush_storage(engine), "storage host flush") ||
      !Assert(context.loads > 0 && context.saves > 0 && !context.wrong_thread,
              "persistent host callbacks run on storage IO thread"))
    return false;
  be_page_config config{sizeof(config), BE_ABI_VERSION, 80, 80};
  be_page* peer = nullptr;
  if (!Check(be_page_create(engine, &config, &peer), "storage peer page") ||
      !Check(be_page_load_html(peer, html, sizeof(html) - 1,
                               "https://engine.invalid/peer"),
             "peer document") ||
      !Pump(engine) ||
      !Evaluate(peer,
                "localStorage.getItem('shared')==='한😀' && "
                "sessionStorage.getItem('session')===null",
                "true", "shared local and isolated page session namespaces") ||
      !Evaluate(peer,
                "window.storageEvents=[];addEventListener('storage',e=>"
                "storageEvents.push({key:e.key,value:e.newValue}));true",
                "true", "storage event subscription") ||
      !Evaluate(page, "localStorage.setItem('event','delivered');true", "true",
                "storage event write") ||
      !Check(be_engine_flush_storage(engine),
             "storage event backend barrier") ||
      !Pump(engine) ||
      !Evaluate(peer,
                "storageEvents.some(e=>e.key==='event'&&e.value==='delivered')",
                "true", "native cross-page storage event") ||
      !Check(be_page_load_html(page, html, sizeof(html) - 1,
                               "https://engine.invalid/next"),
             "same-page storage navigation") ||
      !Pump(engine) ||
      !Evaluate(page, "sessionStorage.getItem('session')==='primary'", "true",
                "session namespace survives navigation") ||
      !Check(be_page_load_html(peer, html, sizeof(html) - 1,
                               "https://other.invalid/isolated"),
             "different origin storage") ||
      !Pump(engine) ||
      !Evaluate(peer, "localStorage.getItem('shared')===null", "true",
                "storage origin isolation") ||
      !Evaluate(
          peer,
          "window.partitionMessage='pending';addEventListener('message',e=>{if("
          "e.data&&'partition' in "
          "e.data)window.partitionMessage=e.data.partition});(()=>{const "
          "f=document.createElement('iframe');f.src='https://engine.invalid/"
          "storage-frame';document.body.append(f)})();true",
          "true", "third-party storage frame") ||
      !Pump(engine) ||
      !Evaluate(peer, "window.partitionMessage===null", "true",
                "third-party storage partition isolation") ||
      !Check(be_page_load_html(peer, html, sizeof(html) - 1,
                               "https://engine.invalid:8443/isolated"),
             "different port storage") ||
      !Pump(engine) ||
      !Evaluate(peer, "localStorage.getItem('shared')===null", "true",
                "storage port isolation") ||
      !Check(be_page_destroy(peer), "storage peer destruction") ||
      !Assert(be_engine_set_storage_client(engine, nullptr) == BE_BUSY,
              "storage policy cannot change after document exposure"))
    return false;
  // Failure reporting must be exercised last: the host status is intentionally
  // sticky for this engine and must not be cleared to manufacture a success.
  context.fail_save = true;
  if (!Evaluate(page, "localStorage.setItem('failed-write','value');true",
                "true", "host save failure submission") ||
      !Assert(be_engine_flush_storage(engine) == BE_RESOURCE_ERROR,
              "host save error is observable") ||
      !Pump(engine) ||
      !Evaluate(page, "localStorage.getItem('failed-write')===null", "true",
                "failed host save rolls back renderer cache"))
    return false;
  be_status status = BE_OK;
  return Check(be_engine_get_storage_status(engine, &status),
               "storage status") &&
         Assert(status == BE_RESOURCE_ERROR && context.errors > 0,
                "storage error callback and sticky failure status");
}

bool FrameSmoke(be_engine* engine, be_page* page) {
  static const char html[] =
      "<!doctype html><style>html,body{margin:0;background:white}</style>";
  if (!Check(be_page_load_html(page, html, sizeof(html) - 1,
                               "https://engine.invalid/frame-root"),
             "frame root") ||
      !Pump(engine) ||
      !Evaluate(page, R"JS((()=>{
        window.frameMessages=[];
        addEventListener('message',e=>frameMessages.push(e.data));
        const f=document.createElement('iframe');f.id='child';
        f.style='position:absolute;left:4px;top:4px;width:64px;height:64px;border:0';
        f.srcdoc="<style>html,body{margin:0;background:rgb(0,255,0)}input{position:absolute;top:30px;left:0;width:50px}</style><input id=edit><script>parent.postMessage('child-ready','*')<\/script>";
        document.body.append(f);return true;
      })())JS",
                "true", "create real srcdoc child") ||
      !Pump(engine) ||
      !Evaluate(page,
                "frames.length===1 && frameMessages.includes('child-ready') && "
                "document.getElementById('child').contentDocument."
                "getElementById('edit')!==null",
                "true", "child document, script and postMessage"))
    return false;
  std::vector<uint8_t> pixels(80 * 80 * 4);
  if (!Check(be_page_paint_rgba(page, pixels.data(), pixels.size(), 80 * 4),
             "child software paint") ||
      !Assert(Pixel(pixels, 80, 8, 8, 0, 255, 0),
              "actual child frame pixels") ||
      !Evaluate(page, R"JS((()=>{
        const f=document.getElementById('child');
        const nested=f.contentDocument.createElement('iframe');
        nested.srcdoc="<script>parent.parent.postMessage('nested-ready','*')<\/script>";
        f.contentDocument.body.append(nested);
        const sandbox=document.createElement('iframe');sandbox.id='isolated';
        sandbox.sandbox='allow-scripts';
        sandbox.srcdoc="<script>parent.postMessage({opaque:self.origin==='null'},'*')<\/script>";
        document.body.append(sandbox);return true;
      })())JS",
                "true", "nested and sandboxed children") ||
      !Pump(engine) ||
      !Evaluate(page,
                "frameMessages.includes('nested-ready') && "
                "frameMessages.some(m=>m&&m.opaque) && "
                "document.getElementById('isolated').contentDocument===null",
                "true", "nested message and opaque sandbox boundary") ||
      !Evaluate(page, R"JS((()=>{
        for(const [id,url] of [['foreign','https://other.invalid/foreign'],['xfo','/frame-deny'],['ancestors','/frame-ancestors']]){
          const f=document.createElement('iframe');f.id=id;f.src=url;document.body.append(f);
        } return true;
      })())JS",
                "true", "host-backed child navigations") ||
      !Pump(engine) ||
      !Evaluate(
          page,
          "document.getElementById('foreign').contentDocument===null && "
          "document.getElementById('xfo').contentWindow.pageId===undefined && "
          "document.getElementById('ancestors').contentWindow.pageId==="
          "undefined",
          "true", "cross-origin access, XFO and frame-ancestors enforcement") ||
      !Evaluate(
          page,
          "document.getElementById('foreign').remove();document.getElementById("
          "'xfo').remove();document.getElementById('ancestors').remove();"
          "document.getElementById('isolated').remove();true",
          "true", "detach children") ||
      !Pump(engine) ||
      !Evaluate(page, "frames.length===1 && frames[0].frames.length===1",
                "true", "live frame tree after sibling removal"))
    return false;
  return true;
}

bool ImeSmoke(be_engine* engine, be_page* page) {
  if (!Check(be_page_set_focus(page, 1), "IME page focus") ||
      !Evaluate(page,
                "document.getElementById('child').contentDocument."
                "getElementById('edit').focus();true",
                "true", "IME child editable focus") ||
      !Pump(engine))
    return false;
  be_ime_span span{0, 1, BE_IME_THICKNESS_THIN, BE_IME_UNDERLINE_SOLID};
  be_ime_composition composition{};
  composition.struct_size = sizeof(composition);
  composition.abi_version = BE_ABI_VERSION;
  composition.text_utf8 = "한";
  composition.text_size = std::string_view("한").size();
  composition.spans = &span;
  composition.span_count = 1;
  composition.selection_start_utf16 = composition.selection_end_utf16 = 1;
  be_ime_state state{};
  state.struct_size = sizeof(state);
  state.abi_version = BE_ABI_VERSION;
  size_t required = 0;
  if (!Check(be_page_ime_set_composition(page, &composition),
             "IME composition") ||
      !Assert(be_page_ime_get_state(page, &state, nullptr, 0, &required) ==
                      BE_BUFFER_TOO_SMALL &&
                  required >= 4 && state.composition_start_utf16 == 0 &&
                  state.composition_end_utf16 == 1 && state.caret_bounds.x >= 4,
              "composition state, UTF-8 size query and root caret") ||
      !Check(be_page_ime_commit_text(page, "한😀",
                                     std::string_view("한😀").size(), 0),
             "IME Unicode commit") ||
      !Evaluate(page,
                "frames[0].document.getElementById('edit').value==='한😀'",
                "true", "IME document edit"))
    return false;
  std::array<char, 64> surrounding{};
  if (!Check(be_page_ime_get_state(page, &state, surrounding.data(),
                                   surrounding.size(), &required),
             "IME surrounding text") ||
      !Assert(required == std::string_view("한😀").size() + 1 &&
                  required <= surrounding.size() &&
                  std::string_view(surrounding.data(), required - 1) == "한😀" &&
                  surrounding[required - 1] == '\0' &&
                  state.text_utf16_length == 3 &&
                  state.selection_start_utf16 == 3 &&
                  state.composition_start_utf16 == -1,
              "UTF-16 selection and finished composition") ||
      !Check(be_page_ime_set_composition(page, &composition),
             "second composition") ||
      !Check(be_page_ime_cancel_composition(page),
             "composition cancellation") ||
      !Evaluate(page,
                "frames[0].document.getElementById('edit').value==='한😀'",
                "true", "cancel preserves committed text") ||
      !Check(be_page_ime_set_composition(page, &composition),
             "final composition") ||
      !Check(be_page_ime_finish_composition(page, 0), "finish composition") ||
      !Evaluate(page,
                "frames[0].document.getElementById('edit').value==='한😀한'",
                "true", "finish keeps composed text"))
    return false;
  return true;
}

bool AwaitScript(be_engine* engine,
                 be_page* page,
                 std::string_view source,
                 const char* action) {
  const auto deadline =
      std::chrono::steady_clock::now() + std::chrono::seconds(15);
  while (std::chrono::steady_clock::now() < deadline) {
    if (!Check(be_engine_pump(engine, 8), "worker task pump"))
      return false;
    be_script_result result{};
    result.struct_size = sizeof(result);
    result.abi_version = BE_ABI_VERSION;
    if (!Check(be_page_evaluate_script(page, source.data(), source.size(),
                                       nullptr, &result),
               action))
      return false;
    if (result.utf8_data &&
        std::string_view(result.utf8_data, result.utf8_size) == "true")
      return true;
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
  return Assert(false, action);
}

bool WorkerSmoke(be_engine* engine, be_page* page) {
  static const char html[] = "<!doctype html><div>Worker host</div>";
  if (!Check(be_page_load_html(page, html, sizeof(html) - 1,
                               "https://engine.invalid/worker-root"),
             "worker document") ||
      !Pump(engine) ||
      !Evaluate(page,
                "window.workerReply=null;window.workerError=false;window."
                "testWorker=new "
                "Worker('/"
                "worker.js');testWorker.onmessage=e=>window.workerReply=e.data;"
                "testWorker.onerror=e=>{e.preventDefault();window.workerError="
                "true};testWorker.postMessage({value:40});true",
                "true", "real Worker construction and structured message") ||
      !AwaitScript(engine, page, "Boolean(workerReply||workerError)",
                   "worker reply arrives") ||
      !Evaluate(page,
                "!workerError && workerReply.answer===42 && "
                "workerReply.data==='engine' && workerReply.worker",
                "true",
                "worker thread, importScripts, fetch and postMessage") ||
      !Evaluate(
          page,
          "window.moduleDenied=false;window.missingDenied=false;const "
          "rejectedModule=new "
          "Worker('/"
          "worker.js',{type:'module'});rejectedModule.onerror=e=>{e."
          "preventDefault();window.moduleDenied=true};const missingWorker=new "
          "Worker('/"
          "missing-worker.js');missingWorker.onerror=e=>{e.preventDefault();"
          "window.missingDenied=true};const immediate=new "
          "Worker('/worker.js');immediate.terminate();true",
          "true", "unsupported/failed workers and immediate termination") ||
      !AwaitScript(engine, page, "moduleDenied && missingDenied",
                   "worker failure events") ||
      !Evaluate(page, "testWorker.terminate();window.testWorker=null;true",
                "true", "Worker terminate") ||
      !Pump(engine))
    return false;
  // This final worker is deliberately left live. Page destruction must request
  // its real Blink thread's termination before shutdown can succeed.
  return Evaluate(
             page,
             "window.liveWorker=new "
             "Worker('/"
             "worker.js');window.liveReply=false;liveWorker.onmessage=()=>"
             "window.liveReply=true;liveWorker.postMessage({value:1});true",
             "true", "worker tied to document lifetime") &&
         AwaitScript(engine, page, "liveReply",
                     "live worker before page destruction");
}
}  // namespace

int main(int argc, char** argv) {
  owner_thread = std::this_thread::get_id();
  // Hosted C++ main guarantees argc nonnegative and argv[0..argc) readable;
  // argv strings remain valid for this program's lifetime.
  SMOKE_UNSAFE_BUFFERS(const std::span<char* const> arguments(
      argv, static_cast<size_t>(argc)));
  const auto icu = Argument(arguments, "icu-data");
  const auto snapshot = Argument(arguments, "v8-snapshot");
  const auto resources = Argument(arguments, "blink-resources");
  const auto scaled = Argument(arguments, "blink-scaled-resources");
  be_engine_config config{};
  config.struct_size = sizeof(config);
  config.abi_version = BE_ABI_VERSION;
  config.icu_data_path = icu.c_str();
  config.v8_snapshot_path = snapshot.c_str();
  config.blink_resources_path = resources.c_str();
  config.blink_scaled_resources_path =
      scaled.empty() ? nullptr : scaled.c_str();
  config.wake_up = Wake;
  config.fetch_resource = Fetch;
  be_engine* engine = nullptr;
  if (!Check(be_engine_create(&config, &engine), "engine initialization"))
    return 1;
  StorageContext storage;
  be_storage_client storage_client{};
  storage_client.struct_size = sizeof(storage_client);
  storage_client.abi_version = BE_ABI_VERSION;
  storage_client.mode = BE_STORAGE_HOST_PERSISTENT;
  storage_client.user_data = &storage;
  storage_client.load = LoadStorage;
  storage_client.save = SaveStorage;
  storage_client.error = StorageError;
  if (!Check(be_engine_set_storage_client(engine, &storage_client),
             "storage host setup"))
    return 14;
  be_page_config page_config{sizeof(page_config), BE_ABI_VERSION, 64, 64};
  be_page* page = nullptr;
  if (!Check(be_page_create(engine, &page_config, &page), "page creation"))
    return 2;
  static const char html[] =
      "<!doctype html><link rel=stylesheet href=theme.css><div id=sample></div>"
      "<script>window.inlineRuns=(window.inlineRuns||0)+1</script>"
      "<script src=bootstrap.js></script>";
  if (!Check(be_page_load_html(page, html, sizeof(html) - 1,
                               "https://engine.invalid/"),
             "HTML commit"))
    return 3;
  for (unsigned turn = 0; turn < 20; ++turn)
    if (!Check(be_engine_pump(engine, 8), "task pump"))
      return 4;
  std::vector<uint8_t> pixels(64 * 64 * 4, 0);
  if (!Check(be_page_paint_rgba(page, pixels.data(), pixels.size(), 64 * 4),
             "software paint"))
    return 5;
  if (fetches < 2 || !Pixel(pixels, 64, 8, 8, 255, 0, 0) ||
      !Pixel(pixels, 64, 48, 48, 255, 255, 255)) {
    std::cerr << "Rendered pixels or host resource request mismatch\n";
    return 6;
  }
  be_status other_thread = BE_OK;
  std::thread wrong_thread([&] { other_thread = be_page_layout(page); });
  wrong_thread.join();
  if (other_thread != BE_WRONG_THREAD || be_engine_shutdown(engine) != BE_BUSY)
    return 7;
  if (!Check(be_page_resize(page, 80, 80), "resize"))
    return 8;
  pixels.assign(80 * 80 * 4, 0);
  if (!Check(be_page_paint_rgba(page, pixels.data(), pixels.size(), 80 * 4),
             "resized software paint") ||
      !Pixel(pixels, 80, 8, 8, 255, 0, 0) ||
      !Pixel(pixels, 80, 72, 72, 255, 255, 255))
    return 9;
  uint64_t checksum = UINT64_C(14695981039346656037);
  for (auto byte : pixels) {
    checksum ^= byte;
    checksum *= UINT64_C(1099511628211);
  }
  NavigationContext navigation;
  if (!JavaScriptSmoke(page))
    return 11;
  if (!InputSmoke(engine, page))
    return 12;
  if (!NavigationSmoke(engine, page, navigation))
    return 13;
  if (!FrameSmoke(engine, page) || !ImeSmoke(engine, page))
    return 15;
  if (!StorageSmoke(engine, page, storage))
    return 16;
  if (!WorkerSmoke(engine, page))
    return 17;
  if (!Check(be_page_destroy(page), "page destruction"))
    return 10;
  const auto deadline =
      std::chrono::steady_clock::now() + std::chrono::seconds(15);
  be_status shutdown = BE_BUSY;
  while (shutdown == BE_BUSY && std::chrono::steady_clock::now() < deadline) {
    if (!Pump(engine, 1))
      return 18;
    shutdown = be_engine_shutdown(engine);
    if (shutdown == BE_BUSY)
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
  if (!Check(shutdown, "worker-aware engine shutdown"))
    return 19;
  std::cout << "{\"schema_version\":3,\"abi_version\":" << be_get_abi_version()
            << ","
               "\"initialized\":true,\"html_committed\":true,"
               "\"host_resource_fetches\":"
            << fetches
            << ",\"pixel_assertions\":true,"
               "\"resized\":true,\"wrong_thread_rejected\":true,"
               "\"javascript\":true,\"input\":true,\"navigation\":true,"
               "\"ime\":true,\"child_frames\":true,\"storage\":true,\"workers\":true,"
               "\"shutdown\":true,\"width\":80,\"height\":80,"
               "\"rgba_fnv1a64\":\""
            << std::hex << std::setfill('0') << std::setw(16) << checksum
            << "\"}\n";
  return 0;
}
