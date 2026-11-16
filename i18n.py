"""User-visible strings, in the user's QGIS language.

The plugin ships no translation files. Every label QGIS also has is looked
up with QGIS's context and English text (``tr``), so the translator QGIS
installs at startup shows it in the user's language; each such pair exists
in QGIS 3.40 and 4.2 (tests/qgis/check_i18n.py checks the installed QGIS).
The few sentences QGIS has no equivalent for carry their own translations
here (``tr_plugin``), using QGIS's terms in each language, with an English
fallback.
"""
from __future__ import annotations

from qgis.PyQt.QtCore import QCoreApplication

ADD_GROUP = ("QgsLayerTreeViewDefaultActions", "&Add Group")
RENAME_GROUP = ("QgsLayerTreeViewDefaultActions", "Re&name Group")
REMOVE_GROUP = ("QgsAppLayerTreeViewMenuProvider", "&Remove Group…")
MOVE_UP = ("QgsEffectStackPropertiesWidgetBase", "Move up")
MOVE_DOWN = ("QgsEffectStackPropertiesWidgetBase", "Move down")
MOVE_TO_TOP = ("QgsLayerTreeViewDefaultActions", "Move to &Top")
MOVE_TO_BOTTOM = ("QgsLayerTreeViewDefaultActions", "Move to &Bottom")
MOVE_ITEMS = ("QgsGraphicsViewMouseHandles", "Move Items")
UNDO = ("UndoWidget", "Undo")
REDO = ("UndoWidget", "Redo")
NEW_GROUP = ("QgsModelDesignerDialog", "New Group")
GROUP_NAME = ("QgsModelOutputReorderWidgetBase", "Group name")
CONTROL_RENDERING_ORDER = ("QgsCustomLayerOrderWidget", "Control rendering order")
LAYER_ORDER = ("QgisApp", "Layer Order")
DRAG_TO_REORDER = ("QgsDiagramPropertiesBase", "Drag and drop to reorder")

ENTRIES = (ADD_GROUP, RENAME_GROUP, REMOVE_GROUP, MOVE_UP, MOVE_DOWN, MOVE_TO_TOP,
           MOVE_TO_BOTTOM, MOVE_ITEMS, UNDO, REDO, NEW_GROUP, GROUP_NAME,
           CONTROL_RENDERING_ORDER, LAYER_ORDER, DRAG_TO_REORDER)


# Sentences QGIS has no equivalent for, with their translations
IMPORT_HIERARCHY = "import_hierarchy"
IMPORT_HIERARCHY_CONFIRM = "import_hierarchy_confirm"

LANGUAGES = ("en", "fr", "de", "es", "it", "pt", "nl")

_PLUGIN_TEXTS: dict[str, dict[str, str]] = {
    IMPORT_HIERARCHY: {
        "en": "Import layer hierarchy",
        "fr": "Importer la hiérarchie des couches",
        "de": "Layerhierarchie importieren",
        "es": "Importar la jerarquía de capas",
        "it": "Importa la gerarchia dei layer",
        "pt": "Importar a hierarquia de camadas",
        "nl": "Laaghiërarchie importeren",
    },
    IMPORT_HIERARCHY_CONFIRM: {
        "en": "This will replace Advanced Layer Order's layer order and groups with those "
              "of the Layers panel. It can be undone.",
        "fr": "Cette action remplacera l'ordre des couches et les groupes d'Advanced Layer "
              "Order par ceux du panneau des couches. Elle peut être annulée.",
        "de": "Dies ersetzt die Layerreihenfolge und die Gruppen von Advanced Layer Order "
              "durch die des Layerfensters. Dies kann rückgängig gemacht werden.",
        "es": "Esto reemplazará el orden de capas y los grupos de Advanced Layer Order por "
              "los del panel de capas. Se puede deshacer.",
        "it": "Questa azione sostituirà l'ordine dei layer e i gruppi di Advanced Layer "
              "Order con quelli del pannello Layer. Può essere annullata.",
        "pt": "Isto substituirá a ordem das camadas e os grupos do Advanced Layer Order "
              "pelos do painel Camadas. Pode ser desfeito.",
        "nl": "Dit vervangt de laagvolgorde en de groepen van Advanced Layer Order door die "
              "van het paneel Lagen. Dit kan ongedaan worden gemaakt.",
    },
}


def ui_language() -> str:
    """Give the language QGIS's interface is in.

    Returns:
        The two-letter code of QGIS's locale (``"pt"`` for ``pt_BR``), or
        ``"en"`` outside QGIS.
    """
    try:
        from qgis.core import QgsApplication
        locale = QgsApplication.locale()
    except (ImportError, AttributeError):
        locale = ""
    return (locale or "en").split("_")[0].lower()


def tr_plugin(key: str) -> str:
    """Give one of the plugin's own sentences in the user's language.

    Args:
        key: One of this module's keys (``IMPORT_HIERARCHY`` …).

    Returns:
        The sentence in QGIS's interface language, or English when it has
        no translation here.
    """
    texts = _PLUGIN_TEXTS[key]
    return texts.get(ui_language(), texts["en"])


def tr(entry: tuple[str, str]) -> str:
    """Translate a borrowed QGIS label into the user's language.

    Args:
        entry: A ``(context, English text)`` pair from this module.

    Returns:
        QGIS's translation (the English text without a translator), without
        keyboard accelerator (``&``) or trailing ellipsis.
    """
    text = QCoreApplication.translate(*entry)
    return text.replace("&", "").rstrip(".… ")
