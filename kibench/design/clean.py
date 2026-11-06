"""Schematic tidy-up: straighten wires, merge wire pieces, drop needless junctions, line up nearly aligned parts,
and place reference/value texts neatly next to their symbol.

Every step is one `Change` with a stable key. The planner applies steps one at a time to an in-memory model
and keeps a step only if the sheet's connectivity is unchanged and no wire overlaps or runs through a symbol.
Deselected keys are passed back as `exclude` and the plan is rebuilt, so any subset stays consistent.

    python -m kibench.design.clean path/to/sheet.kicad_sch            # list proposed changes
    python -m kibench.design.clean path/to/sheet.kicad_sch --write    # apply (backup + netlist check)
"""
from __future__ import annotations

import math
import shutil
import time
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from ..kicad.schematic import (EPS, GRID, Box, Field, Pt, SheetModel, Symbol, Wire, key, on_segment, same)

Seg = Tuple[Pt, Pt]


@dataclass
class CleanOptions:
    wires: bool = True          # straighten diagonal / crooked wires
    merge: bool = True          # merge collinear wire pieces, drop zero-length wires
    junctions: bool = True      # remove junction dots that join fewer than three things
    align: bool = True          # line up parts that are nearly in a row or column (moves by whole grid steps)
    fields: bool = True         # reference / value text next to the symbol


@dataclass
class Change:
    key: str
    kind: str                   # wire | merge | junction | align | fields
    title: str
    detail: str = ""
    refs: List[str] = field(default_factory=list)
    where: Pt = (0.0, 0.0)
    old_segs: List[Seg] = field(default_factory=list)
    new_segs: List[Seg] = field(default_factory=list)
    old_boxes: List[Box] = field(default_factory=list)
    new_boxes: List[Box] = field(default_factory=list)


KIND_LABEL = {"wire": "Straighten wire", "merge": "Merge wires", "junction": "Remove junction",
              "align": "Align part", "fields": "Tidy texts"}


@dataclass
class Plan:
    changes: List[Change]
    text: str                   # new file text (all non-excluded changes applied)
    original: str
    skipped: List[str] = field(default_factory=list)   # steps that were considered but would break something
    model: Optional[SheetModel] = None

    @property
    def changed(self) -> bool:
        return self.text != self.original


# ---------------------------------------------------------------------- state snapshots
def _snapshot(m: SheetModel):
    return ([(w.a, w.b, w.deleted) for w in m.wires], len(m.wires),
            [(j.pos, j.deleted) for j in m.junctions],
            [lb.pos for lb in m.labels],
            [(s.pos, [p.pos for p in s.pins], s.body,
              {k: (f.pos, f.rot, f.centered) for k, f in s.fields.items()}) for s in m.symbols],
            list(m.no_connects))


def _restore(m: SheetModel, snap) -> None:
    wires, n, juncs, labels, syms, ncs = snap
    del m.wires[n:]
    for w, (a, b, d) in zip(m.wires, wires):
        w.a, w.b, w.deleted = a, b, d
    for j, (p, d) in zip(m.junctions, juncs):
        j.pos, j.deleted = p, d
    for lb, p in zip(m.labels, labels):
        lb.pos = p
    for s, (pos, pins, body, flds) in zip(m.symbols, syms):
        s.pos, s.body = pos, body
        for p, pp in zip(s.pins, pins):
            p.pos = pp
        for k, (fp, fr, fc) in flds.items():
            f = s.fields[k]
            f.pos, f.rot, f.centered = fp, fr, fc
    m.no_connects[:] = ncs


def _stable_uuid(*parts: str) -> str:
    return str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, "kibench-clean:" + ":".join(parts)))


# ---------------------------------------------------------------------- geometry checks
class _Geo:
    """Indexes rebuilt per step (cheap enough for sheets with a few thousand items)."""

    def __init__(self, m: SheetModel):
        self.m = m
        self.anchor = {}
        for p, kind in m.anchor_points():
            self.anchor.setdefault(key(p), []).append(kind)
        self.ends: Dict[Tuple[int, int], List[Tuple[Wire, str]]] = {}
        for w in m.live_wires():
            self.ends.setdefault(key(w.a), []).append((w, "a"))
            self.ends.setdefault(key(w.b), []).append((w, "b"))

    def passes_through(self, p: Pt, skip=()) -> List[Wire]:
        return [w for w in self.m.live_wires() if w not in skip and on_segment(p, w.a, w.b)]

    def anchored(self, p: Pt) -> bool:
        return key(p) in self.anchor or bool(self.passes_through(p))

    def junction_at(self, p: Pt):
        return [j for j in self.m.junctions if not j.deleted and same(j.pos, p)]


def _overlap(w: Wire, o: Wire) -> bool:
    """Collinear axis-aligned wires sharing more than a point."""
    if w.horizontal and o.horizontal and abs(w.a[1] - o.a[1]) < EPS:
        lo, hi = max(min(w.a[0], w.b[0]), min(o.a[0], o.b[0])), min(max(w.a[0], w.b[0]), max(o.a[0], o.b[0]))
        return hi - lo > EPS
    if w.vertical and o.vertical and abs(w.a[0] - o.a[0]) < EPS:
        lo, hi = max(min(w.a[1], w.b[1]), min(o.a[1], o.b[1])), min(max(w.a[1], w.b[1]), max(o.a[1], o.b[1]))
        return hi - lo > EPS
    return False


def _seg_hits_box(a: Pt, b: Pt, bx: Box, shrink: float = 0.15) -> bool:
    x1, y1, x2, y2 = bx.x1 + shrink, bx.y1 + shrink, bx.x2 - shrink, bx.y2 - shrink
    if x1 >= x2 or y1 >= y2:
        return False
    if abs(a[1] - b[1]) < EPS:
        return y1 < a[1] < y2 and min(a[0], b[0]) < x2 and max(a[0], b[0]) > x1
    if abs(a[0] - b[0]) < EPS:
        return x1 < a[0] < x2 and min(a[1], b[1]) < y2 and max(a[1], b[1]) > y1
    return Box(x1, y1, x2, y2).hits_segment(a, b)


def _wires_ok(m: SheetModel, touched: List[Wire], moved_syms: Set[str] = frozenset()) -> Optional[str]:
    """Reason why the touched wires are not acceptable, or None."""
    live = m.live_wires()
    for w in touched:
        if w.deleted:
            continue
        if w.length < EPS:
            return "zero-length wire"
        for o in live:
            if o is not w and _overlap(w, o):
                return "wires would overlap"
        for s in m.symbols:
            if s.body is not None and _seg_hits_box(w.a, w.b, s.body):
                return f"wire would cross {s.ref}"
        for p in (w.a, w.b):         # a new T without a junction dot reads like a crossing
            for o in live:
                if o is not w and on_segment(p, o.a, o.b) and not any(
                        same(j.pos, p) for j in m.junctions if not j.deleted):
                    return "wire end would touch another wire"
    return None


# ---------------------------------------------------------------------- the planner
class _Planner:
    def __init__(self, text: str, opts: CleanOptions, exclude: Set[str]):
        self.m = SheetModel(text)
        self.opts = opts
        self.exclude = exclude
        self.base = self.m.connectivity()
        # wires the user chose to keep as they are (any unticked straightening that touched them)
        self.kept_wires = {u for k in self.exclude if k.startswith("wire:") for u in k[5:].split("+")}
        self.changes: List[Change] = []
        self.skipped: List[str] = []

    def _try(self, fn, ch: Change) -> bool:
        """Run one step; keep it only if connectivity is unchanged and fn reports success."""
        snap = _snapshot(self.m)
        try:
            why = fn()
        except Exception as e:                 # a step must never break the plan
            why = f"internal error: {e}"
        if why is None and self.m.connectivity() != self.base:
            why = "would change a connection"
        if why is not None:
            _restore(self.m, snap)
            self.skipped.append(f"{ch.title}: {why}")
            return False
        self.changes.append(ch)
        return True

    # ------------------------------------------------------------ wires
    def _move_point(self, p: Pt, q: Pt) -> List[Wire]:
        moved = []
        for w in self.m.live_wires():
            if same(w.a, p):
                w.a = q
                moved.append(w)
            if same(w.b, p):
                w.b = q
                moved.append(w)
        for j in self.m.junctions:
            if not j.deleted and same(j.pos, p):
                j.pos = q
        return moved

    def _straighten_nodes(self) -> bool:
        """A free wire corner with diagonal wires moves to the one spot that makes all its wires straight."""
        progress = False
        geo = _Geo(self.m)
        for kp, ends in list(geo.ends.items()):
            p = (kp[0] / 1000, kp[1] / 1000)
            wires = [w for w, _e in ends if not w.deleted]
            diag = [w for w in wires if not (w.horizontal or w.vertical) and w.length > EPS]
            if not diag or geo.anchored(p) or len(wires) > 4:
                continue
            k = "wire:" + "+".join(sorted(w.uuid for w in diag))
            if any(w.uuid in self.kept_wires for w in diag) or any(c.key == k for c in self.changes):
                continue
            x, y = None, None
            ok = True
            for w in wires:
                q = w.b if same(w.a, p) else w.a
                if abs(q[0] - p[0]) >= abs(q[1] - p[1]):      # mostly horizontal: share the far end's y
                    ok &= y is None or abs(y - q[1]) < EPS
                    y = q[1] if y is None else y
                else:
                    ok &= x is None or abs(x - q[0]) < EPS
                    x = q[0] if x is None else x
            if not ok:
                continue
            target = (p[0] if x is None else x, p[1] if y is None else y)
            if same(target, p):
                continue
            ch = Change(k, "wire", f"Straighten wire corner at ({p[0]:.2f}, {p[1]:.2f})",
                        f"move the corner to ({target[0]:.2f}, {target[1]:.2f}) so its wires run straight", where=p,
                        old_segs=[(w.a, w.b) for w in wires])

            def do(p=p, target=target, ch=ch):
                moved = self._move_point(p, target)
                if any(not (w.horizontal or w.vertical) for w in moved):
                    return "a wire would stay diagonal"
                ch.new_segs = [(w.a, w.b) for w in moved]
                return _wires_ok(self.m, moved)
            if self._try(do, ch):
                progress = True
                geo = _Geo(self.m)
        return progress

    def straighten(self) -> None:
        for _ in range(4):
            progress = self._straighten_nodes()
            for w in list(self.m.live_wires()):
                if w.deleted or w.horizontal or w.vertical or w.length < EPS:
                    continue
                k = f"wire:{w.uuid}"
                if w.uuid in self.kept_wires or any(c.key == k for c in self.changes):
                    continue
                if self._straighten_one(w, k):
                    progress = True
            if not progress:
                break

    def _straighten_one(self, w: Wire, k: str) -> bool:
        a, b = w.a, w.b
        old = [(a, b)]
        horiz = abs(b[0] - a[0]) >= abs(b[1] - a[1])
        geo = _Geo(self.m)
        attempts = []
        for end, other in (("b", a), ("a", b)):
            p = getattr(w, end)
            if geo.anchored(p):
                continue
            q = (p[0], other[1]) if horiz else (other[0], p[1])
            attempts.append(("move", end, p, q))
        attempts += [("L", (b[0], a[1])), ("L", (a[0], b[1]))]
        for att in attempts:
            ch = Change(k, "wire", "", where=((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), old_segs=list(old))
            if att[0] == "move":
                _, end, p, q = att
                ch.title = f"Straighten wire at ({p[0]:.2f}, {p[1]:.2f})"
                ch.detail = "move the free end so the wire runs " + ("horizontally" if horiz else "vertically")

                def do(p=p, q=q, ch=ch):
                    others = [o for o, _e in _Geo(self.m).ends.get(key(p), []) if o is not w]
                    moved = self._move_point(p, q)
                    for o in others:            # no new diagonals at the moved point
                        if not (o.horizontal or o.vertical):
                            return "other wires at that point would turn diagonal"
                    ch.new_segs = [(x.a, x.b) for x in moved]
                    ch.old_segs = old + [(o.orig[0], o.orig[1]) for o in others]
                    return _wires_ok(self.m, moved)
            else:
                c = att[1]
                ch.title = f"Straighten wire at ({a[0]:.2f}, {a[1]:.2f})"
                ch.detail = "replace the diagonal with a right-angle route"

                def do(c=c, ch=ch):
                    if _Geo(self.m).anchored(c) or any(same(c, x) for x in (a, b)):
                        return "corner would land on something"
                    nw = Wire(_stable_uuid(w.uuid, "L"), c, b, None)
                    w.b = c
                    self.m.wires.append(nw)
                    ch.new_segs = [(w.a, w.b), (nw.a, nw.b)]
                    return _wires_ok(self.m, [w, nw])
            if self._try(do, ch):
                return True
        return False

    def merge(self) -> None:
        for w in list(self.m.live_wires()):
            if w.deleted:
                continue
            if w.length < EPS:
                k = f"zero:{w.uuid}"
                if k in self.exclude:
                    continue
                ch = Change(k, "merge", "Remove zero-length wire", where=w.a)

                def do(w=w):
                    w.deleted = True
                    return None
                self._try(do, ch)
        changed = True
        while changed:
            changed = False
            geo = _Geo(self.m)
            for kp, ends in list(geo.ends.items()):
                if len(ends) != 2:
                    continue
                (w1, e1), (w2, e2) = ends
                if w1 is w2 or w1.deleted or w2.deleted:
                    continue
                p = getattr(w1, e1)
                if not ((w1.horizontal and w2.horizontal) or (w1.vertical and w2.vertical)):
                    continue
                if geo.anchored(p) or geo.junction_at(p):
                    continue
                k = "merge:" + ":".join(sorted([w1.uuid, w2.uuid]))
                if k in self.exclude or any(c.key == k for c in self.changes):
                    continue
                far = w2.b if e2 == "a" else w2.a
                ch = Change(k, "merge", f"Merge wire pieces at ({p[0]:.2f}, {p[1]:.2f})", where=p,
                            old_segs=[(w1.a, w1.b), (w2.a, w2.b)])

                def do(w1=w1, e1=e1, w2=w2, far=far, ch=ch):
                    setattr(w1, e1, far)
                    w2.deleted = True
                    ch.new_segs = [(w1.a, w1.b)]
                    return _wires_ok(self.m, [w1])
                if self._try(do, ch):
                    changed = True
                    break

    def junctions(self) -> None:
        geo = _Geo(self.m)
        for j in self.m.junctions:
            if j.deleted:
                continue
            k = f"junction:{j.uuid}"
            if k in self.exclude:
                continue
            deg = len(geo.ends.get(key(j.pos), [])) + 2 * len(geo.passes_through(j.pos))
            deg += len([x for x in geo.anchor.get(key(j.pos), []) if not x.startswith("bus")])
            if deg >= 3:
                continue
            ch = Change(k, "junction", f"Remove junction dot at ({j.pos[0]:.2f}, {j.pos[1]:.2f})",
                        f"joins only {deg} item{'s' if deg != 1 else ''}", where=j.pos,
                        old_boxes=[Box(j.pos[0] - 0.5, j.pos[1] - 0.5, j.pos[0] + 0.5, j.pos[1] + 0.5)])

            def do(j=j):
                j.deleted = True
                return None
            self._try(do, ch)

    # ------------------------------------------------------------ parts
    def _attached_power(self, s: Symbol) -> List[Symbol]:
        pts = {key(p.pos) for p in s.pins}
        out = []
        for o in self.m.symbols:
            if o is s:
                continue
            hit = [p for p in o.pins if key(p.pos) in pts]
            if hit:
                if not o.is_power or len(o.pins) != 1:
                    raise ValueError(f"pin touches {o.ref} directly")
                out.append(o)
        return out

    def _move_symbol(self, s: Symbol, dx: float, dy: float) -> Optional[str]:
        """Move a symbol (and power symbols / labels sitting on its pins) by whole grid steps; wires attached to
        its pins follow so they stay horizontal/vertical. Returns why it cannot, or None."""
        try:
            group = [s] + self._attached_power(s)
        except ValueError as e:
            return str(e)
        geo = _Geo(self.m)
        start = {key(p.pos): p.pos for g in group for p in g.pins}
        own = set(start)
        # rigid items
        for g in group:
            g.pos = (g.pos[0] + dx, g.pos[1] + dy)
            for p in g.pins:
                p.pos = (round(p.pos[0] + dx, 4), round(p.pos[1] + dy, 4))
            if g.body is not None:
                g.body = g.body.shifted(dx, dy)
            for f in g.fields.values():
                f.pos = (f.pos[0] + dx, f.pos[1] + dy)
        for lb in self.m.labels:
            if key(lb.pos) in own:
                lb.pos = (lb.pos[0] + dx, lb.pos[1] + dy)
        self.m.no_connects[:] = [(p[0] + dx, p[1] + dy) if key(p) in own else p for p in self.m.no_connects]
        # rubber-band wires, keeping them orthogonal
        queue = list(start.values())
        seen: Set[Tuple[int, int]] = set()
        touched: List[Wire] = []
        straight: Dict[int, bool] = {}
        while queue:
            p = queue.pop()
            if key(p) in seen:
                continue
            seen.add(key(p))
            if key(p) not in own:
                if any(not k.startswith("pin:") or not any(k == f"pin:{g.uuid}:{n.number}" for g in group for n in g.pins)
                       for k in geo.anchor.get(key(p), [])):
                    return "a wire would have to drag another part or label"
                if geo.passes_through(p):
                    return "a wire would have to drag a junction on another wire"
            q = (round(p[0] + dx, 4), round(p[1] + dy, 4))
            for w, end in geo.ends.get(key(p), []):
                other = w.b if end == "a" else w.a
                along = (dx == 0 and w.vertical) or (dy == 0 and w.horizontal)
                straight.setdefault(id(w), w.horizontal or w.vertical)
                setattr(w, end, q)
                touched.append(w)
                if not along and key(other) not in seen:
                    queue.append(other)
            for j in self.m.junctions:
                if not j.deleted and same(j.pos, p):
                    j.pos = q
        for w in touched:
            if w.length < EPS:          # the part now sits right on the far end: the wire is not needed
                w.deleted = True
            elif straight.get(id(w)) and not (w.horizontal or w.vertical):
                return "an attached wire would turn diagonal"
        touched = [w for w in touched if not w.deleted]
        nb = s.bbox()
        for o in self.m.symbols:
            if o in group or o.body is None:
                continue
            if nb.overlaps(o.bbox(), -0.1):
                return f"would overlap {o.ref}"
        return _wires_ok(self.m, touched)

    def align(self) -> None:
        cands = [s for s in self.m.symbols if not s.is_power and s.body is not None
                 and len([p for p in s.pins if not p.hidden]) >= 2 and s.body.h <= 15 and s.body.w <= 15]
        for axis in ("row", "col"):
            parent = {id(s): id(s) for s in cands}

            def find(a):
                while parent[a] != a:
                    parent[a] = parent[parent[a]]
                    a = parent[a]
                return a
            for i, s in enumerate(cands):
                for o in cands[i + 1:]:
                    b1, b2 = s.body, o.body
                    if axis == "row":
                        near = abs(b1.cy - b2.cy) <= 2.6 and max(b1.x1, b2.x1) - min(b1.x2, b2.x2) <= 12.7
                    else:
                        near = abs(b1.cx - b2.cx) <= 2.6 and max(b1.y1, b2.y1) - min(b1.y2, b2.y2) <= 12.7
                    if near and round(s.rot) % 180 == round(o.rot) % 180:
                        parent[find(id(s))] = find(id(o))
            groups: Dict[int, List[Symbol]] = {}
            for s in cands:
                groups.setdefault(find(id(s)), []).append(s)
            for g in groups.values():
                if len(g) < 2:
                    continue
                vals = [round(s.body.cy if axis == "row" else s.body.cx, 2) for s in g]
                if len(set(vals)) < 2:
                    continue
                target = max(set(vals), key=lambda v: (vals.count(v), -abs(v - sorted(vals)[len(vals) // 2])))
                for s, v in zip(g, vals):
                    d = target - v
                    steps = round(d / GRID)
                    if abs(d) < EPS or steps == 0 or abs(d - steps * GRID) > 0.02:
                        continue
                    k = f"align:{s.uuid}"
                    if k in self.exclude or any(c.key == k for c in self.changes):
                        continue
                    others = ", ".join(sorted(x.ref for x in g if x is not s)[:4])
                    dx, dy = (0.0, steps * GRID) if axis == "row" else (steps * GRID, 0.0)
                    ch = Change(k, "align", f"Align {s.ref} with {others}",
                                f"move {'down' if dy > 0 else 'up' if dy < 0 else 'right' if dx > 0 else 'left'} "
                                f"{abs(steps) * GRID:.2f} mm", [s.ref], where=s.pos, old_boxes=[s.bbox()])

                    def do(s=s, dx=dx, dy=dy, ch=ch):
                        why = self._move_symbol(s, dx, dy)
                        ch.new_boxes = [s.bbox()]
                        return why
                    self._try(do, ch)

    # ------------------------------------------------------------ texts
    def _obstacles(self, s: Symbol, region: Box):
        boxes: List[Box] = []
        segs: List[Seg] = []
        for o in self.m.symbols:
            if o is s:
                continue
            bb = o.bbox()
            if bb.overlaps(region):
                if o.body is not None:
                    boxes.append(o.body)
                for f in o.fields.values():
                    if not f.hidden and f.value:
                        boxes.append(f.box(o.rot))
                for p in o.pins:
                    if not p.hidden:
                        segs.append((p.pos, (o.body.cx, o.body.cy) if o.body else p.pos))
        for w in self.m.live_wires():
            if Box(w.a[0], w.a[1], w.b[0], w.b[1]).overlaps(region):
                segs.append((w.a, w.b))
        for lb in self.m.labels:
            if region.x1 - 20 < lb.pos[0] < region.x2 + 20 and region.y1 - 20 < lb.pos[1] < region.y2 + 20:
                boxes.append(_label_box(lb))
        for bx in self.m.sheet_boxes:
            if bx.overlaps(region):
                boxes.append(bx)
        return boxes, segs

    def tidy_fields(self) -> None:
        for s in self.m.symbols:
            if s.is_power:
                continue
            vis = _text_group(s)
            if not vis:
                continue
            k = f"fields:{s.uuid}"
            if k in self.exclude:
                continue
            pins = [p for p in s.pins if not p.hidden]
            two_pin = _axial(pins, s.body)
            body = s.body if two_pin else s.bbox()
            region = Box(body.x1 - 15, body.y1 - 15, body.x2 + 15, body.y2 + 15)
            boxes, segs = self._obstacles(s, region)
            boxes += [f.box(s.rot) for f in s.fields.values()          # own other visible texts (footprint...)
                      if f not in vis and not f.hidden and f.value]
            own_segs = [(p.pos, (body.cx, body.cy)) for p in pins]
            hrot = 0.0 if round(s.rot) % 180 == 0 else 90.0

            def cost(place: Dict[str, Pt]) -> float:
                c = 0.0
                placed = []
                for f in vis:
                    fb = _field_box(f, place[f.name], hrot, s.rot)
                    for o in boxes:
                        if fb.overlaps(o, -0.05):
                            c += 10
                    for a, b in segs + own_segs:
                        if _seg_hits_box(a, b, fb, 0.05):
                            c += 10
                    if fb.overlaps(body, -0.05):
                        c += 10
                    for pb in placed:
                        if fb.overlaps(pb, -0.05):
                            c += 10
                    placed.append(fb)
                    c += 0.05 * fb.distance(body)
                return c

            cands = _field_candidates(s, vis, body, two_pin, pins, hrot)
            if not cands:
                continue
            current = {f.name: f.pos for f in vis}
            # text inside the part's own outline is often intentional (old connector symbols); only clashes count
            cur_cost = sum(10 for f in vis for o in boxes if f.box(s.rot).overlaps(o, -0.05))
            if two_pin:
                cur_cost += sum(10 for f in vis if f.box(s.rot).overlaps(body, -0.05))
            far = any(f.box(s.rot).distance(body) > 5.08 for f in vis)
            if cur_cost < 10 and not far and _tidy(vis, s.rot, body):
                continue
            if two_pin:
                matches = any(all(math.hypot(place[f.name][0] - f.pos[0], place[f.name][1] - f.pos[1]) < 0.3
                                  and f.draw_horizontal(s.rot) for f in vis) for place in cands)
                if matches:
                    continue
            elif cur_cost < 10 and not far:
                continue
            cx0 = sum(f.box().cx for f in vis) / len(vis)
            cy0 = sum(f.box().cy for f in vis) / len(vis)

            def moved(p):       # prefer staying on the side the texts are on now
                return math.hypot(sum(x for x, _ in p.values()) / len(p) - cx0, sum(y for _, y in p.values()) / len(p) - cy0)
            scored = sorted(((cost(p), 0.3 * i + 0.15 * moved(p), p) for i, p in enumerate(cands)),
                            key=lambda t: t[0] + t[1])
            best_cost, _, best = scored[0]
            if best_cost >= 10 and best_cost >= cur_cost and not far:
                continue                          # no clean spot; leave it as it is
            names = " / ".join(f.name.lower() for f in vis)
            why = "far from the symbol" if far else "overlapping" if cur_cost >= 10 else "uneven spacing"
            ch = Change(k, "fields", f"Tidy {s.ref} texts", f"{names} next to the symbol ({why})", [s.ref],
                        where=(body.cx, body.cy), old_boxes=[f.box(s.rot) for f in vis])

            def do(best=best, vis=vis, ch=ch, s=s):
                for f in vis:
                    f.pos = (round(best[f.name][0], 3), round(best[f.name][1], 3))
                    f.rot = hrot
                    f.centered = True
                ch.new_boxes = [f.box(s.rot) for f in vis]
                return None
            self._try(do, ch)

    def run(self) -> Plan:
        orig = self.m.text
        if self.opts.align:
            self.align()
        if self.opts.wires:
            self.straighten()
        if self.opts.merge:
            self.merge()
        if self.opts.junctions:
            self.junctions()
        if self.opts.fields:
            self.tidy_fields()
        return Plan(self.changes, self.m.render(), orig, self.skipped, self.m)


def _tidy(vis: List[Field], sym_rot: float, body: Box) -> bool:
    """Texts already read well: horizontal, close to the body, lined up with even gaps (or first line above
    the part and the rest below)."""
    if not all(f.draw_horizontal(sym_rot) for f in vis):
        return False
    bxs = [f.box(sym_rot) for f in vis]
    if any(b.distance(body) > 3.2 for b in bxs):
        return False
    for i, a in enumerate(bxs):
        for b in bxs[i + 1:]:
            if a.overlaps(b, -0.05):
                return False

    def block(bs):
        bs = sorted(bs, key=lambda b: b.cy)
        for a, b in zip(bs, bs[1:]):
            if not (min(abs(a.x1 - b.x1), abs(a.cx - b.cx), abs(a.x2 - b.x2)) < 0.3 and b.y1 - a.y2 < 1.2):
                return False
        return True
    if block(bxs):
        return True
    # a column beside the part, edges lined up, lines spread over the part's height
    for side in ("right", "left"):
        beside = all(b.x1 >= body.cx for b in bxs) if side == "right" else all(b.x2 <= body.cx for b in bxs)
        edge = [b.x1 for b in bxs] if side == "right" else [b.x2 for b in bxs]
        if beside and max(edge) - min(edge) < 0.3:
            ys = sorted(bxs, key=lambda b: b.cy)
            if all(b.y1 - a.y2 < 3.2 for a, b in zip(ys, ys[1:])):
                return True
    above = [b for b in bxs if b.y2 <= body.y1 + 0.3]
    below = [b for b in bxs if b.y1 >= body.y2 - 0.3]
    return len(above) + len(below) == len(bxs) and bool(above) and bool(below) and block(above) and block(below) \
        and min(abs(a.cx - b.cx) for a in above for b in below) < 1.0


def _axial(pins, body: Optional[Box]) -> bool:
    """Two pins on opposite ends of the body (resistor, capacitor, diode...), not a 2-pin connector."""
    if len(pins) != 2 or body is None:
        return False
    (x1, y1), (x2, y2) = pins[0].pos, pins[1].pos
    if abs(x1 - x2) < EPS:
        return body.x1 - EPS <= x1 <= body.x2 + EPS and min(y1, y2) < body.cy < max(y1, y2)
    if abs(y1 - y2) < EPS:
        return body.y1 - EPS <= y1 <= body.y2 + EPS and min(x1, x2) < body.cx < max(x1, x2)
    return False


def _label_box(lb) -> Box:
    L = max(1, len(lb.name)) * 1.27 * 0.82 + (1.5 if lb.kind != "label" else 0)
    h = 1.27 * 1.3
    x, y = lb.pos
    r = round(lb.rot) % 360
    if lb.kind == "label":
        return {0: Box(x, y - h, x + L, y), 180: Box(x - L, y - h, x, y),
                90: Box(x - h, y - L, x, y), 270: Box(x - h, y, x, y + L)}.get(r, Box(x, y - h, x + L, y))
    return {0: Box(x, y - h / 2, x + L, y + h / 2), 180: Box(x - L, y - h / 2, x, y + h / 2),
            90: Box(x - h / 2, y - L, x + h / 2, y), 270: Box(x - h / 2, y, x + h / 2, y + L)}.get(
        r, Box(x, y - h / 2, x + L, y + h / 2))


def _field_box(f: Field, pos: Pt, rot: float, sym_rot: float) -> Box:
    g = Field(f.name, f.value, pos, rot, False, f.size, f.node, "", centered=True, srot=f.srot, smirror=f.smirror)
    return g.box()


def _text_group(s: Symbol) -> List[Field]:
    """Visible texts placed as one block: reference, value, then short extra fields (e.g. 'Resistance')."""
    order = ["Reference", "Value"]
    out = [s.fields[n] for n in order if n in s.fields]
    out += [f for n, f in s.fields.items() if n not in order and n not in ("Footprint", "Datasheet", "Description")
            and len(f.value) <= 16 and not f.value.startswith(("http", "${"))]
    return [f for f in out if not f.hidden and f.value]


def _field_candidates(s: Symbol, vis: List[Field], body: Box, two_pin: bool, pins, hrot: float):
    """Candidate placements {field name: centre}. Texts are stacked line by line in `vis` order."""
    def w(f):
        return _field_box(f, (0, 0), hrot, s.rot).w

    lh = max(_field_box(f, (0, 0), hrot, s.rot).h for f in vis) * 1.15
    g = 1.0
    n = len(vis)
    out: List[Dict[str, Pt]] = []

    def column(x_edge: float, side: int, y_mid: float) -> Dict[str, Pt]:
        y0 = y_mid - lh * (n - 1) / 2
        return {f.name: ((x_edge + side * (g + w(f) / 2)), y0 + i * lh) for i, f in enumerate(vis)}

    if two_pin and abs(pins[0].pos[0] - pins[1].pos[0]) < EPS:       # vertical part: texts beside it
        return [column(body.x2, 1, body.cy), column(body.x1, -1, body.cy)]
    top = body.y1 - g - lh / 2
    bot = body.y2 + g + lh / 2
    if n >= 2:      # first line above, the rest below
        out.append({f.name: (body.cx, top if i == 0 else bot + (i - 1) * lh) for i, f in enumerate(vis)})
    out.append({f.name: (body.cx, top - (n - 1 - i) * lh) for i, f in enumerate(vis)})       # all above
    out.append({f.name: (body.cx, bot + i * lh) for i, f in enumerate(vis)})                 # all below
    if not two_pin:                                                                          # beside a larger part
        out.append({f.name: (body.x2 + g + w(f) / 2, body.y1 + (i + 0.5) * lh) for i, f in enumerate(vis)})
    return out


# ---------------------------------------------------------------------- public API
def plan(text: str, opts: Optional[CleanOptions] = None, exclude: Optional[Set[str]] = None) -> Plan:
    return _Planner(text, opts or CleanOptions(), set(exclude or ())).run()


def verify(original: str, new: str, name: str, project_dir: Optional[Path] = None) -> Tuple[bool, str]:
    """Same connectivity before and after? Uses kicad-cli netlists when available."""
    from ..kicad import cli
    try:
        a = cli.netlist_of_text(original, name, project_dir).partition()
        b = cli.netlist_of_text(new, name, project_dir).partition()
    except cli.CliError as e:
        same_int = SheetModel(original).connectivity() == SheetModel(new).connectivity()
        return same_int, f"checked with KiBench's connectivity model only ({e})"
    if a == b:
        return True, "netlist unchanged (kicad-cli)"
    return False, f"netlist differs in {len(a ^ b)} net(s)"


def is_open_in_kicad(sheet: Path) -> bool:
    """KiCad keeps a lock file next to documents that are open in an editor."""
    d = sheet.parent
    return any(d.glob(f"~{sheet.name}.lck")) or any(d.glob(f".~lock.{sheet.name}*"))


def write(sheet: Path, new_text: str, project_dir: Optional[Path] = None) -> Path:
    """Backup the sheet into <project>/kibench-backups/ and write the new text. Returns the backup path."""
    bdir = Path(project_dir or sheet.parent) / "kibench-backups"
    bdir.mkdir(exist_ok=True)
    bak = bdir / f"{sheet.stem}-{time.strftime('%Y%m%d-%H%M%S')}{sheet.suffix}"
    shutil.copy2(sheet, bak)
    tmp = sheet.with_suffix(sheet.suffix + ".kibench-tmp")
    tmp.write_text(new_text)
    tmp.replace(sheet)
    return bak


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="kibench.design.clean", description="Tidy up a KiCad schematic sheet")
    ap.add_argument("sheet", type=Path)
    ap.add_argument("--write", action="store_true", help="apply (backup + netlist check)")
    ap.add_argument("--skip", default="", help="comma separated kinds to skip: wire,merge,junction,align,fields")
    args = ap.parse_args(argv)
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    opts = CleanOptions(wires="wire" not in skip, merge="merge" not in skip, junctions="junction" not in skip,
                        align="align" not in skip, fields="fields" not in skip)
    text = args.sheet.read_text()
    p = plan(text, opts)
    for c in p.changes:
        print(f"[{c.kind:8}] {c.title}" + (f" — {c.detail}" if c.detail else ""))
    print(f"{len(p.changes)} change(s); {len(p.skipped)} step(s) skipped because they would break something")
    if args.write and p.changed:
        ok, msg = verify(text, p.text, args.sheet.name, args.sheet.parent)
        print(msg)
        if not ok:
            return 1
        if is_open_in_kicad(args.sheet):
            print("note: the sheet is open in KiCad; reload it there (File → Revert) before editing")
        print("backup:", write(args.sheet, p.text, args.sheet.parent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
