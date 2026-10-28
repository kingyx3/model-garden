# Team-runtime private state trust (offline prerequisite)

The host-side multi-Agent team runtime stores each Agent's compiled desired state and
capability manifest under `agents/<agent>/`, and task-budget snapshots under
`tasks/<root-task-id>.json`. These are **authority-bearing inputs**, not
ordinary cache files.

Team state reads and replacements now require owner-only, regular, single-link
files inside owner-only, non-symlink directories. Readers pin an open file
descriptor and reject changes during bounded reads. Writers allocate unpredictable,
exclusive temporary files and fsync the file and containing directory before
returning. A snapshot must identify the same root task as its filename and
must not duplicate task IDs already loaded from another root. Invalid or
exposed state blocks startup instead of resetting task authority.

**Migration:** stop the team host process and inspect existing state before
upgrading. If an old `agents/` or `tasks/` directory is not private, have
an authorized operator establish the expected owner and mode `0700`;
individual JSON state files must be `0600`, owned by the runtime account and
not linked. Do not recursively follow symlinks, discard task snapshots, or
replace a corrupt budget with a new one to force startup. Preserve the
originals for investigation and ledger reconciliation.

**Scope:** credential-free host filesystem trust, not proof of cross-process
budget transaction serialization, distributed fencing, actual multi-Agent
deployment, human notification delivery, or provider-live authorization.
The existing per-process task lock and in-memory task trees still require a
shared transaction coordinator before several writers may safely modify the
same task tree. See #96 and #229. No client infrastructure or provider account
is modified by this change.
