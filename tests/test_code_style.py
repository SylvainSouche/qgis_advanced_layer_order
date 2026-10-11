"""docs/CODE_STYLE.md, checked: every module, class and function of the
plugin (private and nested ones included) is documented, Google style."""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULES = sorted(ROOT.glob("*.py"))
BANNERS = (re.compile(r"^# ={70}$"), re.compile(r"^    # -{66}$"))


def _definitions(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node


def _params(fn: ast.FunctionDef) -> list[str]:
    a = fn.args
    names = [p.arg for p in a.posonlyargs + a.args + a.kwonlyargs]
    names += [f"*{a.vararg.arg}"] if a.vararg else []
    names += [f"**{a.kwarg.arg}"] if a.kwarg else []
    return [n for n in names if n not in ("self", "cls")]


def _returns_value(fn: ast.FunctionDef) -> bool:
    if fn.returns is not None:
        return not (isinstance(fn.returns, ast.Constant) and fn.returns.value is None)
    return False


def _problems(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = [] if ast.get_docstring(tree) else [f"{path.name}: module docstring"]
    for node in _definitions(tree):
        where = f"{path.name}:{node.lineno} {node.name}"
        doc = ast.get_docstring(node)
        if not doc:
            out.append(f"{where}: no docstring")
            continue
        summary = doc.strip().splitlines()[0]
        if not summary.rstrip().endswith((".", ":")):
            out.append(f"{where}: summary line should be a sentence: {summary!r}")
        if isinstance(node, ast.ClassDef):
            continue
        params = _params(node)
        if params:
            if "Args:" not in doc:
                out.append(f"{where}: no Args: section for {params}")
            else:
                args = doc.split("Args:", 1)[1]
                missing = [p for p in params if not re.search(rf"^\s+{re.escape(p)}( \(|:)", args, re.M)]
                if missing:
                    out.append(f"{where}: Args: misses {missing}")
        if _returns_value(node) and "Returns:" not in doc and "Yields:" not in doc:
            out.append(f"{where}: no Returns:/Yields: section")
    return out


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_everything_is_documented(path: pathlib.Path) -> None:
    problems = _problems(path)
    assert not problems, "\n" + "\n".join(problems)


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_section_banners_follow_the_convention(path: pathlib.Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    bad = [f"{path.name}:{i}: {line.strip()[:20]}…" for i, line in enumerate(lines, 1)
           if re.match(r"^\s*# [=\-]{10,}", line) and not any(b.match(line) for b in BANNERS)]
    assert not bad, "\n" + "\n".join(bad)


# QGIS 4 / Qt 6 want scoped enums (Qgis.MessageLevel.Critical, Qt.ItemDataRole.UserRole);
# plugins.qgis.org's Qt6 checker rejects the short form (Qgis.Critical). Type
# annotations name enum classes (-> Qt.CheckState) and are not values.
def _annotation_nodes(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        anns = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            anns = [p.annotation for p in a.posonlyargs + a.args + a.kwonlyargs] + [node.returns]
            anns += [x.annotation for x in (a.vararg, a.kwarg) if x]
        elif isinstance(node, ast.AnnAssign):
            anns = [node.annotation]
        for ann in anns:
            if ann is not None:
                ids.update(id(n) for n in ast.walk(ann))
    return ids


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_enums_are_scoped(path: pathlib.Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _annotation_nodes(tree)
    parents = {id(ch): node for node in ast.walk(tree) for ch in ast.iter_child_nodes(node)}
    bad = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in ("Qgis", "Qt") and node.attr[:1].isupper() and id(node) not in skip):
            parent = parents.get(id(node))
            scoped = isinstance(parent, ast.Attribute) and parent.value is node      # Qgis.MessageLevel.X
            called = isinstance(parent, ast.Call) and parent.func is node           # Qgis.someFunction()
            if not (scoped or called):
                bad.append(f"{path.name}:{node.lineno}: {node.value.id}.{node.attr}")
    assert not bad, "\n" + "\n".join(bad)
