"""Advanced Layer Order — Model: the single source of truth (plain Python, no Qt, no QGIS).

What the Model owns
-------------------
* The ALO document: order groups, their nesting and expanded state, and
  the draw order of the layers inside them. Groups are only ever removed
  by the user: one emptied by layer deletions stays, as in QGIS's Layers
  panel.

What the Model mirrors (written only by the Controller, from QGIS)
------------------------------------------------------------------
* Each layer's display name and effective visibility.
* ``control_enabled`` — QGIS's ``hasCustomLayerOrder``.

Nobody but the Model mutates the tree. Every mutation notifies listeners
with ``(event_type, payload)``. Inside ``block_notifications()`` the
granular events are suppressed and a single resync (``model_loaded``, plus
``order_changed`` if the structure changed) is emitted on exit, so
listeners never miss a change.

Persistence (``serialize`` / ``load_from_json``) and undo snapshots
(``restore_structure``) carry the document only — never the mirrored QGIS
state.
"""
from __future__ import annotations

import copy
import json
import logging
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Union

from .debug import debug_mode

log = logging.getLogger("AdvancedLayerOrder.model")


# ======================================================================
# Schema
# ======================================================================

TREE_JSON_SCHEMA_VERSION = 1

TYPE_GROUP = "group"
TYPE_LAYER = "layer"


# ======================================================================
# Nodes
# ======================================================================

@dataclass
class LayerNode:
    """A leaf of the tree: a QGIS layer, referenced by id."""
    id: str                    # QGIS layer id
    name: str
    visible: bool = True       # mirror of QGIS effective visibility


@dataclass
class GroupNode:
    """An ALO group. Children are LayerNode / GroupNode in draw order, top first."""
    id: str                    # "grp_" + 10 hex chars
    name: str
    expanded: bool = True
    children: list = field(default_factory=list)


Node = Union[GroupNode, LayerNode]   # runtime alias: no `|` (Python 3.9)



def _flatten_layer_ids(nodes: list[Node]) -> list[str]:
    """List the layers under some nodes, depth-first (draw order).

    Args:
        nodes: The nodes to walk.

    Returns:
        The layer ids, top first.
    """
    out: list[str] = []
    for n in nodes:
        if isinstance(n, GroupNode):
            out.extend(_flatten_layer_ids(n.children))
        else:
            out.append(n.id)
    return out


class DocumentError(ValueError):
    """A saved document that cannot be read (see ``load_from_json``)."""


def new_group_id() -> str:
    """Generate a unique group id.

    Returns:
        ``"grp_"`` + 10 hex digits: a stable format, easy to spot in the JSON.
    """
    return "grp_" + uuid.uuid4().hex[:10]


# ======================================================================
# Events
# ======================================================================

# Resync everything (load, undo, reconcile, end of a notification block)
EVENT_MODEL_LOADED = "model_loaded"

# Structural changes
EVENT_LAYER_ADDED = "layer_added"
EVENT_LAYER_REMOVED = "layer_removed"
EVENT_GROUP_CREATED = "group_created"
EVENT_GROUP_DELETED = "group_deleted"
EVENT_ITEM_MOVED = "item_moved"

# Display-only changes
EVENT_LAYER_RENAMED = "layer_renamed"
EVENT_GROUP_RENAMED = "group_renamed"
EVENT_VISIBILITY_CHANGED = "visibility_changed"
EVENT_EXPANDED_CHANGED = "expanded_changed"
EVENT_CONTROL_CHANGED = "control_changed"     # payload: {"enabled"}

# Trailing signal after any structural change: the flattened order may differ.
# Payload {"resync": True} when it follows a model_loaded (already a full resync).
EVENT_ORDER_CHANGED = "order_changed"

_STRUCTURAL_EVENTS = frozenset({
    EVENT_LAYER_ADDED, EVENT_LAYER_REMOVED, EVENT_GROUP_CREATED,
    EVENT_GROUP_DELETED, EVENT_ITEM_MOVED, EVENT_MODEL_LOADED,
})

Listener = Callable[[str, dict], None]


# ======================================================================
# Model
# ======================================================================

class LayerOrderModel:
    """Single source of truth for the order tree. See the module docstring."""

    def __init__(self) -> None:
        """Start with an empty tree, no listener and control off."""
        self._root: list[Node] = []
        # {id: (node, parent, position)}: O(1) lookups. Built lazily by one
        # walk; any structural mutation drops it (_dirty), so each operation
        # costs O(n) at most and a run of lookups O(1) each.
        self._index: dict[str, tuple[Node, GroupNode | None, int]] | None = None
        self._listeners: list[Listener] = []
        self._block_depth = 0
        self._suppressed = False          # something was emitted while blocked
        self._suppressed_structural = False
        self._control_enabled = False     # mirror of QGIS's hasCustomLayerOrder
        self._skipped_children = 0        # malformed nodes seen by load_from_json

    # ------------------------------------------------------------------
    # Listeners and events
    # ------------------------------------------------------------------
    def add_listener(self, cb: Listener) -> None:
        """Register a listener of Model events (once).

        Args:
            cb: Called as ``cb(event_type, payload)`` after every change.
        """
        if cb not in self._listeners:
            self._listeners.append(cb)

    def remove_listener(self, cb: Listener) -> None:
        """Unregister a listener (no-op if it is not registered).

        Args:
            cb: The listener.
        """
        if cb in self._listeners:
            self._listeners.remove(cb)

    @contextmanager
    def block_notifications(self) -> Iterator[LayerOrderModel]:
        """Batch mutations: no granular events inside, one resync on exit.

        On exit (outermost block only), if anything changed, listeners get
        ``model_loaded`` and, if the structure changed, ``order_changed``.
        Blocks nest.

        Yields:
            The Model.
        """
        self._block_depth += 1
        try:
            yield self
        finally:
            self._block_depth -= 1
            if self._block_depth == 0 and self._suppressed:
                structural = self._suppressed_structural
                self._suppressed = self._suppressed_structural = False
                self._emit(EVENT_MODEL_LOADED, {})
                if structural:
                    self._emit(EVENT_ORDER_CHANGED, {"resync": True})

    def _emit(self, event_type: str, payload: dict) -> None:
        """Send an event to the listeners, or note it while notifications are blocked.

        One failing listener must not break the others, so its exception is
        logged; in debug mode it is raised.

        Args:
            event_type: One of the ``EVENT_*`` names.
            payload: The event's data.
        """
        if self._block_depth:
            self._suppressed = True
            if event_type in _STRUCTURAL_EVENTS:
                self._suppressed_structural = True
            return
        for cb in list(self._listeners):  # a listener may remove itself
            try:
                cb(event_type, payload)
            except Exception:
                # One failing listener must not break the others, but it
                # must not vanish either; in debug mode, fail loudly.
                log.exception("listener failed on %s", event_type)
                if debug_mode():
                    raise

    def _structure_changed(self, event_type: str, payload: dict) -> None:
        """Emit a structural event, followed by ``order_changed``.

        Args:
            event_type: The structural ``EVENT_*`` name.
            payload: The event's data.
        """
        self._emit(event_type, payload)
        self._emit(EVENT_ORDER_CHANGED, {"resync": True} if event_type == EVENT_MODEL_LOADED else {})

    # ------------------------------------------------------------------
    # Lookups by id (O(1) through the index)
    # ------------------------------------------------------------------
    def find_item(self, item_id: str) -> Node | None:
        """Find a node by id. O(1).

        Args:
            item_id: Layer or group id.

        Returns:
            The node, or None if unknown.
        """
        return self._lookup(item_id)[0]

    def find_parent(self, item_id: str) -> GroupNode | None:
        """Find the group holding a node. O(1).

        Args:
            item_id: Layer or group id.

        Returns:
            The parent group, or None for a top-level (or unknown) node.
        """
        return self._lookup(item_id)[1]

    def get_index_in_parent(self, item_id: str) -> int | None:
        """Give a node's position among its siblings. O(1).

        Args:
            item_id: Layer or group id.

        Returns:
            The position, or None if unknown.
        """
        return self._lookup(item_id)[2]

    def get_depth(self, item_id: str) -> int:
        """Give a node's nesting depth. O(depth).

        Args:
            item_id: Layer or group id.

        Returns:
            1 for top level, 2 one group deep, …; 0 if unknown.
        """
        return 1 + len(self._ancestors(item_id)) if self.find_item(item_id) else 0

    def _lookup(self, item_id: str | None) -> tuple[Node | None, GroupNode | None, int | None]:
        """Look a node up in the id index, building the index if needed.

        The index is built by one walk and dropped by any structural change
        (``_dirty``): each operation costs O(n) at most, a run of lookups O(1)
        each.

        Args:
            item_id: Layer or group id (None is unknown).

        Returns:
            ``(node, parent, position in parent)``, or ``(None, None, None)``.
        """
        if self._index is None:
            index: dict[str, tuple[Node, GroupNode | None, int]] = {}

            def rec(nodes: list[Node], parent: GroupNode | None) -> None:
                """Index one level of the tree, then the levels below.

                Args:
                    nodes: The nodes of the level.
                    parent: The group holding them (None: top level).
                """
                for i, n in enumerate(nodes):
                    index[n.id] = (n, parent, i)
                    if isinstance(n, GroupNode):
                        rec(n.children, n)
            rec(self._root, None)
            self._index = index
        return self._index.get(item_id, (None, None, None))

    def _dirty(self) -> None:
        """Drop the id index: the structure changed (rebuilt on the next lookup)."""
        self._index = None

    def _siblings(self, parent: GroupNode | None) -> list[Node]:
        """Give the child list of a group, or the top level.

        Args:
            parent: The group, or None for the top level.

        Returns:
            The live list (mutating it changes the tree).
        """
        return parent.children if parent is not None else self._root

    def _ancestors(self, item_id: str) -> list[GroupNode]:
        """List the groups containing a node, innermost first. O(depth).

        Args:
            item_id: Layer or group id.

        Returns:
            The groups; [] for a top-level or unknown node.
        """
        out = []
        parent = self.find_parent(item_id)
        while parent is not None:
            out.append(parent)
            parent = self.find_parent(parent.id)
        return out

    def _outermost(self, item_ids: list[str]) -> list[Node]:
        """Keep the listed nodes that are not inside another listed node.

        Nested nodes travel with their listed ancestor. Unknown ids and
        duplicates are dropped. O(k · depth).

        Args:
            item_ids: Layer / group ids, in the order wanted.

        Returns:
            The nodes, in the given order.
        """
        listed = set(item_ids)
        out: list[Node] = []
        seen: set[str] = set()
        for item_id in item_ids:
            node = self.find_item(item_id)
            if node is None or item_id in seen:
                continue
            seen.add(item_id)
            if not any(g.id in listed for g in self._ancestors(item_id)):
                out.append(node)
        return out

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def get_root(self) -> list[Node]:
        """Give the top-level nodes. Read-only: never mutate what this returns.

        Returns:
            The top-level list.
        """
        return self._root

    def walk(self) -> Iterator[tuple[Node, GroupNode | None]]:
        """Iterate over every node with its parent, pre-order (display order).

        Yields:
            ``(node, parent group or None)``.
        """
        def rec(nodes: list[Node], parent: GroupNode | None) -> Iterator[tuple[Node, GroupNode | None]]:
            """Walk one level, then the levels below.

            Args:
                nodes: The nodes of the level.
                parent: The group holding them (None: top level).

            Yields:
                ``(node, parent)`` pairs.
            """
            for n in nodes:
                yield n, parent
                if isinstance(n, GroupNode):
                    yield from rec(n.children, n)
        yield from rec(self._root, None)

    def has_groups(self) -> bool:
        """Tell whether the tree has any group.

        Returns:
            True if a group exists (any group has a top-level ancestor, so the
            top level is enough to check).
        """
        return any(isinstance(n, GroupNode) for n in self._root)

    def iter_layer_ids(self) -> Iterator[str]:
        """Iterate over the layer ids in draw order.

        Returns:
            An iterator of layer ids, top first.
        """
        return (n.id for n, _ in self.walk() if isinstance(n, LayerNode))

    def get_flattened_layer_ids(self) -> list[str]:
        """List the layer ids in draw order: exactly what goes to QGIS's custom order.

        Returns:
            The layer ids, top first.
        """
        return list(self.iter_layer_ids())

    def get_group_names(self) -> set[str]:
        """Collect every group name in use.

        Returns:
            The names.
        """
        return {n.name for n, _ in self.walk() if isinstance(n, GroupNode)}

    def unique_group_name(self, base: str = "New group") -> str:
        """Make a group name that is not in use.

        Args:
            base: The wished name.

        Returns:
            `base`, or ``base 2``, ``base 3``, … — the first unused one.
        """
        existing = self.get_group_names()
        if base not in existing:
            return base
        n = 2
        while f"{base} {n}" in existing:
            n += 1
        return f"{base} {n}"

    def tree_order_key(self) -> dict[str, int]:
        """Map every node to its display position, for sorting ids.

        Returns:
            ``{node id: pre-order position}``.
        """
        return {n.id: i for i, (n, _) in enumerate(self.walk())}

    def descendant_layer_ids(self, item_id: str) -> list[str]:
        """List the layers a node stands for.

        Args:
            item_id: Layer or group id.

        Returns:
            ``[item_id]`` for a layer; every layer inside a group (any depth);
            [] if unknown.
        """
        node = self.find_item(item_id)
        if isinstance(node, LayerNode):
            return [node.id]
        if not isinstance(node, GroupNode):
            return []
        out: list[str] = []

        def rec(n: GroupNode) -> None:
            """Collect the layers under a group, depth-first.

            Args:
                n: The group.
            """
            for ch in n.children:
                if isinstance(ch, LayerNode):
                    out.append(ch.id)
                else:
                    rec(ch)
        rec(node)
        return out

    # ------------------------------------------------------------------
    # Control of the rendering order (mirrored from QGIS)
    # ------------------------------------------------------------------
    def get_control_enabled(self) -> bool:
        """Tell whether ALO controls the rendering order (mirror of QGIS's setting).

        Returns:
            QGIS's ``hasCustomLayerOrder``, as last mirrored.
        """
        return self._control_enabled

    def set_control_enabled(self, value: bool) -> None:
        """Mirror QGIS's "Control rendering order" (Controller only).

        Emits ``control_changed`` when the value changes.

        Args:
            value: QGIS's ``hasCustomLayerOrder``.
        """
        if self._control_enabled == bool(value):
            return
        self._control_enabled = bool(value)
        self._emit(EVENT_CONTROL_CHANGED, {"enabled": self._control_enabled})

    # ------------------------------------------------------------------
    # Whole document: load, save, replace, undo
    # ------------------------------------------------------------------
    def load_from_json(self, raw_json: str) -> None:
        """Replace the tree with a saved document.

        Inside a readable document, nodes without id, of unknown type, or whose
        id was already seen are skipped, with a warning.

        Args:
            raw_json: The document (``serialize()``'s format); "" for an empty
                tree.

        Raises:
            DocumentError: The document is unreadable (not JSON, or not
                ``{"children": [...]}``). The tree is left untouched, so the
                caller decides what to do with it rather than silently losing it.
        """
        log.debug("load_from_json")
        new_root: list[Node] = []
        seen: set[str] = set()
        if raw_json:
            try:
                obj = json.loads(raw_json)
            except (json.JSONDecodeError, TypeError) as e:
                raise DocumentError(f"not JSON: {e}") from e
            children = obj.get("children", []) if isinstance(obj, dict) else None
            if not isinstance(children, list):
                raise DocumentError('not a {"children": [...]} document')
            skipped = 0
            for raw in children:
                node = self._build_node(raw, seen)
                if node is None:
                    skipped += 1
                else:
                    new_root.append(node)
            skipped += self._skipped_children
            self._skipped_children = 0
            if skipped:
                log.warning("saved layer order: skipped %d malformed node(s)", skipped)
        self._root = new_root
        self._dirty()
        self._structure_changed(EVENT_MODEL_LOADED, {})

    def _build_node(self, raw: object, seen: set[str]) -> Node | None:
        """Build a node, and its children, from saved JSON.

        Args:
            raw: One decoded node.
            seen: Ids already built (duplicates are skipped); updated.

        Returns:
            The node, or None if malformed (counted in ``_skipped_children``
            for nested nodes).
        """
        if not isinstance(raw, dict):
            return None
        t = raw.get("type")
        node_id = raw.get("id") or (new_group_id() if t == TYPE_GROUP else None)
        if node_id is None or node_id in seen:
            return None
        if t == TYPE_GROUP:
            seen.add(node_id)
            built = [self._build_node(ch, seen) for ch in raw.get("children", [])]
            children = [c for c in built if c is not None]
            self._skipped_children += len(built) - len(children)
            return GroupNode(
                id=node_id,
                name=raw.get("name", "Group"),
                expanded=bool(raw.get("expanded", True)),
                children=children,
            )
        if t == TYPE_LAYER:
            seen.add(node_id)
            return LayerNode(id=node_id, name=raw.get("name", ""))
        return None

    def serialize(self) -> str:
        """Save the document (no mirrored QGIS state) as versioned JSON.

        Returns:
            ``{"version": 1, "children": [...]}``, as a string.
        """
        def ser(node: Node) -> dict:
            """Convert one node, and its children, to JSON-ready data.

            Args:
                node: Layer or group.

            Returns:
                The node as a dict.
            """
            if isinstance(node, GroupNode):
                return {"type": TYPE_GROUP, "id": node.id, "name": node.name,
                        "expanded": node.expanded, "children": [ser(c) for c in node.children]}
            return {"type": TYPE_LAYER, "id": node.id, "name": node.name}
        return json.dumps({"version": TREE_JSON_SCHEMA_VERSION,
                           "children": [ser(n) for n in self._root]}, ensure_ascii=False)

    def replace_root(self, nodes: list[Node]) -> None:
        """Swap in a tree computed elsewhere (a reconcile with QGIS's order).

        Args:
            nodes: The new top-level nodes.
        """
        log.debug("replace_root")
        self._root = list(nodes)
        self._dirty()
        self._structure_changed(EVENT_MODEL_LOADED, {})

    def restore_structure(self, raw_json: str) -> None:
        """Restore a document snapshot (undo / redo) without touching QGIS facts.

        The snapshot gives groups, nesting and order. The current layer set
        wins: layers removed since the snapshot are dropped, layers added since
        are kept next to their current flat neighbour. Layer names, visibility
        and the expanded state of existing groups keep their current values
        (they are not part of undo). O(n).

        Args:
            raw_json: The snapshot (``serialize()``'s format).
        """
        log.debug("restore_structure")
        current = {n.id: n for n, _ in self.walk() if isinstance(n, LayerNode)}
        expanded = {n.id: n.expanded for n, _ in self.walk() if isinstance(n, GroupNode)}
        current_order = self.get_flattened_layer_ids()

        snapshot = LayerOrderModel()
        snapshot.load_from_json(raw_json)

        def adopt(nodes: list[Node]) -> list[Node]:
            """Keep the snapshot's structure with the current layers' state.

            Args:
                nodes: Snapshot nodes of one level.

            Returns:
                The level, without layers that no longer exist.
            """
            out = []
            for n in nodes:
                if isinstance(n, GroupNode):
                    n.children = adopt(n.children)
                    n.expanded = expanded.get(n.id, n.expanded)
                    out.append(n)
                elif n.id in current:
                    out.append(copy.copy(current[n.id]))
            return out
        root = adopt(snapshot._root)

        # Layers that did not exist when the snapshot was taken go right
        # after their nearest preceding layer that did (one pass, O(n)).
        present = set(_flatten_layer_ids(root))
        after: dict[str | None, list[Node]] = {}
        prev = None
        for lid in current_order:
            if lid in present:
                prev = lid
            else:
                after.setdefault(prev, []).append(copy.copy(current[lid]))

        def splice(nodes: list[Node]) -> list[Node]:
            """Insert the layers added since the snapshot after their flat neighbour.

            Args:
                nodes: Nodes of one level.

            Returns:
                The level with those layers inserted (groups rebuilt inside).
            """
            out = []
            for n in nodes:
                if isinstance(n, GroupNode):
                    n.children = splice(n.children)
                out.append(n)
                out.extend(after.get(n.id, ()))
            return out
        self._root = after.get(None, []) + splice(root)
        self._dirty()
        self._structure_changed(EVENT_MODEL_LOADED, {})

    def clear(self) -> None:
        """Empty the tree (the project was closed)."""
        log.debug("clear")
        self._root = []
        self._dirty()
        self._structure_changed(EVENT_MODEL_LOADED, {})

    # ------------------------------------------------------------------
    # Layers
    # ------------------------------------------------------------------
    def add_layer(self, layer_id: str, name: str, visible: bool = True,
                  parent_id: str | None = None,
                  index: int | None = None) -> None:
        """Add a layer to a group at a position.

        Args:
            layer_id: QGIS layer id.
            name: Its display name.
            visible: Its effective visibility in QGIS.
            parent_id: The group, or None for the top level.
            index: Position among the group's children; None for the end.
        """
        log.debug("add_layer %s", layer_id)
        parent = self.find_item(parent_id) if parent_id else None
        if not isinstance(parent, GroupNode):
            parent = None
        siblings = self._siblings(parent)
        idx = len(siblings) if index is None else max(0, min(index, len(siblings)))
        siblings.insert(idx, LayerNode(id=layer_id, name=name, visible=visible))
        self._dirty()
        self._structure_changed(EVENT_LAYER_ADDED, {
            "layer_id": layer_id, "name": name, "visible": visible,
            "parent_id": parent.id if parent else None, "index": idx,
        })

    def add_layer_beside(self, layer_id: str, name: str, visible: bool,
                         anchor_id: str | None, after: bool) -> None:
        """Add a layer just before or after another node, in that node's group.

        Args:
            layer_id: QGIS layer id.
            name: Its display name.
            visible: Its effective visibility in QGIS.
            anchor_id: The node to go next to; unknown or None → top of the
                top level.
            after: True for after the anchor, False for before.
        """
        self.add_layers_beside([(layer_id, name, visible, anchor_id, after)])

    def add_layers_beside(self, layers: list[tuple[str, str, bool, str | None, bool]]) -> None:
        """Add several layers next to anchors, in one pass over the tree. O(n + k).

        Anchors are nodes already in the tree; layers sharing an anchor and
        side keep the given order.

        Args:
            layers: ``(layer_id, name, visible, anchor_id, after)`` tuples, as
                for ``add_layer_beside``.
        """
        if not layers:
            return
        log.debug("add_layers_beside %d layer(s)", len(layers))
        before: dict[str, list[Node]] = {}
        after: dict[str, list[Node]] = {}
        top: list[Node] = []
        added: list[str] = []
        for layer_id, name, visible, anchor_id, is_after in layers:
            node = LayerNode(id=layer_id, name=name, visible=bool(visible))
            added.append(layer_id)
            if anchor_id is None or self.find_item(anchor_id) is None:
                top.append(node)
            else:
                (after if is_after else before).setdefault(anchor_id, []).append(node)

        def splice(nodes: list[Node]) -> list[Node]:
            """Insert the new layers around their anchors in one level.

            Args:
                nodes: Nodes of one level.

            Returns:
                The level with the new layers inserted (groups rebuilt inside).
            """
            out = []
            for n in nodes:
                out.extend(before.get(n.id, ()))
                if isinstance(n, GroupNode):
                    n.children = splice(n.children)
                out.append(n)
                out.extend(after.get(n.id, ()))
            return out
        self._root = top + splice(self._root)
        self._dirty()
        for layer_id in added:
            node, parent, idx = self._lookup(layer_id)
            self._emit(EVENT_LAYER_ADDED, {
                "layer_id": layer_id, "name": node.name, "visible": node.visible,
                "parent_id": parent.id if parent else None, "index": idx,
            })
        self._emit(EVENT_ORDER_CHANGED, {})

    def remove_layer(self, layer_id: str) -> None:
        """Remove a layer. Its group stays, even if now empty.

        Args:
            layer_id: The layer.
        """
        self.remove_layers([layer_id])

    def remove_layers(self, layer_ids: Iterable[str]) -> None:
        """Remove several layers in one pass over the tree. O(n).

        Groups stay, even if emptied; unknown ids and groups are ignored.

        Args:
            layer_ids: The layers.
        """
        gone = {lid for lid in layer_ids if isinstance(self.find_item(lid), LayerNode)}
        if not gone:
            return
        log.debug("remove_layers %d layer(s)", len(gone))
        removed: list[str] = []

        def keep(nodes: list[Node]) -> list[Node]:
            """Drop the removed layers from one level.

            Args:
                nodes: Nodes of one level.

            Returns:
                The level without them (groups filtered inside).
            """
            out = []
            for n in nodes:
                if isinstance(n, GroupNode):
                    n.children = keep(n.children)
                elif n.id in gone:
                    removed.append(n.id)
                    continue
                out.append(n)
            return out
        self._root = keep(self._root)
        self._dirty()
        for layer_id in removed:
            self._emit(EVENT_LAYER_REMOVED, {"layer_id": layer_id})
        self._emit(EVENT_ORDER_CHANGED, {})

    def rename_layer(self, layer_id: str, new_name: str) -> None:
        """Mirror a layer's new name (Controller only).

        Args:
            layer_id: The layer.
            new_name: Its name in QGIS.
        """
        node = self.find_item(layer_id)
        if not isinstance(node, LayerNode) or node.name == new_name:
            return
        old, node.name = node.name, new_name
        self._emit(EVENT_LAYER_RENAMED, {"layer_id": layer_id, "old_name": old, "new_name": new_name})

    def set_visibility(self, layer_id: str, visible: bool) -> None:
        """Mirror a layer's effective visibility (Controller only).

        Args:
            layer_id: The layer.
            visible: Its effective visibility in QGIS.
        """
        node = self.find_item(layer_id)
        visible = bool(visible)
        if not isinstance(node, LayerNode) or node.visible == visible:
            return
        node.visible = visible
        self._emit(EVENT_VISIBILITY_CHANGED, {"layer_id": layer_id, "visible": visible})

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------
    def create_group(self, name: str, parent_id: str | None = None,
                     index: int | None = None) -> str:
        """Create an empty group.

        Args:
            name: Its name.
            parent_id: The group to create it in, or None for the top level.
            index: Position among that group's children; None for the end.

        Returns:
            The new group's id.
        """
        log.debug("create_group %r", name)
        parent = self.find_item(parent_id) if parent_id else None
        if not isinstance(parent, GroupNode):
            parent = None
        siblings = self._siblings(parent)
        idx = len(siblings) if index is None else max(0, min(index, len(siblings)))
        gid = new_group_id()
        siblings.insert(idx, GroupNode(id=gid, name=name))
        self._dirty()
        self._structure_changed(EVENT_GROUP_CREATED, {
            "group_id": gid, "name": name,
            "parent_id": parent.id if parent else None, "index": idx,
        })
        return gid

    def delete_group(self, group_id: str, unwrap_children: bool = True) -> None:
        """Delete a group.

        Args:
            group_id: The group.
            unwrap_children: True to put its children in its place; False to
                delete them with it.
        """
        log.debug("delete_group %s", group_id)
        node = self.find_item(group_id)
        if not isinstance(node, GroupNode):
            return
        parent = self.find_parent(group_id)
        siblings = self._siblings(parent)
        idx = self.get_index_in_parent(group_id)
        siblings.pop(idx)
        promoted = list(node.children) if unwrap_children else []
        siblings[idx:idx] = promoted
        self._dirty()
        self._structure_changed(EVENT_GROUP_DELETED, {
            "group_id": group_id,
            "unwrapped_children": [{
                "item_id": ch.id,
                "new_parent_id": parent.id if parent else None,
                "new_index": idx + k,
            } for k, ch in enumerate(promoted)],
        })

    def rename_group(self, group_id: str, new_name: str) -> None:
        """Rename a group.

        Args:
            group_id: The group.
            new_name: Its new name.
        """
        node = self.find_item(group_id)
        if not isinstance(node, GroupNode) or node.name == new_name:
            return
        old, node.name = node.name, new_name
        self._emit(EVENT_GROUP_RENAMED, {"group_id": group_id, "old_name": old, "new_name": new_name})

    def set_expanded(self, group_id: str, expanded: bool) -> None:
        """Store whether a group is shown expanded (part of the document, not undone).

        Args:
            group_id: The group.
            expanded: True if expanded.
        """
        node = self.find_item(group_id)
        expanded = bool(expanded)
        if not isinstance(node, GroupNode) or node.expanded == expanded:
            return
        node.expanded = expanded
        self._emit(EVENT_EXPANDED_CHANGED, {"group_id": group_id, "expanded": expanded})

    # ------------------------------------------------------------------
    # Moves
    # ------------------------------------------------------------------
    def move_items(self, item_ids: list[str], new_parent_id: str | None,
                   before_id: str | None = None) -> bool:
        """Move nodes, as one block, into a group just before a sibling.

        The anchor is resolved after the movers are taken out, so shifting
        indices can't throw it off. Nodes nested inside another mover travel
        with it and are ignored. O(n).

        Args:
            item_ids: Nodes to move, in the order they should end up in.
            new_parent_id: Target group, or None for the top level.
            before_id: Sibling to insert before, or None for the end.

        Returns:
            True if anything moved. False — and no event — if the move is
            invalid (unknown parent, anchor not a child of the parent or itself
            a mover, a group into itself) or changes nothing.
        """
        log.debug("move_items ids=%s parent=%s before=%s", item_ids, new_parent_id, before_id)
        movers = self._outermost(item_ids)
        if not movers:
            return False
        mover_ids = {m.id for m in movers}

        new_parent = self.find_item(new_parent_id) if new_parent_id else None
        if new_parent_id is not None and not isinstance(new_parent, GroupNode):
            return False
        if new_parent is not None and (new_parent.id in mover_ids or any(
                g.id in mover_ids for g in self._ancestors(new_parent.id))):
            return False                                    # into itself
        if before_id is not None and (before_id in mover_ids
                                      or self.find_item(before_id) is None
                                      or self.find_parent(before_id) is not new_parent):
            return False

        before = [self._lookup(m.id)[1:] for m in movers]
        for siblings in {id(s): s for s in (self._siblings(p) for p, _ in before)}.values():
            siblings[:] = [s for s in siblings if s.id not in mover_ids]
        new_siblings = self._siblings(new_parent)
        idx = (len(new_siblings) if before_id is None
               else next(i for i, s in enumerate(new_siblings) if s.id == before_id))
        new_siblings[idx:idx] = movers
        self._dirty()

        if all(p is new_parent and i == idx + k for k, (p, i) in enumerate(before)):
            return False
        self._structure_changed(EVENT_ITEM_MOVED, {
            "item_ids": [m.id for m in movers],
            "new_parent_id": new_parent.id if new_parent else None,
            "new_index": idx,
        })
        return True

    def move_items_by_one(self, item_ids: list[str], up: bool) -> bool:
        """Move each item one step up or down within its own group.

        Every item swaps with the nearest unlisted sibling above (below) it, so
        adjacent items move as one block and keep their order. An item already
        first (last) stays: it never leaves its group. Items nested inside
        another listed item travel with it.

        Args:
            item_ids: The items.
            up: True for up, False for down.

        Returns:
            True if anything moved.
        """
        log.debug("move_items_by_one %s up=%s", item_ids, up)
        ids = {n.id for n in self._outermost(item_ids)}
        moved = False
        parents = {id(self.find_parent(i)): self.find_parent(i) for i in ids}
        for parent in parents.values():
            siblings = self._siblings(parent)
            steps = range(1, len(siblings)) if up else range(len(siblings) - 2, -1, -1)
            for i in steps:
                j = i - 1 if up else i + 1          # the neighbour it would swap with
                if siblings[i].id in ids and siblings[j].id not in ids:
                    siblings[i], siblings[j] = siblings[j], siblings[i]
                    moved = True
        if moved:
            self._dirty()
            self._structure_changed(EVENT_ITEM_MOVED, {"item_ids": sorted(ids)})
        return moved

    def move_items_to_boundary(self, item_ids: list[str], to_top: bool) -> None:
        """Move each item to the top or bottom of its own group, keeping their order.

        Args:
            item_ids: The items.
            to_top: True for the top, False for the bottom.
        """
        log.debug("move_items_to_boundary %s top=%s", item_ids, to_top)
        by_parent: dict = {}
        positioned = [i for i in item_ids if self.get_index_in_parent(i) is not None]
        for item_id in sorted(positioned, key=self.get_index_in_parent):
            parent = self.find_parent(item_id)
            by_parent.setdefault(id(parent), (parent, []))[1].append(item_id)
        moved = False
        for parent, ids in by_parent.values():
            siblings = self._siblings(parent)
            before = [s.id for s in siblings]
            chosen = set(ids)
            picked = [s for s in siblings if s.id in chosen]
            rest = [s for s in siblings if s.id not in chosen]
            siblings[:] = picked + rest if to_top else rest + picked
            moved |= [s.id for s in siblings] != before
        if moved:
            self._dirty()
            self._structure_changed(EVENT_ITEM_MOVED, {"item_ids": list(item_ids)})
