# Repository rules

## Purpose

Create a genuinely standalone Blink browser engine, comparable in role to WebKit.
The engine must build, run and support embedding without the Chromium browser
runtime. Source extraction and repackaging alone do not satisfy this objective.

- This repository produces the extracted Blink engine and its embedding package.
  Do not build a browser application or browser UI here.
- The browser application belongs to
  `https://github.com/wavefnd-labs/wave-browser` and will use this extracted Blink
  as its rendering engine. Keep browser application features in that repository.
- The user's license choice for the browser application's own code is MPL-2.0.
  This does not change the original Blink or dependency license notices here.
- Small engine test harnesses may exercise initialization, rendering and the
  embedding API; they are validation tools, not a browser product.

## Upstream sources

- Place Blink's own directories (`public/`, `renderer/`, eventually `common/`,
  etc.) at this repository's root. Do not nest Blink under `third_party/blink/`.
- Preserve upstream provenance and license notices. Maintain an explicit mapping
  between repository paths and original Chromium paths.
- Keep Blink's rendering and web-platform implementation close to upstream.
- Necessary changes to build, dependency boundaries and platform integration are
  allowed to achieve standalone operation. Prefer auxiliary integration first;
  keep source changes minimal, explain their purpose and record reproducible
  diffs/patches separately from the upstream baseline.
- Do not make unrelated engine changes or silently rewrite sources during builds.
- Import/update sources only from an explicit official upstream revision.
  Record the revision, original file hashes and inventory in
  `standalone/config/upstream.lock.json`.
- The upstream lock describes pristine upstream bytes. Do not rehash local
  adaptations as though they came from upstream. Record local changes separately.
- Check the current lock scope before describing source coverage. A partial
  reconnaissance inventory is not the complete Blink tree or a working engine.

## Project-authored support

- Keep scripts, configuration, tool tests, reports and build outputs in
  `standalone/`. Reserve `.github/` for GitHub CI and repository automation
  configuration; workflows belong in `.github/workflows/`.
- Keep runtime integration outside upstream Blink code in `standalone/`:
  `common/` for shared contracts, `platform/` for host services, `engine/` for
  lifecycle orchestration, and `include/` for the embedding API.
- Blink already has an upstream root `common/`; keep it distinct from our
  `standalone/common/` and give each module a defined responsibility.
- Prefer existing Blink platform/client injection APIs before introducing new
  abstractions or editing core callers. Keep unavoidable integration patches
  minimal and recorded against the upstream baseline.
- Project documentation may live in root Markdown files or `docs/*.md`.
- Keep root `docs/` local-only, ignored by `/docs/` in the root `.gitignore`.
  Do not force-add or publish those documents. Preserve local copies.
- Keep the imported upstream README and license files verbatim. Put project
  instructions in `docs/standalone.md`; do not replace the upstream README.
- Preserve upstream authors, dates and messages when importing history. Retain
  original commit IDs or a mapping for rewritten commits, and include the old
  standalone Blink history as well as Chromium's current and former Blink paths.
- Generate build products and candidate snapshots under
  `standalone/out/` or `standalone/work/`, or outside the repo.
- Replace Chromium-provided runtime services through explicit host interfaces
  and implementations; merely renaming or bundling Chromium is not independence.
- Retaining necessary libraries such as V8, Skia, ICU or selected base code is
  compatible with independence from the Chromium browser runtime.

## Validation

Run these commands after changing support code or relocating sources:

```sh
python3 -B standalone/tools/blink.py verify
python3 -B -m unittest discover -s standalone/tests -v
```

The current verifier checks pristine upstream equality and flags local changes.
It is a baseline check, not a prohibition on necessary adaptations. A separate
reproducible adaptation verification step must be added when source changes are
introduced; until then do not claim that modified files pass pristine checking.
The engine build view uses the exact GN adaptations in
`standalone/config/engine-adaptations.json`; validate them with
`python3 -B standalone/tools/engine.py verify`. Do not overwrite pristine source
files when staging build adaptations. The engine target and physical source view
must exclude browser targets and browser root directories. Check typed action
inputs, source files and arguments too: shared library targets can still refer
to browser version/branding files without a browser target dependency.
Tool tests, graph generation and hash verification do not prove engine compilation
or rendering.

The initial C embedding ABI is in `standalone/include/blink_engine.h`; its
implemented capabilities and unsupported services are listed in
`standalone/runtime.md`. The engine aggregate produces `libblink_embed.so`.
When compilation is explicitly authorized, `engine.py build --with-smoke` also
builds the native validation harness. `engine.py runtime-smoke` executes only a
previously built, hash-verified harness and stages its SDK after real pixel
assertions pass. Do not replace native evidence with mocked test receipts.
