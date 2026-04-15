"""Root-level pytest configuration.

Keeps pytest from ever collecting inside archive/ even if the directory is
created in the future.  The norecursedirs setting in pytest.ini provides a
second layer of protection; this conftest adds the path to collect_ignore so
it is excluded regardless of how pytest is invoked.

audit finding #11: archive/ tree must never be importable/collectable.
"""

from pathlib import Path

collect_ignore_glob = [str(Path(__file__).parent / "archive" / "**" / "*.py")]
