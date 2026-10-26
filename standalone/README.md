# Standalone Blink integration

This directory holds this project's integration code, build and source tools,
configuration, verification tests and provenance. Blink's own source directories
remain at the repository root. GitHub CI configuration belongs in `.github/`.

The complete pinned Blink tree (213,035 entries) is available at the repository
root and verified against the original Git tree. The embedding runtime and C API
implement initialization, local HTML, host subresources, task pumping, software
RGBA painting, classic JavaScript, input/IME, child frames, DOM local/session
storage, dedicated classic workers and host-backed document navigation. The ABI 1 extensions preserve the existing configuration layouts.
On 2026-10-26 the existing Linux x64 development artifact passed the real schema 3
native harness for rendering, JavaScript, input/IME, frames, storage, workers,
navigation and shutdown. The separate wave-browser application also passed
native input, DOM and pixel integration checks with this library. These checks
cover that development artifact and the implemented host services; they do not
establish a release SDK or the unsupported services listed below. Generated
receipts are local build artifacts. Source preservation, tool tests and GN
checks alone do not establish runtime behavior.

The pinned Chromium/Blink and legacy Blink histories now have a complete local
object archive, with original metadata and commit-ID mapping preserved. See
[history-archive.md](history-archive.md) for the fixed-ref proof, historical file
size checks and the separate GitHub transfer-size constraint.

## Project boundary

This repository extracts and packages the Blink engine. Browser application code
and UI belong to [wave-browser](https://github.com/wavefnd-labs/wave-browser),
which will use this extracted Blink as its rendering engine. The user's license
choice for the browser application's own code is MPL-2.0; original Blink and
dependency license notices remain unchanged here. Engine test harnesses serve
only to validate the extracted engine and embedding API.

## Layout

Existing support:

- `tools/`: source integrity, dependency audit and Git history extraction tools.
- `config/`: pinned upstream revision, original file inventory and boundary policy.
- `tests/`: tests for these tools.
- `upstream/`: original documents, licenses, extraction report and commit mapping.
- `reports/`: generated analysis reports.
- `work/`, `out/`: ignored local caches, candidate snapshots and build products.

Initial runtime integration:

- `common/`: shared limits and input/navigation contracts, separate from upstream
  root `common/`.
- `platform/`: host task scheduling, runtime data and subresource loading.
- `engine/`: initialization, pages, JavaScript/input integration, main-document
  navigation and the native validation harness.
- `include/`: versioned C embedding API for host applications.

See [runtime.md](runtime.md) for API ownership, supported operations and the
host services still needed for a complete browser engine. The current profile
uses synchronous, fully buffered host transport and software pixels. It has no
browser UI, popups, file upload, cookies, IndexedDB, shared/service/module workers
or media host. DOM storage defaults to memory, with optional host localStorage
persistence. Worker transport retains the owner-thread host callback contract. Host script evaluation is privileged, returns JSON or a
borrowed diagnostic string and has no execution timeout. The document loader
has a limited CSP/referrer-policy integration; this is not a complete browser
response-policy or network service.

Prefer Blink's existing platform and client injection APIs. Record necessary
upstream integration changes separately from the pristine source baseline.

## Tool validation

Run from the repository root:

```sh
python3 -B standalone/tools/blink.py verify
python3 -B -m unittest discover -s standalone/tests -v
python3 -B standalone/tools/engine.py verify
python3 -B standalone/tools/blink.py audit
```

The lexical audit covers the file sample listed in the upstream lock; it is not
a resolved GN dependency graph. The original evaluated Linux x64 baseline covers
7,398 targets and locks 95 external dependency entries. The current engine graph
declares 6,855 targets; 6,854 are reachable from the production engine root. The
remaining target is a small embedding validation harness. See [graph.md](graph.md)
for the baseline and [engine.md](engine.md) for the current build entry, reviewed
runtime additions and browser boundary checks.
`python3 -B standalone/tools/materialize.py` restores the pinned source and
tool/resource inputs without compilation. `engine.py gen` stages the build view
and regenerates its GN reports; `engine.py verify` verifies that staged view.
`engine.py build --with-smoke --dry-run` regenerates the declared-source and
command reports without compiling. Consult the new `standalone/reports/`
reports after an integration change; earlier dry-run counts are not evidence
for a changed source tree. A dry-run cannot establish compiler-implicit input
closure or native behavior. For a fresh release-profile build and SDK validation:

```sh
python3 -B standalone/tools/engine.py build --with-smoke --jobs 4
python3 -B standalone/tools/engine.py runtime-smoke
```

`--jobs` is optional. The smoke tool must execute successfully before native
runtime behavior is considered validated. The schema 3 harness requires real
pixel, JavaScript, input/IME, frame, storage and worker assertions. `runtime-smoke` runs an
already-built, hash-verified harness and stages an SDK only after those checks
pass; it does not compile or accept a fixture receipt as native evidence.
The source-view rustfmt configuration
is now an unchanged regular-file copy, fixing the reported generated-bindings
formatter panic without disabling formatting. CI, upstream tracking, package
versioning and release readiness are described in [automation.md](automation.md).

To recheck an existing development build without compiling or staging a release
SDK, run `python3 -B standalone/tools/engine.py runtime-check --profile dev`.
The command rejects stale source/configuration or artifact hashes and executes
the native harness; it does not reuse a previously successful receipt.

## Preserved history

`config/history.lock.json` records the official repositories and exact revisions.
`upstream/history/blink-commit-map.tsv.gz` maps original Chromium commits to
extracted commits and their projected source trees. The extraction preserves
370,258 relevant commits and verifies authors, committers, dates, encoding,
messages and source trees. During the staged WebKit-to-Blink directory migration,
24,518 commits preserve the old tree under `.upstream-WebKit/` alongside the new
Blink tree; the current source revision has no such extra directory.

The earlier standalone Blink history is preserved separately with its original
182,131 commit IDs. Historical file blobs can be fetched on demand from the
original remotes; these partial-clone archives are not offline complete copies.

Root upstream README and license files remain verbatim. Additional project notes
under root `docs/` are local and excluded from Git.
