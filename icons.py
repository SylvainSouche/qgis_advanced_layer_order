"""Icons for Advanced Layer Order.

Toolbar and group icons are SVGs shipped in ``icons/``; layer icons come
from QGIS (``QgsIconUtils.iconForLayer``), with a bundled generic icon for
layers QGIS no longer knows.
"""
from __future__ import annotations

import os

from qgis.core import QgsIconUtils, QgsMapLayer, QgsProject
from qgis.PyQt.QtGui import QIcon

_ICONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")


def bundled_icon(filename: str) -> QIcon:
    """Load an icon shipped with the plugin.

    Args:
        filename: File name inside the plugin's ``icons/`` folder.

    Returns:
        The icon, or an empty QIcon if the file is missing.
    """
    path = os.path.join(_ICONS_DIR, filename)
    return QIcon(path) if os.path.isfile(path) else QIcon()


def icon_group() -> QIcon:
    """Give the icon shown next to groups in the tree.

    Returns:
        The folder icon.
    """
    return bundled_icon("folder.svg")


def icon_add_group() -> QIcon:
    """Give the Add Group toolbar / menu icon.

    Returns:
        The icon.
    """
    return bundled_icon("add_group.svg")


def icon_remove_group() -> QIcon:
    """Give the Remove Group toolbar / menu icon.

    Returns:
        The icon.
    """
    return bundled_icon("remove_group.svg")


def icon_rename_group() -> QIcon:
    """Give the Rename Group toolbar / menu icon.

    Returns:
        The icon.
    """
    return bundled_icon("rename_group.svg")


def icon_move_up() -> QIcon:
    """Give the Move up toolbar / menu icon.

    Returns:
        The icon.
    """
    return bundled_icon("move_up.svg")


def icon_move_down() -> QIcon:
    """Give the Move down toolbar / menu icon.

    Returns:
        The icon.
    """
    return bundled_icon("move_down.svg")


def icon_undo() -> QIcon:
    """Give the Undo toolbar icon.

    Returns:
        The icon.
    """
    return bundled_icon("undo.svg")


def icon_redo() -> QIcon:
    """Give the Redo toolbar icon.

    Returns:
        The icon.
    """
    return bundled_icon("redo.svg")


def icon_for_layer(layer: QgsMapLayer | None) -> QIcon:
    """Give QGIS's own icon for a layer's type and geometry.

    Args:
        layer: The layer, or None when it is no longer in the project.

    Returns:
        QGIS's icon, or the bundled generic layer icon.
    """
    if layer is not None:
        icon = QgsIconUtils.iconForLayer(layer)
        if not icon.isNull():
            return icon
    return bundled_icon("layer.svg")


def icon_for_layer_id(layer_id: str) -> QIcon:
    """Give the icon for a layer of the current project.

    Args:
        layer_id: The QGIS layer id.

    Returns:
        See :func:`icon_for_layer`.
    """
    return icon_for_layer(QgsProject.instance().mapLayer(layer_id))
