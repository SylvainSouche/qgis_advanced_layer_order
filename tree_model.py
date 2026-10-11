"""LayerOrderItemModel — the Qt item model behind the tree (part of the View).

It presents what the ViewController last rendered (node dicts) to a
QTreeView, and turns Qt's editing entry points into intents:

* ``dropMimeData`` → ``drop_intent(moving_ids, target_id, position)``
* ``setData(CheckStateRole)`` → ``check_intent(item_id, checked)``

Both return False, so Qt never changes anything itself (a drop moves no
row, a click flips no checkbox). The content only changes through
``render()``, ``set_name()`` and ``set_visible()``.

``render()`` keeps item identity: when the set of ids is unchanged it is a
layout change with persistent indexes remapped by id, so the view keeps
its selection, expansion and scroll position on its own.
"""
from __future__ import annotations

import json
from collections.abc import Iterator

from qgis.PyQt.QtCore import QAbstractItemModel, QMimeData, QModelIndex, QObject, Qt, pyqtSignal
from qgis.PyQt.QtGui import QIcon

from .icons import icon_group
from .model import TYPE_GROUP, TYPE_LAYER

# Semantic drop positions
DROP_ON = "on"          # onto an item
DROP_ABOVE = "above"    # just above an item, same parent
DROP_BELOW = "below"    # just below an item, same parent
DROP_END = "end"        # empty area below the last row → bottom of the top level

ROLE_ID = Qt.ItemDataRole.UserRole + 1     # data role: the layer / group id
ROLE_TYPE = Qt.ItemDataRole.UserRole + 2   # data role: TYPE_LAYER or TYPE_GROUP

MIME_IDS = "application/x-advanced-layer-order-ids"   # drag payload: JSON list of ids

_FLAGS = (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
          | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled)


class _Item:
    """One rendered row: plain data, never restructured.

    The whole tree of items is replaced on every render, so each item's row
    is fixed at construction. A group's checkbox comes from its visible/total
    layer counts, kept up to date by ``LayerOrderItemModel.set_visible``;
    both reads are O(1).
    """

    __slots__ = ("children", "icon", "id", "layers", "name", "parent", "row_",
                 "shown", "type", "visible")

    def __init__(self, node: dict | None, parent: _Item | None) -> None:
        """Build an item and, recursively, its children from a rendered node.

        Args:
            node: The node dict (see ``LayerOrderView.render``), or None for the
                invisible root.
            parent: The parent item, or None for the root.
        """
        self.parent = parent
        node = node or {"type": TYPE_GROUP, "id": "", "name": ""}
        self.id: str = node["id"]
        self.type: str = node["type"]
        self.name: str = node.get("name", "")
        self.visible: bool = bool(node.get("visible", True))
        self.icon: QIcon | None = node.get("icon")
        self.children = [_Item(ch, self) for ch in node.get("children", [])]
        self.row_ = 0
        for i, ch in enumerate(self.children):
            ch.row_ = i
        if self.type == TYPE_LAYER:
            self.layers, self.shown = 1, int(self.visible)
        else:
            self.layers = sum(ch.layers for ch in self.children)
            self.shown = sum(ch.shown for ch in self.children)

    def row(self) -> int:
        """Give the item's position among its siblings.

        Returns:
            The row, fixed at construction.
        """
        return self.row_

    def walk(self) -> Iterator[_Item]:
        """Iterate over the descendants, pre-order (display order).

        Yields:
            Each item below this one.
        """
        for ch in self.children:
            yield ch
            yield from ch.walk()

    def check_state(self) -> Qt.CheckState:
        """Give the checkbox state shown for this row.

        Returns:
            A layer's own visibility; for a group, Checked / Unchecked /
            PartiallyChecked from the visible/total layer counts (an empty group
            shows Checked).
        """
        if self.type == TYPE_LAYER:
            return Qt.CheckState.Checked if self.visible else Qt.CheckState.Unchecked
        if self.layers and not self.shown:
            return Qt.CheckState.Unchecked
        if 0 < self.shown < self.layers:
            return Qt.CheckState.PartiallyChecked
        return Qt.CheckState.Checked


class LayerOrderItemModel(QAbstractItemModel):
    """Read-only presentation of the rendered tree; edits become intents."""

    drop_intent = pyqtSignal(list, str, str)   # moving ids, target id, DROP_* position
    check_intent = pyqtSignal(str, bool)       # item id, checked

    def __init__(self, parent: QObject | None = None) -> None:
        """Start with an empty tree.

        Args:
            parent: Owning QObject (the dock).
        """
        super().__init__(parent)
        self._root = _Item(None, None)
        self._by_id: dict[str, _Item] = {}

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def render(self, nodes: list[dict]) -> None:
        """Show a new tree.

        When the set of ids is unchanged this is a layout change with persistent
        indexes remapped by id, so the view keeps its selection, expansion and
        scroll on its own; otherwise it is a model reset.

        Args:
            nodes: Top-level node dicts (shape: ``LayerOrderView.render``).
        """
        new_root = _Item({"type": TYPE_GROUP, "id": "", "children": nodes}, None)
        new_by_id = {it.id: it for it in new_root.walk()}
        if new_by_id.keys() == self._by_id.keys():
            # Same items, new arrangement/data: keep identity for the view
            self.layoutAboutToBeChanged.emit()
            old = self.persistentIndexList()
            ids = [ix.data(ROLE_ID) for ix in old]
            self._root, self._by_id = new_root, new_by_id
            self.changePersistentIndexList(old, [self.index_of(i) for i in ids])
            self.layoutChanged.emit()
            self._emit_data_changed(self._root)
        else:
            self.beginResetModel()
            self._root, self._by_id = new_root, new_by_id
            self.endResetModel()

    def set_name(self, item_id: str, name: str) -> None:
        """Show a new name for one row.

        Args:
            item_id: Layer or group id.
            name: The name to display.
        """
        item = self._by_id.get(item_id)
        if item is not None:
            item.name = name
            ix = self.index_of(item_id)
            self.dataChanged.emit(ix, ix, [Qt.ItemDataRole.DisplayRole])

    def set_visible(self, layer_id: str, visible: bool) -> None:
        """Show a layer's visibility and the derived state of its groups.

        The groups' visible counts are updated along the ancestors: O(depth).

        Args:
            layer_id: The layer.
            visible: Its effective visibility in QGIS.
        """
        item = self._by_id.get(layer_id)
        if item is None or item.visible == bool(visible):
            return
        item.visible = bool(visible)
        delta = 1 if item.visible else -1
        node = item
        while node is not None:
            node.shown += delta
            node = node.parent
        while item is not None and item is not self._root:
            ix = self.index_of(item.id)
            self.dataChanged.emit(ix, ix, [Qt.ItemDataRole.CheckStateRole])
            item = item.parent

    def index_of(self, item_id: str) -> QModelIndex:
        """Give the Qt index of an item.

        Args:
            item_id: Layer or group id.

        Returns:
            Its index, or an invalid index if it is not shown.
        """
        item = self._by_id.get(item_id)
        if item is None:
            return QModelIndex()
        return self.createIndex(item.row(), 0, item)

    def nodes_in_order(self) -> list[str]:
        """List every shown id in display (pre-order) order.

        Returns:
            The ids, top to bottom.
        """
        return [it.id for it in self._root.walk()]

    def _emit_data_changed(self, parent: _Item) -> None:
        """Tell the view that every row under `parent` may have new data.

        Args:
            parent: The item whose subtree changed (the root after a render).
        """
        if parent.children:
            first = self.createIndex(0, 0, parent.children[0])
            last = self.createIndex(len(parent.children) - 1, 0, parent.children[-1])
            self.dataChanged.emit(first, last)
        for ch in parent.children:
            self._emit_data_changed(ch)

    # ------------------------------------------------------------------
    # QAbstractItemModel — structure
    # ------------------------------------------------------------------
    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        """Give the index of a child row (QAbstractItemModel).

        Args:
            row: Row under `parent`.
            column: Always 0 (one column).
            parent: Parent index; invalid for the top level.

        Returns:
            The index, or an invalid index if there is no such row.
        """
        if column != 0 or not self.hasIndex(row, column, parent):
            return QModelIndex()
        return self.createIndex(row, 0, self._item(parent).children[row])

    def parent(self, index: QModelIndex | None = None) -> QModelIndex | QObject | None:
        """Give an index's parent (QAbstractItemModel), or the QObject parent.

        Qt uses the same name for ``QObject.parent()``: called without an index
        it answers that one.

        Args:
            index: A Qt index, or None for the QObject parent.

        Returns:
            The parent index (invalid for top-level rows), or the QObject parent.
        """
        if index is None:
            return super().parent()  # QObject.parent()
        if not index.isValid():
            return QModelIndex()
        p = index.internalPointer().parent
        if p is None or p is self._root:
            return QModelIndex()
        return self.createIndex(p.row(), 0, p)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        """Give the number of child rows (QAbstractItemModel).

        Args:
            parent: Parent index; invalid for the top level.

        Returns:
            The number of children.
        """
        return 0 if parent.column() > 0 else len(self._item(parent).children)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        """Give the number of columns (QAbstractItemModel).

        Args:
            parent: Unused: every row has one column.

        Returns:
            1.
        """
        return 1

    def _item(self, index: QModelIndex) -> _Item:
        """Give the item behind an index.

        Args:
            index: A Qt index of this model.

        Returns:
            Its item, or the invisible root for an invalid index.
        """
        return index.internalPointer() if index.isValid() else self._root

    # ------------------------------------------------------------------
    # QAbstractItemModel — data
    # ------------------------------------------------------------------
    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        """Give a row's data for a role (QAbstractItemModel).

        Args:
            index: The row.
            role: Display (name), Decoration (icon), CheckState (visibility),
                ROLE_ID or ROLE_TYPE.

        Returns:
            The value, or None for other roles and invalid indexes.
        """
        if not index.isValid():
            return None
        item: _Item = index.internalPointer()
        if role == Qt.ItemDataRole.DisplayRole:
            return item.name
        if role == Qt.ItemDataRole.DecorationRole:
            return icon_group() if item.type == TYPE_GROUP else item.icon
        if role == Qt.ItemDataRole.CheckStateRole:
            return item.check_state()
        if role == ROLE_ID:
            return item.id
        if role == ROLE_TYPE:
            return item.type
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        # The root accepts drops too (empty area below the last row)
        """Give what a row allows (QAbstractItemModel).

        Args:
            index: The row; invalid for the empty area below the last row.

        Returns:
            Enabled, selectable, checkable, draggable and droppable for rows;
            drop-only for the empty area.
        """
        return _FLAGS if index.isValid() else Qt.ItemFlag.ItemIsDropEnabled

    def setData(self, index: QModelIndex, value: object, role: int = Qt.ItemDataRole.EditRole) -> bool:
        """Report a checkbox click as an intent, and change nothing (QAbstractItemModel).

        The checkbox keeps showing the real state until QGIS answers and the
        ViewController renders it.

        Args:
            index: The row clicked.
            value: Qt's new value (unused: the wish is the opposite of the state).
            role: Only CheckStateRole is handled.

        Returns:
            False, so Qt does not update the row itself.
        """
        if index.isValid() and role == Qt.ItemDataRole.CheckStateRole:
            item: _Item = index.internalPointer()
            self.check_intent.emit(item.id, item.check_state() != Qt.CheckState.Checked)
        return False

    # ------------------------------------------------------------------
    # QAbstractItemModel — drag and drop
    # ------------------------------------------------------------------
    def supportedDragActions(self) -> Qt.DropAction:
        """Tell Qt that rows are only ever moved (QAbstractItemModel).

        Returns:
            MoveAction.
        """
        return Qt.DropAction.MoveAction

    def supportedDropActions(self) -> Qt.DropAction:
        """Tell Qt that drops are only ever moves (QAbstractItemModel).

        Returns:
            MoveAction.
        """
        return Qt.DropAction.MoveAction

    def mimeTypes(self) -> list[str]:
        """Name the drag payload format (QAbstractItemModel).

        Returns:
            The plugin's own MIME type: drags from elsewhere are refused.
        """
        return [MIME_IDS]

    def mimeData(self, indexes: list[QModelIndex]) -> QMimeData:
        """Pack the dragged rows' ids (QAbstractItemModel).

        Args:
            indexes: The selected indexes being dragged.

        Returns:
            MIME data holding the ids as JSON, without duplicates.
        """
        ids = list(dict.fromkeys(ix.data(ROLE_ID) for ix in indexes if ix.isValid()))
        mime = QMimeData()
        mime.setData(MIME_IDS, json.dumps(ids).encode("utf-8"))
        return mime

    @staticmethod
    def _moving_ids(mime: QMimeData) -> list[str]:
        """Unpack the dragged ids.

        Args:
            mime: The drag payload.

        Returns:
            The ids, or [] if the payload is missing or not ours.
        """
        if mime is None or not mime.hasFormat(MIME_IDS):
            return []
        try:
            return [str(i) for i in json.loads(bytes(mime.data(MIME_IDS)).decode("utf-8"))]
        except ValueError:
            return []

    def canDropMimeData(self, mime: QMimeData, action: Qt.DropAction, row: int, column: int,
                        parent: QModelIndex) -> bool:
        """Tell whether a drop is allowed here (QAbstractItemModel).

        Args:
            mime: The drag payload.
            action: The drop action (always a move).
            row: Row under `parent` the drop lands before; -1 for "onto".
            column: Unused.
            parent: The group (or invalid: top level) the drop lands in.

        Returns:
            False for foreign payloads and for drops into a dragged item or its
            subtree.
        """
        moving = set(self._moving_ids(mime))
        if not moving:
            return False
        item = self._item(parent)
        while item is not None and item is not self._root:
            if item.id in moving:
                return False  # into a dragged item or its subtree
            item = item.parent
        return True

    def dropMimeData(self, mime: QMimeData, action: Qt.DropAction, row: int, column: int,
                     parent: QModelIndex) -> bool:
        """Report a drop as an intent, and move nothing (QAbstractItemModel).

        Args:
            mime: The drag payload.
            action: The drop action (always a move).
            row: Row under `parent` the drop lands before; -1 for "onto".
            column: Unused.
            parent: The group (or invalid: top level) the drop lands in.

        Returns:
            False, so Qt never removes the source rows.
        """
        if not self.canDropMimeData(mime, action, row, column, parent):
            return False
        moving = self._moving_ids(mime)
        target, position = self._drop_target(set(moving), row, parent)
        self.drop_intent.emit(moving, target, position)
        return False  # nothing was moved here: Qt must not remove source rows

    def _drop_target(self, moving: set, row: int, parent: QModelIndex) -> tuple[str, str]:
        """Turn Qt's (row, parent) drop position into an anchor the Model understands.

        Anchoring on the nearest sibling that is not being dragged makes the
        target independent of rows that are about to move.

        Args:
            moving: Ids being dragged.
            row: Row under `parent` the drop lands before; -1 for "onto".
            parent: The group (or invalid: top level) the drop lands in.

        Returns:
            ``(target id, DROP_* position)``; ``("", DROP_END)`` for the empty
            area below the list.
        """
        group = self._item(parent)
        if row < 0:
            return (group.id, DROP_ON) if parent.isValid() else ("", DROP_END)
        siblings = group.children
        # Anchor on the nearest sibling that is not being dragged
        after = next((s for s in siblings[row:] if s.id not in moving), None)
        if after is not None:
            return after.id, DROP_ABOVE
        before = next((s for s in reversed(siblings[:row]) if s.id not in moving), None)
        if before is not None:
            return before.id, DROP_BELOW
        return (group.id, DROP_ON) if parent.isValid() else ("", DROP_END)
