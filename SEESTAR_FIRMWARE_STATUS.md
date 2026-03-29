# Seestar Firmware Compatibility — Status Report

_Last updated: 2026-03-25_

---

## Background

The Seestar S50 firmware was updated (believed to be firmware >2300, possibly >2582)
and several behaviours changed that broke Flymoon's telescope control.

---

## What We Know

### Firmware behaviour (confirmed by testing)

| Observation | Evidence |
|---|---|
| TCP port 4700 still accepts connections | `nc` connects; app socket stays ESTABLISHED |
| Scope sends `PiStatus` push events (temp) | Logged by reader thread on connect |
| **All query/response commands time out** | Tested `scope_get_horiz_coord`, `get_view_state`, `scope_get_equ_coord`, `get_device_state`, `pi_is_verified`, `get_setting` — all timeout after 5–8 s |
| **Scope sends zero responses** — even to control commands | Raw socket reader receives nothing back |
| `scope_speed_move` does not produce movement | Tested at speed=4000, dur=10 s; no physical movement |
| `iscope_start_view mode=sun` appears to work | Scope is in solar tracking on startup |
| UDP port 4720 accepts `scan_iscope` broadcast | No connection refused; no response received |

### ALP reference findings ([seestar_alp](https://github.com/smart-underworld/seestar_alp))

- ALP uses a **dedicated background reader thread** (`watch_events`) that continuously
  drains the socket and populates a `response_dict`.
- ALP injects `"verify": true` into command params for firmware >2582 (but NOT for
  firmware ≥2706 with dict params — those are "SSL-authenticated").
- ALP sends `set_setting {"master_cli": True}` after connect to claim master control.
  Without being master, the scope silently ignores motor commands.
- ALP sends a **UDP `scan_iscope` broadcast to port 4720** before the TCP connect to
  "satisfy seestar's guest mode to gain control properly."
- The scope sends a `Client` event with `{"is_master": true/false}` confirming whether
  the connecting client has been granted master status.
- ALP's `move_scope` calls `send_message_param_sync` (waits for response), implying the
  scope DOES respond to `scope_speed_move` in a working ALP session.

### Best hypothesis for why movement fails

The Seestar iPhone app, if open in background, holds the **master client** role.
The scope sends `{"Event": "Client", "is_master": false}` to Flymoon's connection.
Without a reader thread, Flymoon never sees this event, never retries claiming master,
and all motor commands are silently dropped by the firmware.

---

## What Works

| Feature | Status |
|---|---|
| Auto-discovery (UDP scan) | ✅ Working |
| TCP connection + auto-connect on startup | ✅ Working |
| Solar tracking mode (`start_solar_mode`) | ✅ Working — scope tracks sun |
| Lunar tracking mode (`start_lunar_mode`) | ✅ Untested but same code path |
| Scenery mode (`start_scenery_mode`) | ✅ Command sends; `_viewing_mode` updated |
| Recording start/stop | ✅ Fire-and-forget; assumed working |
| Alt/Az pointing readout (Skyfield) | ✅ Working in sun/moon mode |
| GoTo alt/az → converts to RA/Dec + `iscope_start_view mode=star` | ✅ Command dispatches |
| Heartbeat (keep-alive ping) | ✅ `pi_is_verified` fire-and-forget |
| Background reader thread (event drain) | ✅ Code added; logs `Client` event |

---

## What Does Not Work

| Feature | Status | Root Cause |
|---|---|---|
| Manual nudge (`scope_speed_move`) | ❌ No movement | Scope ignores — not master, or firmware change |
| GoTo alt/az physical slew | ❌ No movement | Same — `iscope_start_view mode=star` dispatches but scope may not execute |
| `iscope_stop_view` confirmation | ❌ Unknown | Fire-and-forget; no way to confirm |
| Any query command | ❌ All timeout | Firmware no longer sends responses to queries |
| Alt/Az from scope hardware | ❌ Not available | `scope_get_horiz_coord` times out; using Skyfield instead |
| Viewing mode sync from scope | ❌ Not available | `get_view_state` times out; tracking via internal state |

---

## Code Changes Made (This Session)

### `src/seestar_client.py`
- `stop_view_mode()` — both `iscope_stop_view` calls now use `expect_response=False`
  (was timing out and corrupting the socket)
- `_ping()` — replaced `get_view_state` (which timed out, held socket lock for 5 s)
  with `pi_is_verified` fire-and-forget
- `_send_init_sequence()` — added:
  - UDP `scan_iscope` to scope IP:4720 before TCP connect
  - `set_setting {"master_cli": True}` to claim master
  - `set_setting {"cli_name": "Flymoon/hostname"}` to identify client
- `_reader_loop()` — new background thread that drains the socket continuously,
  handles all push events, and logs `Client` master status
- `_reader_thread` started in `_do_connect()`, stopped in `disconnect()`
- `_is_master` flag tracks master status from `Client` events

### `src/telescope_routes.py`
- `telescope_position()` — returns 200 (not 503) with `alt: null` when not in
  sun/moon mode (prevents noisy browser console errors)
- `telescope_goto()` alt/az path — replaced closed-loop `manual_goto()` (broken;
  needed `scope_get_horiz_coord`) with `goto_altaz()` (native firmware GoTo)
- Added `telescope_debug_cmd()` endpoint (`POST /telescope/debug/cmd`) for raw
  command testing without restarting the app

### `static/telescope.js`
- GoTo response handling cleaned up (removed `manual_slew` flag check)

---

## Next Steps / Hypotheses to Test

1. **Force-quit the Seestar iPhone app** completely before starting Flymoon.
   If the terminal shows `[Reader] We are master client` after connect, and nudge
   then works, the iPhone app was stealing master control.

2. **Check `Client` event in reader logs.** If `[Reader] Scope says we are NOT master`
   appears, we need to retry `set_setting master_cli: True` until we get master,
   or implement the full ALP guest-mode handshake.

3. **Verify `iscope_start_view mode=sun` actually causes movement** vs the scope
   booting into sun mode on its own. Switch the scope to scenery mode from the
   iPhone app, then disconnect the iPhone app, then reconnect Flymoon — if the scope
   switches back to sun mode, our command works.

4. **Consider `verify` injection** for list-param commands (ALP always appends
   `"verify"` to list params for firmware >2582). Not needed for dict params on
   firmware ≥2706.

5. If master_cli works but `scope_speed_move` still fails, the speed scale may have
   changed — ALP uses 4000 as the normal speed vs our 50–100.

---

## Reference

- [Seestar ALP source](https://github.com/smart-underworld/seestar_alp) — `device/seestar_device.py`
- Key methods: `guest_mode_init()`, `transform_message_for_verify()`, `is_client_master()`,
  `move_scope()`, `reconnect()`
