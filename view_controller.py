"""Advanced Layer Order — ViewController: View intents → Model, Model events → View.

The ViewController is the only object that talks to both the View and the
Model, and it does exactly two things:

1. **Acts on intents.** Each intent from the View becomes either
   * one undoable Model transaction (create / rename / delete group, move,
     drop) — see ``_user_edit``; or
   * a plain Model update for UI-only document state (expanded); or
   * a *request* for state that QGIS owns (layer visibility, control of the
     rendering order). Those are re-emitted as ``visibility_requested`` /
     ``control_requested`` for the Controller; the result comes back
     through the Model like any other change.
2. **Renders.** Every Model event is turned into View ``render*`` calls.

It never touches QGIS and never reads state back from the View: intents
carry their data.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager

from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QIcon

from .compat import QUndoStack
from .i18n import (
    ADD_GROUP,
    GROUP_NAME,
    IMPORT_HIERARCHY,
    IMPORT_HIERARCHY_CONFIRM,
    MOVE_DOWN,
    MOVE_ITEMS,
    MOVE_TO_BOTTOM,
    MOVE_TO_TOP,
    MOVE_UP,
    NEW_GROUP,
    REMOVE_GROUP,
    RENAME_GROUP,
    tr,
    tr_plugin,
)
from .model import (
    EVENT_CONTROL_CHANGED,
    EVENT_EXPANDED_CHANGED,
    EVENT_GROUP_RENAMED,
    EVENT_LAYER_RENAMED,
    EVENT_MODEL_LOADED,
    EVENT_ORDER_CHANGED,
    EVENT_VISIBILITY_CHANGED,
    TYPE_GROUP,
    TYPE_LAYER,
    GroupNode,
    LayerNode,
    LayerOrderModel,
    Node,
)
from .tree_model import DROP_ABOVE, DROP_BELOW, DROP_END, DROP_ON
from .undo import TreeStateCommand
from .view import LayerOrderView

log = logging.getLogger("AdvancedLayerOrder.view_controller")



def _shape(nodes: list[Node]) -> list:
    """Describe a tree without its group ids, to compare two trees.

    Args:
        nodes: Top-level nodes.

    Returns:
        Nested lists: a group is ``(name, children)``, a layer its id.
    """
    return [(n.name, _shape(n.children)) if isinstance(n, GroupNode) else n.id for n in nodes]


def _layer_ids(nodes: list[Node]) -> list[str]:
    """List the layers of a tree in draw order.

    Args:
        nodes: Top-level nodes.

    Returns:
        The layer ids, top first.
    """
    out: list[str] = []
    for n in nodes:
        out.extend(_layer_ids(n.children) if isinstance(n, GroupNode) else [n.id])
    return out

class ViewController(QObject):
    """Mediator between LayerOrderView and LayerOrderModel; owns the undo stack.

    It never touches QGIS and never reads state back from the View: intents
    carry their data, and QGIS-owned state is requested through signals.
    """

    # Requests for QGIS-owned state — plugin.py wires them to the Controller
    visibility_requested = pyqtSignal(list, bool)   # layer ids, visible
    control_requested = pyqtSignal(bool)            # take over the rendering order

    def __init__(self, model: LayerOrderModel, view: LayerOrderView,
                 layer_icon: Callable[[str], QIcon] | None = None,
                 hierarchy: Callable[[], list[Node]] | None = None,
                 parent: QObject | None = None) -> None:
        """Connect to the View's intents and the Model's events, then render.

        Args:
            model: The document.
            view: The dock.
            layer_icon: Gives the icon of a layer id (QGIS's own; none in tests).
            hierarchy: Reads the Layers panel's hierarchy as Model nodes
                (``LayerOrderController.layers_panel_hierarchy``); without
                it, importing does nothing.
            parent: Owning QObject (the dock, so both are deleted together).
        """
        super().__init__(parent)
        self._model = model
        self._view = view
        self._layer_icon = layer_icon or (lambda _lid: None)
        self._hierarchy = hierarchy
        self.undo_stack = QUndoStack(self)

        model.add_listener(self._on_model_event)
        v = view
        v.create_group_requested.connect(self._on_create_group)
        v.rename_group_requested.connect(self._on_rename_group)
        v.delete_groups_requested.connect(self._on_delete_groups)
        v.move_to_boundary_requested.connect(self._on_move_to_boundary)
        v.move_by_one_requested.connect(self._on_move_by_one)
        v.expand_requested.connect(self._on_expand)
        v.check_requested.connect(self._on_check)
        v.drop_requested.connect(self._on_drop)
        v.import_hierarchy_requested.connect(self._on_import_hierarchy)
        v.undo_requested.connect(self._on_undo_requested)   # a method, not a lambda: see LayerOrderView
        stack = self.undo_stack
        for sig in (stack.canUndoChanged, stack.canRedoChanged, stack.undoTextChanged, stack.redoTextChanged):
            sig.connect(self._render_undo_state)
        v.control_toggled.connect(self.control_requested)
        self._render_all()

    # ------------------------------------------------------------------
    # Undo history
    # ------------------------------------------------------------------
    def record_step(self, before: str, after: str, text: str) -> None:
        """Add an edit made outside the View to the undo history.

        Reorders made in QGIS's Layer Order panel are already applied; recording
        them keeps one linear history whichever panel changed the order.

        Args:
            before: The document before the edit.
            after: The document after it.
            text: The step's name (Undo / Redo tooltips).
        """
        if before != after:
            self.undo_stack.push(TreeStateCommand(self._model, before, after, text))

    def reset_history(self) -> None:
        """Forget the undo history (a project was opened, closed or created)."""
        self.undo_stack.clear()

    def _on_undo_requested(self, undo: bool) -> None:
        """Undo or redo one step (the panel's buttons).

        Args:
            undo: True for Undo, False for Redo.
        """
        if undo:
            self.undo_stack.undo()
        else:
            self.undo_stack.redo()

    @contextmanager
    def _user_edit(self, text: str) -> Iterator[None]:
        """Run one user action as a single Model transaction and undo step.

        Inside, Model events are batched: one resync on exit renders the View
        and lets the Controller apply the new order once. Nothing is recorded if
        the action changed nothing.

        Args:
            text: The step's name (Undo / Redo tooltips).

        Yields:
            Nothing; the Model is edited inside the ``with`` block.
        """
        before = self._model.serialize()
        with self._model.block_notifications():
            yield
        after = self._model.serialize()
        if after != before:
            self.undo_stack.push(TreeStateCommand(self._model, before, after, text))

    # ------------------------------------------------------------------
    # Model → View
    # ------------------------------------------------------------------
    def _on_model_event(self, event_type: str, payload: dict) -> None:
        """Turn one Model event into View render calls.

        Every structural change ends with ``order_changed``, so the tree is
        rendered once, there (unless it trails a ``model_loaded`` already
        rendered in full).

        Args:
            event_type: One of the Model's ``EVENT_*`` names.
            payload: The event's data.
        """
        v = self._view
        if event_type == EVENT_MODEL_LOADED:
            self._render_all()
        elif event_type == EVENT_ORDER_CHANGED:
            # Every structural change ends with ORDER_CHANGED: render once, here
            # (unless it trails a model_loaded that was just rendered)
            if not payload.get("resync"):
                self._render_tree()
        elif event_type == EVENT_LAYER_RENAMED:
            v.render_name(payload["layer_id"], payload["new_name"])
        elif event_type == EVENT_GROUP_RENAMED:
            v.render_name(payload["group_id"], payload["new_name"])
        elif event_type == EVENT_VISIBILITY_CHANGED:
            v.render_visibility(payload["layer_id"], payload["visible"])
        elif event_type == EVENT_EXPANDED_CHANGED:
            v.render_expanded(payload["group_id"], payload["expanded"])
        elif event_type == EVENT_CONTROL_CHANGED:
            v.render_control_enabled(payload["enabled"])

    def _render_all(self) -> None:
        """Render everything: tree, undo buttons, control checkbox."""
        self._render_tree()
        self._render_undo_state()
        self._view.render_control_enabled(self._model.get_control_enabled())

    def _render_tree(self) -> None:
        """Render the whole tree from the Model."""
        def as_dict(node: Node) -> dict:
            """Convert a Model node to the dict the View renders.

            Args:
                node: A layer or group.

            Returns:
                The node dict (shape: ``LayerOrderView.render``), children included.
            """
            if isinstance(node, GroupNode):
                return {"type": TYPE_GROUP, "id": node.id, "name": node.name,
                        "expanded": node.expanded,
                        "children": [as_dict(ch) for ch in node.children]}
            return {"type": TYPE_LAYER, "id": node.id, "name": node.name,
                    "visible": node.visible, "icon": self._layer_icon(node.id)}
        self._view.render([as_dict(n) for n in self._model.get_root()])

    def _render_undo_state(self, *_args: object) -> None:
        """Render the Undo / Redo buttons from the undo stack.

        Args:
            *_args: Ignored (the QUndoStack signal's value).
        """
        s = self.undo_stack
        self._view.render_undo_state(s.canUndo(), s.undoText(), s.canRedo(), s.redoText())

    # ------------------------------------------------------------------
    # Intents → groups
    # ------------------------------------------------------------------
    def _on_create_group(self, selected_ids: list) -> None:
        """Create a group from the selection (Add Group).

        With a single group selected, an empty subgroup is created at its top;
        with other items selected, the new group wraps them in their place;
        with nothing selected, an empty group goes at the bottom.

        Args:
            selected_ids: The selected items without a selected ancestor.
        """
        model = self._model
        selected = self._in_tree_order(selected_ids)
        default = model.unique_group_name(tr(NEW_GROUP))
        name = self._view.ask_text(tr(NEW_GROUP), f"{tr(GROUP_NAME)}:", default)
        if name is None:
            return
        if name in model.get_group_names():
            name = model.unique_group_name(name)

        if len(selected) == 1 and isinstance(model.find_item(selected[0]), GroupNode):
            # One group selected → new empty subgroup at its top
            parent_id, index, movers = selected[0], 0, []
        elif selected:
            # Wrap the selection: new group as their sibling, after the last one
            parents = {getattr(model.find_parent(i), "id", None) for i in selected}
            if len(parents) == 1:
                parent_id = parents.pop()
                index = max(model.get_index_in_parent(i) for i in selected) + 1
            else:
                parent_id, index = None, None
            movers = selected
        else:
            parent_id, index, movers = None, None, []

        with self._user_edit(tr(ADD_GROUP)):
            gid = model.create_group(name, parent_id=parent_id, index=index)
            if movers:
                model.move_items(movers, gid)
            model.set_expanded(gid, True)
        self._view.render_selection([gid])

    def _on_rename_group(self, group_id: str) -> None:
        """Ask for a new name and rename a group (Rename Group).

        A name already used gets a number appended.

        Args:
            group_id: The group.
        """
        node = self._model.find_item(group_id)
        if not isinstance(node, GroupNode):
            return
        name = self._view.ask_text(tr(RENAME_GROUP), f"{tr(GROUP_NAME)}:", node.name)
        if name is None or name == node.name:
            return
        if name in self._model.get_group_names() - {node.name}:
            name = self._model.unique_group_name(name)
        with self._user_edit(tr(RENAME_GROUP)):
            self._model.rename_group(group_id, name)

    def _on_delete_groups(self, group_ids: list) -> None:
        """Remove groups, their contents taking their place (Remove Group).

        Deepest groups go first so nested selections unwrap cleanly.

        Args:
            group_ids: The groups.
        """
        if not group_ids:
            return
        ordered = sorted(group_ids, key=self._model.get_depth, reverse=True)  # deepest first
        with self._user_edit(tr(REMOVE_GROUP)):
            for gid in ordered:
                self._model.delete_group(gid, unwrap_children=True)

    def _on_expand(self, group_id: str, expanded: bool) -> None:
        # Expansion is UI state stored in the document; it is not undoable
        """Store a group's expanded state (not undoable: it is display state).

        Args:
            group_id: The group.
            expanded: True to expand, False to collapse.
        """
        self._model.set_expanded(group_id, expanded)

    # ------------------------------------------------------------------
    # Intents → moves
    # ------------------------------------------------------------------
    def _on_move_by_one(self, item_ids: list, up: bool) -> None:
        """Move items one step up or down within their own group (Move up / down).

        Args:
            item_ids: The selected items.
            up: True for up, False for down.
        """
        if not item_ids:
            return
        with self._user_edit(tr(MOVE_UP if up else MOVE_DOWN)):
            self._model.move_items_by_one(item_ids, up)
        self._view.render_selection(self._in_tree_order(item_ids))

    def _on_move_to_boundary(self, item_ids: list, to_top: bool) -> None:
        """Move items to the top or bottom of their own group (Move to Top / Bottom).

        Args:
            item_ids: The selected items.
            to_top: True for the top, False for the bottom.
        """
        if not item_ids:
            return
        with self._user_edit(tr(MOVE_TO_TOP if to_top else MOVE_TO_BOTTOM)):
            self._model.move_items_to_boundary(item_ids, to_top=to_top)
        self._view.render_selection(item_ids)

    def _on_drop(self, moving_ids: list, target_id: str, position: str) -> None:
        # Deferred: the View is still inside QDrag/dropEvent; re-rendering it
        # from there would delete items Qt is still using.
        """Apply a drop on the next event-loop turn.

        The View is still inside QDrag / dropEvent: re-rendering it from there
        would delete items Qt is still using.

        Args:
            moving_ids: The dragged items.
            target_id: The item dropped on / next to ("" for the empty area).
            position: One of the ``DROP_*`` positions.
        """
        ids = list(moving_ids)
        QTimer.singleShot(0, lambda: self.handle_drop(ids, target_id, position))

    def handle_drop(self, moving_ids: list, target_id: str, position: str) -> None:
        """Apply a drop intent as one undoable move.

        Every case is one atomic ``Model.move_items()`` anchored on a sibling id,
        so multi-item drops keep their order. Dropping onto a layer creates a
        group holding that layer and the dropped items.

        Args:
            moving_ids: The dragged items.
            target_id: The item dropped on / next to ("" for the empty area).
            position: One of the ``DROP_*`` positions.
        """
        model = self._model
        movers = self._in_tree_order(moving_ids)
        if not movers:
            return
        log.debug("drop: moving=%s target=%s pos=%s", movers, target_id, position)

        if position == DROP_END:
            with self._user_edit(tr(MOVE_ITEMS)):
                model.move_items(movers, None, None)
            self._view.render_selection(movers)
            return

        target = model.find_item(target_id)
        if target is None or target_id in movers:
            log.debug("drop: invalid target %s", target_id)
            return
        parent = model.find_parent(target_id)
        parent_id = parent.id if parent is not None else None

        with self._user_edit(tr(MOVE_ITEMS)):
            if position == DROP_ON and isinstance(target, LayerNode):
                # Onto a layer → new group at the target's slot: [target, movers...]
                gid = model.create_group(model.unique_group_name(tr(NEW_GROUP)), parent_id=parent_id,
                                         index=model.get_index_in_parent(target_id))
                if model.move_items([target_id, *movers], gid):
                    model.set_expanded(gid, True)
                else:
                    model.delete_group(gid, unwrap_children=False)
            elif position == DROP_ON:
                model.move_items(movers, target_id, None)        # end of the group
            elif position == DROP_ABOVE:
                model.move_items(movers, parent_id, target_id)
            elif position == DROP_BELOW:
                model.move_items(movers, parent_id, self._next_sibling_id(target_id, set(movers)))
        self._view.render_selection(movers)

    def _in_tree_order(self, item_ids: Iterable[str]) -> list[str]:
        """Sort ids in display order, dropping unknown ones and duplicates.

        Args:
            item_ids: Layer / group ids.

        Returns:
            The known ids, top to bottom.
        """
        order = self._model.tree_order_key()
        return sorted((i for i in dict.fromkeys(item_ids) if i in order), key=order.get)

    def _next_sibling_id(self, item_id: str, exclude: set) -> str | None:
        """Find the first sibling after an item that is not excluded.

        Args:
            item_id: The item.
            exclude: Ids to skip (the items being moved).

        Returns:
            That sibling's id, or None for "at the end".
        """
        parent = self._model.find_parent(item_id)
        siblings = parent.children if parent is not None else self._model.get_root()
        idx = self._model.get_index_in_parent(item_id)
        return next((s.id for s in siblings[idx + 1:] if s.id not in exclude), None)

    # ------------------------------------------------------------------
    # Intents → import the Layers panel's hierarchy
    # ------------------------------------------------------------------
    def _on_import_hierarchy(self) -> None:
        """Replace the tree with the Layers panel's groups, as one undo step.

        The user is asked first when the import would lose something ALO
        defined itself: groups, or an order other than the Layers panel's.
        Nothing happens when the result would be identical. Redo restores
        the hierarchy as it was imported, even if the Layers panel changed
        since.
        """
        if self._hierarchy is None:
            return
        nodes = self._with_model_layers(self._hierarchy())
        if _shape(nodes) == _shape(self._model.get_root()):
            return
        own = self._model.has_groups() or self._model.get_flattened_layer_ids() != _layer_ids(nodes)
        if own and not self._view.confirm(tr_plugin(IMPORT_HIERARCHY), tr_plugin(IMPORT_HIERARCHY_CONFIRM)):
            return
        with self._user_edit(tr_plugin(IMPORT_HIERARCHY)):
            self._model.replace_root(nodes)

    def _with_model_layers(self, nodes: list[Node]) -> list[Node]:
        """Make an imported tree hold exactly the Model's layers.

        The Model mirrors QGIS's layers, so both sets normally match; layers
        the Model doesn't know are dropped and missing ones are appended at
        the top level, so the import can never add or lose a layer.

        Args:
            nodes: The imported top-level nodes.

        Returns:
            The nodes, filtered and completed.
        """
        known = set(self._model.iter_layer_ids())

        def keep(level: list[Node]) -> list[Node]:
            """Drop unknown layers from one level (groups are kept).

            Args:
                level: Nodes of one level.

            Returns:
                The level without unknown layers.
            """
            out: list[Node] = []
            for n in level:
                if isinstance(n, GroupNode):
                    n.children = keep(n.children)
                elif n.id not in known:
                    continue
                out.append(n)
            return out

        nodes = keep(nodes)
        present = set(_layer_ids(nodes))
        missing = [self._model.find_item(lid) for lid in self._model.iter_layer_ids() if lid not in present]
        return nodes + [LayerNode(id=n.id, name=n.name, visible=n.visible) for n in missing]

    # ------------------------------------------------------------------
    # Intents → requests for QGIS-owned state
    # ------------------------------------------------------------------
    def _on_check(self, item_id: str, checked: bool) -> None:
        # Visibility belongs to QGIS: ask for it, the Model will follow
        """Ask QGIS to show or hide the layers of a clicked checkbox.

        Visibility belongs to QGIS: the request goes to the Controller and the
        Model follows QGIS's answer.

        Args:
            item_id: The layer or group clicked.
            checked: The wished state.
        """
        layer_ids = self._model.descendant_layer_ids(item_id)
        if layer_ids:
            self.visibility_requested.emit(layer_ids, checked)
