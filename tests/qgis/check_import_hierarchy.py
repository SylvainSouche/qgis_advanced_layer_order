"""Import layer hierarchy: ALO takes the Layers panel's groups and order.

One undo step; a confirmation only when ALO would lose an order or groups
of its own; redo restores the hierarchy as imported, even after the Layers
panel changed.
"""
import importlib

from harness import FakeIface, check, finish, pump
from qgis.core import QgsProject, QgsVectorLayer

from advanced_layer_order.i18n import IMPORT_HIERARCHY, tr_plugin

STEP = tr_plugin(IMPORT_HIERARCHY)   # in QGIS's interface language

proj = QgsProject.instance()
proj.clear()
layers = {n: QgsVectorLayer("Point?crs=EPSG:4326", n, "memory") for n in "ABCDEFG"}
proj.addMapLayers(list(layers.values()), False)
root = proj.layerTreeRoot()
# Layers panel:  Roads[A, B], Water[C, Rivers[D]], E, Empty[], F
# G is in the project but not in the Layers panel: QGIS doesn't draw it, ALO doesn't list it
roads = root.addGroup("Roads")
roads.addLayer(layers["A"])
roads.addLayer(layers["B"])
water = root.addGroup("Water")
water.addLayer(layers["C"])
water.addGroup("Rivers").addLayer(layers["D"])
root.addLayer(layers["E"])
root.addGroup("Empty")
root.addLayer(layers["F"])
water.setExpanded(False)

iface = FakeIface()
plugin = importlib.import_module("advanced_layer_order").classFactory(iface)
plugin.initGui()
pump()
model, view, vc = plugin.model, plugin.view, plugin.view_controller
stack = vc.undo_stack
asked = []
answer = [True]


def fake_confirm(title: str, text: str) -> bool:
    asked.append((title, text))
    return answer[0]


view.confirm = fake_confirm
if not model.get_control_enabled():
    view.chk_control.click()
    pump()


def name(lid: str) -> str:
    return proj.mapLayer(lid).name()


def shape(nodes=None) -> list:
    nodes = model.get_root() if nodes is None else nodes
    return [(n.name, shape(n.children)) if hasattr(n, "children") else name(n.id) for n in nodes]


def qgis_order() -> str:
    return "".join(lyr.name() for lyr in root.customLayerOrder())


PANEL = [("Roads", ["A", "B"]), ("Water", ["C", ("Rivers", ["D"])]), "E", ("Empty", []), "F"]

check(not model.has_groups() and "".join(map(name, model.get_flattened_layer_ids())) == "ABCDEF",
      f"ALO starts flat, in the Layers-panel order: {shape()}")
check(view.btn_import_hierarchy.isEnabled(), "button enabled while ALO controls the order")

# 1. Nothing of ALO's own to lose: no popup, one undo step
view.btn_import_hierarchy.click()
pump()
check(not asked, f"no confirmation when ALO has nothing of its own: {asked}")
check(shape() == PANEL, f"hierarchy imported: {shape()}")
water_node = next(n for n in model.get_root() if getattr(n, "name", "") == "Water")
check(not water_node.expanded, "a collapsed Layers-panel group is imported collapsed")
check(qgis_order() == "ABCDEF", f"QGIS renders the imported order: {qgis_order()}")
check(stack.count() == 1 and stack.undoText() == STEP,
      f"one undo step: {stack.undoText()!r}")
vc.undo_stack.undo()
pump()
check(not model.has_groups(), "undo: back to the flat tree")
vc.undo_stack.redo()
pump()
check(shape() == PANEL, "redo: hierarchy again")

# 2. Importing an identical hierarchy does nothing
view.btn_import_hierarchy.click()
pump()
check(not asked and stack.count() == 1, "identical: no popup, no undo step")

# 3. ALO has its own order: the user is asked; Cancel changes nothing
vc.handle_drop([layers["A"].id()], "", "end")      # A to the bottom: ALO's own order
pump()
before, steps = shape(), stack.count()
answer[0] = False
view.btn_import_hierarchy.click()
pump()
check(len(asked) == 1 and asked[0][0] == STEP, f"confirmation asked: {asked}")
check(shape() == before and stack.count() == steps, "Cancel: nothing changed")
answer[0] = True
view.btn_import_hierarchy.click()
pump()
check(len(asked) == 2 and shape() == PANEL and stack.count() == steps + 1, "OK: imported, one more undo step")

# 4. Redo restores the hierarchy as imported, even after the Layers panel changed
# (renamed and added groups: changes that leave QGIS's drawing order alone;
# a layer moved in the Layers panel also changes QGIS's custom order, which
# ALO reconciles as a new undo step, and a new step drops the redo history)
roads.setName("Highways")
root.addGroup("Later")
pump()
vc.undo_stack.undo()                          # undo the import
pump()
check("".join(map(name, model.get_flattened_layer_ids())) == "BCDEFA", "undo: back to ALO's own order")
vc.undo_stack.redo()                          # redo it
pump()
check(shape() == PANEL, f"redo restores the hierarchy as imported, not the changed panel: {shape()}")

# 5. ALO groups of its own (same order) also ask before being replaced
model.create_group("Mine")
pump()
asked.clear()
answer[0] = False
view.btn_import_hierarchy.click()
pump()
check(len(asked) == 1, "own groups: confirmation asked")

# 6. Control off: the tools are disabled
view.chk_control.click()
pump()
check(not view.btn_import_hierarchy.isEnabled(), "button disabled while ALO doesn't control the order")

plugin.unload()
finish()
