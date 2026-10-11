"""Advanced Layer Order — Controller: QGIS ↔ Model, nothing else.

The Controller is the only object that talks to QGIS. It never sees the
View or the ViewController.

QGIS → Model
    * project open / new / close          → load the document, sync layer set
    * layer added / removed / renamed     → mirror into the Model
    * layer tree visibility               → mirror effective visibility
    * hasCustomLayerOrder                 → mirror ``control_enabled``
    * custom order changed elsewhere      → reconcile (see reconcile.py)

Model → QGIS
    * order_changed                       → setCustomLayerOrder (debounced)
    * document changes                    → tree_json project entry, only while
                                            the tree has groups (removed otherwise)

Requests (wired by plugin.py from the ViewController)
    * set_layers_visible(ids, visible)    → layer tree checkboxes
    * set_control_enabled(on)             → setHasCustomLayerOrder

Every QGIS signal we cause ourselves fires synchronously while
``_applying`` is set; that one flag is how our own echoes are recognised.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from qgis.core import QgsLayerTree, QgsProject
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from .debug import debug_mode
from .i18n import LAYER_ORDER, tr
from .model import (
    EVENT_EXPANDED_CHANGED,
    EVENT_GROUP_RENAMED,
    EVENT_LAYER_RENAMED,
    EVENT_MODEL_LOADED,
    EVENT_ORDER_CHANGED,
    DocumentError,
    LayerOrderModel,
)
from .reconcile import plan_new_layers, reconcile_tree

if TYPE_CHECKING:
    from qgis.core import QgsLayerTreeNode, QgsMapLayer
    from qgis.gui import QgisInterface
    from qgis.PyQt.QtCore import pyqtBoundSignal

log = logging.getLogger("AdvancedLayerOrder.controller")

ENTRY_SCOPE = "AdvancedLayerOrder"   # project-entry scope of this plugin
LEGACY_SCOPE = "BetterLayerOrder"     # Layer Order Plus and early fork builds: read, never written
ENTRY_TREE = "tree_json"              # the document (groups and order), JSON
ENTRY_TREE_UNREADABLE = "tree_json_unreadable"   # an unreadable tree_json, kept aside
# Entries removed when the document holds nothing QGIS doesn't (no group);
# never the unreadable copy. removeEmptyGroups: setting of 1.3.4 and before.
_DOCUMENT_ENTRIES = ((ENTRY_SCOPE, ENTRY_TREE), (ENTRY_SCOPE, "removeEmptyGroups"),
                     (LEGACY_SCOPE, ENTRY_TREE), (LEGACY_SCOPE, "removeEmptyGroups"))

APPLY_DEBOUNCE_MS = 50   # coalesces a burst of Model changes into one setCustomLayerOrder
PLACE_DELAY_MS = 0       # next event-loop turn: QGIS has put the new layers in its tree by then

# Model events after which the document may differ from the saved one
_DOCUMENT_EVENTS = frozenset({
    EVENT_MODEL_LOADED, EVENT_ORDER_CHANGED, EVENT_GROUP_RENAMED,
    EVENT_LAYER_RENAMED, EVENT_EXPANDED_CHANGED,
})


class LayerOrderController(QObject):
    """Keeps the Model and QGIS in sync; the only object that talks to QGIS.

    See the module docstring for what flows in each direction.
    """

    project_loaded = pyqtSignal()   # a project's document is now in the Model
    # A reorder made outside the plugin (QGIS's Layer Order panel) changed
    # the document: (before_json, after_json, description). Wired to the
    # ViewController so it becomes a step of the layer-order history.
    external_edit = pyqtSignal(str, str, str)

    def __init__(self, model: LayerOrderModel, iface: QgisInterface,
                 parent: QObject | None = None) -> None:
        """Connect to QGIS's project and layer tree, and to the Model's events.

        Args:
            model: The document.
            iface: QGIS's interface (a fake one in the checks).
            parent: Owning QObject (the dock, so both are deleted together).
        """
        super().__init__(parent)
        self._model = model
        self._iface = iface
        self._applying = False   # we are writing to QGIS right now
        self._loading = False    # a project is being loaded into the Model

        self._apply_timer = self._timer(APPLY_DEBOUNCE_MS, self._apply_custom_order)
        # Layers seen listed twice in the stock Layer Order panel's mid-drag
        # state; they are the dragged layers once the drag completes.
        self._dragged: set = set()
        # New layers have no layer-tree node yet when layersAdded fires, so
        # placing them (next to their Layers-panel neighbours) waits a turn.
        self._place_timer = self._timer(PLACE_DELAY_MS, self._place_pending_layers)
        self._pending_layers: list = []

        model.add_listener(self._on_model_event)
        self._connections = self._qgis_connections()
        for signal, slot in self._connections:
            signal.connect(slot)

    def teardown(self) -> None:
        """Disconnect from QGIS and save the document (plugin unload)."""
        for t in (self._apply_timer, self._place_timer):
            t.stop()
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except TypeError:
                pass  # already disconnected
        self._model.remove_listener(self._on_model_event)
        self._save_document()

    # ------------------------------------------------------------------
    # Connections to QGIS
    # ------------------------------------------------------------------
    def _qgis_connections(self) -> list[tuple[pyqtBoundSignal, Callable[..., None]]]:
        """List the QGIS signals the Controller listens to.

        Returns:
            ``(signal, slot)`` pairs, connected in ``__init__`` and disconnected
            in ``teardown``.
        """
        proj = QgsProject.instance()
        root = proj.layerTreeRoot()
        return [
            (proj.layersAdded, self._on_layers_added),
            (proj.layersWillBeRemoved, self._on_layers_removed),
            (proj.aboutToBeCleared, self._on_project_about_to_be_cleared),
            (proj.cleared, self._on_project_cleared),
            (self._iface.projectRead, self.load_project),
            (self._iface.newProjectCreated, self.load_project),
            (root.nameChanged, self._on_tree_name_changed),
            (root.visibilityChanged, self._on_tree_visibility_changed),
            (root.hasCustomLayerOrderChanged, self._on_has_custom_order_changed),
            (root.customLayerOrderChanged, self._on_custom_order_changed),
        ]

    def _timer(self, ms: int, slot: Callable[[], None]) -> QTimer:
        """Make a single-shot timer owned by the Controller.

        Args:
            ms: Delay in milliseconds.
            slot: Called when the timer fires.

        Returns:
            The timer, not started.
        """
        t = QTimer(self)
        t.setSingleShot(True)
        t.setInterval(ms)
        t.timeout.connect(slot)
        return t

    # ------------------------------------------------------------------
    # Requests (from the ViewController, via plugin.py)
    # ------------------------------------------------------------------
    def set_layers_visible(self, layer_ids: list, visible: bool) -> None:
        """Check or uncheck layers in QGIS's layer tree.

        Checking also checks their Layers-panel groups, so the layer really
        shows (the Model mirrors effective visibility). The Model follows through
        QGIS's visibilityChanged signal.

        Args:
            layer_ids: The layers.
            visible: True to show them.
        """
        nodes = self._layer_tree_nodes()
        for lid in layer_ids:
            node = nodes.get(lid)
            if node is None:
                continue
            if visible:
                node.setItemVisibilityCheckedParentRecursive(True)
            else:
                node.setItemVisibilityChecked(False)

    def set_control_enabled(self, enabled: bool) -> None:
        """Let ALO drive (or stop driving) the rendering order.

        Args:
            enabled: QGIS's "Control rendering order" state to set.
        """
        QgsProject.instance().layerTreeRoot().setHasCustomLayerOrder(bool(enabled))
        QgsProject.instance().setDirty(True)

    # ------------------------------------------------------------------
    # Project lifecycle
    # ------------------------------------------------------------------
    def load_project(self) -> None:
        """Load the current project's document into the Model.

        The saved tree gives the structure; QGIS gives the facts: layers that no
        longer exist are dropped, new ones are placed next to their Layers-panel
        neighbours, names and visibility come from QGIS. An unreadable saved tree
        is kept aside (``tree_json_unreadable``) and the panel starts from QGIS's
        order. Anything else that fails is logged (raised in debug mode).
        """
        proj = QgsProject.instance()
        root = proj.layerTreeRoot()
        model = self._model
        self._apply_timer.stop()
        self._pending_layers.clear()
        self._loading = True
        try:
            scope = ENTRY_SCOPE if proj.readEntry(ENTRY_SCOPE, ENTRY_TREE, "")[0] else LEGACY_SCOPE
            raw = proj.readEntry(scope, ENTRY_TREE, "")[0] or ""
            with model.block_notifications():
                model.set_control_enabled(root.hasCustomLayerOrder())
                try:
                    model.load_from_json(raw)
                except DocumentError as e:
                    # Keep it: the next save would otherwise overwrite it
                    proj.writeEntry(ENTRY_SCOPE, ENTRY_TREE_UNREADABLE, raw)
                    log.warning("the saved layer order is unreadable (%s): it is kept in the project "
                                "as %s/%s; starting from QGIS's order", e, ENTRY_SCOPE, ENTRY_TREE_UNREADABLE)
                    model.load_from_json("")
                self._sync_layer_set()
        except Exception:
            # Anything else must not take the plugin (or QGIS's project
            # loading) down, but in debug mode it should fail loudly
            log.exception("loading the project's layer order failed")
            if debug_mode():
                raise
        finally:
            self._loading = False
        self.project_loaded.emit()
        if model.get_control_enabled():
            self._apply_custom_order()   # QGIS may hold a stale order (1.2.22)
        self._refresh_native_panel()

    def _on_project_about_to_be_cleared(self) -> None:
        """Stop mirroring: the project is being closed or replaced.

        QGIS removes all its layers next. Mirroring those removals would edit the
        Model and save the leftover tree into the project being opened, which
        then inherited the previous project's groups.
        """
        self._apply_timer.stop()
        self._pending_layers.clear()
        self._loading = True

    def _on_project_cleared(self) -> None:
        """Empty the Model once QGIS has cleared the project, and resume mirroring."""
        try:
            self._model.clear()
        finally:
            self._loading = False

    def _sync_layer_set(self) -> None:
        """Make the Model's layers exactly QGIS's, with QGIS's names and visibility."""
        model = self._model
        qgis_layers = {lyr.id(): lyr for lyr in QgsProject.instance().mapLayers().values()}
        model.remove_layers([lid for lid in model.iter_layer_ids() if lid not in qgis_layers])
        if not any(True for _ in model.iter_layer_ids()):
            # Fresh document: take QGIS's draw order as is
            for lyr in self._qgis_draw_order():
                model.add_layer(lyr.id(), lyr.name())
        else:
            known = set(model.iter_layer_ids())
            self._place_layers([lid for lid in self._layer_panel_order() if lid not in known])
        nodes = self._layer_tree_nodes()
        for lid, lyr in qgis_layers.items():
            model.rename_layer(lid, lyr.name())
            node = nodes.get(lid)
            model.set_visibility(lid, node.isVisible() if node is not None else True)

    @staticmethod
    def _qgis_draw_order() -> list[QgsMapLayer]:
        """Give the order a project without groups starts from.

        Returns:
            QGIS's custom order (kept and saved by QGIS even while control is
            off, so it is the order ALO showed last), else the Layers-panel order.
        """
        root = QgsProject.instance().layerTreeRoot()
        return list(root.customLayerOrder() or root.layerOrder())

    # ------------------------------------------------------------------
    # Layers added / removed / renamed / visibility
    # ------------------------------------------------------------------
    def _on_layers_added(self, layers: list[QgsMapLayer]) -> None:
        """Queue new layers; they are placed on the next event-loop turn.

        They have no layer-tree node yet when layersAdded fires.

        Args:
            layers: The layers QGIS added.
        """
        if self._loading:
            return
        self._pending_layers.extend(lyr.id() for lyr in layers)
        self._place_timer.start()

    def _place_pending_layers(self) -> None:
        """Place the queued new layers that the Model doesn't know yet."""
        pending, self._pending_layers = self._pending_layers, []
        known = set(self._model.iter_layer_ids())
        self._place_layers([lid for lid in dict.fromkeys(pending) if lid not in known])

    def _place_layers(self, layer_ids: list[str]) -> None:
        """Insert new layers next to their Layers-panel neighbours.

        See ``reconcile.plan_new_layers``: one panel sweep and one Model splice,
        O(n).

        Args:
            layer_ids: Layers to place (known or vanished ones are skipped).
        """
        proj = QgsProject.instance()
        known = set(self._model.iter_layer_ids())
        new = [lid for lid in dict.fromkeys(layer_ids)
               if lid not in known and proj.mapLayer(lid) is not None]
        if not new:
            return
        plan = plan_new_layers(self._layer_panel_order(), known, new)
        nodes = self._layer_tree_nodes()

        def facts(lid: str) -> tuple[str, bool]:
            """Read a layer's name and effective visibility from QGIS.

            Args:
                lid: The layer id.

            Returns:
                ``(name, visible)``.
            """
            node = nodes.get(lid)
            return proj.mapLayer(lid).name(), node.isVisible() if node is not None else True

        if known:
            self._model.add_layers_beside([(lid, *facts(lid), anchor, after) for lid, anchor, after in plan])
        else:
            with self._model.block_notifications():     # nothing to anchor on: append
                for lid, _anchor, _after in plan:
                    self._model.add_layer(lid, *facts(lid))

    def _on_layers_removed(self, layer_ids: list[str]) -> None:
        """Remove layers QGIS is removing from the Model.

        Args:
            layer_ids: The layers.
        """
        if self._loading:
            return
        removed = set(layer_ids or [])
        self._pending_layers = [lid for lid in self._pending_layers if lid not in removed]
        self._model.remove_layers(removed)

    def _on_tree_name_changed(self, node: QgsLayerTreeNode, name: str) -> None:
        """Mirror a layer renamed in QGIS.

        Args:
            node: The layer-tree node renamed (groups are ignored).
            name: Its new name.
        """
        if QgsLayerTree.isLayer(node):
            self._model.rename_layer(node.layerId(), name)

    def _on_tree_visibility_changed(self, node: QgsLayerTreeNode) -> None:
        """Mirror the effective visibility of the layers under a changed node.

        Args:
            node: The layer or Layers-panel group whose checkbox changed.
        """
        layers = node.findLayers() if QgsLayerTree.isGroup(node) else [node]
        for ltl in layers:
            self._model.set_visibility(ltl.layerId(), ltl.isVisible())

    @staticmethod
    def _layer_panel_order() -> list:
        """List the layers in Layers-panel order, ignoring any custom order.

        Returns:
            Layer ids, top first.
        """
        return [n.layerId() for n in QgsProject.instance().layerTreeRoot().findLayers()]

    @staticmethod
    def _layer_tree_nodes() -> dict:
        """Index QGIS's layer-tree nodes by layer id.

        One walk, where ``QgsLayerTree.findLayer()`` walks the tree for every
        lookup.

        Returns:
            ``{layer id: layer-tree node}``.
        """
        return {n.layerId(): n for n in QgsProject.instance().layerTreeRoot().findLayers()}

    # ------------------------------------------------------------------
    # Rendering order: QGIS ↔ Model
    # ------------------------------------------------------------------
    def _on_has_custom_order_changed(self, *_args: object) -> None:
        """Mirror "Control rendering order" changed in QGIS.

        When it is turned on, ALO pushes its own order (QGIS's may be stale);
        either way QGIS's Layer Order panel is refreshed.

        Args:
            *_args: Ignored (the signal's value).
        """
        enabled = QgsProject.instance().layerTreeRoot().hasCustomLayerOrder()
        self._model.set_control_enabled(enabled)
        if self._applying or self._loading:
            return  # our own apply / the load refreshes when it is done
        if enabled:
            # ALO takes over: push its order (QGIS's may be stale)
            self._apply_custom_order()
        self._refresh_native_panel()

    def _on_custom_order_changed(self) -> None:
        """Reconcile with a custom order changed outside ALO, synchronously.

        QGIS's Layer Order panel applies a drag in two steps: it inserts the
        layer at its new row (the layer is then listed twice), then removes the
        old row. That drag runs its own event loop, so anything deferred could
        run between the two steps. Each signal is therefore handled at once:

        * a state with duplicates is mid-drag: remember the duplicated layers
          (they are the dragged ones) and wait;
        * the next clean state completes the drag: reconcile with that exact
          hint, and report it as an undo step (``external_edit``).

        Skipped for our own writes, while loading, and while an ALO change is
        waiting to be applied (QGIS's order is then stale).
        """
        if self._applying or self._loading:
            return
        if self._apply_timer.isActive() or not self._model.get_control_enabled():
            self._dragged.clear()
            return
        qgis_order = [lyr.id() for lyr in QgsProject.instance().layerTreeRoot().customLayerOrder()]
        seen: set = set()
        duplicates = {lid for lid in qgis_order if lid in seen or seen.add(lid)}
        if duplicates:
            self._dragged |= duplicates
            return
        dragged, self._dragged = self._dragged, set()
        if qgis_order == self._model.get_flattened_layer_ids():
            return
        new_root = reconcile_tree(self._model.get_root(), qgis_order, dragged)
        if new_root is None:
            # Not a pure reorder (layers being added/removed): those paths handle it
            log.debug("reconcile skipped: QGIS order is not a permutation of ALO layers")
            return
        log.info("reconciled with the QGIS order (dragged: %s)", sorted(dragged))
        before = self._model.serialize()
        self._model.replace_root(new_root)
        self.external_edit.emit(before, self._model.serialize(), tr(LAYER_ORDER))

    def _apply_custom_order(self) -> None:
        """Write the Model's flattened order to QGIS's custom layer order.

        A different order changes the project (QGIS saves it), so the project is
        marked modified; re-applying the same order is not a change (opening a
        project stays clean).
        """
        self._apply_timer.stop()
        if self._applying or not self._model.get_control_enabled():
            return
        proj = QgsProject.instance()
        layers = [lyr for lyr in (proj.mapLayer(lid) for lid in self._model.get_flattened_layer_ids())
                  if lyr is not None]
        root = proj.layerTreeRoot()
        changed = (not root.hasCustomLayerOrder()
                   or [lyr.id() for lyr in root.customLayerOrder()] != [lyr.id() for lyr in layers])
        self._applying = True
        try:
            root.setHasCustomLayerOrder(True)
            root.setCustomLayerOrder(layers)
            log.debug("applied %d layer(s) to the custom order", len(layers))
        finally:
            self._applying = False
        if changed:
            proj.setDirty(True)
        self._iface.mapCanvas().refresh()

    def _qgis_has_model_order(self) -> bool:
        """Tell whether QGIS already renders the Model's order.

        True right after a reconcile, for example: scheduling an apply then would
        be a no-op that only blocks the next Layer Order panel drag while pending.

        Returns:
            True if control is on and QGIS's custom order equals the Model's.
        """
        root = QgsProject.instance().layerTreeRoot()
        return (root.hasCustomLayerOrder()
                and [lyr.id() for lyr in root.customLayerOrder()] == self._model.get_flattened_layer_ids())

    def _refresh_native_panel(self) -> None:
        """Make QGIS's own Layer Order panel show the real order.

        That panel re-reads the order only on customLayerOrderChanged, never when
        hasCustomLayerOrder flips, and QGIS skips the signal when the order is
        unchanged. Turning control on (or off, or loading a project) could leave
        it showing the previous order while the map uses another. Setting the
        same order through an empty one forces the signal; ``_applying`` keeps
        the Controller from reacting to its own echo.
        """
        root = QgsProject.instance().layerTreeRoot()
        order = root.customLayerOrder()
        self._applying = True
        try:
            root.setCustomLayerOrder([])
            root.setCustomLayerOrder(order)
        finally:
            self._applying = False

    # ------------------------------------------------------------------
    # Document: Model → project
    # ------------------------------------------------------------------
    def _on_model_event(self, event_type: str, payload: dict) -> None:
        """Push Model changes to QGIS: the order (debounced) and the document.

        Args:
            event_type: One of the Model's ``EVENT_*`` names.
            payload: The event's data (unused).
        """
        if self._loading:
            return
        if event_type == EVENT_ORDER_CHANGED and not self._qgis_has_model_order():
            self._apply_timer.start()
        if event_type in _DOCUMENT_EVENTS:
            self._save_document()

    def _save_document(self) -> None:
        """Store the document in the project, only while it has groups.

        Groups are the one thing QGIS doesn't keep. Without any, the order is
        QGIS's own custom layer order (saved by QGIS), so the plugin's entries
        (and the legacy ``BetterLayerOrder`` ones) are removed: a project ALO was
        never needed for carries no ALO data. The project is marked modified only
        when an entry actually changes.
        """
        proj = QgsProject.instance()
        if self._model.has_groups():
            raw = self._model.serialize()
            if proj.readEntry(ENTRY_SCOPE, ENTRY_TREE, "")[0] != raw:
                proj.writeEntry(ENTRY_SCOPE, ENTRY_TREE, raw)
                proj.setDirty(True)
            return
        present = [(scope, key) for scope, key in _DOCUMENT_ENTRIES if proj.readEntry(scope, key, "")[1]]
        for scope, key in present:
            proj.removeEntry(scope, key)
        if present:
            proj.setDirty(True)
