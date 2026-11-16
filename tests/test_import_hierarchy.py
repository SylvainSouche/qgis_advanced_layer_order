"""Import layer hierarchy (ViewController) and the plugin's own translations."""
from __future__ import annotations

import pytest

from advanced_layer_order import i18n
from advanced_layer_order.model import GroupNode, LayerNode, LayerOrderModel


def _tree(spec):
    """Build nodes from ``["A", ("G", ["B"])]`` (fresh group ids)."""
    return [GroupNode(id=f"g_{s[0]}", name=s[0], children=_tree(s[1])) if isinstance(s, tuple)
            else LayerNode(id=s, name=s) for s in spec]


def _shape(nodes):
    return [(n.name, _shape(n.children)) if isinstance(n, GroupNode) else n.id for n in nodes]


@pytest.fixture
def rig(qapp):
    from advanced_layer_order.view import LayerOrderView
    from advanced_layer_order.view_controller import ViewController

    class Rig:
        def __init__(self):
            self.panel = ["A", "B", "C"]     # what the Layers panel shows
            self.answer = True
            self.view = LayerOrderView()
            self.model = LayerOrderModel()
            self.model.set_control_enabled(True)
            self.model.replace_root(_tree(["A", "B", "C"]))
            self.vc = ViewController(self.model, self.view, hierarchy=lambda: _tree(self.panel))
            self.asked = []
            self.view.confirm = lambda title, text: self.asked.append(title) or self.answer

        def shape(self):
            return _shape(self.model.get_root())

        def import_(self):
            self.view.import_hierarchy_requested.emit()

    r = Rig()
    yield r
    r.view.deleteLater()


def test_import_from_a_default_tree_needs_no_confirmation(rig):
    rig.panel = [("Roads", ["A", "B"]), "C"]
    rig.import_()
    assert rig.shape() == [("Roads", ["A", "B"]), "C"]
    assert not rig.asked
    assert rig.vc.undo_stack.count() == 1


def test_import_is_one_undo_step(rig):
    rig.panel = [("Roads", ["A", ("Small", ["B"])]), "C"]
    rig.import_()
    rig.vc.undo_stack.undo()
    assert rig.shape() == ["A", "B", "C"]
    rig.vc.undo_stack.redo()
    assert rig.shape() == [("Roads", ["A", ("Small", ["B"])]), "C"]


def test_redo_restores_the_hierarchy_as_imported(rig):
    rig.panel = [("Roads", ["A", "B"]), "C"]
    rig.import_()
    rig.panel = [("Elsewhere", ["C"]), "B", "A"]      # the Layers panel changes afterwards
    rig.vc.undo_stack.undo()
    rig.vc.undo_stack.redo()
    assert rig.shape() == [("Roads", ["A", "B"]), "C"]


def test_identical_hierarchy_does_nothing(rig):
    rig.import_()                                       # panel A B C == tree A B C
    assert not rig.asked and rig.vc.undo_stack.count() == 0


@pytest.mark.parametrize("own", ["order", "groups"])
def test_confirmation_when_alo_has_its_own_order_or_groups(rig, own):
    if own == "order":
        rig.model.replace_root(_tree(["C", "A", "B"]))
    else:
        rig.model.replace_root(_tree([("Mine", ["A", "B"]), "C"]))
    before = rig.shape()
    rig.panel = [("Roads", ["A", "B"]), "C"]
    rig.answer = False
    rig.import_()
    assert rig.asked == [i18n.tr_plugin(i18n.IMPORT_HIERARCHY)]
    assert rig.shape() == before and rig.vc.undo_stack.count() == 0      # Cancel: nothing
    rig.answer = True
    rig.import_()
    assert rig.shape() == [("Roads", ["A", "B"]), "C"]


def test_import_keeps_exactly_the_models_layers(rig):
    rig.panel = [("Roads", ["A", "X"]), "C"]           # X unknown to the Model, B missing
    rig.import_()
    assert rig.shape() == [("Roads", ["A"]), "C", "B"]


def test_import_without_a_hierarchy_reader_does_nothing(qapp):
    from advanced_layer_order.view import LayerOrderView
    from advanced_layer_order.view_controller import ViewController
    view, model = LayerOrderView(), LayerOrderModel()
    model.replace_root(_tree(["A", "B"]))
    vc = ViewController(model, view)
    view.import_hierarchy_requested.emit()
    assert _shape(model.get_root()) == ["A", "B"] and vc.undo_stack.count() == 0
    view.deleteLater()


# ---------- the plugin's own translations ----------

@pytest.mark.parametrize("key", sorted(i18n._PLUGIN_TEXTS))
def test_every_own_sentence_is_translated_in_every_language(key):
    texts = i18n._PLUGIN_TEXTS[key]
    assert set(texts) == set(i18n.LANGUAGES), set(i18n.LANGUAGES) ^ set(texts)
    assert all(t.strip() and "  " not in t for t in texts.values())


def test_own_sentences_follow_qgis_language(monkeypatch):
    monkeypatch.setattr(i18n, "ui_language", lambda: "fr")
    assert i18n.tr_plugin(i18n.IMPORT_HIERARCHY) == "Importer la hiérarchie des couches"
    monkeypatch.setattr(i18n, "ui_language", lambda: "sv")          # no translation: English
    assert i18n.tr_plugin(i18n.IMPORT_HIERARCHY) == "Import layer hierarchy"


def test_ui_language_outside_qgis_is_english():
    assert i18n.ui_language() == "en"     # the test stubs have no QgsApplication.locale
