"""Debug mode: QGIS runs with the QGIS_DEBUG environment variable set
(Settings → Options → System → Environment). Pure Python, usable anywhere.

In debug mode the plugin shows its Verbose logging box and lets unexpected
errors propagate instead of logging them and carrying on.
"""
import os


def debug_mode() -> bool:
    return bool(os.environ.get("QGIS_DEBUG"))
