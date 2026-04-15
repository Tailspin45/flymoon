"""src.telescope — telescope control sub-package.

v0.2.0 §3.1 mechanical split of src/telescope_routes.py.

Sub-modules:
  debug_log        — NDJSON agent debug logger
  motor_state      — GoTo/nudge mutex + _CtrlState enum
  recorder_wiring  — TransitRecorder scheduling glue
  routes           — compatibility shim (real code in src/telescope_routes.py)

NOTE: This __init__.py intentionally does NOT re-export from telescope_routes.
Doing so creates a circular import because telescope_routes.py imports from
sub-modules of this package.  Callers should import src.telescope_routes or
src.telescope.<submodule> directly.
"""
