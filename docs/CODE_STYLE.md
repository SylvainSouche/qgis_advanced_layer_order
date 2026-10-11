# Advanced Layer Order — Code style

How the plugin's code is laid out and documented. `ruff` (`make lint`)
and `tests/test_code_style.py` enforce what can be checked.

## Module layout

Every module, top to bottom:

1. **Docstring.** What the module is for, its place in the MVC
   ([ARCHITECTURE.md](ARCHITECTURE.md)) and the contract others rely on.
2. `from __future__ import annotations`, so `X | None` works on Python 3.9.
3. **Imports** in three groups (ruff/isort): standard library, then
   QGIS / Qt, then this package. Imports needed only for annotations go
   in a final `if TYPE_CHECKING:` block.
4. **Constants**, `UPPER_CASE`, each with a comment when its value isn't
   self-explanatory. `_PRIVATE` constants are module-internal.
5. **Functions**, helpers before the code that uses them.
6. **Classes.**

When a module has several parts, they are separated by a module-level
banner:

```python
# ======================================================================
# Part name
# ======================================================================
```

## Class layout

1. **Docstring.** Responsibility, and what the class must not do.
2. **Qt signals**, with a comment giving the meaning of each argument.
3. **Class constants.**
4. `__init__`, then the lifecycle (`teardown`, …).
5. **Sections**, one per responsibility, public methods first and then
   their private helpers. A section starts with:

```python
    # ------------------------------------------------------------------
    # Section name
    # ------------------------------------------------------------------
```

Only these two banners are used: `=` between parts of a module, `-`
between sections of a class.

## Naming

* `_leading_underscore` for anything not used outside its module/class.
* Qt-style `camelCase` only for Qt overrides (`data`, `dropMimeData`, …).
* Signal handlers are named `_on_<what happened>`; methods that turn user
  input into an intent are named `_request_<what>`; View methods the
  ViewController calls are named `render_<what>`.

## Docstrings (Google style)

**Every** function has one: public, private, nested, Qt overrides.

```python
def move_items(self, item_ids: list[str], new_parent_id: str | None,
               before_id: str | None = None) -> bool:
    """Move nodes, as one block, into a group just before a sibling.

    The anchor is resolved after the movers are taken out, so shifting
    indices can't throw it off. O(n).

    Args:
        item_ids: Nodes to move, in the order they should end up in.
        new_parent_id: Target group, or None for the top level.
        before_id: Sibling to insert before, or None for the end.

    Returns:
        True if anything moved; False (and no event) otherwise.
    """
```

* **Summary line**: one sentence saying what the function does, ending
  with a period.
* **Body** (optional): why, rules, edge cases, complexity.
* **`Args:`** every parameter except `self` / `cls`, by name
  (`*args: Ignored.` is fine for signal handlers that ignore their
  arguments).
* **`Returns:`** whenever the function returns something (`-> None`
  needs none). `Yields:` for generators.
* **`Raises:`** for exceptions raised on purpose.
* **Qt overrides** say what Qt asks and what the plugin answers; they
  still list their arguments.

Comments explain *why*, never repeat *what* the code says.
