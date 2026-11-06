"""Board tidy-up: small, safe layout touches.

* Reference texts: placed next to their part (above, below, beside), not on pads, other texts,
  silkscreen strokes or the board edge. Texts that already look fine are left alone.
* Align parts: identical parts that are almost in a row or column (within `tol` mm) are lined up.
* Even spacing: rows/columns of three or more identical parts with nearly equal gaps get equal gaps.

Only parts without tracks or vias on their pads (not routed yet) and not locked are moved, and a move is
kept only if it adds no courtyard overlap, keeps copper clearance and stays on the board. Text moves never
change copper. `verify` runs KiCad's DRC before and after.

Every step is one `Change` with a stable key; deselected keys go back in as `exclude`.

    python -m kibench.design.board_tidy board.kicad_pcb            # list proposed changes
    python -m kibench.design.board_tidy board.kicad_pcb --write    # apply (backup + DRC compare)
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from ..kicad.board import (BoardModel, Footprint, Pt, Text, copper_layers_of, poly_overlap, seg_dist,
                           seg_hits_box)
from ..kicad.schematic import Box
from .board_check import REF_FAR_MM
from .clean import Change, Plan, is_open_in_kicad, write  # noqa: F401  (re-exported for the UI)

KIND_LABEL = {"align": "Align part", "spacing": "Even spacing", "refs": "Tidy reference"}
GAP = 0.2              # text to part body
PAD_CLR = 0.1          # silkscreen to pad
COPPER_CLR = 0.2       # moved pads to other copper
HARD = {"off the board", "over the board edge", "on pads", "over another text"}


@dataclass
class BoardTidyOptions:
    refs: bool = True
    align: bool = True
    spacing: bool = True
    tol: float = 0.5   # mm, largest shift for align / spacing


def _natkey(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]


class _Grid:
    def __init__(self, cell: float = 2.0):
        self.cell = cell
        self.d: Dict[Tuple[int, int], list] = {}

    def _cells(self, b: Box):
        c = self.cell
        for x in range(int(math.floor(b.x1 / c)), int(math.floor(b.x2 / c)) + 1):
            for y in range(int(math.floor(b.y1 / c)), int(math.floor(b.y2 / c)) + 1):
                yield (x, y)

    def add(self, b: Box, item) -> None:
        for k in self._cells(b):
            self.d.setdefault(k, []).append(item)

    def query(self, b: Box):
        seen = set()
        for k in self._cells(b):
            for it in self.d.get(k, ()):
                if id(it) not in seen:
                    seen.add(id(it))
                    yield it


class _Planner:
    def __init__(self, text: str, opts: BoardTidyOptions, exclude: Set[str]):
        self.text = text
        self.opts = opts
        self.exclude = exclude
        self.m = BoardModel(text)
        self.changes: List[Change] = []
        self.skipped: List[str] = []
        routed = {i for i, _n in self.m.routed_pads()}
        self.movable = [not fp.locked and i not in routed and bool(fp.pads) and any(p.net for p in fp.pads)
                        for i, fp in enumerate(self.m.footprints)]
        self.n_routed = len(routed)
        self._copper = _Grid(2.0)
        for t in self.m.tracks:
            r = t.width / 2
            self._copper.add(Box(min(t.a[0], t.b[0]) - r, min(t.a[1], t.b[1]) - r,
                                 max(t.a[0], t.b[0]) + r, max(t.a[1], t.b[1]) + r),
                             ("t", t.a, t.b, r, t.net, t.layer))
        for v in self.m.vias:
            r = v.size / 2
            self._copper.add(Box(v.pos[0] - r, v.pos[1] - r, v.pos[0] + r, v.pos[1] + r),
                             ("v", v.pos, v.pos, r, v.net, "*"))

    def run(self) -> Plan:
        if self.opts.align:
            self.align()
        if self.opts.spacing:
            self.spacing()
        if self.opts.refs:
            self.refs()
        plan = Plan(self.changes, self.m.render(), self.text, self.skipped, self.m)
        plan.fixed_parts = sum(1 for mv in self.movable if not mv)       # routed or locked: never moved
        return plan

    # ------------------------------------------------------------ moving parts
    def _fp_grid(self) -> _Grid:
        g = _Grid(4.0)
        for fp in self.m.footprints:
            g.add(fp.courtyard(), fp)
        return g

    def _can_move(self, fp: Footprint, new: Pt) -> Tuple[bool, str]:
        old = fp.pos
        others = self._fp_grid()
        cy_old = fp.courtyard()
        ob = self.m.outline

        def overlapping(cy):
            return {id(o) for o in others.query(cy) if o is not fp and (o.side == fp.side or _tht(o) or _tht(fp))
                    and cy.overlaps(o.courtyard(), -0.01)}
        before = overlapping(cy_old)
        clr_before = self._clearance(fp)
        fp.move_to(new)
        try:
            cy = fp.courtyard()
            if not overlapping(cy) <= before:
                return False, "would overlap another part"
            if ob and _inside(cy_old, ob) and not _inside(cy, ob):
                return False, "would stick out of the board"
            if self._clearance(fp) < min(COPPER_CLR, clr_before) - 1e-6:
                return False, "would get too close to other copper"
            return True, ""
        finally:
            fp.move_to(old)

    def _clearance(self, fp: Footprint) -> float:
        """Smallest gap from this part's pads to copper of other nets (tracks, vias, other parts' pads)."""
        best = 99.0
        fps = self._fp_grid()
        for p in fp.pads:
            a, b, r = p.capsule()
            lays = set(copper_layers_of(p.layers, self.m.copper))
            q = Box(min(a[0], b[0]) - r - 1, min(a[1], b[1]) - r - 1, max(a[0], b[0]) + r + 1, max(a[1], b[1]) + r + 1)
            for kind, c, d, r2, net, lay in self._copper.query(q):
                if (lay == "*" or lay in lays) and not (net and net == p.net):
                    best = min(best, seg_dist(a, b, c, d) - r - r2)
            for o in fps.query(q):
                if o is fp:
                    continue
                for op in o.pads:
                    if op.net and op.net == p.net:
                        continue
                    if not lays & set(copper_layers_of(op.layers, self.m.copper)):
                        continue
                    c, d, r2 = op.capsule()
                    best = min(best, seg_dist(a, b, c, d) - r - r2)
        return best

    def _groups(self) -> Dict[tuple, List[int]]:
        g: Dict[tuple, List[int]] = {}
        for i, fp in enumerate(self.m.footprints):
            if fp.pads and fp.ref and abs(fp.rot / 90 - round(fp.rot / 90)) < 1e-3:
                g.setdefault((fp.side, round(fp.rot) % 180, fp.lib), []).append(i)
        return g

    def _move(self, i: int, axis: int, target: float, kind: str, why: str) -> bool:
        fp = self.m.footprints[i]
        key = f"{kind}:{fp.uuid or fp.ref}:{'xy'[axis]}"
        if key in self.exclude:
            return False
        d = target - fp.pos[axis]
        if abs(d) < 1e-4:
            return False
        new = (target, fp.pos[1]) if axis == 0 else (fp.pos[0], target)
        ok, reason = self._can_move(fp, new)
        if not ok:
            self.skipped.append(f"{fp.ref}: {reason}")
            return False
        old_box = fp.courtyard()
        fp.move_to(new)
        dirn = ("right" if d > 0 else "left") if axis == 0 else ("down" if d > 0 else "up")
        self.changes.append(Change(key, kind, f"{fp.ref} {abs(d):.2f} mm {dirn}", why, [fp.ref], new,
                                   [], [], [old_box], [fp.courtyard()]))
        return True

    def _lines(self, idx: List[int], axis: int, tol: float) -> List[List[int]]:
        """Clusters of parts nearly on one line: |offset across| <= tol, close along the line."""
        fps = self.m.footprints
        across = 1 - axis
        parent = list(range(len(idx)))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a
        sizes = [max(fps[i].courtyard().w, fps[i].courtyard().h) for i in idx]
        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                pa, pb = fps[idx[a]].pos, fps[idx[b]].pos
                reach = max(8.0, 3 * max(sizes[a], sizes[b]))
                if abs(pa[across] - pb[across]) <= tol and abs(pa[axis] - pb[axis]) <= reach:
                    parent[find(a)] = find(b)
        out: Dict[int, List[int]] = {}
        for a in range(len(idx)):
            out.setdefault(find(a), []).append(idx[a])
        return [c for c in out.values() if len(c) >= 2]

    def align(self) -> None:
        tol = self.opts.tol
        fps = self.m.footprints
        for axis in (0, 1):                      # axis 0: rows (same y), 1: columns (same x)
            across = 1 - axis
            for _k, idx in sorted(self._groups().items(), key=lambda kv: str(kv[0])):
                if len(idx) < 2:
                    continue
                for cl in self._lines(idx, axis, tol):
                    vals = [round(fps[i].pos[across], 4) for i in cl]
                    if max(vals) - min(vals) < 1e-4:
                        continue
                    fixed = [round(fps[i].pos[across], 4) for i in cl if not self.movable[i]]
                    pool = fixed or vals
                    cnt = Counter(pool)
                    # most members on it, then most other parts on the board on that line, then the median
                    med = sorted(pool)[len(pool) // 2]
                    target = max(cnt, key=lambda v: (cnt[v], self._support(across, v), -abs(v - med)))
                    names = ", ".join(fps[i].ref for i in sorted(cl, key=lambda i: fps[i].pos[axis]))
                    for i in cl:
                        if not self.movable[i] or abs(fps[i].pos[across] - target) > tol:
                            continue
                        self._move(i, across, target, "align", f"line up with {names}")

    def _support(self, axis: int, v: float) -> int:
        return sum(1 for fp in self.m.footprints if abs(fp.pos[axis] - v) < 1e-3)

    def spacing(self) -> None:
        tol = self.opts.tol
        fps = self.m.footprints
        for axis in (0, 1):
            across = 1 - axis
            for _k, idx in sorted(self._groups().items(), key=lambda kv: str(kv[0])):
                if len(idx) < 3:
                    continue
                for cl in self._lines(idx, axis, 1e-3):
                    cl = sorted(cl, key=lambda i: fps[i].pos[axis])
                    if len(cl) < 3:
                        continue
                    pos = [fps[i].pos[axis] for i in cl]
                    gaps = [b - a for a, b in zip(pos, pos[1:])]
                    if max(gaps) - min(gaps) < 0.05 or max(gaps) - min(gaps) > min(tol, 0.25 * min(gaps)):
                        continue
                    step = (pos[-1] - pos[0]) / (len(cl) - 1)
                    names = ", ".join(fps[i].ref for i in cl)
                    for n, i in enumerate(cl[1:-1], 1):
                        if self.movable[i]:
                            self._move(i, axis, round(pos[0] + n * step, 4), "spacing",
                                       f"equal {step:.2f} mm pitch in {names}")

    # ------------------------------------------------------------ reference texts
    def refs(self) -> None:
        m = self.m
        ob = m.outline
        sides = {}
        for side in ("F", "B"):
            silk = f"{side}.SilkS"
            pads, texts, strokes, bodies = _Grid(2.0), _Grid(3.0), _Grid(2.0), _Grid(4.0)
            for fp in m.footprints:
                for p in fp.pads:
                    if f"{side}.Cu" in copper_layers_of(p.layers, m.copper) or p.kind in ("thru_hole", "np_thru_hole"):
                        pads.add(p.box(), (fp, p))
                if fp.layer.startswith(side):
                    for a, b in fp.silk_segs():
                        strokes.add(Box(a[0], a[1], b[0], b[1]), (fp, a, b))
                    bodies.add(fp.body(), fp)
                for t in fp.texts:
                    if t.layer == silk and not t.hidden and t.text:
                        texts.add(t.box(), (fp, t))
            for a, b in m.board_silk.get(silk, []):
                strokes.add(Box(a[0], a[1], b[0], b[1]), (None, a, b))
            for t in m.board_texts:
                if t.layer == silk and not t.hidden:
                    texts.add(t.box(), (None, t))
            vias = _Grid(2.0)
            for v in m.vias:
                r = v.size / 2
                vias.add(Box(v.pos[0] - r, v.pos[1] - r, v.pos[0] + r, v.pos[1] + r), v)
            sides[side] = (pads, texts, strokes, bodies, vias)
        edge = _Grid(4.0)
        for a, b in m.outline_segs:
            edge.add(Box(a[0], a[1], b[0], b[1]), (a, b))

        def crowd(fp):
            bodies = sides[fp.side][3]
            return -sum(1 for o in bodies.query(fp.body().shifted(0, 0)) if o is not fp)
        todo = []
        for fp in m.footprints:
            t = fp.reference
            if t is None or t.hidden or not t.on_silk or not fp.pads or not t.text or fp.ref.startswith("#"):
                continue
            b = fp.body()
            if ob is not None and not (ob.x1 <= b.cx <= ob.x2 and ob.y1 <= b.cy <= ob.y2):
                continue                                 # not placed on the board yet
            todo.append(fp)
        todo.sort(key=lambda fp: (crowd(fp), _natkey(fp.ref)))
        for fp in todo:
            t = fp.reference
            side = "B" if t.layer.startswith("B.") else "F"
            key = f"refs:{fp.uuid or fp.ref}"
            if key in self.exclude:
                continue
            ctx = sides[side] + (edge, ob)
            cur_cost, cur_why = self._text_cost(fp, t, t.pos, t.rot, ctx)
            if not cur_why:
                continue                                 # already fine: leave it where the designer put it
            best = None
            for pos, rot, pref, where in self._candidates(fp, t, side):
                c, why = self._text_cost(fp, t, pos, rot, ctx)
                if (set(why) & HARD) - set(cur_why):
                    continue                             # never trade one problem for a worse kind
                c += pref
                if best is None or c < best[0]:
                    best = (c, pos, rot, where, why)
            if best is None or best[0] + 0.5 >= cur_cost:
                continue
            old_box = t.box()
            t.pos, t.rot = best[1], best[2]
            for g in (sides[side][1],):
                g.add(t.box(), (fp, t))
            detail = best[3]
            if cur_why:
                detail += " · was " + ", ".join(cur_why)
            if best[4]:
                detail += " · still " + ", ".join(best[4])
            self.changes.append(Change(key, "refs", f"{fp.ref} reference", detail, [fp.ref],
                                       (t.box().cx, t.box().cy), [], [], [old_box], [t.box()]))

    def _candidates(self, fp: Footprint, t: Text, side: str):
        body = fp.body()
        natural = 0.0 if round(fp.rot) % 180 == 0 else (90.0 if side == "F" else 270.0)
        other = (90.0 if side == "F" else 270.0) if natural == 0.0 else 0.0
        rots = [(t.rot, 0.0)] + [(r, p) for r, p in ((natural, 0.3), (other, 0.6)) if abs(r - t.rot) > 1e-6]
        for rot, rpref in rots:
            cs = t.corners((0.0, 0.0), rot)
            bw = max(c[0] for c in cs) - min(c[0] for c in cs)
            bh = max(c[1] for c in cs) - min(c[1] for c in cs)
            off = ((max(c[0] for c in cs) + min(c[0] for c in cs)) / 2, (max(c[1] for c in cs) + min(c[1] for c in cs)) / 2)
            for ring, gpref in ((GAP, 0.0), (GAP + 0.6, 0.8)):
                ay, by = body.y1 - ring - bh / 2, body.y2 + ring + bh / 2
                lx, rx = body.x1 - ring - bw / 2, body.x2 + ring + bw / 2
                slots = [((body.cx, ay), 0.0, "above"), ((body.cx, by), 0.15, "below"),
                         ((lx, body.cy), 0.3, "left"), ((rx, body.cy), 0.3, "right"),
                         ((body.x1 + bw / 2, ay), 0.4, "above, left"), ((body.x2 - bw / 2, ay), 0.4, "above, right"),
                         ((body.x1 + bw / 2, by), 0.45, "below, left"), ((body.x2 - bw / 2, by), 0.45, "below, right"),
                         ((lx, body.y1 + bh / 2), 0.5, "left, top"), ((rx, body.y1 + bh / 2), 0.5, "right, top"),
                         ((lx, body.y2 - bh / 2), 0.55, "left, bottom"), ((rx, body.y2 - bh / 2), 0.55, "right, bottom")]
                for (cx, cy), spref, where in slots:
                    pos = (round(cx - off[0], 3), round(cy - off[1], 3))
                    yield pos, rot, rpref + gpref + spref, where + (
                        "" if abs(rot - t.rot) < 1e-6 else " (vertical)" if rot in (90.0, 270.0) else " (horizontal)")

    def _text_cost(self, fp: Footprint, t: Text, pos: Pt, rot: float, ctx) -> Tuple[float, List[str]]:
        pads, texts, strokes, bodies, vias, edge, ob = ctx
        cs = t.corners(pos, rot)
        bx = Box(min(c[0] for c in cs), min(c[1] for c in cs), max(c[0] for c in cs), max(c[1] for c in cs))
        cost, why = 0.0, []
        if ob is not None and not (ob.x1 + 0.2 <= bx.x1 and bx.x2 <= ob.x2 - 0.2 and ob.y1 + 0.2 <= bx.y1
                                   and bx.y2 <= ob.y2 - 0.2):
            cost += 1000
            why.append("off the board")
        elif any(seg_hits_box(a, b, bx, 0.2) for a, b in edge.query(bx)):
            cost += 1000
            why.append("over the board edge")
        n = sum(1 for _o, p in pads.query(bx) if poly_overlap(p.corners(), cs, PAD_CLR))
        if n:
            cost += 100 * min(n, 5)
            why.append("on pads")
        n = sum(1 for o, ot in texts.query(bx) if ot is not t and poly_overlap(ot.corners(), cs, 0.1))
        if n:
            cost += 60 * min(n, 3)
            why.append("over another text")
        n = sum(1 for _o, a, b in strokes.query(bx) if seg_hits_box(a, b, bx, 0.05))
        if n:
            cost += 15 * min(n, 4)
            why.append("over silkscreen lines")
        own = fp.body()
        pb = _pads_box(fp)
        if pb is not None and bx.overlaps(pb, -0.15):
            cost += 4                                    # between its own pads: hidden once assembled
        elif bx.overlaps(own, -0.15):
            cost += 3                                    # inside the outline: allowed, but outside is nicer
        n = sum(1 for o in bodies.query(bx) if o is not fp and bx.overlaps(o.body(), -0.1))
        cost += 4 * min(n, 3)                            # over another part's outline: avoid when there's room
        n = sum(1 for v in vias.query(bx) if math.hypot(max(bx.x1 - v.pos[0], 0, v.pos[0] - bx.x2),
                                                         max(bx.y1 - v.pos[1], 0, v.pos[1] - bx.y2)) < v.size / 2)
        cost += 1.5 * min(n, 4)
        d = bx.distance(own)
        cost += 2 * d + (10 * (d - REF_FAR_MM) if d > REF_FAR_MM else 0)
        if d > REF_FAR_MM:
            why.append(f"{d:.1f} mm from the part")
        return cost, why


def _pads_box(fp: Footprint) -> Optional[Box]:
    if len(fp.pads) < 2:
        return None
    bs = [p.box() for p in fp.pads]
    return Box(min(b.x1 for b in bs), min(b.y1 for b in bs), max(b.x2 for b in bs), max(b.y2 for b in bs))


def _tht(fp: Footprint) -> bool:
    return any(p.kind in ("thru_hole", "np_thru_hole") for p in fp.pads)


def _inside(b: Box, ob: Box) -> bool:
    return ob.x1 <= b.x1 and b.x2 <= ob.x2 and ob.y1 <= b.y1 and b.y2 <= ob.y2


# ---------------------------------------------------------------------- public API
def plan(text: str, opts: Optional[BoardTidyOptions] = None, exclude: Optional[Set[str]] = None) -> Plan:
    return _Planner(text, opts or BoardTidyOptions(), set(exclude or ())).run()


def _drc_counts(viols) -> Counter:
    return Counter((v.type, v.severity) for v in viols if v.severity in ("error", "warning"))


def verify(original: str, new: str, board: Path) -> Tuple[bool, str, List[Pt]]:
    """DRC before and after (temp copies next to the project's rules). OK when no violation type gets more
    frequent. Returns the positions of new violations so the caller can drop the changes near them."""
    from ..kicad import cli
    try:
        a = cli.run_drc(board, parity=False, text=original)
        b = cli.run_drc(board, parity=False, text=new)
    except cli.CliError as e:
        return True, f"DRC not run ({e}); only KiBench's own clearance checks were used", []
    ca, cb = _drc_counts(a), _drc_counts(b)
    worse = {k: cb[k] - ca.get(k, 0) for k in cb if cb[k] > ca.get(k, 0)}
    fixed = sum(max(0, ca[k] - cb.get(k, 0)) for k in ca)
    if not worse:
        return True, "DRC: no new problems" + (f", {fixed} fewer" if fixed else ""), []
    old_pos = {(round(it["pos"]["x"], 2), round(it["pos"]["y"], 2)) for v in a for it in v.items if it.get("pos")}
    new_pos = [(it["pos"]["x"], it["pos"]["y"]) for v in b if (v.type, v.severity) in worse for it in v.items
               if it.get("pos") and (round(it["pos"]["x"], 2), round(it["pos"]["y"], 2)) not in old_pos]
    what = ", ".join(f"{n} × {t.replace('_', ' ')}" for (t, _s), n in worse.items())
    return False, f"DRC found new problems: {what}", new_pos


def changes_near(changes: List[Change], pts: List[Pt], dist: float = 1.0) -> Set[str]:
    out = set()
    for ch in changes:
        for bx in ch.new_boxes + ch.old_boxes:
            g = Box(bx.x1 - dist, bx.y1 - dist, bx.x2 + dist, bx.y2 + dist)
            if any(g.x1 <= x <= g.x2 and g.y1 <= y <= g.y2 for x, y in pts):
                out.add(ch.key)
    return out


def plan_verified(text: str, board: Path, opts: Optional[BoardTidyOptions] = None,
                  exclude: Optional[Set[str]] = None) -> Tuple[Plan, bool, str, Set[str]]:
    """Plan, DRC-compare, and if DRC finds new problems drop the changes near them and try once more."""
    exclude = set(exclude or ())
    p = plan(text, opts, exclude)
    if not p.changed:
        return p, True, "nothing to change", set()
    ok, msg, pts = verify(text, p.text, board)
    dropped: Set[str] = set()
    if not ok and pts:
        dropped = changes_near(p.changes, pts)
        if dropped:
            p = plan(text, opts, exclude | dropped)
            ok, msg, _pts = verify(text, p.text, board) if p.changed else (True, "nothing left to change", [])
    return p, ok, msg, dropped


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="kibench.design.board_tidy", description="Tidy up a KiCad board")
    ap.add_argument("board", type=Path)
    ap.add_argument("--write", action="store_true", help="apply (backup + DRC compare)")
    ap.add_argument("--skip", default="", help="comma separated: refs,align,spacing")
    ap.add_argument("--tol", type=float, default=0.5)
    args = ap.parse_args(argv)
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    opts = BoardTidyOptions(refs="refs" not in skip, align="align" not in skip,
                            spacing="spacing" not in skip, tol=args.tol)
    text = args.board.read_text()
    p = plan(text, opts)
    for c in p.changes:
        print(f"[{c.kind:8}] {c.title}" + (f" — {c.detail}" if c.detail else ""))
    print(f"{len(p.changes)} change(s); {len(p.skipped)} move(s) skipped")
    if args.write and p.changed:
        p, ok, msg, dropped = plan_verified(text, args.board, opts)
        print(msg + (f" (dropped {len(dropped)} change(s) near new DRC problems)" if dropped else ""))
        if not ok:
            return 1
        if is_open_in_kicad(args.board):
            print("note: the board is open in KiCad; reload it there (File → Revert) before editing")
        print("backup:", write(args.board, p.text, args.board.parent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
