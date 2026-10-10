"""Sync algorithms between the ALO tree and QGIS's flat lists: reconciling
with a new layer order (below), and placing new layers (plan_new_layers).

When the user reorders layers in the stock Layer Order panel, QGIS only
gives us a new flat list. The group structure has to be inferred. This
module is pure Python (no Qt, no QGIS) so it can be unit-tested.

Algorithm:

1. Find which layers moved. The stock panel's intermediate signal (the
   dragged layer is briefly present twice) gives an exact hint. Without
   it a move can be ambiguous (``E A B`` → ``A E B``: E moved down, or A
   moved up), so each candidate is tried and the most plausible result
   kept: fewest group-membership changes, then a layer landing strictly
   between two members of a group rather than on a group's edge.
2. Detach the moved layers. Every other layer ("anchor") kept its relative
   order, so every group is still contiguous and keeps its members.
3. Re-insert each moved layer between its new flat neighbours. The places
   it can go without breaking group contiguity are the deepest group
   containing both neighbours, plus any group that ends at the previous
   neighbour or starts at the next one (the boundary cases).
     - If one of those is the layer's original group, it stays there
       (reordering inside a group never changes membership, even when
       the layer lands first/last).
     - Otherwise it goes to the outermost choice: dropping next to a
       group makes it a sibling, dropping strictly between two members
       of a group makes it a member (the only choice in that case).

Groups (including empty ones) are never created or deleted.
"""
from __future__ import annotations

import copy
from bisect import bisect_left
from collections.abc import Iterable

from .model import GroupNode, Node


def _flatten(nodes: list[Node]) -> list[str]:
    out = []
    for n in nodes:
        if isinstance(n, GroupNode):
            out.extend(_flatten(n.children))
        else:
            out.append(n.id)
    return out


def _lcs_ids(old: list, new: list) -> set:
    """Ids of a longest common subsequence of two permutations (via LIS)."""
    pos = {lid: i for i, lid in enumerate(old)}
    seq = [pos[lid] for lid in new]
    tails, tails_idx, prev = [], [], [-1] * len(seq)
    for i, v in enumerate(seq):
        k = bisect_left(tails, v)
        if k == len(tails):
            tails.append(v)
            tails_idx.append(i)
        else:
            tails[k] = v
            tails_idx[k] = i
        prev[i] = tails_idx[k - 1] if k > 0 else -1
    keep = set()
    i = tails_idx[-1] if tails_idx else -1
    while i != -1:
        keep.add(new[i])
        i = prev[i]
    return keep


def _same_without(old: list, new: list, ids: set) -> bool:
    return [x for x in old if x not in ids] == [x for x in new if x not in ids]


def _single_moves(old: list, new: list) -> list:
    """Every single layer whose move alone turns `old` into `new`, in `old`
    order. O(n): a move from p to q changes exactly the span p..q, so the
    layer is the span's first (moved down) or last (moved up) one."""
    n = len(old)
    i = 0
    while i < n and old[i] == new[i]:
        i += 1
    if i == n:
        return []
    j = n - 1
    while old[j] == new[j]:
        j -= 1
    out = []
    if new[j] == old[i] and old[i + 1:j + 1] == new[i:j]:
        out.append({old[i]})
    if new[i] == old[j] and old[i:j] == new[i + 1:j + 1]:
        out.append({old[j]})
    return out


def _moved_candidates(old: list, new: list, hint: Iterable[str]) -> list:
    """Possible sets of moved layers, most likely first.

    A valid drag hint is the answer. Otherwise a single-layer move is often
    ambiguous (``E A B`` → ``A E B``: E moved down, or A moved up), so every
    single layer that explains the change is a candidate; failing that, the
    complement of a longest common subsequence (O(n log n)).
    """
    hint = set(hint or ()) & set(old)
    if hint and _same_without(old, new, hint):
        return [hint]
    return _single_moves(old, new) or [set(old) - _lcs_ids(old, new)]


def _parents(nodes: list[Node], parent: str | None = None,
             out: dict[str, str | None] | None = None) -> dict[str, str | None]:
    """{layer id: parent group id or None}."""
    out = {} if out is None else out
    for n in nodes:
        if isinstance(n, GroupNode):
            _parents(n.children, n.id, out)
        else:
            out[n.id] = parent
    return out


def _edges(nodes: list[Node], parent: GroupNode | None = None,
           out: dict[str, bool] | None = None) -> dict[str, bool]:
    """{layer id: True if top level, or first or last in its group}."""
    out = {} if out is None else out
    last = len(nodes) - 1
    for i, n in enumerate(nodes):
        if isinstance(n, GroupNode):
            _edges(n.children, n, out)
        else:
            out[n.id] = parent is None or i in (0, last)
    return out


def _score(old_root: list, new_root: list, moved: set) -> tuple:
    """Lower is more plausible: fewest membership changes, then layers that
    landed strictly between two members of a group (unambiguous) first."""
    before, after = _parents(old_root), _parents(new_root)
    changed = sum(before[lid] != after[lid] for lid in before)
    edges = _edges(new_root)
    return changed, sum(edges[lid] for lid in moved)


def plan_new_layers(panel_order: list[str], known: set[str],
                    new: list[str]) -> list[tuple[str, str | None, bool]]:
    """Where new layers go: ``(layer id, anchor id, after)`` in insertion order.

    Each new layer goes just before the nearest known layer below it in the
    Layers panel (`panel_order`, top first); below every known layer, just
    after the lowest one. Layers missing from the panel count as being at
    its bottom. With no known layer at all, anchors are None: append in
    panel order. O(n).
    """
    targets = set(new)
    order = [lid for lid in panel_order if lid in known or lid in targets]
    in_panel = set(order)
    order += [lid for lid in new if lid not in in_panel]
    plan: list[tuple[str, str | None, bool]] = []
    below: str | None = None
    trailing: list[str] = []
    for lid in reversed(order):          # bottom-up: the nearest known layer below
        if lid in known:
            below = lid
        elif below is not None:
            plan.append((lid, below, False))
        else:
            trailing.append(lid)
    plan.reverse()
    lowest = next((lid for lid in reversed(order) if lid in known), None)
    return plan + [(lid, lowest, True) for lid in reversed(trailing)]


def reconcile_tree(root: list, new_order: list, moved_hint: Iterable[str] = ()) -> list | None:
    """Return a new tree whose flattened order is `new_order`.

    `root` is not modified. Returns None if `new_order` is not a
    permutation of the tree's layers (duplicates, additions, removals).
    """
    old_order = _flatten(root)
    if len(new_order) != len(set(new_order)) or set(new_order) != set(old_order):
        return None
    if new_order == old_order:
        return copy.deepcopy(root)

    candidates = _moved_candidates(old_order, new_order, moved_hint)
    results = [(_place_moved(root, new_order, moved), moved) for moved in candidates]
    return min(results, key=lambda r: _score(root, r[0], r[1]))[0]


class _Index:
    """Parent links of a tree being edited: O(1) parent, O(depth) ancestry."""

    def __init__(self, root: list[Node]) -> None:
        self.parent: dict[str, GroupNode | None] = {}
        self.node: dict[str, Node] = {}
        stack: list[tuple[list[Node], GroupNode | None]] = [(root, None)]
        while stack:
            children, p = stack.pop()
            for ch in children:
                self.parent[ch.id] = p
                self.node[ch.id] = ch
                if isinstance(ch, GroupNode):
                    stack.append((ch.children, ch))

    def chain(self, node_id: str | None) -> list[GroupNode]:
        """Ancestor groups of node_id, outermost first ([] for top level or None)."""
        out = []
        p = self.parent[node_id] if node_id else None
        while p is not None:
            out.append(p)
            p = self.parent[p.id]
        return out[::-1]

    def child_containing(self, group: GroupNode | None, node_id: str | None) -> str | None:
        """Id of the child of `group` (None = top level) that is, or contains,
        node_id; None if node_id is not inside `group`."""
        if node_id is None:
            return None
        cur = node_id
        while self.parent[cur] is not group:
            if self.parent[cur] is None:
                return None
            cur = self.parent[cur].id
        return cur


def _detach(nodes: list[Node], ids: set[str]) -> list[Node]:
    """`nodes` without the layers in `ids` (groups are kept, even emptied)."""
    out = []
    for n in nodes:
        if n.id in ids:
            continue
        if isinstance(n, GroupNode):
            n.children = _detach(n.children, ids)
        out.append(n)
    return out


def _next_anchors(new_order: list[str], moved: set[str]) -> list[str | None]:
    """For each position, the next layer after it that did not move."""
    out: list[str | None] = [None] * len(new_order)
    anchor = None
    for pos in range(len(new_order) - 1, -1, -1):
        out[pos] = anchor
        if new_order[pos] not in moved:
            anchor = new_order[pos]
    return out


def _target_group(prev_chain: list[GroupNode], next_chain: list[GroupNode],
                  orig_parent_id: str | None) -> GroupNode | None:
    """Group a moved layer goes into, between neighbours with these ancestries.

    Valid places: the deepest group containing both neighbours, plus the
    groups that end at the previous neighbour or start at the next one.
    The layer's original group wins if it is one of them; otherwise the
    deepest common group (see the module docstring).
    """
    depth = 0
    while (depth < min(len(prev_chain), len(next_chain))
           and prev_chain[depth] is next_chain[depth]):
        depth += 1
    common = prev_chain[depth - 1] if depth else None
    for c in (common, *prev_chain[depth:], *next_chain[depth:]):
        if (c.id if c is not None else None) == orig_parent_id:
            return c
    return common


class _Insertions:
    """Where each moved layer goes, relative to the tree without them:
    just before / just after a node, or at the end of a group. Applied by
    one splice at the end, so placing k layers costs O(n + k), not O(n·k)."""

    def __init__(self) -> None:
        self.before: dict[str, list[Node]] = {}
        self.after: dict[str, list[Node]] = {}
        self.end: dict[str | None, list[Node]] = {}

    def add(self, index: _Index, target: GroupNode | None, prev_id: str | None,
            next_id: str | None, layer: Node) -> None:
        """Right after the previous neighbour's branch in `target`, else right
        before the next one's, else at the end of `target`."""
        x = index.child_containing(target, prev_id)
        y = index.child_containing(target, next_id) if x is None else None
        if x is not None:
            self.after.setdefault(x, []).append(layer)
        elif y is not None:
            self.before.setdefault(y, []).append(layer)
        else:
            self.end.setdefault(getattr(target, "id", None), []).append(layer)

    def splice(self, children: list[Node], group_id: str | None = None) -> list[Node]:
        out: list[Node] = []
        stack = [(True, ch) for ch in reversed([*children, *self.end.get(group_id, ())])]
        while stack:   # iterative: chains of "after" can be as long as k
            expand, n = stack.pop()
            if not expand:
                if isinstance(n, GroupNode):
                    n.children = self.splice(n.children, n.id)
                out.append(n)
                continue
            stack.extend((True, m) for m in reversed(self.after.get(n.id, ())))
            stack.append((False, n))
            stack.extend((True, m) for m in reversed(self.before.get(n.id, ())))
        return out


def _place_moved(root: list[Node], new_order: list[str], moved: set[str]) -> list[Node]:
    """Detach `moved` layers and re-insert them at their new flat positions.

    O(n + k·depth): the tree is indexed once, each moved layer is placed
    through parent links, and one final splice inserts them all.
    """
    root = copy.deepcopy(root)
    moved = set(moved)
    index = _Index(root)
    orig_parent = {lid: getattr(index.parent[lid], "id", None) for lid in moved}
    root = _detach(root, moved)
    next_anchor = _next_anchors(new_order, moved)
    insertions = _Insertions()
    for pos, lid in enumerate(new_order):
        if lid not in moved:
            continue
        prev_id = new_order[pos - 1] if pos > 0 else None
        next_id = next_anchor[pos]
        target = _target_group(index.chain(prev_id), index.chain(next_id), orig_parent[lid])
        insertions.add(index, target, prev_id, next_id, index.node[lid])
        index.parent[lid] = target   # now placed: an anchor for the moved layers after it
    return insertions.splice(root)
