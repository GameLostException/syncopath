# Deletion — functional specification

Status: **Accepted by the owner, 2026-11-09. Implemented; tests in `tests/test_deletion.py`.** Answers ADR-0002's open question on deletion.

## Outcome

Once an account has been fully synced, a deletion on one side reaches the other side
according to the mode: immediately in unsafe mode, and only after the user confirms it in
safe mode. Every deletion SyncoPath performs moves the item to a bin (the Linux desktop
trash, or Drive's bin) and never empties a bin.

## Use cases

### UC-1 — Delete locally, safe mode
- **Actor:** the user, in their file manager or a terminal.
- **Main flow:** the user deletes a synced file. SyncoPath keeps the Drive copy and adds the
  file to the list of pending local deletions. The tray menu shows "Pending deletions (N)";
  choosing it opens a window listing the items, with one button that sends them all to
  Drive's bin. SyncoPath moves every listed item to Drive's bin and empties the list.
- **Alternative flows:**
  - The user deletes a whole folder: the folder is listed once, as one item.
  - The user puts the file back (restores it from the trash, or recreates it with the
    same content) before flushing: it leaves the list and is synced again. This is the
    way to keep a single item; the list itself has no per-item controls.
  - The file changed on Drive after the local deletion: it is downloaded again and the
    deletion is dropped (AC-16).
  - The file belongs to someone else: it is listed as "will stop syncing"; flushing stops
    syncing it and leaves Drive unchanged (AC-19).
- **Error flows:** moving an item to Drive's bin fails: it stays in the list with the
  error shown, and the others are still processed.

### UC-2 — Delete locally, unsafe mode
- **Actor:** the user.
- **Main flow:** the user deletes a synced file or folder. SyncoPath moves it to Drive's
  bin at the next sync, without asking.
- **Error flows:** Drive is unreachable: the deletion is retried like any other failed
  change (ADR-0001) and is not lost.

### UC-3 — Deleted on Drive, safe mode
- **Actor:** anyone with access to the file on Drive (the user on another device, a collaborator).
- **Main flow:** the file is moved to Drive's bin. SyncoPath leaves the local file in place
  and shows a "deleted on Drive" emblem on it. Later the user chooses "Delete files removed
  from Drive" from the tray menu, sees how many items will go to the trash, confirms, and
  SyncoPath moves them to the desktop trash.
- **Alternative flows:**
  - The user cancels the confirmation: nothing is deleted, the emblems stay.
  - The file is restored from Drive's bin: the emblem is removed and syncing resumes.
  - A whole folder is deleted on Drive: the folder and everything in it get the emblem.

### UC-4 — Deleted on Drive, unsafe mode
- **Actor:** anyone with access on Drive.
- **Main flow:** the file or folder is moved to Drive's bin; SyncoPath moves the local copy
  to the desktop trash at the next sync.

### UC-5 — Before the first full sync
- **Main flow:** while an account's initial sync has not completed, no deletion is
  propagated in either direction.

## Done when

- **AC-1** WHEN a synced local file is deleted in safe mode THE system SHALL keep the Drive file and list the deletion in the app, with the number of pending deletions.
- **AC-2** WHEN pending local deletions exist THE system SHALL offer a tray menu item that moves all of them to Drive's bin and clears the list.
- **AC-3** WHEN a synced local file or folder is deleted in unsafe mode THE system SHALL move it to Drive's bin.
- **AC-4** WHEN a synced file is moved to Drive's bin in safe mode THE system SHALL keep the local file and show a "deleted on Drive" emblem on it.
- **AC-5** WHEN files carry the "deleted on Drive" emblem THE system SHALL offer a tray menu item that, after a confirmation dialog stating the number of items, moves them to the desktop trash.
- **AC-6** WHEN a synced file is moved to Drive's bin in unsafe mode THE system SHALL move the local copy to the desktop trash.
- **AC-7** WHEN SyncoPath deletes anything locally THE system SHALL move it to the desktop trash (recoverable from the file manager), never delete it permanently.
- **AC-8** WHEN SyncoPath deletes anything on Drive THE system SHALL move it to Drive's bin, never delete it permanently.
- **AC-9** WHEN SyncoPath performs a deletion THE system SHALL NOT empty the desktop trash or Drive's bin.
- **AC-10** WHEN an account's initial sync has not completed THE system SHALL NOT propagate deletions in either direction.
- **AC-11** WHEN a listed or emblem-marked item is restored before the user flushes it THE system SHALL drop it from the pending deletions and sync it normally.
- **AC-12** WHEN the daemon restarts THE system SHALL keep the pending local deletions and the "deleted on Drive" marks.
- **AC-13** WHEN reconcile runs THE system SHALL NOT re-download a file that is a pending local deletion.
- **AC-14** WHEN SyncoPath starts or reconciles and an account's sync folder is missing or empty while its state lists synced files THE system SHALL report an error for that account and propagate no deletion.
- **AC-15** WHEN SyncoPath starts and a synced file is missing locally (folder present and not empty) THE system SHALL treat it as a local deletion (AC-1 or AC-3).
- **AC-16** WHEN a file deleted locally has changed on Drive since the last sync THE system SHALL download the Drive version again and not delete it on Drive, in both modes, and log it.
- **AC-17** WHEN a file deleted on Drive has unsynced local changes THE system SHALL keep the local file, upload it to Drive as a new file, and log it, in both modes.
- **AC-18** WHEN there are pending local deletions THE system SHALL show "Pending deletions (N)" in the tray menu, opening a window that lists them with a single action that sends them all to Drive's bin.
- **AC-19** WHEN a deleted local file is not owned by the user THE system SHALL stop syncing it and SHALL NOT change it on Drive, and the pending list SHALL say so.
- **AC-20** WHEN the user sends the pending local deletions to Drive THE system SHALL first show a confirmation dialog stating the number of items, and SHALL change nothing if the user cancels.

## Must not

- Delete anything permanently, on either side, or empty any bin.
- Change the meaning of the `safe_mode` key or its default (`true`).
- Restore locally deleted files in the background (today's reconcile does this in safe mode).
- Lose a pending deletion or a "deleted on Drive" mark across restarts.
- Change how files are synced when nothing is deleted.

## Sources of truth

- `safe_mode` key: [configuration reference](../ARCHITECTURE.md#configuration).
- Retry of failed changes: [ADR-0001](../decisions/0001-failed-sync-items-are-retried.md).
- Broader sync model this fits into: [ADR-0002](../decisions/0002-three-way-reconcile.md).
- Drive's bin is already used for Drive-side deletions (`DriveAPI.delete_file` trashes); local deletions are permanent today.
- User-facing description to update once built: [user manual](../USER_MANUAL.md), "Safe Mode".

## Open questions

1. ~~Deleted while SyncoPath was not running.~~ **Settled (owner):** AC-14, AC-15.
2. ~~Edited on one side, deleted on the other.~~ **Settled (owner):** AC-16, AC-17.
3. ~~Where the pending local deletions are listed.~~ **Settled (owner):** AC-18.
4. ~~Confirmation for flushing local deletions.~~ **Settled (owner):** both flushes confirm (AC-5, AC-20).
5. ~~Selective flush.~~ **Settled (owner):** all at once; no per-item or "keep all" controls.
   An item is kept by restoring it (AC-11).
6. ~~Files you don't own.~~ **Settled (owner):** AC-19.
7. ~~No desktop trash available.~~ **Settled (owner, 2026-11-09):** local deletions use the
   system (freedesktop) trash. The sync folder lives on the same filesystem as the trash.
   If moving to the trash ever fails, the item is kept and marked with an error; there is
   no fallback to permanent deletion (see Must not).
8. ~~Emblem.~~ **Settled (owner, 2026-11-09):** "deleted on Drive" is a purple circle with
   a white bin (`#8e44ad`, the one colour the emblem palette does not use yet). A red
   circle with a white cross stays the *sync error* emblem.
