"""The plugin stores data in a project only while it holds something QGIS
doesn't — groups — and removes it once the last group is gone; without
groups, the order survives a save / reopen through QGIS's own custom order."""
import importlib
import os
import tempfile

from harness import FakeIface, check, finish, pump
from qgis.core import QgsProject, QgsVectorLayer

proj = QgsProject.instance()
proj.clear()
for name in "ABCDE":
    proj.addMapLayer(QgsVectorLayer("Point?crs=EPSG:4326", name, "memory"))
path = os.path.join(tempfile.mkdtemp(), "plain.qgz")
proj.write(path)
proj.read(path)

iface = FakeIface()
plugin = importlib.import_module("advanced_layer_order").classFactory(iface)
plugin.initGui()
pump()
model, vc, view = plugin.model, plugin.view_controller, plugin.view


def entries() -> dict:
    found = {s: proj.entryList(s, "") for s in ("AdvancedLayerOrder", "BetterLayerOrder")}
    return {s: keys for s, keys in found.items() if keys}


def names() -> str:
    return "".join(proj.mapLayer(i).name() for i in model.get_flattened_layer_ids())


def reopen() -> None:
    proj.write(path)
    proj.read(path)
    iface.projectRead.emit()
    pump()


check(not entries() and not proj.isDirty(), "opening a project changes nothing")
reopen()
check(not entries() and not proj.isDirty(), "save + reopen, nothing done: still nothing")

view.chk_control.click()
pump()
check(proj.isDirty() and not entries(), "control on: project modified (QGIS's setting), no ALO data")

proj.setDirty(False)
vc.handle_drop([model.get_flattened_layer_ids()[0]], "", "end")
pump()
order = names()
check(proj.isDirty() and not entries(), "reorder without groups: modified (QGIS's order), no ALO data")
reopen()
check(names() == order and not entries(), f"the order comes back from QGIS's own data: {names()} vs {order}")

view.chk_control.click()                       # control off: ALO still shows that order after reopening
pump()
reopen()
check(names() == order, f"control off: same order after reopening: {names()} vs {order}")
view.chk_control.click()
pump()

view.ask_text = lambda *a: "G"
view.create_group_requested.emit(model.get_flattened_layer_ids()[:2])
pump()
check(entries() == {"AdvancedLayerOrder": ["tree_json"]}, f"a group is ALO data: {entries()}")
vc.undo_stack.undo()
pump()
check(not entries(), f"undoing the group removes the data: {entries()}")
vc.undo_stack.redo()
pump()
gid = next(n.id for n in model.get_root() if hasattr(n, "children"))
view.delete_groups_requested.emit([gid])
pump()
check(not entries(), f"deleting the last group removes the data: {entries()}")

# A project from Layer Order Plus / early builds: once its groups are gone,
# the legacy entry goes too (or the groups would come back on reopening)
legacy = ('{"version": 1, "children": '
          '[{"type": "group", "id": "grp_0000000001", "name": "Old", "children": []}]}')
proj.writeEntry("BetterLayerOrder", "tree_json", legacy)
proj.writeEntry("BetterLayerOrder", "removeEmptyGroups", True)
reopen()
check([n.name for n in model.get_root() if hasattr(n, "children")] == ["Old"], "legacy groups read")
view.delete_groups_requested.emit([model.get_root()[0].id])
pump()
check(not entries(), f"legacy entries removed with the last group: {entries()}")
reopen()
check(not model.has_groups(), "the deleted groups don't come back")

plugin.unload()
finish()
