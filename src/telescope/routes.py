"""Compatibility shim — Flask telescope route handlers.

v0.2.0 §3.1: The actual route implementation still lives in
``src/telescope_routes`` while the 133 bare-except blocks are cleaned
up in v0.3.  This shim makes ``from src.telescope.routes import *``
work for any future callers that adopt the new path.

In v0.3 the code will be moved here and this comment removed.
"""

from src.telescope_routes import *  # noqa: F401, F403
from src.telescope_routes import register_routes, get_telescope_client  # noqa: F401
