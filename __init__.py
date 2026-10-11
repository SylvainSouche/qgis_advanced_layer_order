"""Advanced Layer Order — QGIS plugin entry point.

QGIS imports this package and calls :func:`classFactory`; everything else
lives in :mod:`.plugin` (wiring) and the MVC modules it assembles.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.gui import QgisInterface

    from .plugin import AdvancedLayerOrderPlugin


def classFactory(iface: QgisInterface) -> AdvancedLayerOrderPlugin:
    """Create the plugin instance (QGIS's required entry point).

    The import is deferred so that loading the package costs nothing until
    QGIS actually starts the plugin.

    Args:
        iface: QGIS's interface to the running application.

    Returns:
        The plugin object; QGIS then calls its ``initGui()``.
    """
    from .plugin import AdvancedLayerOrderPlugin
    return AdvancedLayerOrderPlugin(iface)
