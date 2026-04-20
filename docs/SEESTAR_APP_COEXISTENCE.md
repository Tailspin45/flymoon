# Seestar App Coexistence: `master_cli` Contention Model

The Seestar S50 firmware (>2300) enforces a **single master client** for
motor commands. Exactly one connected client can issue `scope_speed_move`,
`iscope_start_view` with a target mode, focuser commands, and similar
motion-class commands at a time. The iPhone Seestar app and Zipcatcher can
both stay connected; whoever last sent `set_setting {"master_cli": true}`
owns motion.

Symptoms of a **lost master race**:

- Motion commands return successfully over TCP but the scope does not move.
- `start_solar_mode` / `start_lunar_mode` dispatches OK but `get_view_state`
  still reports the previous mode.
- Telemetry looks live (position, telemetry ticks) but nudges are silently
  dropped.

## How Zipcatcher handles this

1. **Init reclaim.** `_send_init_sequence` calls `_reclaim_master()` and waits
   up to 2 s for the ack. Result is logged at INFO (held) or WARNING (contested).
2. **Per-command reclaim.** `start_solar_mode`, `start_lunar_mode`,
   `start_scenery_mode`, and `autofocus` all invoke `_reclaim_master()` before
   issuing motion. Failure is logged as
   `[Master] Reclaim failed before <cmd> — iPhone Seestar app likely holds master; motion may be ignored`.
3. **Heartbeat probe.** Every 30 s the heartbeat loop re-probes master and
   logs on **state transitions only** (held → contested, contested → held) —
   never on steady state.
4. **Status surface.** `client.master_state` is exposed on `get_status()` as
   one of `held | contested | unknown` so UI and diagnostic scripts can see
   who is winning.
5. **Mode verification.** After `iscope_start_view`, Zipcatcher polls
   `get_view_state` for up to 5 s and confirms the returned mode matches the
   requested target (accepting firmware aliases `solar_sys` / `lunar`).
6. **Tracking verification.** After mode is confirmed, ALPACA `Tracking` is
   queried; if False, Zipcatcher retries `set_tracking(True)` once.

## Recovery playbook

If Sun / Moon acquisition stops working during a session:

1. Check the log for `[Master] Reclaim failed` or
   `[Master] master_cli now contested`.
2. Force-quit the iPhone Seestar app (background the app is not enough —
   swipe it away from the app switcher).
3. Trigger any mode change in Zipcatcher (solar mode, lunar mode, or
   scenery). The per-command reclaim should win and return `master_state=held`.
4. Confirm via `/telescope/status` that `master_state` is `held` before
   retrying the failed GoTo / acquisition.

## Future UI work

A banner rendering `master_state=contested` in the telescope page is tracked
as a follow-up — this doc and the status payload are the current surface.
