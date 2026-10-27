# Font resolution integration

The engine stages four font source adaptations from Chromium revision
`443fc1cc6746166ba1d0deba6bee26bbd8f7f84c`. The original files under `renderer/`
remain pristine. Exact original hashes, replacements and resulting hashes are
in `config/engine-adaptations.json`; the reviewable diff is included in
`patches/engine-build-imports.patch`.
The font hunks use zero context; checking that diff manually requires
`git apply --check --unidiff-zero` against a separate canonical Chromium layout.
Normal engine staging applies the exact-count, hash-checked replacement recipe.

## Failure and boundary correction

The previous engine crashed in
`FontVariantAlternates::GetResolvedFontFeatures()` on a small document using
`font-variant-alternates: styleset(disambiguation)`. Both a generic sans family
and an installed named family reproduced the unresolved-description invariant.

`CSSFontSelector` resolves each family's `@font-feature-values` aliases in a
copy of the requested description. Its platform-font path previously returned
null for named families whose name did not come from the generic settings map.
`FontFallbackList` then asked `FontCache` with the unresolved original. The
adaptation makes the selector perform that platform lookup itself, using the
same family-resolved description as the web-font path.

Character fallback, last-resort fallback and callers without a selector can
also reach `FontCache` with an unresolved request. That boundary now calls the
existing alternate resolver with empty alias tables before platform cache-key
creation. An absent family table contributes no alias features, while
`historical-forms` still becomes the OpenType `hist` feature. Already-resolved
requests retain every alias and are not resolved a second time. Resolution
state is therefore retained in release builds as well as DCHECK builds.

The existing DCHECK remains enabled. Neither page CSS nor the native feature
merge/precedence implementation is removed. The family selector still resolves
real aliases; the fallback path does not invent a family alias table.

The pinned Linux Skia Fontconfig interface recognizes `Sans`, `Serif` and
`Monospace` as generic fallback names. It conservatively rejects `sans-serif`
when that name does not identify an installed family. Standalone page defaults
must use the supported platform alias, and initialize the platform system font
for `system-ui`, rather than depending on last-resort selection for ordinary
sans text.

## Verification status

The three adapted `.cc` translation units pass syntax checks using the pinned
engine compiler and original build flags with the adapted header staged in an
isolated include overlay. The Python tests verify exact patch/hash agreement,
reject upstream drift, and prove overlay staging leaves pristine sources
unchanged. These checks do not establish linked-engine or pixel behavior.

The native smoke harness must be rebuilt and run to establish recovery for
generic, installed, missing and pending web fonts, historical forms, alias
features and missing-glyph fallback. The project-authored tiny TrueType font in
`engine/rendering_fixtures.h` provides deterministic feature glyphs for native
high-level/low-level pixel comparisons in `engine/rendering_smoke.cc`. Its
generator is `tools/generate_rendering_fixtures.py`; no upstream test font is
copied or altered. Existing prebuilt libraries still have the old behavior until
a new engine build completes.
