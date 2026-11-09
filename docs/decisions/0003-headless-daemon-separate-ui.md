# ADR-0003 — A headless daemon, with the tray and preferences as separate clients

- **Status:** Proposed
- **Date:** 2026-11-09

## Context

The daemon runs inside GTK's main loop. It cannot start without a display: the
systemd unit hard-codes `DISPLAY=:0.0`, and the `--no-tray` option never worked
(removed on 2026-11-09). The preferences window runs in the same process and edits
the daemon's files underneath it: "Deauthenticate" deletes the state DB the daemon
has open, "Reset state" deletes the whole state directory, and saved settings only
apply after a restart. Thunar emblems are set by spawning one `gio set` process per
file and status change, on the sync worker thread.

## Proposal

1. `syncopath` (daemon) has no GTK dependency. It exposes a small D-Bus interface
   on the session bus: status and progress signals; `Pause`, `Resume`, `SyncNow`,
   `ReloadConfig`, `Deauthenticate(account)`, `ResetState(account)`.
2. `syncopath-tray` and `syncopath-prefs` are clients of that interface. Account
   and state changes go through the daemon, which stops the account, changes its
   files, and restarts it — no restart of the whole service.
3. Emblems are set in-process through GIO (`Gio.File.set_attributes_from_info`)
   from a dedicated low-priority thread, batched, never on the sync worker.
4. The systemd unit drops `DISPLAY`; the tray is started by the desktop session
   (XDG autostart).

## Consequences

- The daemon can run on a headless machine or over SSH, and can be tested without
  a display.
- One more process and a D-Bus interface to keep stable.
- Emblem updates stop slowing down large syncs.
