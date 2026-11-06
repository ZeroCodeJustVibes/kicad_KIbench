"""Geometry of one .kicad_sch sheet: symbols with pin positions, wires, labels, junctions, fields.

Coordinates are sheet mm (y down). Objects keep their parse nodes, so edits can be written back as
small text replacements (`SheetModel.render`) and untouched parts of the file stay byte-for-byte.
"""
from __future__ import annotations

import math
import re
import uuid as uuidlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .. import sexpr

Pt = Tuple[float, float]
EPS = 1e-3
GRID = 1.27


def key(p: Pt) -> Tuple[int, int]:
    return (round(p[0] * 1000), round(p[1] * 1000))


def same(a: Pt, b: Pt) -> bool:
    return abs(a[0] - b[0]) < EPS and abs(a[1] - b[1]) < EPS


def on_segment(p: Pt, a: Pt, b: Pt, interior: bool = True) -> bool:
    """p lies on segment a-b (strictly inside it when `interior`)."""
    if interior and (same(p, a) or same(p, b)):
        return False
    (x, y), (x1, y1), (x2, y2) = p, a, b
    if min(x1, x2) - EPS > x or x > max(x1, x2) + EPS or min(y1, y2) - EPS > y or y > max(y1, y2) + EPS:
        return False
    cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
    ln = math.hypot(x2 - x1, y2 - y1) or 1.0
    return abs(cross) / ln < EPS


class Box:
    def __init__(self, x1, y1, x2, y2):
        self.x1, self.y1, self.x2, self.y2 = min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)

    @property
    def cx(self):
        return (self.x1 + self.x2) / 2

    @property
    def cy(self):
        return (self.y1 + self.y2) / 2

    @property
    def w(self):
        return self.x2 - self.x1

    @property
    def h(self):
        return self.y2 - self.y1

    def overlaps(self, o: "Box", m: float = 0.0) -> bool:
        return not (self.x2 + m <= o.x1 or o.x2 + m <= self.x1 or self.y2 + m <= o.y1 or o.y2 + m <= self.y1)

    def hits_segment(self, a: Pt, b: Pt) -> bool:
        """Axis-aligned or diagonal segment crosses the box (sampled; good enough for layout costs)."""
        n = max(2, int(math.hypot(b[0] - a[0], b[1] - a[1]) / 0.3))
        for i in range(n + 1):
            t = i / n
            x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            if self.x1 < x < self.x2 and self.y1 < y < self.y2:
                return True
        return False

    def distance(self, o: "Box") -> float:
        dx = max(0.0, o.x1 - self.x2, self.x1 - o.x2)
        dy = max(0.0, o.y1 - self.y2, self.y1 - o.y2)
        return math.hypot(dx, dy)

    def shifted(self, dx, dy) -> "Box":
        return Box(self.x1 + dx, self.y1 + dy, self.x2 + dx, self.y2 + dy)

    def __repr__(self):
        return f"Box({self.x1:.2f},{self.y1:.2f},{self.x2:.2f},{self.y2:.2f})"


# ---------------------------------------------------------------------- items
@dataclass
class Wire:
    uuid: str
    a: Pt
    b: Pt
    node: Optional[sexpr.SList] = None     # None for wires added by a cleanup
    bus: bool = False
    deleted: bool = False
    orig: Tuple[Pt, Pt] = ((0, 0), (0, 0))

    @property
    def horizontal(self) -> bool:
        return abs(self.a[1] - self.b[1]) < EPS

    @property
    def vertical(self) -> bool:
        return abs(self.a[0] - self.b[0]) < EPS

    @property
    def length(self) -> float:
        return math.hypot(self.b[0] - self.a[0], self.b[1] - self.a[1])


@dataclass
class Junction:
    uuid: str
    pos: Pt
    node: sexpr.SList
    deleted: bool = False
    orig: Pt = (0, 0)


@dataclass
class Label:
    kind: str           # label | global_label | hierarchical_label | netclass_flag | directive_label
    name: str
    pos: Pt
    rot: float
    node: sexpr.SList
    orig: Pt = (0, 0)


@dataclass
class Field:
    name: str
    value: str
    pos: Pt
    rot: float
    hidden: bool
    size: float
    node: sexpr.SList
    justify: str = ""
    orig: Tuple[Pt, float] = ((0, 0), 0)
    centered: bool = False             # rewrite justify away

    srot: float = 0.0                  # parent symbol orientation (text offsets follow it)
    smirror: str = ""

    def _dir(self) -> Pt:
        a = math.radians(self.rot)
        return screen_xform(math.cos(a), -math.sin(a), self.srot, self.smirror)

    def draw_horizontal(self, sym_rot: Optional[float] = None) -> bool:
        return abs(self._dir()[0]) > 0.5

    def box(self, sym_rot: Optional[float] = None) -> Box:
        """Displayed text box. KiCad stores the anchor as displayed but angle/justify in the symbol's own frame:
        the offsets from the anchor are rotated by the text angle, then by the symbol transform."""
        w = max(1, len(self.value)) * self.size * 0.9
        h = self.size * 1.1
        j = "" if self.centered else self.justify
        xs = (0, w) if "left" in j else (-w, 0) if "right" in j else (-w / 2, w / 2)
        ys = (0, h) if "top" in j else (-h, 0) if "bottom" in j else (-h / 2, h / 2)
        a = math.radians(self.rot)
        c, s = math.cos(a), math.sin(a)
        pts = []
        for x in xs:
            for y in ys:
                rx, ry = x * c + y * s, -x * s + y * c
                pts.append(screen_xform(rx, ry, self.srot, self.smirror))
        px, py = self.pos
        return Box(px + min(p[0] for p in pts), py + min(p[1] for p in pts),
                   px + max(p[0] for p in pts), py + max(p[1] for p in pts))


@dataclass
class Pin:
    number: str
    name: str
    type: str
    pos: Pt              # connection point, sheet coordinates
    hidden: bool = False


@dataclass
class Symbol:
    uuid: str
    ref: str
    value: str
    lib_id: str
    pos: Pt
    rot: float
    mirror: str
    unit: int
    node: sexpr.SList
    pins: List[Pin] = field(default_factory=list)
    body: Optional[Box] = None            # graphics bbox (no pins), sheet coordinates
    fields: Dict[str, Field] = field(default_factory=dict)
    orig: Pt = (0, 0)

    @property
    def is_flag(self) -> bool:
        return self.ref.startswith("#FLG") or self.value == "PWR_FLAG"

    @property
    def is_power(self) -> bool:
        return self.ref.startswith("#") or self.lib_id.lower().startswith("power:")

    def bbox(self) -> Box:
        xs = [p.pos[0] for p in self.pins if not p.hidden]
        ys = [p.pos[1] for p in self.pins if not p.hidden]
        if self.body:
            xs += [self.body.x1, self.body.x2]
            ys += [self.body.y1, self.body.y2]
        if not xs:
            return Box(self.pos[0] - 1, self.pos[1] - 1, self.pos[0] + 1, self.pos[1] + 1)
        return Box(min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------------- transforms
def screen_xform(x: float, y: float, rot: float, mirror: str) -> Pt:
    """Linear part of a symbol orientation on sheet coordinates (rotate counter-clockwise on screen, then mirror)."""
    a = math.radians(rot)
    c, s = round(math.cos(a), 9), round(math.sin(a), 9)
    x, y = x * c + y * s, -x * s + y * c
    if mirror == "x":
        y = -y
    elif mirror == "y":
        x = -x
    return (x, y)


def transform(px: float, py: float, at: Pt, rot: float, mirror: str) -> Pt:
    """Library point (y up) to sheet (y down): flip y, rotate counter-clockwise on screen, mirror, translate."""
    x, y = px, -py
    a = math.radians(rot)
    c, s = round(math.cos(a), 9), round(math.sin(a), 9)
    x, y = x * c + y * s, -x * s + y * c
    if mirror == "x":        # about the horizontal axis
        y = -y
    elif mirror == "y":
        x = -x
    return (round(at[0] + x, 4), round(at[1] + y, 4))


def _lib_units(lib: sexpr.SList) -> Dict[Tuple[int, int], sexpr.SList]:
    out = {}
    for sub in sexpr.findall(lib, "symbol"):
        m = re.search(r"_(\d+)_(\d+)$", str(sub[1]))
        if m:
            out[(int(m.group(1)), int(m.group(2)))] = sub
    return out


def _graphics_points(sub: sexpr.SList) -> List[Pt]:
    pts: List[Pt] = []
    for g in sub:
        if not isinstance(g, list) or not g:
            continue
        h = g[0]
        if h == "rectangle":
            for k in ("start", "end"):
                n = sexpr.find(g, k)
                if n is not None:
                    pts.append((float(n[1]), float(n[2])))
        elif h in ("polyline", "bezier"):
            pn = sexpr.find(g, "pts")
            for xy in (sexpr.findall(pn, "xy") if pn is not None else []):
                pts.append((float(xy[1]), float(xy[2])))
        elif h == "circle":
            c, r = sexpr.find(g, "center"), sexpr.find(g, "radius")
            if c is not None and r is not None:
                cx, cy, rr = float(c[1]), float(c[2]), float(r[1])
                pts += [(cx - rr, cy - rr), (cx + rr, cy + rr)]
        elif h == "arc":
            for k in ("start", "mid", "end"):
                n = sexpr.find(g, k)
                if n is not None:
                    pts.append((float(n[1]), float(n[2])))
    return pts


def _hidden(node: sexpr.SList) -> bool:
    hide = sexpr.find(node, "hide")
    if hide is not None and str(sexpr.value(hide, 1, "yes")) != "no":
        return True
    eff = sexpr.find(node, "effects")
    if eff is not None:
        if "hide" in [x for x in eff if isinstance(x, str)]:
            return True
        h2 = sexpr.find(eff, "hide")
        if h2 is not None and str(sexpr.value(h2, 1, "yes")) != "no":
            return True
    return False


# ---------------------------------------------------------------------- model
class SheetModel:
    def __init__(self, text: str):
        self.text = text
        self.root = sexpr.parse(text)
        self.wires: List[Wire] = []
        self.buses: List[Wire] = []
        self.junctions: List[Junction] = []
        self.labels: List[Label] = []
        self.no_connects: List[Pt] = []
        self.anchors_extra: List[Pt] = []        # sheet pins, bus entries
        self.symbols: List[Symbol] = []
        self.sheet_boxes: List[Box] = []
        libs = {}
        lsn = sexpr.find(self.root, "lib_symbols")
        for ls in (sexpr.findall(lsn, "symbol") if lsn is not None else []):
            libs[str(ls[1])] = ls
        self._libs = libs
        for n in self.root:
            if not isinstance(n, list) or not n:
                continue
            h = n[0]
            if h in ("wire", "bus"):
                pts = sexpr.findall(sexpr.find(n, "pts"), "xy")
                a, b = (float(pts[0][1]), float(pts[0][2])), (float(pts[1][1]), float(pts[1][2]))
                w = Wire(str(sexpr.value(sexpr.find(n, "uuid"), 1, "")), a, b, n, h == "bus", orig=(a, b))
                (self.buses if w.bus else self.wires).append(w)
            elif h == "junction":
                at = sexpr.find(n, "at")
                p = (float(at[1]), float(at[2]))
                self.junctions.append(Junction(str(sexpr.value(sexpr.find(n, "uuid"), 1, "")), p, n, orig=p))
            elif h in ("label", "global_label", "hierarchical_label", "netclass_flag", "directive_label"):
                at = sexpr.find(n, "at")
                p = (float(at[1]), float(at[2]))
                self.labels.append(Label(h, str(n[1]), p, float(at[3]) if len(at) > 3 else 0.0, n, orig=p))
            elif h == "no_connect":
                at = sexpr.find(n, "at")
                self.no_connects.append((float(at[1]), float(at[2])))
            elif h == "bus_entry":
                at, sz = sexpr.find(n, "at"), sexpr.find(n, "size")
                x, y = float(at[1]), float(at[2])
                self.anchors_extra += [(x, y), (x + float(sz[1]), y + float(sz[2]))]
            elif h == "sheet":
                at, sz = sexpr.find(n, "at"), sexpr.find(n, "size")
                self.sheet_boxes.append(Box(float(at[1]), float(at[2]), float(at[1]) + float(sz[1]),
                                            float(at[2]) + float(sz[2])))
                for p in sexpr.findall(n, "pin"):
                    pa = sexpr.find(p, "at")
                    self.anchors_extra.append((float(pa[1]), float(pa[2])))
            elif h == "symbol":
                self.symbols.append(self._symbol(n))

    def _symbol(self, n: sexpr.SList) -> Symbol:
        at = sexpr.find(n, "at")
        pos = (float(at[1]), float(at[2]))
        rot = float(at[3]) if len(at) > 3 else 0.0
        mirror = str(sexpr.value(sexpr.find(n, "mirror"), 1, ""))
        unit = int(sexpr.value(sexpr.find(n, "unit"), 1, 1) or 1)
        body_style = int(sexpr.value(sexpr.find(n, "body_style"), 1, 0) or sexpr.value(sexpr.find(n, "convert"), 1, 1) or 1)
        lib_id = str(sexpr.value(sexpr.find(n, "lib_id"), 1, ""))
        lib_name = str(sexpr.value(sexpr.find(n, "lib_name"), 1, "")) or lib_id
        props = sexpr.prop_map(n)
        ref = props.get("Reference", "")
        inst = next(iter(sexpr.walk(n, "reference")), None)
        if inst is not None:
            ref = str(inst[1])
        sym = Symbol(str(sexpr.value(sexpr.find(n, "uuid"), 1, "")), ref, props.get("Value", ""), lib_id, pos, rot,
                     mirror, unit, n, orig=pos)
        for p in sexpr.findall(n, "property"):
            pa = sexpr.find(p, "at")
            if pa is None:
                continue
            eff = sexpr.find(p, "effects")
            size = 1.27
            just = ""
            if eff is not None:
                font = sexpr.find(eff, "font")
                sz = sexpr.find(font, "size") if font is not None else None
                if sz is not None:
                    size = float(sz[1])
                jn = sexpr.find(eff, "justify")
                if jn is not None:
                    just = " ".join(str(x) for x in jn[1:])
            fp = (float(pa[1]), float(pa[2]))
            frot = float(pa[3]) if len(pa) > 3 else 0.0
            name = str(p[1])
            sym.fields[name] = Field(name, str(p[2]), fp, frot, _hidden(p), size, p, just, orig=(fp, frot),
                                     srot=rot, smirror=mirror)
        lib = self._libs.get(lib_name)
        if lib is not None and sexpr.find(lib, "extends") is not None:
            lib = self._libs.get(str(sexpr.value(sexpr.find(lib, "extends"), 1, "")), lib)
        if lib is not None:
            units = _lib_units(lib)
            gpts: List[Pt] = []
            for (u, b), sub in units.items():
                if u not in (0, unit) or b not in (0, body_style):
                    continue
                gpts += _graphics_points(sub)
                for pn in sexpr.findall(sub, "pin"):
                    pa = sexpr.find(pn, "at")
                    num = str(sexpr.value(sexpr.find(pn, "number"), 1, ""))
                    nm = str(sexpr.value(sexpr.find(pn, "name"), 1, ""))
                    hidden = "hide" in [x for x in pn if isinstance(x, str)] or _hidden(pn)
                    sym.pins.append(Pin(num, nm, str(pn[1]) if len(pn) > 1 else "",
                                        transform(float(pa[1]), float(pa[2]), pos, rot, mirror), hidden))
            if gpts:
                tp = [transform(x, y, pos, rot, mirror) for x, y in gpts]
                sym.body = Box(min(p[0] for p in tp), min(p[1] for p in tp), max(p[0] for p in tp), max(p[1] for p in tp))
        if sym.is_power:                      # power ports connect where they are placed, hidden pin or not
            for p in sym.pins:
                p.hidden = False
        return sym

    # ------------------------------------------------------------ queries
    def live_wires(self) -> List[Wire]:
        return [w for w in self.wires if not w.deleted]

    def anchor_points(self) -> List[Tuple[Pt, str]]:
        """Points that wires must stay attached to: (point, kind)."""
        out: List[Tuple[Pt, str]] = []
        for s in self.symbols:
            for p in s.pins:
                out.append((p.pos, f"pin:{s.uuid}:{p.number}"))
        for lb in self.labels:
            out.append((lb.pos, f"label:{lb.name}"))
        out += [(p, "nc") for p in self.no_connects]
        out += [(p, "extra") for p in self.anchors_extra]
        for b in self.buses:
            out += [(b.a, "bus"), (b.b, "bus")]
        return out

    def connectivity(self) -> set:
        """Pins grouped by electrical connection on this sheet (labels and power symbols join by name).
        Used to prove that a cleanup did not change any connection."""
        parent: Dict[object, object] = {}

        def find(a):
            parent.setdefault(a, a)
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        wires = self.live_wires()
        # points that connect: wire ends, pins, labels, junctions
        items: List[Tuple[Pt, object]] = []
        for i, w in enumerate(wires):
            items += [(w.a, ("w", i)), (w.b, ("w", i))]
        pins = []
        for s in self.symbols:
            for p in s.pins:
                pid = ("p", s.uuid, p.number)
                find(pid)
                pins.append(pid)
                if s.is_power and not s.is_flag:
                    union(pid, ("name", s.value))
                items.append((p.pos, pid))          # hidden pins still connect where they are
                if p.hidden and p.type == "power_in" and not s.is_power:   # legacy hidden power pins: by name
                    union(pid, ("name", p.name))
        for lb in self.labels:
            lid = ("l", id(lb))
            items.append((lb.pos, lid))
            union(lid, ("name", lb.name))      # labels and power nets of one name are one net
        for j in self.junctions:
            if not j.deleted:
                items.append((j.pos, ("j", id(j))))
        for p in self.anchors_extra:
            items.append((p, ("x", key(p))))
        by_pt: Dict[Tuple[int, int], List[object]] = {}
        for p, ident in items:
            by_pt.setdefault(key(p), []).append(ident)
        for ids in by_pt.values():
            for o in ids[1:]:
                union(ids[0], o)
        # any connection point lying inside a wire joins that wire
        horiz: Dict[int, List[int]] = {}
        vert: Dict[int, List[int]] = {}
        diag: List[int] = []
        for i, w in enumerate(wires):
            if w.horizontal:
                horiz.setdefault(round(w.a[1] * 1000), []).append(i)
            elif w.vertical:
                vert.setdefault(round(w.a[0] * 1000), []).append(i)
            else:
                diag.append(i)
        for kp, ids in by_pt.items():
            p = (kp[0] / 1000, kp[1] / 1000)
            cands = horiz.get(kp[1], []) + vert.get(kp[0], []) + diag
            for i in cands:
                w = wires[i]
                if on_segment(p, w.a, w.b):
                    union(ids[0], ("w", i))
        groups: Dict[object, set] = {}
        for pid in pins:
            groups.setdefault(find(pid), set()).add(f"{pid[1]}:{pid[2]}")
        return {frozenset(g) for g in groups.values()}

    # ------------------------------------------------------------ writing
    def render(self) -> str:
        """The file text with all edits applied."""
        edits: List[Tuple[int, int, str]] = []
        new_wires: List[str] = []

        def rm(node):
            s, e = node.span
            # take the whole line(s) with the node
            ls = self.text.rfind("\n", 0, s) + 1
            if self.text[ls:s].strip() == "":
                s = ls
            if self.text[e:e + 1] == "\n":
                e += 1
            edits.append((s, e, ""))

        for w in self.wires:
            if w.node is None:
                if not w.deleted:
                    new_wires.append(wire_text(w))
                continue
            if w.deleted:
                rm(w.node)
            elif not (same(w.a, w.orig[0]) and same(w.b, w.orig[1])):
                pts = sexpr.find(w.node, "pts")
                edits.append((pts.span[0], pts.span[1],
                              f"(pts\n\t\t\t(xy {sexpr.num(w.a[0])} {sexpr.num(w.a[1])}) "
                              f"(xy {sexpr.num(w.b[0])} {sexpr.num(w.b[1])})\n\t\t)"))
        for j in self.junctions:
            if j.deleted:
                rm(j.node)
            elif not same(j.pos, j.orig):
                at = sexpr.find(j.node, "at")
                edits.append((at.span[0], at.span[1], f"(at {sexpr.num(j.pos[0])} {sexpr.num(j.pos[1])})"))
        for lb in self.labels:
            if not same(lb.pos, lb.orig):
                at = sexpr.find(lb.node, "at")
                edits.append((at.span[0], at.span[1], f"(at {sexpr.num(lb.pos[0])} {sexpr.num(lb.pos[1])} {sexpr.num(lb.rot)})"))
        for s in self.symbols:
            if not same(s.pos, s.orig):
                at = sexpr.find(s.node, "at")
                edits.append((at.span[0], at.span[1], f"(at {sexpr.num(s.pos[0])} {sexpr.num(s.pos[1])} {sexpr.num(s.rot)})"))
            for f in s.fields.values():
                if not same(f.pos, f.orig[0]) or abs(f.rot - f.orig[1]) > EPS:
                    at = sexpr.find(f.node, "at")
                    edits.append((at.span[0], at.span[1],
                                  f"(at {sexpr.num(round(f.pos[0], 4))} {sexpr.num(round(f.pos[1], 4))} {sexpr.num(f.rot)})"))
                if f.centered:
                    eff = sexpr.find(f.node, "effects")
                    jn = sexpr.find(eff, "justify") if eff is not None else None
                    if jn is not None:
                        edits.append((jn.span[0], jn.span[1], ""))
        if new_wires:
            pos = self._insert_pos()
            edits.append((pos, pos, "".join(new_wires)))
        out = self.text
        for s, e, rep in sorted(edits, key=lambda t: (t[0], t[1]), reverse=True):
            out = out[:s] + rep + out[e:]
        return out

    def _insert_pos(self) -> int:
        """Before (sheet_instances ...) / (embedded_fonts ...) or the final ')'; after the last wire if any."""
        last = None
        for n in self.root:
            if isinstance(n, list) and n and n[0] == "wire":
                last = n
        if last is not None:
            e = last.span[1]
            return e + 1 if self.text[e:e + 1] == "\n" else e
        for n in self.root:
            if isinstance(n, list) and n and n[0] in ("sheet_instances", "embedded_fonts", "symbol_instances"):
                return self.text.rfind("\n", 0, n.span[0]) + 1
        return self.root.span[1] - 1


def wire_text(w: Wire) -> str:
    return (f"\t(wire\n\t\t(pts\n\t\t\t(xy {sexpr.num(w.a[0])} {sexpr.num(w.a[1])}) (xy {sexpr.num(w.b[0])} {sexpr.num(w.b[1])})\n"
            f"\t\t)\n\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n\t\t(uuid \"{w.uuid}\")\n\t)\n")


def new_uuid() -> str:
    return str(uuidlib.uuid4())
