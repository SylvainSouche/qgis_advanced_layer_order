from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qgis.gui import QgisInterface

    from .plugin import AdvancedLayerOrderPlugin


def classFactory(iface: QgisInterface) -> AdvancedLayerOrderPlugin:
    from .plugin import AdvancedLayerOrderPlugin
    return AdvancedLayerOrderPlugin(iface)
