# Embedding runtime, ABI 1

This implementation supplies an engine host and a versioned C boundary outside
the pristine Blink tree. On 2026-10-26 the existing Linux x64 development build,
with incremental project-host fixes, passed the hash-verified schema 3 native
harness: rendering, JavaScript, input/IME, navigation, child frames, storage,
dedicated workers and shutdown. A separate Qt Widgets browser also passed real
input, DOM and pixel integration checks using this DSO. Local generated evidence
is recorded in `standalone/reports/profiles/dev-cache/linux-x64-runtime-check.json`
and `standalone/reports/wave-browser-integration.json`. Machine-specific receipts
and large generated profile graphs are ignored by Git; regenerate them locally
or collect them as CI artifacts. The pinned baseline graph remains source input.
This establishes the recorded development artifact's tested scope. Release SDK
staging and the unsupported services below are not established by these runs.

The 2026-10-27 image/font crash corrections require a new engine build. The
older schema 3 artifact above does not validate them. Current schema 4 smoke
coverage adds real PNG/JPEG/WebP/data-URL pixels and font alternate-feature
comparisons; only executing the rebuilt harness establishes those assertions.
See [font-resolution.md](font-resolution.md) for the separately recorded source
adaptations and their failure analysis.

The full-history publication audit now verifies 7,292,746 reachable objects
locally, with lazy fetching disabled: zero missing objects and zero blobs above
100 MiB. The largest historical blob is 51,579,983 bytes. The initial partial
clone's 3,140,367 missing historical objects were restored from the official
Chromium and legacy Blink stores. All 370,258 mapped Chromium Blink commits
retain their exact author/committer/date/encoding/message and projected tree;
the original legacy Blink ref retains its 182,131 commits. The fixed refs and
object-size evidence are recorded in
`standalone/upstream/history/archive-verification.json` and bound by the history
lock. Rerun `history_archive.py audit` before publishing changed refs. This
source archive check does not validate a release SDK or GitHub push pack sizing.

## Upstream maintenance automation

`.github/workflows/upstream.yml` checks the configured official Linux release
channel daily at 07:23 UTC (16:23 Korea time), and on manual dispatch. The
default tracking channel is Stable; the current engine pin is a development
version and may be ahead of that channel. An older candidate is reported without
downgrading the engine. Equal-version/different-revision candidates require
manual investigation. Discovery resolves the official Chromium tag to a full
commit and verifies its original `chrome/VERSION` bytes.

After publishing the repository with default branch `main`, set the Actions
repository variable `BLINK_UPSTREAM_PROPOSALS=true` and allow GitHub Actions to
create pull requests. Eligible newer versions then produce one draft PR per
version/revision; existing open or closed proposals are not duplicated. Leave
the variable unset for read-only discovery and job summaries. The proposal job
has only contents/pull-request write permissions, uses a short-lived repository
token, and never force-pushes existing branches.

The draft changes **only** inactive `standalone/config/upstream-update.json`.
It does not import sources, refresh active dependency locks, verify compatibility
or publish an SDK. Complete the source/history import, dependency and adaptation
review, engine-only graph checks, native tests and wave-browser embedding checks
before completing that PR. Remove the inactive manifest as part of the actual
update; release planning refuses to publish while it remains. These are still
engineering tasks, not an implemented unattended upstream importer.

The hosted integrity workflow checks PRs. GitHub may require approval to start
checks on a PR opened by the repository token; see the
[workflow-trigger rules](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).
PR checks do not run the full compiler or prove engine compatibility. Candidate
native builds need an isolated review/build environment. The existing dedicated
self-hosted full-build workflow accepts only trusted `main`, is manually
dispatchable, and runs on main pushes only with `BLINK_ENABLE_FULL_CI=true`.
It requires the `blink-engine` runner and `blink-engine-build` environment.
This maintenance setup has been tested locally. Remote activation still requires
publishing the repository and configuring the repository settings above; local
verification alone does not establish a successful remote CI run.

## Scope

`standalone/include/blink_engine.h` is the C11-compatible embedding header.
`standalone/engine/engine.cc` owns the process initialization and opaque page
handles. `standalone/platform/` implements host task scheduling, runtime resource
access and subresource loading. `standalone/common/limits.h` owns buffer limits.
These modules have no Chromium browser or Content runtime implementation.

ABI 1 commits caller-provided UTF-8 HTML, resolves HTTP(S) subresources through a
caller callback, updates Blink's document lifecycle and paints into caller-owned
premultiplied RGBA8888 memory. Pages are non-ordinary Blink `Page` instances,
created by the production entry point intended for pages without a `WebView`.
`LocalFrame` and `LocalFrameView` provide document parsing, layout and painting.
The platform uses the production `EmptyChromeClient` / `EmptyLocalFrameClient`
client bases with explicit host loaders, local-frame lifecycle clients, a DOM
storage service and dedicated-worker factories.
`CreateURLLoaderForTesting` is the upstream injection method used here; it is a
method in the production client interface and adds no test-support dependency.

The additional ABI 1 entry points expose classic JavaScript evaluation, mouse,
wheel, keyboard and text input, IME, focus, host-backed document navigation,
child frames and DOM storage. Dedicated classic workers use the native DOM API.
These are additive extensions: existing engine, page and subresource response
structures retain their layout. Initialize each new structure with its
`struct_size` and `BE_ABI_VERSION`, and query `be_get_capabilities()` before
selecting a feature. The implemented flags are `BE_CAP_LOCAL_HTML`,
`BE_CAP_SOFTWARE_RGBA`, `BE_CAP_HOST_RESOURCES`, `BE_CAP_TASK_PUMP`,
`BE_CAP_JAVASCRIPT`, `BE_CAP_INPUT`, `BE_CAP_NAVIGATION`, `BE_CAP_IME`,
`BE_CAP_CHILD_FRAMES`, `BE_CAP_DOM_STORAGE` and `BE_CAP_DEDICATED_WORKERS`. A capability flag
describes an implemented API path, not successful native validation of a binary.

This is a software-rendering profile with in-process local child frames,
IME composition, DOM local/session storage and dedicated classic workers.
It supplies no popups, file uploads, cookies, IndexedDB, shared/service/module
workers, GPU/compositor host, accessibility host, audio/video host or browser UI.
It does not provide a browser network service, streaming downloads,
authentication, a network cache or a complete browser security-policy host.
Selected V8, Skia, ICU, base and Mojo libraries remain dependencies; Chromium's
browser and Content runtime are not the engine host. The source and build
adaptations live under `standalone/` and in the staged build view; the imported
Blink implementation and notices remain the pristine upstream baseline.

## Process and thread ownership

The Linux DSO exports only `be_*` C entry points. Its allocator and C++ symbols
remain private, so a host can load it after Python/Qt without replacing the
host's allocator. The host integration retains PartitionAlloc for engine-owned
memory and routes libc-owned buffers (for example from `realpath` inside
Fontconfig) back to GNU libc for freeing, resizing and size queries. This
ownership dispatch is installed through the existing allocator-shim interface
and retained until process exit. The tested profile assumes GNU libc for these
foreign buffers; alternate interposed allocators have not been validated.

Call `be_engine_create` once, on the thread that will own every engine operation.
It initializes ICU, V8 startup data, Mojo core, the Blink platform, an IO thread,
the in-process main-thread scheduler and Blink itself. Blink has no supported
global teardown/restart entry point at this revision. `be_engine_shutdown`
requires all pages to have been destroyed and all worker threads to have
terminated, then retires the handle and shuts down its scheduler. A busy worker
returns `BE_BUSY`; continue pumping and retry. Destroyed pages retain their
parent agent-group scheduler until termination acknowledgments drain. Storage
IO shutdown waits for queued persistence work before callbacks are disabled. It retains the process-global infrastructure until process exit.
Reinitialization and unloading `libblink_embed.so` are unsupported. An existing
Blink platform or global resource bundle causes initialization to be refused;
this first profile expects to own those services in its process.

Before Blink or its workers can decode images, initialization installs a
process-lived discardable-memory provider using the production Skia memory
pool. An existing provider is preserved. Pool access and residency accounting
are serialized; locked blocks retain their bytes, and unlocked entries may be
evicted and recreated by the decoder. The 128 MiB budget bounds retained cache
memory, not all image memory: live locked allocations can exceed it. The provider
also supports explicit free-memory release; automatic OS pressure notifications
are not connected in this profile. Its isolated production base/Skia native
contracts cover locking, eviction, budgets, lifetime, dumps, concurrent access
and the actual Skia allocation bridge. These contracts do not render a page.

All handle operations enforce the owning thread. Calls from within callbacks
must be deferred until the outer call returns; reentry is rejected as busy.
Callbacks must not throw C++ exceptions. Host `user_data` must remain valid until
shutdown returns. Shutdown closes the wake-up gate and waits for already admitted
wake-up callbacks, so background threads cannot invoke that callback afterward.
Initialization failure after process infrastructure becomes live also prevents
another initialization attempt in the same process. Missing input paths are
checked before that irreversible stage.
Nonempty memory mapping and pak structure are also checked first. A V8 snapshot
that is corrupt or belongs to a different binary may trigger V8's process-fatal
integrity checks; the C error return cannot recover from that condition. Use
the snapshot from the same build and verify the package's file hashes before
loading it. The runtime data files must not change while initialization runs.

`wake_up(user_data, delay_ms)` can run on any thread. It must queue a wake-up in
the application's event loop, rather than enter the engine immediately. On the
owning thread call `be_engine_pump(engine, budget_ms)`, where the budget is
1..1000 milliseconds. The custom pump executes queued/due work without waiting
for future timers, reports the next delay to the host and yields at its budget.
A single Blink task can take longer than the budget; it cannot be preempted.
In particular, synchronous host fetch callbacks and JavaScript can block that
thread beyond the pump budget. There is no JavaScript execution timeout.

## Resource ownership and policy

The configuration requires absolute paths to:

- `icudtl.dat` from the same pinned build.
- `v8_context_snapshot.bin` when `USE_V8_CONTEXT_SNAPSHOT` is enabled, otherwise
  `snapshot_blob.bin` from that build. The recorded Linux x64 profile enables
  the Blink context snapshot; do not substitute an unrelated V8 snapshot.
- `gen/third_party/blink/public/resources/blink_resources.pak`.

`blink_scaled_resources_100_percent.pak` is optional for image resources used by
some controls. It is produced below the same generated resources directory.
The host platform explicitly loads only these packs and serves decompressed
resource strings/bytes, including Blink's built-in CSS. It does not infer a
browser resource bundle or browser locale files.

`be_page_load_html` supplies HTML as bytes; a base URL establishes origin and
relative URL resolution without opening that URL. Allowed base URLs are
`about:blank` or absolute HTTP(S) URLs without credentials. External GET
subresources are denied unless `fetch_resource` returns `BE_OK`. The engine has no fallback
network service or file URL loader. Requests using another method, a non-HTTP(S)
scheme, credentials or more than 64 MiB of response bytes are denied. The host
must choose the URL policy and may return in-memory CSS/images/fonts/scripts.
This engine-wide **subresource** callback supports complete responses with MIME
type and charset. It has no redirects, streaming, request/response header
interface or authentication negotiation. The separate main-document navigation
callback below carries headers and redirects; it does not extend subresource
transport. Browser-grade network/CORS/cookie integration remains future work.
The Linux code path uses direct Fontconfig/Skia system-font fallback without a
browser broker. System-font discovery and text rendering were exercised in the
recorded browser integration run. CSS `@font-face` `src:local()` unique-name lookup requires a separate
font host integration and is unavailable in this initial profile;
callback-served font URLs are the explicit resource path.

On a successful fetch callback, keep response bytes, MIME type and charset
valid until `response.release` is called. The loader copies them before that
call and invokes release exactly once even when the response is rejected as
invalid. A NULL release function means those pointers already have sufficient
lifetime. On an error return the host retains responsibility for its response
storage. Destroy every page before engine shutdown. Keep each page's navigation
client `user_data` valid until that client is replaced or the page is destroyed.
The asynchronous host loaders copy these bounded complete bodies into Mojo data
pipes, including an EOF-bearing empty pipe for an empty response. This supplies
Blink's normal Fetch body consumers; it does not add streaming to the host
callback's network contract. Script responses also expose the supplied MIME
type as an HTTP `Content-Type` header for strict worker script checks.

## JavaScript

JavaScript is enabled in page settings. Inline classic scripts and external
classic scripts loaded through the host subresource callback use Blink's normal
document loader and script machinery. They run in the current document's main
world. Module-script loading remains outside this profile; dedicated classic worker
loading is described below.

`be_page_evaluate_script` runs a trusted host-supplied classic script in that
same world and origin. `source_url_utf8` supplies a diagnostic source URL; it
does not fetch that URL or change the document's origin. The source must be
valid UTF-8 and the diagnostic URL must be HTTP(S) or `about:blank`; a NULL or
empty source URL selects the document URL. Source and copied result bytes are
bounded at 64 MiB.

Host evaluation honors Blink's script-enabled and sandbox restrictions, but
intentionally bypasses the document's `script-src` check. This is a privileged
embedding/debugging entry point, not a network-script loader or a boundary for
untrusted callers. Document inline/external scripts still take Blink's normal
loader CSP checks. Main-document response CSP is described below; this does
not claim complete browser CSP reporting or response-policy integration.

`be_script_result.utf8_data` contains JSON for serializable primitives,
objects and arrays, the literal `undefined`, or a diagnostic error string.
`utf8_size` is the byte count. The pointer is borrowed until that page's next
evaluation, document commit or destruction; copy it if it must outlive that
point, and never free it. An exception returns `BE_SCRIPT_EXCEPTION` with
`is_exception == 1`; execution failures return `BE_SCRIPT_ERROR`. JSON
serialization can invoke getters/`toJSON` and can itself throw. Top-level
function, symbol and Promise results return `BE_NOT_SUPPORTED`; there is no
Promise-await API. A Promise may still be used within a script whose returned
value is representable, and Blink's normal microtask processing remains active.

Evaluation, result serialization and a single queued task have no execution
deadline or interrupt API. An infinite loop can block the owning thread, and
the pump budget does not provide a timeout or isolate recovery mechanism.

## Input and focus

`be_page_dispatch_input` updates layout and passes validated events to Blink's
production event handlers. It does not synthesize DOM events as a substitute
for input. `BE_OK` means the event was accepted; `handled_out` indicates Blink
consumed it or default scrolling ran. Mouse leave is delivered without a
handled result. Event handlers can run JavaScript and queue navigation, so pump
after input when those effects need another turn.

Supported events are mouse move/down/up/leave, precise-pixel wheel scrolling,
key down/up and committed UTF-8 text. The changed button is one of left,
middle, right, back and forward; `buttons` is the complete state **after** the
event. Down/up require a click count of 1..3. Coordinates are viewport CSS
pixels; positive wheel deltas scroll right/down. Values must be finite and
have absolute value at most 1,048,576. Wheel dispatch runs the DOM wheel handler
first and only scrolls when it was not consumed, so `preventDefault()` is not
followed by an extra scroll. Nested scrolling follows Blink's propagation and
overscroll rules. Ctrl/Meta-wheel returns `BE_NOT_SUPPORTED`, since page zoom
needs a separate host integration.

The supported modifier flags are `BE_MOD_SHIFT`, `BE_MOD_CTRL`, `BE_MOD_ALT`,
`BE_MOD_META`, `BE_MOD_CAPS_LOCK`, `BE_MOD_NUM_LOCK`, `BE_MOD_AUTO_REPEAT` and
`BE_MOD_KEY_PAD`. Auto-repeat and keypad flags apply only to keyboard/text
events. `dom_code` and `dom_key` use `KeyboardEvent.code`/`key` names; the bounded
arrays must be NUL-terminated, and text must be valid UTF-8. Key down/up uses
`windows_key_code` (0..255), optional native key code and DOM code/key values;
it does not insert the `text_utf8` field. Send insertion separately as
`BE_INPUT_TEXT`, with at most 255 UTF-8 bytes. That call commits text, without
composition or an IME session. Touch, pen, drag-and-drop, wheel momentum/phase
tracking and browser context-menu UI are unavailable.

`be_page_set_focus(page, 1)` activates/focuses the page; pass 0 to blur it.
Keyboard and text dispatch require page focus. This API provides engine focus
state; the embedder still owns window-system focus and its event loop.

## IME composition

The additive `blink_ime.h` header is included by `blink_engine.h`.
`be_page_ime_set_composition` dispatches through the focused frame's real
`InputMethodController`. It accepts UTF-8 text, ordered nonoverlapping underline
spans and selection offsets, all measured in UTF-16 code units. Surrogate-pair
splits, invalid UTF-8, text over 1 MiB, more than 1024 spans and invalid ABI/enum
values are rejected. Empty composition cancels an active composition without
removing an ordinary selected range.

`be_page_ime_commit_text`, `be_page_ime_finish_composition` and
`be_page_ime_cancel_composition` implement the corresponding native edit paths.
The commit caret offset is relative to the end of inserted text, must be <= 0
and remain at a scalar boundary within that text. `be_page_ime_get_state` copies
surrounding text into a host buffer and reports UTF-16 selection/composition
ranges, input type and caret/control bounds in root viewport CSS pixels. Query
with NULL/0 to obtain the required size including the trailing NUL; the return
is `BE_BUFFER_TOO_SMALL`, with state populated. Password text is sensitive host
data. Focused child frames receive keyboard/text/IME operations too.

Readonly/disabled or unfocused edits fail. Active `EditContext` integration is
explicitly unsupported. OS IME sessions, candidate windows and screen-coordinate
placement belong to the embedder; this engine supplies document composition and
viewport geometry, with no browser or window-system UI.

## Child frames

The frame client constructs real `LocalFrame` children, including nested
iframes, and maintains traced per-frame navigation state. Child commits use the
same host document transport as the main frame. `srcdoc` is snapshotted at
navigation submission and inherits the actual parent's origin and policy;
about:blank uses its initiator policy. Initial/committed sandbox restrictions,
opaque-origin derivation, owner permissions/container policy, frame names,
sibling insertion and removal are passed to Blink's native lifecycle.
Response X-Frame-Options and enforced CSP frame-ancestors are checked against
every ancestor with production parsers. Violation reporting is not supplied.

The page software paint path includes child views; DOM same-origin access and
postMessage use Blink's implementations. At most 256 simultaneously retained
frame states, including the main frame, are admitted. Detached states are removed
on a pump turn. Frames are in one engine process; no site-isolation process host
or remote frames are provided. Per-frame session history is tracked, while a
browser-wide joint history tree, restoration of child history during a main-frame
history traversal, credentialless frames, iframe embedded-CSP/connection-allowlist
requirements and full browser response policy integration remain unsupported.
Credentialless and embedded-CSP/connection-allowlist requirements are rejected
at frame creation/navigation rather than treated as ordinary frame loads.
The C navigation state/events describe the main frame; child lifecycle can be
observed through the document DOM. Host policy applies to child fetches too.

## DOM storage

`blink_storage.h` supplies additive storage configuration without changing old
engine/page struct layouts. Before the first page, optionally call
`be_engine_set_storage_client`; after any page has existed it returns `BE_BUSY`.
The default is in-memory DOM storage; `BE_STORAGE_DISABLED` denies it.
`BE_STORAGE_HOST_PERSISTENT` requires paired load/save callbacks for localStorage.
The engine never chooses a filesystem directory or opens a storage database.

Blink's actual `StorageController`, `StorageNamespace` and `CachedStorageArea`
connect to an in-process Mojo service on the engine IO thread. Local areas use
the complete canonical `StorageKey`; child commits explicitly retain third-party
partitioning with the top-level site and ancestor-chain bit. Session areas also
use a unique page namespace, survive same-page navigation and disappear when that
page is destroyed. Opaque origins are denied by the native DOM access checks.
Matching documents share local storage and receive native storage events.

The backend enforces a 10 MiB UTF-16 area quota. Host snapshots preserve strings,
NULs and lone surrogates, have a versioned native-byte-order format and are bounded
to 64 MiB. Load/save/error callbacks run on the storage IO thread; their user data
must remain valid through successful shutdown. Save atomically replaces the
whole opaque key snapshot. Hosts own durability, encryption and migrations.
Nonce-bearing keys remain ephemeral even in persistent mode. No cookie,
IndexedDB, CacheStorage, storage permission or general filesystem service is
implemented by this DOM storage host.

Writes are asynchronous to Blink's cache. `be_engine_flush_storage` drains the
actual storage pipes; document lifecycle observers also flush final unload writes
before the window disconnects. This can block on host callbacks and has no
timeout. Backend errors are delivered to the error callback and retained by
`be_engine_get_storage_status`. They cannot be returned as synchronous DOM
exceptions by this Mojo interface. Failed loads leave areas unavailable; failed
mutations roll back/reset renderer caches through native observer notifications.
A page can still be destroyed after a persistence error; inspect the sticky
status when deciding whether host data was saved successfully.

## Dedicated workers

Document-created, classic, same-origin HTTP(S) `Worker` instances use Blink's real
`DedicatedWorker`, `WorkerThread`, event loop, structured cloning, postMessage
and terminate machinery. A standalone factory supplies valid scoped host/broker
endpoints and a `WebWorkerFetchContext`. Native script loading, synchronous
`importScripts` and asynchronous same-origin GET fetch use the existing host
resource callback. Every resource/release callback is marshalled to the engine
owner thread; only copied plain response data crosses back to the worker heap.
The host event loop must keep pumping while worker requests are pending.

Module/nested/shared/service workers, blob/data worker URLs, cross-origin worker
requests, COEP workers, service-worker responses and other worker browser
services are unavailable. Unsupported dedicated starts take Blink's script-load
failure/error-event path. Termination cancels pending transport waits; a running
host callback is allowed to finish and its cancelled response is discarded.

One read-only `DedicatedWorker` script-type accessor is the only rendering-source
adaptation for this integration. Its exact original/adapted hashes and diff are
recorded in `config/engine-adaptations.json` and
`patches/engine-build-imports.patch`. It is applied only in the private build
view; root upstream bytes and the pristine lock remain unchanged. This permits
an explicit classic/module decision without changing web-platform behavior.

## Document navigation

Install a `be_navigation_client` with `be_page_set_navigation_client`, then use
`be_page_navigate`, `be_page_reload`, `be_page_go_back_forward` and
`be_page_stop`. Main-frame links, forms, script location changes and supported
same-document navigation use the same path. Navigation URLs are credential-free
HTTP(S) or exactly `about:blank`. Other schemes and non-current-tab targets are
rejected rather than launching another window or a browser service.

`be_page_navigate` returning `BE_OK` means **submitted**, not proof of a finished
load. Pump on the owning thread to process queued navigation. The `fetch`
callback runs synchronously in that pump and returns the entire main-document
response; neither the transport nor parsing starts an asynchronous download service.
Requests include method, CRLF-delimited header lines and any in-memory body.
Transport URLs omit fragments; the committed document URL retains them.
GET and ordinary form POST are supported. File/blob/stream form parts fail
explicitly. Bodies are limited to 64 MiB and header blocks to 64 KiB. Request
and event pointers are borrowed only during their callbacks.

Successful responses include HTTP status, MIME type, charset, body bytes and
header lines. Keep them valid until `release`; it is called exactly once after
`BE_OK`, including when a response is invalid. On a callback error the host
owns its response storage. If `fetch` is NULL, only GET can fall back to the
engine's simpler subresource callback, which supplies no status/header metadata.
`about:blank` performs no host fetch. HTTP 204/205 finish without replacing the
document; 304 is rejected because this profile has no cache revalidation host.

301/302/303/307/308 require `redirect_url_utf8`, resolved against the request
URL. A chain is limited to 20 transport attempts. A 303 changes a non-HEAD
method to GET; a 301/302 changes POST to GET. Changing to GET drops the body and
body-related headers; crossing origins drops authorization/cookie headers.
Redirect response referrer policy is parsed before the next request, and
Blink's referrer generator updates both transport headers and commit metadata.
This limited redirect handling does not implement authentication, cookie jars
or a full browser redirect/security-policy stack.

Main-document CSP and CSP report-only headers are parsed with Blink's production
parser into the new document policy container; enforced CSP sandbox flags are
applied before origin creation. `Referrer-Policy` is parsed with Blink's
production parser. A new `about:blank` document inherits its initiator's complete
current policy container. Other response headers are passed to Blink's static response, but
that alone is not evidence that every browser response policy is implemented.
Responses carrying Permissions-Policy, Document-Policy, COOP/COEP,
Document-Isolation-Policy, Integrity-Policy or Origin-Agent-Cluster headers are
rejected with `BE_NOT_SUPPORTED`. The COOP/COEP, Document-Isolation-Policy and
Integrity-Policy checks also reject their report-only header families.
Those policies require missing host behavior.
There is no browser policy store, CSP reporting host,
cross-process isolation or claim of general browser security equivalence.

The session history retains at most 64 entries, using real Blink
`WebHistoryItem` values. New commits remove forward entries; the oldest entry
is evicted at the limit and retired from the Navigation API during pumping.
JavaScript's Navigation API receives only contiguous same-origin entries;
the host session-history API may traverse across origin boundaries.
Same-document back/forward traversal preserves the
live document, JavaScript realm and history state rather than refetching HTML.
Cross-document traversal and reload use the host transport again for fetched
documents. Entries created by `be_page_load_html` retain their supplied bytes
for that traversal/reload instead. This is not persistent browser history or a
back/forward cache. A failed fetch leaves the previous document and committed
history available.

`be_page_get_navigation_state` reports pending/loading state, navigation ID,
last status, history length/index and current URL. Its URL pointer is borrowed
until the next operation on the page. The event callback reports queued,
started, redirected, committed, finished, failed, cancelled and same-document
events. Do not reenter the API from either navigation callback; defer work to
the host event loop. A new submission replaces a pending one. Stop cancels
queued navigation and active Blink loaders, but cannot interrupt a synchronous
host fetch already running on the same thread. Replace a navigation client only
when the page is neither pending nor loading.

## Minimal call sequence

1. Initialize a zeroed `be_engine_config`, set `struct_size`, `abi_version`,
   paths and callbacks, then call `be_engine_create`.
2. Optionally configure storage before any page, then create a page with `be_page_create` and a valid `be_page_config` (dimensions
   1..16384 per axis).
3. Call `be_page_load_html`, then pump queued work. Completion of the commit
   function means that Blink accepted the navigation; it does not mean every
   subresource has loaded.
4. Call `be_page_layout` or `be_page_paint_rgba`. Painting also updates the
   lifecycle. Allocate at least `stride * height` bytes, where `stride` is at
   least `width * 4`. Padding is caller-owned; rows contain RGBA bytes with
   premultiplied alpha and start at the top left.
5. Destroy pages, continue pumping while worker termination drains, then retry
   shutdown until `BE_OK` (`BE_BUSY` indicates live worker threads). Never use
   a retired page handle again.

The local-document sequence does not require a navigation transport. To load a
URL, install the navigation client and queue a navigation instead of supplying
HTML; observe state/events and continue pumping until completion. JavaScript
and input calls operate on the current document. Native smoke evidence is
separately required to establish that a particular binary works.

## Native validation after the user builds

Validate an existing development build without running GN, Ninja, downloading
tools or staging a release SDK:

```sh
python3 -B standalone/tools/engine.py runtime-check --profile dev
```

This checks source/configuration/artifact fingerprints before and after the
real native harness, validates every schema 4 assertion, and writes a separate
development check receipt. `--no-cache` selects the corresponding isolated
development build. `runtime-check --profile release` similarly checks an existing
release build without staging. Neither command replaces release smoke receipts.
The earlier schema 3 development check was executed successfully on 2026-10-27;
the image/font source changes invalidate that build's receipt. Rebuild before
running the current check.

The SDK commands below select the release profile; browser integration uses
`--profile dev` in the separate Wave Browser repository.

The GN targets are `//standalone/engine:blink_embed` (shared library,
`libblink_embed.so`) and `//standalone/engine:blink_embed_smoke` (a small engine
validation executable). The smoke executable must be built explicitly if the
engine build command only selected the library aggregate. The supported wrapper
reads the generated V8 build flag and snapshot filename, checks the build's
source/artifact hashes, runs the native harness, and stages the SDK after its
assertions pass:

```sh
python3 -B standalone/tools/engine.py build --with-smoke --jobs 4
python3 -B standalone/tools/engine.py runtime-smoke
```

The optional `//standalone/engine:blink_discardable_memory_test` executable
exercises the real base/Skia provider without initializing Blink. It can be
selected directly with Ninja in the generated engine output directory and is
not included in the default engine aggregate or substituted for native smoke.

For direct diagnosis, the equivalent native arguments for the recorded
context-snapshot profile are:

```sh
/absolute/build/output/blink_embed_smoke \
  --icu-data=/absolute/build/output/icudtl.dat \
  --v8-snapshot=/absolute/build/output/v8_context_snapshot.bin \
  --blink-resources=/absolute/build/output/gen/third_party/blink/public/resources/blink_resources.pak
```

The schema 4 smoke tool requires actual native assertions for:

- Initialization, host stylesheet/script loading, exact rendered pixels,
  resizing, wrong-thread rejection and shutdown refusing an open page.
- Inline/external classic JavaScript, JSON/undefined/exception results,
  microtasks and a JavaScript change reflected in painted pixels.
- Focus, mouse clicks, key events, UTF-8 text insertion, wheel cancellation
  through `preventDefault()` and real default scrolling.
- Queued main-document GET, redirect, reload, back/forward, failed and stopped
  navigation, links, form POST, same-document history state without refetching,
  response CSP blocking document inline script and same-document Navigation
  API completion, cross-origin history visibility, bounded history eviction
  and CSP sandbox opaque-origin creation.
- Real srcdoc/nested/sandbox/cross-origin children, child pixels, postMessage,
  sibling removal, XFO and frame-ancestors rejection.
- Child-frame IME composition, UTF-16/UTF-8 surrounding text, root caret bounds,
  Unicode commit, cancellation and finish.
- Actual DOM local/session areas, host IO persistence callbacks, same-origin
  sharing, page-session separation, origin/port/third-party isolation, storage
  events, flush and failed-save cache rollback/sticky errors.
- Real dedicated worker script/importScripts/fetch, structured messages, module
  and failed-script error events, immediate terminate and page destruction with
  a live worker, including shutdown waiting for real worker termination.
- Host-loaded PNG/JPEG/WebP and a data-URL image, exact decoded pixels, resize
  and a second page's destruction without losing the original page's images.
- Generic, named, missing and unavailable web fonts with alternate CSS retained;
  an authored test font proves `styleset()` alias pixels equal direct `ss02`,
  differ from the plain glyph, preserve `hist` and explicit feature precedence,
  and still paint missing-glyph fallback.

It emits a successful receipt only after those assertions pass, opens no
windows and supplies no browser application. `runtime-smoke` executes only the
already-built, hash-verified harness from a successful full build receipt; it
never compiles as a fallback. It rejects stale source/artifact hashes and the
old schema 1/2/3 receipts, and stages the SDK only after the current native receipt
passes. The staged SDK carries the header, library, runtime data, original
notices, pinned locks and this scope document. Merely constructing a fixture
receipt in a Python test is not evidence that these native assertions passed.

The Python suite compiles the public header as C11 and C++20 and executes the
dimension/buffer-overflow, input/IME validation, navigation, storage quota/format
and actual cross-thread worker-ticket cancellation contracts.
Those small contract programs do not link Blink. They must never be reported
as native engine compilation or runtime validation of these features.

## Upstream integration references

The API calls were checked against pinned Chromium revision
`443fc1cc6746166ba1d0deba6bee26bbd8f7f84c`, notably
`third_party/blink/public/web/blink.h`,
`third_party/blink/public/platform/scheduler/web_thread_scheduler.h`,
`third_party/blink/renderer/core/page/page.h`,
`third_party/blink/renderer/core/frame/local_frame.h`,
`third_party/blink/renderer/core/frame/local_frame_view.h`, and the production
`Empty*Client` interfaces. Frame setup follows the lifetime/initialization
ordering demonstrated by upstream's `DummyPageHolder`, without linking that
test-only helper. The upstream notices for that reference are retained in
`standalone/engine/NOTICE`.
