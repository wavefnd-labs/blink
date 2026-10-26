# Evaluated Blink dependency graph

The pinned Linux x64 release profile has an evaluated upstream baseline and a
separate engine-owned GN graph. An initial embedding runtime and C API are now
implemented outside the pristine source tree. The existing Linux x64 development
artifact passed the schema 3 native harness and a separate wave-browser embedding
check on 2026-10-26. These runtime checks are separate from the graph evidence
below; they do not establish a release SDK or unsupported host services.

## Preserved upstream baseline

- Chromium revision: `443fc1cc6746166ba1d0deba6bee26bbd8f7f84c`.
- GN: `2598 (f90c3d9df882)`, taken from that revision's DEPS.
- Root target: `//third_party/blink/public:blink`.
- Arguments: [`config/linux-x64-graph.args.gn`](config/linux-x64-graph.args.gn).
- 7,398 reachable targets; no forbidden browser targets or test-only targets.
- 95 external dependency entries referenced by targets, typed file/include paths,
  evaluation inputs or explicit build infrastructure: 83 Git, 7 GCS, 5 CIPD.
- All 275 root DEPS Git pins match the original Chromium Gitlinks.

The counts describe this Linux profile, including host generators and resources.
They are not a universal minimum for every Blink configuration. Files and include
paths may name a repository even when its targets are declared in a Chromium
wrapper, as happens with Skia; counting target labels alone misses dependencies.

The source graph has no `chrome`, `content`, `headless`, `android_webview` or
`ios/chrome` target dependencies under the boundary policy. Two browser-layer
build inputs remain: `chrome/common/features.gni` and
`content/public/common/features.gni`. The engine-owned build view now replaces these imports with explicit engine
configuration; see [engine.md](engine.md). This original baseline retains them
for comparison.
This original capture did not compile or link an engine or browser binary.

## Current engine graph

The engine entry declares 6,855 targets. Its production root `//:blink_engine`
reaches 6,854; `//standalone/engine:blink_embed_smoke` is a separately declared
validation harness. The added project targets provide the root, embedding
shared library and harness. Three additional original upstream targets generate
the matching Blink/V8 context snapshot; their exact fingerprints and source
build-input hash are recorded in
[`config/runtime-graph-additions.json`](config/runtime-graph-additions.json).

The original 7,398-target baseline is retained for comparison. The engine graph
has 549 individually fingerprinted unreachable baseline targets, including WebUI
and further browser support removals. Shared targets retain exact comparisons
after source/output path normalization; reviewed source/dependency cuts and
transitive configuration changes are recorded separately in the adaptations.
The engine view excludes browser targets, browser source-root directories,
browser GN regeneration inputs and typed browser source/action paths. It uses
the engine-owned root GN files and stages reviewed adaptations without changing
the original Blink tree. Its unchanged rustfmt configuration is a regular-file
copy so generated bindings remain inside the formatter's configuration root.
See [engine.md](engine.md) for the layout fix, source checks and build commands.

These graph checks establish dependency boundaries and reviewed input changes.
They do not establish C++ compilation, linking, initialization or rendering.
The current engine-plus-harness graph finds all 43,454 declared source leaves
present. Remaining Ninja commands depend on profile and local build state. This
latest inventory does not repeat
the archived pre-embedding 44,820-input Git ownership classification and does
not establish compiler-implicit input closure.

## Recorded artifacts

- [`config/dependencies.lock.json`](config/dependencies.lock.json): selected
  repositories, commits, package conditions, GCS generations/checksums and CIPD
  immutable content references. The Chromium root DEPS is authoritative for this
  integrated profile; child DEPS must not be independently synced with different
  defaults.
- [`reports/linux-x64-graph-summary.json`](reports/linux-x64-graph-summary.json):
  counts, runtime resource paths and remaining browser build inputs.
- [`reports/linux-x64-boundary.json`](reports/linux-x64-boundary.json): transitive
  target boundary result, including a failure path if a violation is found.
- [`reports/linux-x64-blink-graph.json.gz`](reports/linux-x64-blink-graph.json.gz):
  complete unmodified `gn desc` JSON, compressed; its hashes are in the lock.
- [`reports/linux-x64-gn-inputs.json`](reports/linux-x64-gn-inputs.json): GN's Ninja
  regeneration dependencies, hashes, original Git blob checks, generated inputs
  and directory/existence dependencies.
- [`reports/linux-x64-graph-files.json`](reports/linux-x64-graph-files.json): typed
  file/include paths and generated paths from the graph. Include paths are
  directories, so this is not an inventory of all C++ headers actually consumed.
- [`reports/linux-x64-evaluated-args.json`](reports/linux-x64-evaluated-args.json):
  evaluated GN arguments, including defaults.
- [`reports/chromium-declared-dependencies.json`](reports/chromium-declared-dependencies.json):
  all 435 root DEPS declarations, with conditions retained.
- `upstream/chromium/`: original root DEPS, GN dotfile and relevant child DEPS.

## Reproduce

From the repository root on Linux x64, with Git and Python 3.11 or newer:

```sh
python3 -B standalone/tools/graph/prepare.py
python3 -B standalone/tools/graph/evaluate.py
python3 -B standalone/tools/graph/capture.py
python3 -B standalone/tools/graph/resolve_packages.py
python3 -B standalone/tools/blink.py verify
python3 -B -m unittest discover -s standalone/tests -v
```

Network access and several GiB of local space are needed. All temporary sources,
packages and outputs live under ignored `standalone/work/graph/`. For evaluating
unchanged upstream GN paths only, that workspace uses the original Chromium
layout and a Blink source link; the repository's actual Blink sources stay at
its root. Preparation refuses to proceed if pristine Blink verification fails.

The preparer reuses the existing Chromium object cache when available; otherwise
it fetches the exact commit into a separate partial cache. It restores original
GN/Python/metadata inputs, generates gclient arguments with the pinned official
condition evaluator, validates selected compiler/sysroot archives and explicitly
runs the original `lastchange.py` generator. It does not run the general DEPS hook
list. Missing GN inputs are fetched from their original pinned repositories;
unrecognized generated inputs stop evaluation instead of being replaced by stubs.

`--root-target` and `--root-pattern` restrict evaluation to Blink's closure. The
same switches and Python override must also be passed to `gn desc` and `gn args`;
these commands re-evaluate the graph. The capture tool does this automatically.
The relevant switches and outputs are described in the
[official GN reference](https://gn.googlesource.com/gn/+/main/docs/reference.md).
CIPD resolution uses the official
[ResolveVersion RPC](https://chromium.googlesource.com/infra/luci/luci-go/+/main/cipd/api/cipd/v1/repo.proto).

## Limits and validation

The preserved baseline is an upstream-layout investigation graph. At its capture
time non-Blink source bodies were only partially materialized.
The engine input preparation in [engine.md](engine.md) now restores those source
trees, reviewed nested inputs and tool/resource packages with separate evidence.
The host Python version is recorded instead of using the upstream hermetic Python package.
A successful graph does not validate C++ includes, action execution, linking,
service implementations or rendering. The baseline runtime dependency list names
expected paths; recording those paths does not prove the resources were built.

An engine-only entry point and the two feature-configuration extractions are now
implemented and validated in [engine.md](engine.md), including removal of direct
browser version/branding file inputs from two library actions. The user can
compile the embedding SDK and harness after the documented input preparation
and dry-run:

```sh
python3 -B standalone/tools/engine.py build --with-smoke --jobs 4
python3 -B standalone/tools/engine.py runtime-smoke
```

Full compilation is intentionally left to the user. The initial runtime's
supported services and limits are described in [runtime.md](runtime.md); it
still needs native compile/link and execution evidence. [automation.md](automation.md)
describes CI, SDK staging, versioning and the release-readiness gate. Browser
application code and UI remain in the separate wave-browser repository.
