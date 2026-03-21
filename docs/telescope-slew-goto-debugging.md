# Telescope Slew & GoTo Debugging Session
**Date:** 2026-03-20  
**Scope:** Seestar S50 manual slew (nudge) and GoTo alt/az  
**Status:** Partially fixed; core telemetry issue unresolved

---

## Symptoms

- GoTo alt/az does nothing (scope doesn't move)
- Manual slew (nudge) does nothing
- Telemetry alt/az frozen — doesn't change when scope is physically moved via iPhone app
- Displayed compass (~265°) and tilt (~1.4°) don't agree with actual scope orientation

---

## Root Causes Identified

### 1. `scope_get_equ_coord` returns a frozen stale GoTo target

**Smoking gun from debug log:**
```
scope_get_equ_coord → ra: 23.128889, dec: 19.647778  (identical every single poll)
Skyfield Sun az: 241.5°  vs  computed scope az: 270.9°  (29° apart)
```

`scope_get_equ_coord` was returning a position corresponding to the Sun's location in late February — not March 20 (vernal equinox, RA ≈ 0h). The value never changed regardless of physical scope movement.

**Effect:** Every alt/az computed from this frozen RA/Dec was stale. `manual_goto`'s servo loop had no useful position feedback and drove randomly or stalled. The GoTo verification check (pre/post alt comparison) always saw "no change" and always triggered the manual fallback.

**Likely causes (not yet confirmed):**
- `pi_set_time` may have failed during init → scope clock is ~28 days behind → LST is wrong → RA/Dec computed from motor steps is wrong
- Alternatively, `scope_get_equ_coord` returns the *last explicit GoTo target* rather than an encoder-based current position; in solar tracking mode the firmware uses ephemeris, not RA/Dec step counting, so this field stays stale

### 2. HTML nudge Up/Down buttons were wired to wrong angles

The `▲` button passed `angle=270` and `▼` passed `angle=90`. Given the `+180` firmware conversion in `speed_move`, pressing Up physically drove the scope **down** and vice versa. Left/Right were correct.

### 3. `manual_goto` angle formula used `−90` instead of `+90`

The servo loop computed direction using `(atan2(d_az, d_alt) − 90) % 360`. For a target above the current position (d_alt > 0) this produced angle=270, which after the `+180` firmware conversion became fw=90 (firmware DOWN). The scope drove away from above-horizon targets, stalled detection never triggered, and it timed out every time.

### 4. GoTo verification wait was too short, threshold too tight

1.5 s wait / 0.4° threshold — far too sensitive for an async firmware slew. This guaranteed the manual fallback was always triggered even when the firmware GoTo was working.

### 5. Solar tracking fighting manual slew

`speed_move` only switched to scenery mode when `_viewing_mode is None`. When in solar mode it sent `scope_speed_move` directly — the firmware's tracking loop immediately corrected back, making nudge appear to do nothing.

---

## Fixes Applied

### Round 1 — Angle bugs
| File | Change |
|------|--------|
| `templates/index.html` | Swapped `▲` and `▼` nudge angles: Up `270→90`, Down `90→270` |
| `src/seestar_client.py` | `_manual_goto_inner`: offset `−90 → +90` in angle formula |
| `src/telescope_routes.py` | GoTo verification wait `1.5s → 5.0s`, threshold `0.4° → 1.0°` |
| `src/seestar_client.py` | Corrected `speed_move` docstring (was "0=up", should be "90=up") |

### Round 2 — Telemetry and tracking interference
| File | Change |
|------|--------|
| `src/telescope_routes.py` | **Removed the broken 5s alt-change verification entirely** — pre/post alt from stale RA/Dec always looked unchanged, always falsely triggered manual fallback |
| `src/seestar_client.py` | `speed_move`: always stop tracking and switch to scenery mode before nudge (was: only when `_viewing_mode is None`) |
| `src/seestar_client.py` | Telemetry: in sun/moon mode, use **Skyfield ephemeris alt/az as primary** instead of stale `scope_get_equ_coord` conversion |

### Round 3 — GoTo consistency
| File | Change |
|------|--------|
| `src/telescope_routes.py` | GoTo alt/az now stops tracking, switches to scenery mode, then dispatches `manual_goto` in a background thread — same pattern as nudge |

---

## What Likely Still Doesn't Work

**`manual_goto` servo loop** — still relies on `get_telemetry()` for position feedback. In scenery mode the stale RA/Dec issue *may* be less severe (no tracking mode to freeze it), but this is unconfirmed. If `scope_get_equ_coord` still returns stale data in scenery mode, the servo loop remains blind and GoTo alt/az will not converge.

---

## Recommended Next Steps

### 1. Confirm `pi_set_time` is working
Add log output confirming the scope acknowledges the time sync at connect. If the scope's clock is ~28 days behind, all position math (RA/Dec ↔ alt/az) will be wrong by a large angle.

### 2. Check whether `scope_get_equ_coord` updates after `scope_speed_move` in scenery mode
Move the scope via nudge, then immediately call `scope_get_equ_coord`. If the RA/Dec changed, `manual_goto` will work. If still frozen, the servo loop needs a different position source.

### 3. Try `scope_get_horiz_coord`
Check if the Seestar firmware supports this command (seestar_alp reference code doesn't use it, but it may exist in newer firmware). If it works, it returns direct alt/az without the broken RA/Dec conversion chain and can replace `scope_get_equ_coord` in `get_telemetry`.

### 4. Parse position from unsolicited Event messages
The firmware broadcasts Event messages continuously. Some events likely carry actual mount position (e.g. `MotionInfo`, `ScopeGoto`, or similar). Currently Flymoon discards all Events except viewing-mode and recording changes. Capturing a position event in `_handle_event` and caching it would give reliable real-time position for the `manual_goto` servo loop.

### 5. Fallback: use balance sensor + compass as position proxy
`tilt_angle` (from `balance_sensor`) tracks arm elevation angle; `compass_direction` (from `compass_sensor`) tracks azimuth. They're imprecise but physically updated as the scope moves. Could serve as a rough position source for `manual_goto` when RA/Dec telemetry is stale.

---

## Reference

- Debug log: `/Users/Tom/flymoon/.cursor/debug-616e1a.log`
- Protocol reference: `SEESTAR_CONNECTION_IMPROVEMENTS.md`, `architecture.svg`
- seestar_alp reference: `https://github.com/smart-underworld/seestar_alp/blob/main/device/seestar_device.py`
- Key files changed: `src/seestar_client.py`, `src/telescope_routes.py`, `templates/index.html`
