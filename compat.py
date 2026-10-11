"""Qt5 / Qt6 differences that qgis.PyQt does not paper over.

The plugin runs on QGIS 3.40 / 3.44 (Qt 5.15, PyQt5) and QGIS 4.x (Qt 6,
PyQt6). QGIS's own ``qgis.PyQt`` layer already maps most moved classes
(e.g. ``QAction`` is importable from ``QtGui`` on both); the rest is here.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

try:   # Qt 6: the undo framework moved to QtGui
    from qgis.PyQt.QtGui import QUndoCommand, QUndoStack
except ImportError:   # Qt 5
    from qgis.PyQt.QtWidgets import QUndoCommand, QUndoStack

if TYPE_CHECKING:
    from qgis.PyQt.QtCore import QPoint
    from qgis.PyQt.QtGui import QDropEvent, QMouseEvent

__all__ = ["QUndoCommand", "QUndoStack", "event_pos"]


def event_pos(event: QMouseEvent | QDropEvent) -> QPoint:
    """Give a mouse or drop event's position in widget coordinates.

    Qt 6 replaced ``pos()`` with ``position()`` (a QPointF).

    Args:
        event: The mouse or drop event.

    Returns:
        The position as an integer QPoint, on Qt 5 and Qt 6 alike.
    """
    if hasattr(event, "position"):   # Qt 6
        return event.position().toPoint()
    return event.pos()               # Qt 5
