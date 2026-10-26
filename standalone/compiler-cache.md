# Development compiler cache

`tools/compiler_cache.py` manages an optional project-local ccache 4.14.2.
The development profile uses it by default; `--no-cache` permits development
configuration without installing the tool. Release builds keep their existing
uncached configuration unless explicitly requested. Profile selection and cache
selection belong to `tools/engine.py`, and use separate guarded build views.

```sh
python3 -B standalone/tools/compiler_cache.py install
python3 -B standalone/tools/compiler_cache.py status
python3 -B standalone/tools/compiler_cache.py probe
```

Installation downloads only the official Linux x86_64 glibc binary archive in
`config/compiler-cache.json`. Its SHA256 is frozen from the official release
asset metadata. The extracted binary and verbatim upstream `LICENSE.md` and
`GPL-3.0.txt` are also checked individually. Nothing is installed globally, and
the tool is not part of the embedding SDK. ccache is a GPL-3.0-or-later build
tool; this does not replace any Blink or dependency license notices.

Cache entries live in `work/compiler-cache/data/`, with compression and a 20G
maximum. The generated `work/compiler-cache/ccache.conf` is isolated through
`CCACHE_CONFIGPATH`; inherited `CCACHE_*` overrides and global ccache settings
are excluded. Hard links and file cloning are disabled. Compiler contents are
hashed, and directory hashing remains enabled. Cache installation/status never
clears existing entries or changes an engine output directory.

The selected tool version matters. Older ccache versions did not reliably hash
explicit Clang module contents. This engine uses Clang `-fmodules` with explicit
`.pcm` inputs, rather than standard C++20 modules. Upstream ccache requires
`direct_mode=true`, `depend_mode=true` and `sloppiness=modules` for this mode.
That is the only enabled sloppiness; time, system-header and file-stat shortcuts
are not enabled. The project wrapper changes compiler `-MMD` to `-MD` so depend
mode includes system headers. It leaves other compiler arguments intact.

The wrapper bypasses caching for implicit module state and unreviewed response
files. It still executes the real compiler in those cases. The upstream module
configuration, explicit module map and module generation remain unchanged.
Do not replace the wrapper with direct invocation of an older system ccache,
enable implicit module caches or weaken header checks to increase hit counts.

The explicit `probe` command creates a tiny isolated synthetic fixture and cache
under `work/compiler-cache/probes/`, using the already materialized pinned
Chromium Clang. It proves an initial miss, an identical direct hit, a miss after
changing valid `.pcm` bytes while keeping source/header/module-map/consumer
arguments identical, and another direct hit. It also changes a system header
and verifies a miss with changed object bytes followed by an identical hit.
It does not run GN, Ninja, engine compilation or linking. Each probe preserves
its commands, tool hashes, statistics and output hashes in its own receipt.

Cache reuse accelerates repeated identical C/C++ compilations. It does not make
an empty-cache first build or final link disappear, and does not cover the Rust
compiler or all generators. The 20G limit may evict entries before an entire
engine fits. HDD lookup costs, changed compiler flags, module bytes and paths
can reduce the benefit. The synthetic proof is not an engine build or runtime
validation.

Sources: [official download](https://ccache.dev/download.html),
[ccache 4.14.2 module contract](https://ccache.dev/manual/4.14.2.html#_c_modules),
[depend-mode header tracking](https://ccache.dev/manual/4.14.2.html#_the_depend_mode),
and [release notes](https://ccache.dev/releasenotes.html).
