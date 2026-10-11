"""Debug mode: QGIS runs with the QGIS_DEBUG environment variable set.

Set it in Settings → Options → System → Environment, then restart QGIS.
Pure Python, usable anywhere.

In debug mode the plugin shows its Verbose logging box and lets unexpected
errors propagate instead of logging them and carrying on.
"""
from __future__ import annotations

import os


def debug_mode() -> bool:
    """Tell whether QGIS runs in debug mode.

    Returns:
        True if the QGIS_DEBUG environment variable is set and not empty.
    """
    return bool(os.environ.get("QGIS_DEBUG"))
