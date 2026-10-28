# Private schedule/trigger state (v0.8.196)

The host worker and authenticated chat owner commands share the per-tenant
`ScheduleStore` in `platform/core/schedules.py`. A schedule can carry a goal,
owner, delegated authority and budget. Its file is therefore **private runtime
authority state**, not a harmless cache. The existing cooperative lock and
owner checks are necessary but insufficient if filesystem entries can be
replaced or linked.

`platform/core/schedule_store_io.py` now requires a same-UID, private
(mode 0700) real directory and private, single-link regular lock/state files
(mode 0600). Reads pin the directory and lock descriptors, refuse symlinks,
hardlinks, foreign ownership, permissive files, oversized or malformed JSON,
and wrong tenant/schema. Writes refuse concurrent out-of-band state changes,
use an unpredictable exclusive temporary file, fsync the snapshot, atomically
replace state and fsync the directory. It **does not silently chmod** unsafe
pre-existing state. The old predictable `<tenant>.schedules.tmp` file is
ignored; operators can inspect/remove it only after stopping the runtime.

**Upgrade/operator action:** ensure the schedule-state directory is private
and owned by the actual runtime UID, and the existing
`<tenant>.schedules.json` and `.<tenant>.schedules.lock` files are
single-link, regular and private before upgrading. On an unsafe entry,
`ScheduleError` intentionally blocks schedule reads, claims and writes;
stop the worker and investigate filesystem ownership/links rather than
deleting or chmodding unknown files during live execution. Preserve a
verified, tenant-matched backup before repairing state.

Credential-free regression coverage is in
`tests/test_schedule_store_integrity.py` and existing schedule/scheduler
tests. This closes a host-local storage trust boundary, **not** the original
issue #99 live provider/scheduler/owner-notification acceptance: external
channel delivery, event execution, real owner receipts and redeployment
continuity still require an authorized client DEV run.
