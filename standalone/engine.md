# Engine-owned build entry point

The engine now has its own GN dotfile, root target and feature configuration.
The original Blink source tree remains unchanged. The entry now includes an
`libblink_embed.so` implementation and C embedding headers with JavaScript, input/IME, child frames, DOM storage,
dedicated classic workers and host-backed document navigation alongside the
required upstream libraries, generators and runtime data. The existing Linux x64
development artifact passed the schema 3 native harness and a separate
wave-browser embedding check on 2026-10-26. Release SDK validation remains
separate; graph checks alone do not establish native behavior. See
[runtime.md](runtime.md) for the API and its deliberately limited first profile.

## Result

- Entry target: `//:blink_engine` in `gn/BUILD.gn`.
- 6,855 declared targets, with 6,854 reachable from the production engine root.
  `//standalone/engine:blink_embed_smoke` is declared separately as the small
  engine validation harness.
- The original 7,398-target baseline is retained for comparison. Of those,
  6,798 match in every GN field after source/output path normalization. Direct
  metadata, SDK visibility and source/dependency cuts are reviewed separately.
  There are 549 individually fingerprinted unreachable baseline targets, including
  the earlier WebUI cut and 136 further removals. Transitive configuration effects
  have exact old/new ordered lists; only removals and permutations are allowed.
- Runtime additions comprise the engine root, embedding library, smoke harness
  and three unchanged upstream V8 context-snapshot targets. The snapshot target
  fingerprints and original build-input hash are separately locked in
  [`config/runtime-graph-additions.json`](config/runtime-graph-additions.json).
- No browser or test-only target dependencies under the boundary policy.
- No browser directories, GN regeneration inputs or typed source/action browser
  file references in the engine build view.
- Neither the Chromium root dotfile nor its root BUILD file is loaded.
- Pinned source/tool/resource preparation is complete for this profile, including
  the reviewed nested source/package and revision metadata inputs.
- All 43,454 declared source leaves in the current graph are present. Remaining
  Ninja command counts depend on the selected profile and local build state;
  they are not a source-publication requirement. This inventory does not prove
  compiler-implicit include or generator-import closure. Earlier Git ownership
  classification covered 44,820 source leaves in the library-only profile and
  is retained as archived evidence, without claiming it was repeated for the SDK.

The low-level pinned compiler/dependency framework remains in use, as do Blink's
required libraries. The engine entry point currently supports the recorded Linux
x64 profile with static upstream component libraries linked into the embedding
shared library. It does not implement browser UI or a browser app.

## Feature configuration

`gn/platform_features.gni` extracts the options consumed by Blink and shared
resources from `content/public/common/features.gni`: external popup menus, plugin
build support and screen-capture build conditions. The browser's separate renderer
binary option is omitted. Build flags do not implement host services.

The unused `gn/resource_features.gni` provider and five unused browser-resource
GN adaptations have been retired. Generic GRIT templates under the cached
`ui/webui/resources/tools` directory remain legitimate configuration inputs;
they do not add browser application/resource targets to the engine closure.

Thirteen upstream GN files have recorded build adaptations, including Blink core's BUILD,
two base-library version actions and exact visibility grants that let the SDK
use Blink's core/modules/platform APIs and inside-Blink configuration.
A separate header adaptation adds only a read-only dedicated-worker script-type
accessor, so the host can explicitly reject unsupported module workers.
The exact original/adapted hashes and replacements are recorded in
[`config/engine-adaptations.json`](config/engine-adaptations.json), with a reviewable
[`patches/engine-build-imports.patch`](patches/engine-build-imports.patch).
An unexpected upstream change stops adaptation. Pristine original hashes are
never replaced by hashes of the adapted files.

The version actions also refer to engine-owned `gn/VERSION` and `gn/BRANDING`
instead of browser files. The compatibility version stays at the pinned upstream
value (`158.0.8114.0`); the engine library product name is `Blink`. Both header
generators were executed successfully. Original Blink/Chromium notices remain
unchanged; these files do not define a browser application.

## Commands

From the repository root on the supported Linux x64 host:

```sh
python3 -B standalone/tools/materialize.py --plan
python3 -B standalone/tools/materialize.py
python3 -B standalone/tools/materialize_packages.py --check-tools
python3 -B standalone/tools/engine.py gen
python3 -B standalone/tools/engine.py verify
python3 -B standalone/tools/build_prerequisites.py
python3 -B standalone/tools/engine.py build --dry-run --with-smoke
# Run the full compilation yourself when ready:
python3 -B standalone/tools/engine.py build --with-smoke --jobs 4
python3 -B standalone/tools/engine.py runtime-smoke
# --jobs is optional; choose concurrency for your machine.
```

`materialize.py` restores the complete Git source trees of the 83 selected
root-DEPS dependencies, the pinned depot_tools Python infrastructure, and conservative
main-repository library/support subtrees. This includes headers, generator
templates, Python imports and resource files which GN does not individually
enumerate. Existing source bytes and executable modes must match their original
Git blobs; differences stop restoration for review. Blink's checked-out sources
are only verified, never rewritten. Downloads use original upstream pins.
The additional OpenGL-Registry source required by Dawn's generator has a separate
`config/nested-source-dependencies.lock.json`: its parent commit, original DEPS,
gitmodules and Gitlink must agree. This explicitly selected nested dependency
does not trigger recursive vendor synchronization or arbitrary DEPS hooks.
For large dependencies, official archives of the exact commit reduce download
round trips. Every archive file must match the original Git blob, executable
mode and symlink target before source installation. `--git-blobs` selects the
Git-only transport. Archive transport failures may fall back to Git; archive
integrity failures stop that dependency.

The same command installs all 13 tool/resource package roots using
`materialize_packages.py`. GCS downloads use the frozen generation, size and
SHA-256. CIPD downloads use immutable resolved ObjectRefs and archive SHA-256,
without re-resolving tags. Installed archive contents are verified as well.
The 12 root-DEPS package roots retain their original baseline lock. DevTools'
esbuild package is frozen separately in `config/nested-build-packages.lock.json`,
with parent revision and original DEPS provenance.
`--sources-only` and the package tool can prepare these two parts separately.
`--jobs` controls concurrent source restorations, with a default of three.
These commands download/extract inputs and generate GN files; they do not compile.

Compiler-implicit production headers can require a dependency absent from the
GN-selected baseline. `base/gtest_prod_util.h` includes the original GoogleTest
`gtest_prod.h` through Chromium's forwarding header. The separately reviewed
[`config/implicit-source-dependencies.lock.json`](config/implicit-source-dependencies.lock.json)
adds that source repository at its exact root-DEPS/Gitlink pin without enabling
GoogleTest targets or changing the 83-repository baseline lock. `materialize.py`
restores this supplement automatically, including on a fresh CI runner. Its tool
verifies the original production include chain and every dependency Git blob,
mode and required header. It does not claim to discover every implicit include.

To repair or check this input independently without compiling:

```sh
python3 -B standalone/tools/materialize_implicit_sources.py
python3 -B standalone/tools/materialize_implicit_sources.py --verify
```

The original failed `base/base64.cc` command and diagnostic are preserved in
[`reports/linux-x64-missing-implicit-header.json`](reports/linux-x64-missing-implicit-header.json).
After restoration that same translation unit passes a syntax-only check with
the pinned compiler; no object or linked engine was produced by the probe.

`prepare_metadata.py` invokes the original pinned `lastchange.py` for three
reviewed library hooks: GPU lists, Skia and Dawn. It generates three revision
headers plus `gpu/webgpu/DAWN_VERSION` in isolation and checks their actual pinned
commit IDs before publication. It rejects the upstream generator's zero-revision
fallback. These headers can be implicit C++ includes which Ninja's input tool
does not enumerate. The profile retains upstream's fixed `.dummy` version inputs
for reproducible non-official builds; they are original files.

`gen` prepares a generated source view, runs GN using the engine's dotfile/root,
checks the browser boundary and compares all dependency targets with the recorded
baseline, permitting only reviewed version-action and SDK visibility changes.
The three added upstream context-snapshot targets require exact locked graph
fingerprints; unexpected added upstream targets fail verification. Missing GN
metadata may require network access; it is fetched at the
locked original revisions. Browser imports are rejected instead of restored.
There is no need to load the Chromium browser root build to generate this view.

The view lives under ignored `work/engine/views/<configuration-hash>/src/`.
Canonical paths such as `third_party/blink/` exist only as build aliases there;
the repository's Blink sources remain at its root. The view excludes `chrome/`,
`content/`, `headless/`, `android_webview/` and `ios/`. It retains canonical library
paths to reuse the upstream library/toolchain definitions.

Most unchanged inputs use source links. The small set of adapted GN files and
the engine-owned entry files are private copies in this view. `.rustfmt.toml`
is also a private regular file containing the exact unchanged upstream bytes.
The pinned formatter canonicalizes its configuration path before matching
ignore patterns: linking this configuration into the source cache made generated
engine-view bindings fall outside its root and caused the reported panic.
[`config/rustfmt-layout.json`](config/rustfmt-layout.json) records this layout
adaptation and original hash. Formatting and upstream ignore rules remain
enabled. A pinned-formatter regression reproduces the old failure and verifies
the copied-configuration path without compiling Blink.

Parent directory links
are split before writing an adaptation, so no writes reach pristine sources.
Newly materialized source files are exposed when preparing the view again.
Configuration changes create a new view rather than overwriting an old one.
`verify` checks the current recipe, staged files and pristine Blink baseline.

`version-check` runs only the two engine version-header generators and checks
the resulting product name/version. It does not compile the engine.

`build` invokes the pinned Ninja's `blink_engine` target and streams its output
while saving a log. `--with-smoke` also builds `blink_embed_smoke`; `--jobs`
optionally limits concurrent compilation. `runtime-smoke` executes that compiled
harness and records real initialization, host-resource, painting and lifetime
checks. It does not compile missing binaries. See [runtime.md](runtime.md) for
the required runtime data and [automation.md](automation.md) for SDK staging,
validation receipts, CI and release readiness.
A dry-run does not compile any source. It checks Ninja's declared input graph;
it cannot prove that implicit C++ includes, generator imports or compilation
will succeed. `version-check` remains available to run just the two version-header
generators.

## Evidence

- [`reports/linux-x64-engine-entry.json`](reports/linux-x64-engine-entry.json):
  entry point, source view, target boundary, artifact hashes and validation scope.
- [`reports/linux-x64-engine-parity.json`](reports/linux-x64-engine-parity.json):
  full dependency-target comparison with the original graph.
- [`reports/linux-x64-runtime-graph-additions.json`](reports/linux-x64-runtime-graph-additions.json):
  separately reviewed upstream V8 context-snapshot targets.
- [`reports/linux-x64-engine-header-dependencies.json`](reports/linux-x64-engine-header-dependencies.json):
  successful GN header dependency checks for the SDK and validation harness;
  this is not a backend compilation.
- [`reports/linux-x64-rustfmt-layout.json`](reports/linux-x64-rustfmt-layout.json):
  actual pinned formatter failure/success regression and source-view probe.
- [`reports/linux-x64-engine-graph.json.gz`](reports/linux-x64-engine-graph.json.gz):
  evaluated engine graph.
- [`reports/linux-x64-engine-inputs.json`](reports/linux-x64-engine-inputs.json):
  GN/Ninja regeneration inputs with no browser build paths.
- [`reports/linux-x64-engine-build.json`](reports/linux-x64-engine-build.json):
  latest Ninja attempt, version, exit code and output tail.

- [`reports/linux-x64-engine-version.json`](reports/linux-x64-engine-version.json):
  successful version-header generation and verified engine metadata.
- [`reports/linux-x64-materialized-sources.json`](reports/linux-x64-materialized-sources.json):
  restored source pins, original Git inventory hashes and per-repository results.
- [`reports/linux-x64-materialized-packages.json`](reports/linux-x64-materialized-packages.json):
  immutable package identities, archive hashes and installed-content inventories.
- [`reports/linux-x64-build-tools.json`](reports/linux-x64-build-tools.json):
  executable-version checks and generated Ninja tool paths.
- [`reports/linux-x64-engine-build-inputs.json`](reports/linux-x64-engine-build-inputs.json):
  current engine-plus-harness inventory of 44,832 Ninja-declared source leaves,
  with no missing files; it does not classify every input's original Git owner.
- [`reports/linux-x64-materialized-nested-sources.json`](reports/linux-x64-materialized-nested-sources.json):
  explicitly selected nested source provenance and original Git blob verification.
- [`reports/linux-x64-generated-metadata.json`](reports/linux-x64-generated-metadata.json):
  original generator hash and verified revision metadata outputs.
- [`reports/linux-x64-source-input-closure.json`](reports/linux-x64-source-input-closure.json):
  archived pre-embedding ownership classification of 44,820 declared source
  leaves and its original materialization/dry-run evidence. Its historical input
  report hash and command count do not describe the current SDK graph.

## Current limit

GN generation, reviewed target parity and both version-header generators have
passed. Source/package materialization and dry-run results apply to their
recorded views and target sets. A report's `complete` or zero dry-run exit code
applies only to that check. The user's initial compilation attempt failed in
rustfmt while formatting generated bindings; the configuration-layout fix has
been checked with the pinned formatter. The full backend compilation has not
been rerun here. The later GoogleTest production-header failure was repaired
using the exact root-DEPS source pin; the original failing translation unit
passes a syntax-only check without producing an object. Actual backend compilation, linking and rendering remain
unvalidated.
No replacement content or success stubs are created for missing inputs.

The full compilation is intentionally left to the user. Any compiler/generator
failure still needs to be investigated against the pinned inputs. The extended
runtime and C API implementations are now present, but they do not yet supply
every host service needed for a complete browser engine. A successful native
smoke run is required before claiming even this first profile works. Browser
application features remain in the separate wave-browser repository.


The later `rand_util_posix.cc` failure was a missing compiler-implicit LSS header.
The original root DEPS Linux profile also selects cros_system_api, whose headers
are included by production BlueZ sources. Both complete source trees are now
restored at their official pins in the supplemental implicit-source lock, with
526 files verified across GoogleTest/LSS/cros_system_api. The original failing
LSS translation unit passed its exact pinned command in syntax-only mode. See
`reports/linux-x64-missing-lss-header.json` and
`reports/linux-x64-implicit-include-audit.json`; the static include audit is
bounded and does not prove complete preprocessor/compiler closure.

The new native helper syntax attempts encounter Mojo headers that this partial
user build has not generated yet. No generated stubs were substituted. A full
native compilation and schema 3 smoke run remain required to validate IME,
frames, storage and workers, as well as the earlier runtime implementation.


## Dawn generator source-view layout

The later Dawn header/module generator failure was a Python entry-path mismatch,
not a missing source file. CPython resolves a linked entry script when choosing
`sys.path[0]`; Dawn then discovers imports in the source cache while GN expects
the engine view. `config/generator-layout.json` pins five original entry scripts
and their SHA-256/Git blobs/modes at the selected official Dawn revision.
`engine.py prepare` stages only those scripts as unchanged regular files, leaving
helpers/templates/data linked and all expected-input/output checks enabled.

This layout upgrade retains the existing view key and completed object files.
A guarded upgrade journal permits retry after an interruption between entry
copies without accepting unrelated local files. Fresh views apply the same rule. Drift in
original bytes, pins or recorded copies stops preparation/verification.
`reports/linux-x64-dawn-generator-layout.json` retains the real failure,
a failing symlink control and nine successful original generator actions in the
existing source view. No native compiler or linker ran for that check; 3,940
existing object files retained their sizes and timestamps.


## Excluding unused browser WebUI resources

A later TypeScript failure came from browser WebUI resources pulled through
`ui/base:base`. The recorded static-engine adaptation removes only
`jstemplate_builder.{cc,h}`, `web_ui_util.{cc,h}` and their
`//ui/webui/resources:resources_grit` dependency. Native ui/base services and
`i18n_source_stream` remain. A bounded source/include audit found no external
callers; this is not native link evidence.

The cut removes 413 unreachable targets. Each removed node has a complete
normalized baseline fingerprint in `config/engine-adaptations.json`; unexpected
removals, stale reviews, changed hashes and unrelated retained-node changes still
fail graph verification. GN assertions, the target boundary policy and typed
source/action path checks reject browser WebUI resources. The original
TypeScript validation remains enabled and unchanged.

The existing complete output directory was moved into the new reviewed build
view, then GN was regenerated. All 6,138 existing object files retained their
sizes and timestamps. The 28,009 common compiler commands are unchanged, and
the incremental dry-run has 24,609 remaining commands. Ninja still determines
which generated inputs or dependency timestamps require rebuilding. Support
tests (194), pristine verification, adaptation verification and graph checks
pass. No full native compilation or runtime execution was performed. See
`reports/linux-x64-webui-dependency-cut.json` for the failure, adaptation, bounded
audit and cache-preservation evidence.


## Additional browser dependency cleanup

The current profile excludes unused Chromium profile cookie-encryption
orchestration, password-manager UI/autofill helpers, regional profile-service/UI
metrics, an unused compositor DWA edge and its metrics upload/keep-alive stack,
and unused native UI resource packs/WebUI templates. Web Credentials types,
regional search data, rendering/compositing, native theme/IME/accessibility and
actual Blink inspector overlay resources remain. No Blink implementation or
web-platform feature was disabled for this cleanup.

Six affected native libraries pass GN header-dependency checks. All 136 newly
unreachable targets are pinned against their complete original baseline nodes.
Exact target and source-file rules, plus canonical-path checks, reject browser
implementations reintroduced through a shared target or source alias. The view
remains a symlink layout over a preparation cache, not a filesystem sandbox.
Static audits and header checks do not replace compilation or runtime tests.

The latest complete output cache was moved to the reviewed view and GN was
regenerated; 8,699 existing object files retained their sizes and timestamps.
Ninja may rebuild affected outputs when inherited compiler/link flags change.
Support tests (201), pristine/adaptation checks, real GN parity and the
21,222-command incremental dry-run pass. Full engine compilation remains the
user's task. Detailed bounded audit and retained-dependency decisions are in
`reports/linux-x64-browser-dependency-cleanup.json`.

The later Tint source-generation failure also required Dawn's pinned Go 1.26.6
package. Its complete official archive and installed inventory are now verified,
and the original Tint Python wrapper is copied unchanged into the view to keep
its module root consistent. The exact source-generation command passed both in
an isolated directory and the current real output directory with networking
disabled: all 26 declared outputs plus the upstream dictionary were generated.
No engine C/C++ compilation or linking ran. See
`reports/linux-x64-tint-go-generator.json`.


## Indirect generator prerequisites

`tools/build_prerequisites.py` now runs automatically before Ninja, including
`engine.py build --dry-run`. It checks the reviewed 83 action-entry scripts and
fingerprints against the pinned dependency locks, seven real Blink entry
imports, 11 additional executable version/help probes, bindgen's libclang load,
required package tool paths and the complete selected nested-source inventories.
It uses the GN profile's Python interpreter, never compiles engine sources, and
fails before starting a long compilation if a reviewed prerequisite is missing
or changes. New revisions/script selections require explicit review.

The indirect audit covered 1690 actions, 72 PythonPath wrappers and 482 parsed
Python modules. Conditional/optional import candidates were separately triaged;
this is bounded analysis, not proof of every possible dynamic dependency. The
selected header-only EGL Registry dependency was absent despite a zero-missing
Ninja leaf inventory. Its complete 393-file tree is now restored at the official
Dawn Gitlink, with original hashes and modes verified.

Seven Blink generator entries are copied unchanged according to
`config/blink-python-layout.json`. This preserves the lexical Chromium-shaped
package roots expected by web_idl/bind_gen and the instrumentation generator's
vendored Jinja2 lookup. The common copy journal preserves the current view key
and protects original sources. GN and Ninja children receive a source-safe Python
environment; existing source-tree bytecode was archived under work rather than
hidden in the upstream verifier. No core Blink source was patched.

Actual execution of all 21 selected consumers passed: IDL collection/database,
11 binding classes,validation,file-list checks,event metadata and probes. A total
of 5870 expected outputs were checked. Support tests 215, pristine/adaptation
checks,preflight and the 15,297-command incremental dry-run pass. All 13,796 existing
object files retained their sizes and timestamps. Full native compilation and
runtime validation remain separate. See
`reports/linux-x64-indirect-prerequisites-audit.json`,
`reports/linux-x64-blink-python-layout.json` and
`reports/linux-x64-indirect-build-tools-audit.json`.
