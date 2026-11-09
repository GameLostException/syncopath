# ADR-0001 — Failed sync items are retried, and recorded when retries run out

- **Status:** Accepted
- **Date:** 2026-11-09

## Context

The engine caught every exception per item and only logged it. The worker's retry
with backoff therefore never ran, while the Changes API page token had already moved
on. A transient network error during an upload, or while checking ownership of a new
remote file, lost that change for good. Error classification matched message text,
so a `ConnectionResetError` was filed as "permanently unsyncable".

## Decision

1. `drive_api.is_transient(e)` and `drive_api.is_not_found(e)` classify errors by type,
   HTTP status and reason. Nothing else in the code matches error text for that purpose.
2. `handle_local_changes` and `handle_remote_changes` still process the whole batch,
   then raise `SyncBatchError(failed_items)` when some items failed.
3. The worker retries **only the failed items**, with its existing backoff
   (3 attempts in total: retried after 30 s, then after 1 min).
4. An upload that fails is recorded as `sync_status='error'` with the
   *previous* `local_md5`. Reconcile (startup and every 10 min) re-uploads any error
   row whose local hash differs from `local_md5`, and re-downloads the others.
5. A Drive lookup that fails transiently raises; it never falls back to a guess
   (no "assume shared", no partial path).

## Consequences

- An item is retried three times, then reconcile becomes the backstop for uploads
  and downloads.
- A **remote** change that still fails after three attempts, and was never
  recorded in the state DB (for example a new file whose ownership could not be
  checked), is only recovered by the startup scan of remote files. ADR-0002
  (three-way reconcile) removes that gap.
