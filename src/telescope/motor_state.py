"""Motor control state machine for the Seestar telescope.

Owns the GoTo/nudge mutex and the state enum that serialises telescope motion.
Extracted from src/telescope_routes.py (v0.2.0 §3.1 mechanical split).

Usage in telescope_routes.py / src/telescope/routes.py:

    from src.telescope.motor_state import ctrl as _motor_ctrl, _CtrlState

    with _motor_ctrl.lock:
        _motor_ctrl.state = _CtrlState.SLEWING
"""

import threading
from enum import Enum


class _CtrlState(Enum):
    IDLE = "idle"
    SLEWING = "slewing"
    NUDGING = "nudging"
    GOTO_RESUMING = "goto_resuming"


class _MotorCtrl:
    """Mutable container so motor state can be shared safely across modules.

    Using an object attribute avoids the cross-module ``global`` keyword
    problem: mutating ``ctrl.state`` in any importing module is visible
    to all others because the object reference itself is shared.
    """

    def __init__(self) -> None:
        self.state: _CtrlState = _CtrlState.IDLE
        self.lock: threading.Lock = threading.Lock()
        # Whether the active nudge was started while solar/lunar tracking was on.
        # Used to decide whether to re-enable ALPACA tracking after nudge/stop.
        self.pre_nudge_tracking: bool = False


# Singleton shared by telescope_routes and any future sub-modules.
ctrl = _MotorCtrl()
