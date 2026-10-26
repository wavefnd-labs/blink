/* Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE. */
#ifndef BLINK_STANDALONE_INCLUDE_BLINK_ENGINE_H_
#define BLINK_STANDALONE_INCLUDE_BLINK_ENGINE_H_

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#if defined(BLINK_EMBED_IMPLEMENTATION)
#define BE_EXPORT __declspec(dllexport)
#else
#define BE_EXPORT __declspec(dllimport)
#endif
#else
#define BE_EXPORT __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define BE_ABI_VERSION 1u
typedef struct be_engine be_engine;
typedef struct be_page be_page;

typedef enum be_status {
  BE_OK = 0,
  BE_INVALID_ARGUMENT = 1,
  BE_ABI_MISMATCH = 2,
  BE_WRONG_THREAD = 3,
  BE_INVALID_STATE = 4,
  BE_BUSY = 5,
  BE_NOT_SUPPORTED = 6,
  BE_RESOURCE_ERROR = 7,
  BE_BUFFER_TOO_SMALL = 8,
  BE_RENDER_ERROR = 9,
  BE_SCRIPT_EXCEPTION = 10,
  BE_SCRIPT_ERROR = 11
} be_status;

enum {
  BE_CAP_LOCAL_HTML = 1u << 0,
  BE_CAP_SOFTWARE_RGBA = 1u << 1,
  BE_CAP_HOST_RESOURCES = 1u << 2,
  BE_CAP_TASK_PUMP = 1u << 3,
  BE_CAP_JAVASCRIPT = 1u << 4,
  BE_CAP_INPUT = 1u << 5,
  BE_CAP_NAVIGATION = 1u << 6,
  BE_CAP_IME = 1u << 7,
  BE_CAP_CHILD_FRAMES = 1u << 8,
  BE_CAP_DOM_STORAGE = 1u << 9,
  BE_CAP_DEDICATED_WORKERS = 1u << 10
};

typedef struct be_resource_response {
  uint32_t struct_size;
  uint32_t abi_version;
  const uint8_t* data;
  size_t size;
  const char* mime_type;
  const char* charset;
  void* release_user_data;
  /* Called exactly once after a successful callback, even for invalid data.
   * Keep all response pointers valid until this callback. May be NULL when
   * all response pointers have process lifetime. */
  void (*release)(void* release_user_data);
} be_resource_response;

typedef struct be_engine_config {
  uint32_t struct_size;
  uint32_t abi_version;
  /* Absolute paths to the pinned build's data files. */
  const char* icu_data_path;
  const char* v8_snapshot_path;
  const char* blink_resources_path;
  /* Optional blink_scaled_resources_100_percent.pak. */
  const char* blink_scaled_resources_path;
  const char* locale; /* UTF-8 BCP 47; NULL selects en-US. */
  void* user_data;
  /* Can run on ANY thread. Queue a pump on the owning thread after delay_ms;
   * never call engine functions from inside any callback. */
  void (*wake_up)(void* user_data, uint64_t delay_ms);
  /* Owning thread, synchronous, GET only. NULL denies every subresource.
   * Return BE_OK with a complete immutable response, or an error to deny.
   * The engine never opens URLs/files or contacts a network service itself. */
  be_status (*fetch_resource)(void* user_data,
                              const char* url_utf8,
                              be_resource_response* response);
} be_engine_config;

typedef struct be_page_config {
  uint32_t struct_size;
  uint32_t abi_version;
  uint32_t width;
  uint32_t height;
} be_page_config;

/* Additive ABI 1 extensions; existing config structures retain their layout. */
typedef struct be_script_result {
  uint32_t struct_size;
  uint32_t abi_version;
  /* JSON, literal "undefined", or an exception/error message. Borrowed until
   * this page's next evaluation, document commit or destruction. */
  const char* utf8_data;
  size_t utf8_size;
  uint32_t is_exception;
} be_script_result;

typedef enum be_input_type {
  BE_INPUT_MOUSE_MOVE = 0,
  BE_INPUT_MOUSE_DOWN = 1,
  BE_INPUT_MOUSE_UP = 2,
  BE_INPUT_MOUSE_LEAVE = 3,
  BE_INPUT_WHEEL = 4,
  BE_INPUT_KEY_DOWN = 5,
  BE_INPUT_KEY_UP = 6,
  BE_INPUT_TEXT = 7
} be_input_type;
enum {
  BE_MOD_SHIFT = 1u << 0,
  BE_MOD_CTRL = 1u << 1,
  BE_MOD_ALT = 1u << 2,
  BE_MOD_META = 1u << 3,
  BE_MOD_CAPS_LOCK = 1u << 4,
  BE_MOD_NUM_LOCK = 1u << 5,
  BE_MOD_AUTO_REPEAT = 1u << 6,
  BE_MOD_KEY_PAD = 1u << 7
};
typedef enum be_mouse_button {
  BE_BUTTON_NONE = 0,
  BE_BUTTON_LEFT = 1,
  BE_BUTTON_MIDDLE = 2,
  BE_BUTTON_RIGHT = 3,
  BE_BUTTON_BACK = 4,
  BE_BUTTON_FORWARD = 5
} be_mouse_button;
enum {
  BE_BUTTONS_LEFT = 1u << 0,
  BE_BUTTONS_RIGHT = 1u << 1,
  BE_BUTTONS_MIDDLE = 1u << 2,
  BE_BUTTONS_BACK = 1u << 3,
  BE_BUTTONS_FORWARD = 1u << 4
};
typedef struct be_input_event {
  uint32_t struct_size;
  uint32_t abi_version;
  uint32_t type;
  uint32_t modifiers;
  /* Viewport CSS pixels. Wheel deltas are positive right/down. */
  double x, y;
  double delta_x, delta_y;
  uint32_t button, buttons, click_count;
  uint32_t windows_key_code, native_key_code;
  /* Bounded, NUL-terminated UTF-8 strings. Text uses BE_INPUT_TEXT separately
   * from key down/up. dom_code/key follow KeyboardEvent.code/key names. */
  char dom_code[32];
  char dom_key[64];
  char text_utf8[256];
} be_input_event;

typedef struct be_navigation_request {
  uint32_t struct_size;
  uint32_t abi_version;
  const char* url_utf8; /* Transport URL without a fragment. */
  const char* method_utf8;
  /* Request header lines, "Name: value\r\n". Borrowed during the callback. */
  const char* headers_utf8;
  const uint8_t* body;
  size_t body_size;
} be_navigation_request;
typedef struct be_navigation_response {
  uint32_t struct_size;
  uint32_t abi_version;
  uint32_t http_status;
  const char* mime_type;
  const char* charset;
  const uint8_t* body;
  size_t body_size;
  const char* headers_utf8;
  /* Required for 301/302/303/307/308; resolved relative to the request URL. */
  const char* redirect_url_utf8;
  void* release_user_data;
  void (*release)(void* release_user_data);
} be_navigation_response;
typedef enum be_navigation_event_type {
  BE_NAV_QUEUED = 0,
  BE_NAV_STARTED = 1,
  BE_NAV_REDIRECTED = 2,
  BE_NAV_COMMITTED = 3,
  BE_NAV_FINISHED = 4,
  BE_NAV_FAILED = 5,
  BE_NAV_CANCELLED = 6,
  BE_NAV_SAME_DOCUMENT = 7
} be_navigation_event_type;
typedef struct be_navigation_event {
  uint32_t struct_size;
  uint32_t abi_version;
  uint64_t navigation_id;
  uint32_t type;
  be_status status;
  const char* url_utf8;
} be_navigation_event;
typedef struct be_navigation_client {
  uint32_t struct_size;
  uint32_t abi_version;
  void* user_data;
  /* Synchronous main-document transport. Return BE_OK with a complete
   * response. Release is called exactly once after BE_OK, including invalid
   * responses. No callbacks may reenter engine functions. NULL uses the
   * engine's GET resource callback; other methods then fail explicitly. */
  be_status (*fetch)(void*,
                     const be_navigation_request*,
                     be_navigation_response*);
  void (*event)(void*, be_page*, const be_navigation_event*);
} be_navigation_client;
typedef struct be_navigation_state {
  uint32_t struct_size;
  uint32_t abi_version;
  uint64_t navigation_id;
  uint32_t loading;
  uint32_t pending;
  uint32_t history_length;
  int32_t history_index; /* -1 before the first commit. */
  be_status last_status;
  /* Borrowed until the next operation on this page. */
  const char* url_utf8;
} be_navigation_state;

BE_EXPORT uint32_t be_get_abi_version(void);
BE_EXPORT uint64_t be_get_capabilities(void);
BE_EXPORT const char* be_status_string(be_status status);

/* One initialization per process; the calling thread owns every handle.
 * Blink has no supported global teardown/restart API. Shutdown retires the
 * engine and releases page resources; process-global Blink/V8 infrastructure
 * stays alive until process exit. Unloading the library is unsupported. */
BE_EXPORT be_status be_engine_create(const be_engine_config* config,
                                     be_engine** engine_out);
BE_EXPORT be_status be_engine_shutdown(be_engine* engine);
/* Execute queued/due tasks for at most budget_ms (1..1000). Work that needs
 * another turn is signalled via wake_up. Does not wait for future timers. */
BE_EXPORT be_status be_engine_pump(be_engine* engine, uint32_t budget_ms);
BE_EXPORT be_status be_page_create(be_engine* engine,
                                   const be_page_config* config,
                                   be_page** page_out);
BE_EXPORT be_status be_page_destroy(be_page* page);
/* Commits supplied bytes as text/html UTF-8. base_url is about:blank or an
 * absolute http(s) URL used solely for origin and relative URL resolution.
 * JavaScript is enabled. Local child frames, DOM storage and dedicated
 * classic workers use the configured host services. Pump to finish queued
 * parsing, navigation and worker transport; see the runtime scope document. */
BE_EXPORT be_status be_page_load_html(be_page* page,
                                      const char* html_utf8,
                                      size_t html_size,
                                      const char* base_url_utf8);
BE_EXPORT be_status be_page_resize(be_page* page,
                                   uint32_t width,
                                   uint32_t height);
BE_EXPORT be_status be_page_layout(be_page* page);
/* Top-down, premultiplied RGBA8888; stride >= width*4. Caller owns memory.
 * No pointer is retained. Buffer must contain stride*height bytes. */
BE_EXPORT be_status be_page_paint_rgba(be_page* page,
                                       uint8_t* pixels,
                                       size_t pixels_size,
                                       size_t stride);

/* Runs in the current document's main world. Does not await Promises and has
 * no execution timeout. Result pointers are page-owned, never free them. */
BE_EXPORT be_status be_page_evaluate_script(be_page* page,
                                            const char* source_utf8,
                                            size_t source_size,
                                            const char* source_url_utf8,
                                            be_script_result* result);
BE_EXPORT be_status be_page_dispatch_input(be_page* page,
                                           const be_input_event* event,
                                           uint32_t* handled_out);
BE_EXPORT be_status be_page_set_focus(be_page* page, uint32_t focused);
BE_EXPORT be_status
be_page_set_navigation_client(be_page* page,
                              const be_navigation_client* client);
/* HTTP(S) and about:blank main-frame navigation. Transport and commits are
 * processed by pump; return BE_OK means submitted, not finished. Links,
 * forms and script location changes use the same host transport. */
BE_EXPORT be_status be_page_navigate(be_page* page, const char* url_utf8);
BE_EXPORT be_status be_page_reload(be_page* page);
BE_EXPORT be_status be_page_go_back_forward(be_page* page, int32_t offset);
BE_EXPORT be_status be_page_stop(be_page* page);
BE_EXPORT be_status be_page_get_navigation_state(be_page* page,
                                                 be_navigation_state* state);

#ifdef __cplusplus
} /* extern "C" */
#endif
#include "blink_ime.h"
#include "blink_storage.h"

#endif
