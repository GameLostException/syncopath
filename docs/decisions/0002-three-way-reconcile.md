# ADR-0002 — Sync by three-way comparison, and split the engine around it

- **Status:** Proposed — needs a decision on the open questions below
- **Date:** 2026-11-09

## Context

`SyncEngine` reacts to each event with its own logic (download, upload, rename,
delete, conflict), and reconcile repairs only two cases: rows in `error`/`unknown`
and synced files missing locally. Whatever an event handler gets wrong stays wrong.
Every data-loss bug fixed on 2026-11-09 (ADR-0001) came from a handler that had no
backstop. The class mixes planning, file I/O, path resolution, conflict policy,
error handling and UI callbacks in one 1,300-line unit.

## Proposal

1. **One planner.** For each file, compare three versions: the *base* (state DB,
   last synced), *local* (disk now) and *remote* (Drive now). The result is a list
   of actions (`download`, `upload`, `rename_local`, `move_remote`, `conflict`,
   `delete_local`, `delete_remote`, `forget`). It is a pure function, tested by
   table, with no I/O.
2. **Events become hints.** A watcher event only marks paths or file IDs as dirty;
   the planner decides. Reconcile runs the same planner on everything. A missed or
   failed event is then fixed by the next pass, by construction.
3. **Split the engine** into `planner.py` (pure), `executor.py` (applies actions,
   one per call, idempotent), `paths.py` (remote path resolution and the folder
   index), and a thin `sync_engine.py` that wires them.

## Open questions — the owner's call

- **Deletion semantics — settled 2026-11-09** in the
  [deletion specification](../specs/deletion.md) (safe mode lists and marks deletions until
  the user confirms; unsafe mode mirrors them; everything goes through a bin). The two
  bullets below are kept for the record and are superseded by it.
- ~~**A file missing locally, unchanged remotely, no delete event seen**~~ (deleted while
  the daemon was stopped, or the disk was not mounted). Today it is re-downloaded,
  so local deletions come back; in safe mode this happens every 10 minutes.
  *Recommendation:* if the sync root itself is missing or empty, do nothing and
  raise an error (an unmounted disk); otherwise treat it as a local delete and
  apply the safe-mode rule (safe mode on: forget it locally, keep it on Drive).
- ~~**Safe mode and a local delete event.**~~ Today, with safe mode on, the file is
  restored by the next reconcile. *Recommendation:* safe mode means "never delete
  on the *other* side" — stop tracking it, do not restore it.
- **Reset state** (Preferences). Today the next start runs an initial sync, which
  refuses a non-empty folder, so the account stays stopped. *Recommendation:* reset
  keeps the `initial_sync` marker and starts with the three-way pass, where an
  unknown base means "compare contents; equal → link, different → conflict".
- **Conflict copies** are saved locally only and are excluded from upload.
  *Recommendation:* upload them, so the other side sees the conflict too.
- **Conflict winner by mtime** trusts the local clock and `cp -p`/archive mtimes.
  *Recommendation:* when both sides changed since the base, always keep both
  (conflict copy), never pick a winner silently.

## Consequences

- One mechanism instead of a dozen special cases; the planner's table tests become
  the specification of sync behaviour.
- A full pass needs the remote listing; it reuses the existing 5-minute cache, and
  dirty-marked paths are planned individually between full passes.
- Large refactor of `sync_engine.py`; the 2026-11-09 regression tests stay as its
  safety net.
