"""Resolve HERMES_HOME for standalone skill scripts.

Skill scripts may run outside the Hermes process (e.g. system Python,
nix env, CI) where ``hermes_constants`` is not importable.  This module
provides the same ``get_hermes_home()`` and ``display_hermes_home()``
contracts as ``hermes_constants`` without requiring it on ``sys.path``.

When ``hermes_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``hermes_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``HERMES_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from hermes_constants import display_hermes_home as display_hermes_home
    from hermes_constants import get_hermes_home as get_hermes_home
except (ModuleNotFoundError, ImportError):

    def get_hermes_home() -> Path:
        """Return the Workspace state home directory.

        Neutral-first order: ``GOOGLE_WORKSPACE_HOME``, then legacy
        ``HERMES_HOME`` (kept so old harness configs keep working), then the
        Hermes default. Canonical home sets GOOGLE_WORKSPACE_HOME always."""
        for var in ("GOOGLE_WORKSPACE_HOME", "HERMES_HOME"):
            val = os.environ.get(var, "").strip()
            if val:
                return Path(val)
        try:
            from hermes_constants import get_hermes_home as _ghh

            return _ghh()
        except (ModuleNotFoundError, ImportError):
            return Path.home() / ".hermes"

    def display_hermes_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``hermes_constants.display_hermes_home()``."""
        home = get_hermes_home()
        try:
            return "~/" + str(home.relative_to(Path.home()))
        except ValueError:
            return str(home)
