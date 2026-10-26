# Full-history publication checks

The extracted history retains original metadata and a Chromium-to-Blink commit
mapping. A partial clone can have every commit and tree while historical blobs
are still promised by an official upstream. Normal partial-clone `git fsck`
therefore does not prove that a GitHub upload can include the full history.

Use an explicit offline audit of the refs that will be published:

```sh
python3 -B standalone/tools/history_archive.py audit \
  --repository /mnt/hdd/blink \
  --ref main --ref upstream/blink --ref upstream/legacy-blink \
  --report standalone/work/publication/history-audit.json
```

The audit pins the refs, rejects shallow ancestry, disables lazy fetching,
records missing objects, checks all present reachable object types and sizes,
and refuses readiness if a reachable blob exceeds 100 MiB or a ref changed.
Reports and durable object manifests stay under ignored `standalone/work/`.
Its readiness result covers history availability and the historical blob-size
limit; it does not establish remote credentials, pack sizing or server acceptance.

For a durable missing inventory routed to its corresponding official promisor:

```sh
python3 -B standalone/tools/history_archive.py restore \
  --repository /mnt/hdd/blink --remote blink-upstream \
  --missing standalone/work/publication/modern-missing-objects.txt \
  --batch-size 5000 \
  --report standalone/work/publication/modern-restore.json
```

Restoration requests only missing object IDs, preserves refs, Git configuration
and source files, and checks each requested batch afterward. Reruns inspect the
actual object database before downloading; per-batch requests, logs and atomic
reports remain available after interruption. Use the legacy official promisor
for old standalone Blink objects. Do not send mixed inventories to a server
that does not contain all requested IDs. A missing tree can conceal further
missing descendants, so rerun the complete offline audit after recovery.

Keep original README/license bytes and author/date/message/ID mapping intact.
Do not squash or truncate history to avoid a transfer limit. GitHub also limits
a single push to 2 GiB; complete large histories may need ordered ancestral
checkpoint pushes before the final explicit refs. Do not mirror unrelated cache
refs, and do not assume `pack.packSizeLimit` changes the streamed push pack size.
See the [official GitHub push-limit guidance](https://docs.github.com/en/get-started/using-git/troubleshooting-the-2-gb-push-limit).

The completed fixed-ref audit is recorded in
[`archive-verification.json`](upstream/history/archive-verification.json) and
hashed by `config/history.lock.json`. It reports zero missing objects, zero
oversized blobs, unchanged original source bytes, and the preserved commit
mapping. This is an audit of those exact pins; future source rolls must renew
that evidence rather than inheriting the old result.
