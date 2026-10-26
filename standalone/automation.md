# CI, upstream tracking and engine versions

The repository contains executable GitHub Actions configuration. Remote runs
require the workflows on the published default branch. Local verification does
not establish that a remote runner can build or render.

## What runs automatically

`verify.yml` runs on pull requests and pushes to `main`. It checks the pristine
Blink tree, support-tool tests and version metadata on a GitHub-hosted runner.
Its shallow checkout fetches the immutable extracted baseline commit when the
original root tree is not already present. Private repositories use the job's
read-only token for that fetch without persisting credentials. This job does not
compile Blink.

`upstream.yml` runs daily at 07:23 UTC and can be dispatched manually. It reads
the official Chrome Linux stable VersionHistory feed, resolves that version's
tag in the official Chromium source repository, and verifies `chrome/VERSION`
at the resolved immutable commit. Its artifact contains both the current pin
and candidate, the original VERSION hash and an update checklist. A changed
version is a candidate, not an imported source update or a tested engine release.
The discovery job has read-only repository permission. With default branch
`main`, repository variable `BLINK_UPSTREAM_PROPOSALS=true`, and the Actions
setting allowing pull request creation, a separate write-scoped job opens one
draft proposal PR for a newer version/revision. Existing open or closed proposals
are not duplicated. Older versions cannot downgrade the current development
pin; equal-version/different-revision candidates need manual investigation.

The draft changes only inactive `standalone/config/upstream-update.json`. It
does not import sources or update active locks. Complete the history-preserving
source import, dependency/adaptation review, engine graph checks, native testing
and wave-browser integration checks before completing that update. Remove the
inactive manifest as part of the real roll: release planning blocks publication
while it remains. No automatic merge, tag or SDK release is performed.

GitHub's repository-token-created PR checks require a user with write access to
approve their runs, as described in the official
[workflow trigger rules](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).
These hosted checks verify source and support tools, not engine compilation.
Candidate native builds require an isolated review/build environment; the trusted
self-hosted workflow below continues to accept only `main`.

`engine-build.yml` restores pinned build inputs, compiles the SDK plus the small
embedding harness, and executes the runtime smoke test. It only accepts trusted
`main` code. Run it manually, or set repository variable
`BLINK_ENABLE_FULL_CI=true` to run it automatically on main pushes. Pull requests
never enter this self-hosted job.
The support tests run before large dependency downloads. After the native smoke,
the job uploads exactly the SDK directory named by that successful receipt,
after rechecking its source and file hashes. It does not upload other cached SDK
directories. A single `tar.gz` preserves the smoke executable's permissions;
GitHub uploads that archive directly. This is a downloadable CI artifact with
a 14-day retention, not a
published engine release. The job then resolves the official release channel
again and supplies its candidate
to the release checker, so a future `stable` configuration cannot skip that
compatibility check. Release publication remains disabled.

Provision a dedicated clean Linux x64 runner with the labels `self-hosted`,
`linux`, `x64`, `blink-engine`. It needs an x86_64 Linux host, Python 3.12 or newer
available as `/usr/bin/python3` as well as `python3` (GN uses the absolute system
path), current GitHub runner software supporting the pinned Node 24 actions,
Git, `pkg-config`, `cc` and `c++`, the pinned compiler's Linux host libraries,
build-host tools and enough RAM and disk for Chromium-sized engine
dependencies and outputs. The disk preflight currently requires 120 GiB free;
this is a lower-bound guard, not a measured maximum. The workflow uses four
compile jobs and a 24-hour timeout. These values can be tuned for the machine.
Configure the `blink-engine-build` environment to allow only main deployments.
Prefer an ephemeral runner with no unrelated credentials or workloads.
The preflight checks architecture, both Python entry points, host tool presence
and free disk before dependency downloads. The recorded graph profile was
evaluated with Python 3.14.8; this lower Python guard describes preparation-tool
compatibility and does not certify an untested compiler host.

The pinned actions' official `action.yml` files all specify Node 24:
[checkout v7.0.1](https://github.com/actions/checkout/blob/3d3c42e5aac5ba805825da76410c181273ba90b1/action.yml),
[setup-python v7.0.0](https://github.com/actions/setup-python/blob/5fda3b95a4ea91299a34e894583c3862153e4b97/action.yml),
[upload-artifact v7.0.2](https://github.com/actions/upload-artifact/blob/cf430e030ddbb5b0abf93d22962f4752f3646cd9/action.yml).
The checkout and setup-python documentation requires Actions Runner
[v2.327.1 or newer for this runtime](https://github.com/actions/checkout/blob/3d3c42e5aac5ba805825da76410c181273ba90b1/README.md#checkout-v5).
Keep the runner updated rather than treating that compatibility minimum as a
fixed supported runner release.

`release-preflight.yml` produces a readiness report on manual dispatch. It
does not publish. A release manifest produced without real validation evidence
has `publishable: false`. Actions dependencies are pinned to immutable commits;
Dependabot proposes weekly updates for review.

GitHub documents why [untrusted code must not run on self-hosted
runners](https://docs.github.com/en/actions/reference/security/secure-use#hardening-for-self-hosted-runners),
how [workflow triggers and runner labels
work](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax),
and how to configure [environment protection
rules](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments).

## Versioning

```sh
python3 -B standalone/tools/automation.py version
python3 -B standalone/tools/automation.py release-plan
```

The four upstream version components come from the byte-preserved pinned
VERSION file. The current compatibility version is `158.0.8114.0`, package
version is `158.0.8114.0-engine.1`, and proposed tag is
`blink-158.0.8114.0-engine.1`. `package_revision` identifies our integration
updates at the same upstream revision. `abi_version` identifies the embedding
contract separately; do not infer C++ ABI compatibility from the Chromium
version. Both project fields live in `standalone/config/release.json`.

The current track is `development`. The pinned Chromium commit has not been
established as a stable release by this repository. Stable version alignment
means resolving an actual official stable tag and importing that exact revision;
it must not relabel an arbitrary Chromium-main snapshot as stable. Blink's source
revision follows Chromium here; this repository's engine packages have their
own integration and validation status.

The [official VersionHistory API
guide](https://developer.chrome.com/docs/web-platform/versionhistory/guide)
describes Chrome release-channel versions. Our discovery step additionally
resolves those versions into official Chromium source commits, because a Chrome
version feed alone is not source provenance.

## Release evidence

After a full SDK build, `engine.py runtime-smoke` executes the compiled harness.
Its native schema 3 receipt includes JavaScript, input/IME, child frames, DOM storage,
dedicated worker and navigation assertions as well as software pixels. Earlier
schema 1/2 harnesses cannot
satisfy the current validation step.
Its receipt must bind to the current Git commit, project source hashes, all
dependency/adaptation locks and output artifact hashes. A successful dry-run,
GN generation or support test suite cannot replace this execution evidence.

```sh
python3 -B standalone/tools/engine.py build --with-smoke --jobs 4
python3 -B standalone/tools/engine.py runtime-smoke
python3 -B standalone/tools/automation.py release-plan \
  --evidence standalone/reports/linux-x64-runtime-smoke.json \
  --require-ready
```

The release checker requires `compiled: true`, `runtime_smoke_passed: true`, a
matching `snapshot`, and unchanged real artifacts. Changing an integration
source or lock invalidates previous evidence. Uncommitted source changes block
release readiness, because they cannot be identified by the proposed Git tag.
The SDK stages both original attribution and notices from the materialized
dependencies alongside binaries, headers, resources and the pinned source locks.
It also includes `notices/blink/ENGINE_NOTICE` for the frame initialization
reference and `docs/runtime.md`, which documents the implemented ABI and remaining unsupported host integrations.
SDK staging uses a source/artifact fingerprint in its directory name and never
replaces an existing different build. A smoke failure invalidates the previous
runtime receipt immediately; no failed invocation inherits an older success.
When `release_track` is `stable`,
also supply `--candidate` with a matching verified stable discovery report.
This local receipt is provenance for review; it is not a cryptographic build
attestation and is not authorization to publish a release.

Actual release publication, signing and binary distribution remain disabled.
They should only be connected after the first complete SDK compilation and
rendering run, package validation and preservation of all required imported
dependency notices. The browser application's MPL-2.0 choice has no effect on
the licenses of these imported engine libraries.

## Upstream import automation boundary

```sh
python3 -B standalone/tools/automation.py upstream
```

Discovery can run unattended. A full source roll still needs the existing
history-preserving extraction, a pristine source inventory, new dependency pins,
reviewed integration patches, regenerated browser-free graphs and successful
engine compilation/rendering. The generated candidate report lists these steps
explicitly. This implementation does not substitute a snapshot copy or a lock
edit for that history-preserving import. Fully automatic source rolls and
publication are not enabled.
