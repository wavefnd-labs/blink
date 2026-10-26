# Pristine GN fixtures

These three files are unchanged Chromium build definitions from official revision
`443fc1cc6746166ba1d0deba6bee26bbd8f7f84c`. Their paths relative to this directory
are their original Chromium paths. Original copyright headers are preserved;
Chromium's original license is also retained in
[`LICENSE`](../../../../LICENSE) at the repository root.

`test_build_profiles.py` checks their Git blob IDs and SHA-256 hashes against the
production pins in `config/engine-profiles.json` and
`tools/build_profiles.py::OPTIMIZATION_ALIASES` before exercising the real guarded
profile overlays. The fixtures let the support tests run without an ignored
materialized Chromium workspace or network downloads. Refresh them only after a
reviewed upstream pin change; do not replace their original hashes with adapted
file hashes.
