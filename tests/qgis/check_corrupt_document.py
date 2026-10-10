"""An unreadable saved tree is never silently lost: it is kept aside in the
project, a warning is logged, and the panel starts from QGIS's order."""
import logging

from harness import FakeIface, check, finish, open_test_project, pump
from qgis.core import QgsProject

from advanced_layer_order.controller import (
    ENTRY_SCOPE,
    ENTRY_TREE,
    ENTRY_TREE_UNREADABLE,
    LayerOrderController,
)
from advanced_layer_order.model import LayerOrderModel


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


capture = Capture()
logging.getLogger("AdvancedLayerOrder").addHandler(capture)

open_test_project()
proj = QgsProject.instance()
corrupt = '{"version": 1, "children": [{"type": "group", "id": "g1", "name": "AB", "chil'   # truncated
proj.writeEntry(ENTRY_SCOPE, ENTRY_TREE, corrupt)

model = LayerOrderModel()
ctl = LayerOrderController(model, FakeIface())
ctl.load_project()
pump()

qgis_layers = {lyr.id() for lyr in proj.mapLayers().values()}
check(set(model.get_flattened_layer_ids()) == qgis_layers, "the panel starts from QGIS's layers")


def kept() -> str:
    return proj.readEntry(ENTRY_SCOPE, ENTRY_TREE_UNREADABLE, "")[0]


check(kept() == corrupt, "the unreadable tree is kept aside")
check(any("unreadable" in m for m in capture.messages), f"a warning is logged: {capture.messages}")

# The next save writes a fresh tree but the kept copy survives
model.create_group("New")
pump()
check(proj.readEntry(ENTRY_SCOPE, ENTRY_TREE, "")[0] != corrupt, "a fresh tree is saved")
check(kept() == corrupt, "the kept copy survives the save")
ctl.teardown()
finish()
