"""Minimal .kicad_pcb model: footprints (pads, texts, courtyard, silkscreen), tracks, vias, board outline.

Coordinates are board mm (y down), as in the file. Footprint children are stored relative to the footprint
(rotated with it); their angles are stored absolute. Flipped (B.Cu) footprints already hold mirrored
geometry, so absolute = footprint position + rotate(local, footprint angle).

Edits are written back as span replacements of `(at ...)` nodes, so untouched text stays byte-for-byte.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .. import sexpr
from .schematic import Box

Pt = Tuple[float, float]
Seg = Tuple[Pt, Pt]


def rot_pt(p: Pt, deg: float) -> Pt:
    """Rotate counter-clockwise on screen (y down) by `deg`."""
    if not deg:
        return p
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return (p[0] * c + p[1] * s, -p[0] * s + p[1] * c)


def _xy(node, default=(0.0, 0.0, 0.0)) -> Tuple[float, float, float]:
    if node is None:
        return default
    vals = [float(x) for x in node[1:4] if isinstance(x, str) and re.match(r"^-?[\d.]+(e-?\d+)?$", x)]
    while len(vals) < 3:
        vals.append(0.0)
    return vals[0], vals[1], vals[2]


def norm_angle(a: float) -> float:
    a = a % 360.0
    return 0.0 if abs(a - 360.0) < 1e-6 else a


def seg_dist(a: Pt, b: Pt, c: Pt, d: Pt) -> float:
    """Shortest distance between segments ab and cd."""
    def pt_seg(p, s, e):
        dx, dy = e[0] - s[0], e[1] - s[1]
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - s[0]) * dx + (p[1] - s[1]) * dy) / L))
        return math.hypot(p[0] - s[0] - t * dx, p[1] - s[1] - t * dy)

    def ccw(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    if a != b and c != d:
        d1, d2, d3, d4 = ccw(a, b, c), ccw(a, b, d), ccw(c, d, a), ccw(c, d, b)
        if (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0) and d1 and d2 and d3 and d4:
            return 0.0
    return min(pt_seg(a, c, d), pt_seg(b, c, d), pt_seg(c, a, b), pt_seg(d, a, b))


def poly_overlap(a: List[Pt], b: List[Pt], margin: float = 0.0) -> bool:
    """Convex polygons intersect (separating axis test); a negative margin ignores touching edges."""
    for poly in (a, b):
        n = len(poly)
        for i in range(n):
            p, q = poly[i], poly[(i + 1) % n]
            ax, ay = q[1] - p[1], p[0] - q[0]
            L = math.hypot(ax, ay)
            if L == 0:
                continue
            ax, ay = ax / L, ay / L
            pa = [x * ax + y * ay for x, y in a]
            pb = [x * ax + y * ay for x, y in b]
            if max(pa) + margin <= min(pb) or max(pb) + margin <= min(pa):
                return False
    return True


def seg_hits_box(a: Pt, b: Pt, bx: Box, m: float = 0.0) -> bool:
    """Segment crosses the (grown by m) box (Liang-Barsky clipping)."""
    x1, y1, x2, y2 = bx.x1 - m, bx.y1 - m, bx.x2 + m, bx.y2 + m
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x1), (dx, x2 - a[0]), (-dy, a[1] - y1), (dy, y2 - a[1])):
        if p == 0:
            if q < 0:
                return False
        else:
            t = q / p
            if p < 0:
                if t > t1:
                    return False
                t0 = max(t0, t)
            else:
                if t < t0:
                    return False
                t1 = min(t1, t)
    return t0 <= t1


def box_corners(b: Box) -> List[Pt]:
    return [(b.x1, b.y1), (b.x2, b.y1), (b.x2, b.y2), (b.x1, b.y2)]


def copper_layers_of(layers: List[str], all_cu: List[str]) -> List[str]:
    out: List[str] = []
    for l in layers:
        if l in ("*.Cu", "F&B.Cu"):
            return list(all_cu) if l == "*.Cu" else ["F.Cu", "B.Cu"]
        if l.endswith(".Cu"):
            out.append(l)
    return out


# ---------------------------------------------------------------------- items
@dataclass
class Pad:
    num: str
    kind: str                    # smd | thru_hole | np_thru_hole | connect
    shape: str
    pos: Pt                      # absolute
    size: Pt
    rot: float                   # absolute
    layers: List[str]
    net: str
    local: Pt = (0.0, 0.0)
    drill: float = 0.0

    def corners(self) -> List[Pt]:
        w, h = self.size[0] / 2, self.size[1] / 2
        return [(self.pos[0] + q[0], self.pos[1] + q[1])
                for q in (rot_pt(c, self.rot) for c in ((-w, -h), (w, -h), (w, h), (-w, h)))]

    def box(self) -> Box:
        cs = self.corners()
        return Box(min(c[0] for c in cs), min(c[1] for c in cs), max(c[0] for c in cs), max(c[1] for c in cs))

    def capsule(self) -> Tuple[Pt, Pt, float]:
        """Centre line and radius (rectangles are approximated by their inscribed capsule)."""
        w, h = self.size
        if abs(w - h) < 1e-6:
            return self.pos, self.pos, w / 2
        if w > h:
            d, r = rot_pt(((w - h) / 2, 0.0), self.rot), h / 2
        else:
            d, r = rot_pt((0.0, (h - w) / 2), self.rot), w / 2
        return (self.pos[0] - d[0], self.pos[1] - d[1]), (self.pos[0] + d[0], self.pos[1] + d[1]), r


@dataclass
class Text:
    name: str                    # Reference | Value | user
    text: str
    pos: Pt                      # absolute
    rot: float                   # absolute
    layer: str
    size: Pt                     # (h, w) as in the file: (size H W)
    thickness: float
    hidden: bool
    justify: List[str]
    node: sexpr.SList
    at: Optional[sexpr.SList]
    orig: Tuple[Pt, float] = ((0.0, 0.0), 0.0)
    knockout: bool = False

    def corners(self, pos: Optional[Pt] = None, rot: Optional[float] = None) -> List[Pt]:
        """The rotated text rectangle."""
        pos = self.pos if pos is None else pos
        rot = self.rot if rot is None else rot
        h, w = self.size
        tw = max(1, len(self.text)) * w * 0.85 + self.thickness
        th = h + self.thickness
        ox = 0.0
        if "left" in self.justify:
            ox = tw / 2
        elif "right" in self.justify:
            ox = -tw / 2
        if "mirror" in self.justify:
            ox = -ox
        oy = th / 2 if "top" in self.justify else (-th / 2 if "bottom" in self.justify else 0.0)
        cs = [rot_pt((ox + sx * tw / 2, oy + sy * th / 2), rot) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        return [(pos[0] + c[0], pos[1] + c[1]) for c in cs]

    def box(self, pos: Optional[Pt] = None, rot: Optional[float] = None) -> Box:
        pos = self.pos if pos is None else pos
        rot = self.rot if rot is None else rot
        h, w = self.size
        tw = max(1, len(self.text)) * w * 0.85 + self.thickness
        th = h + self.thickness
        ox = 0.0
        if "left" in self.justify:
            ox = tw / 2
        elif "right" in self.justify:
            ox = -tw / 2
        if "mirror" in self.justify:
            ox = -ox
        oy = th / 2 if "top" in self.justify else (-th / 2 if "bottom" in self.justify else 0.0)
        cs = [rot_pt((ox + sx * tw / 2, oy + sy * th / 2), rot) for sx in (-1, 1) for sy in (-1, 1)]
        return Box(pos[0] + min(c[0] for c in cs), pos[1] + min(c[1] for c in cs),
                   pos[0] + max(c[0] for c in cs), pos[1] + max(c[1] for c in cs))

    @property
    def on_silk(self) -> bool:
        return self.layer.endswith(".SilkS")


@dataclass
class Footprint:
    ref: str
    value: str
    lib: str
    uuid: str
    pos: Pt
    rot: float
    layer: str                   # F.Cu | B.Cu
    locked: bool
    pads: List[Pad]
    texts: List[Text]
    node: sexpr.SList
    at: sexpr.SList
    court_local: List[Pt] = field(default_factory=list)     # courtyard points (local)
    body_local: List[Pt] = field(default_factory=list)      # fab/silk points (local) when there is no courtyard
    silk_local: List[Seg] = field(default_factory=list)     # silkscreen strokes (local)
    attrs: List[str] = field(default_factory=list)
    orig: Pt = (0.0, 0.0)

    @property
    def side(self) -> str:
        return "B" if self.layer.startswith("B.") else "F"

    def to_abs(self, p: Pt, pos: Optional[Pt] = None) -> Pt:
        pos = self.pos if pos is None else pos
        r = rot_pt(p, self.rot)
        return (pos[0] + r[0], pos[1] + r[1])

    def to_local(self, p: Pt) -> Pt:
        return rot_pt((p[0] - self.pos[0], p[1] - self.pos[1]), -self.rot)

    def body(self) -> Box:
        """Pads plus fab/silk outline: what the part covers on the board."""
        pts = [self.to_abs(p) for p in self.body_local]
        boxes = [p.box() for p in self.pads]
        if pts:
            boxes.append(Box(min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts),
                             max(p[1] for p in pts)))
        if not boxes:
            return self.courtyard()
        return Box(min(b.x1 for b in boxes), min(b.y1 for b in boxes), max(b.x2 for b in boxes),
                   max(b.y2 for b in boxes))

    def courtyard(self) -> Box:
        pts = [self.to_abs(p) for p in (self.court_local or self.body_local)]
        boxes = [p.box() for p in self.pads]
        if pts:
            b = Box(min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
            boxes.append(b)
        if not boxes:
            return Box(self.pos[0] - 0.5, self.pos[1] - 0.5, self.pos[0] + 0.5, self.pos[1] + 0.5)
        return Box(min(b.x1 for b in boxes), min(b.y1 for b in boxes), max(b.x2 for b in boxes),
                   max(b.y2 for b in boxes))

    def silk_segs(self) -> List[Seg]:
        return [(self.to_abs(a), self.to_abs(b)) for a, b in self.silk_local]

    @property
    def reference(self) -> Optional[Text]:
        return next((t for t in self.texts if t.name == "Reference"), None)

    def move_to(self, pos: Pt) -> None:
        dx, dy = pos[0] - self.pos[0], pos[1] - self.pos[1]
        self.pos = pos
        for p in self.pads:
            p.pos = (p.pos[0] + dx, p.pos[1] + dy)
        for t in self.texts:
            t.pos = (t.pos[0] + dx, t.pos[1] + dy)


@dataclass
class Track:
    a: Pt
    b: Pt
    width: float
    layer: str
    net: str
    kind: str = "segment"        # segment | arc
    mid: Optional[Pt] = None


@dataclass
class Via:
    pos: Pt
    size: float
    layers: List[str]
    net: str


# ---------------------------------------------------------------------- the model
def _shape_points(g: sexpr.SList) -> Tuple[List[Pt], List[Seg]]:
    """Points (for bounding boxes) and strokes of a graphic item."""
    h = g.head
    pts: List[Pt] = []
    segs: List[Seg] = []
    if h in ("fp_line", "gr_line"):
        a, b = _xy(sexpr.find(g, "start"))[:2], _xy(sexpr.find(g, "end"))[:2]
        pts, segs = [a, b], [(a, b)]
    elif h in ("fp_rect", "gr_rect"):
        a, b = _xy(sexpr.find(g, "start"))[:2], _xy(sexpr.find(g, "end"))[:2]
        cs = [a, (b[0], a[1]), b, (a[0], b[1])]
        pts, segs = cs, [(cs[i], cs[(i + 1) % 4]) for i in range(4)]
    elif h in ("fp_circle", "gr_circle"):
        c, e = _xy(sexpr.find(g, "center"))[:2], _xy(sexpr.find(g, "end"))[:2]
        r = math.hypot(e[0] - c[0], e[1] - c[1])
        cs = [(c[0] + r * math.cos(t * math.pi / 8), c[1] + r * math.sin(t * math.pi / 8)) for t in range(16)]
        pts, segs = cs, [(cs[i], cs[(i + 1) % 16]) for i in range(16)]
    elif h in ("fp_arc", "gr_arc"):
        a, m, b = (_xy(sexpr.find(g, k))[:2] for k in ("start", "mid", "end"))
        pts, segs = [a, m, b], [(a, m), (m, b)]
    elif h in ("fp_poly", "gr_poly"):
        ptsn = sexpr.find(g, "pts")
        cs = [_xy(x)[:2] for x in (sexpr.findall(ptsn, "xy") if ptsn is not None else [])]
        pts = cs
        segs = [(cs[i], cs[(i + 1) % len(cs)]) for i in range(len(cs))] if len(cs) > 1 else []
    return pts, segs


def _layer(node) -> str:
    return str(sexpr.value(sexpr.find(node, "layer"), 1, ""))


def _layers(node) -> List[str]:
    ln = sexpr.find(node, "layers")
    return [str(x) for x in ln[1:]] if ln is not None else []


def _hidden(node) -> bool:
    h = sexpr.find(node, "hide")
    if h is not None:
        return str(sexpr.value(h, 1, "yes")) != "no"
    eff = sexpr.find(node, "effects")
    return eff is not None and any(x == "hide" for x in eff[1:])


class BoardModel:
    def __init__(self, text: str):
        self.text = text
        self.root = sexpr.parse(text)
        self.net_names: Dict[str, str] = {}
        self.copper: List[str] = []
        for n in sexpr.findall(self.root, "net"):
            if len(n) >= 3:
                self.net_names[str(n[1])] = str(n[2])
        lay = sexpr.find(self.root, "layers")
        for l in (lay[1:] if lay is not None else []):
            if isinstance(l, list) and len(l) > 1 and str(l[1]).endswith(".Cu"):
                self.copper.append(str(l[1]))
        if not self.copper:
            self.copper = ["F.Cu", "B.Cu"]
        self.footprints: List[Footprint] = []
        self.tracks: List[Track] = []
        self.vias: List[Via] = []
        self.outline_segs: List[Seg] = []
        self.board_texts: List[Text] = []
        self.board_silk: Dict[str, List[Seg]] = {}      # silkscreen strokes per layer
        for node in self.root[1:]:
            if not isinstance(node, list):
                continue
            h = node.head
            if h == "footprint":
                self.footprints.append(self._footprint(node))
            elif h in ("segment", "arc"):
                a, b = _xy(sexpr.find(node, "start"))[:2], _xy(sexpr.find(node, "end"))[:2]
                mid = _xy(sexpr.find(node, "mid"))[:2] if sexpr.find(node, "mid") is not None else None
                self.tracks.append(Track(a, b, float(sexpr.value(sexpr.find(node, "width"), 1, 0.2)),
                                         _layer(node), self._net(node), h, mid))
            elif h == "via":
                at = _xy(sexpr.find(node, "at"))
                self.vias.append(Via(at[:2], float(sexpr.value(sexpr.find(node, "size"), 1, 0.6)),
                                     _layers(node) or ["F.Cu", "B.Cu"], self._net(node)))
            elif h in ("gr_line", "gr_rect", "gr_circle", "gr_arc", "gr_poly"):
                pts, segs = _shape_points(node)
                if _layer(node) == "Edge.Cuts":
                    self.outline_segs += segs
                elif _layer(node).endswith(".SilkS"):
                    self.board_silk.setdefault(_layer(node), []).extend(segs)
            elif h == "gr_text" and _layer(node).endswith(".SilkS"):
                t = self._text(node, "user", str(node[1]) if len(node) > 1 else "", None)
                if t:
                    self.board_texts.append(t)
        for fp in self.footprints:
            fp.orig = fp.pos
            for t in fp.texts:
                t.orig = (t.pos, t.rot)
        ob = self.outline_box()
        self.outline = ob

    # ------------------------------------------------------------ parsing
    def _net(self, node) -> str:
        n = sexpr.find(node, "net")
        if n is None or len(n) < 2:
            return ""
        if len(n) >= 3:
            return str(n[2])
        v = n[1]
        if isinstance(v, sexpr.QStr):
            return str(v)
        return self.net_names.get(str(v), "")

    def _text(self, node, name: str, text: str, fp: Optional["Footprint"]) -> Optional[Text]:
        at = sexpr.find(node, "at")
        x, y, r = _xy(at)
        pos = fp.to_abs((x, y)) if fp is not None else (x, y)
        eff = sexpr.find(node, "effects")
        font = sexpr.find(eff, "font") if eff is not None else None
        sz = sexpr.find(font, "size") if font is not None else None
        h = float(sexpr.value(sz, 1, 1.0)) if sz is not None else 1.0
        w = float(sexpr.value(sz, 2, h)) if sz is not None else h
        th = float(sexpr.value(sexpr.find(font, "thickness"), 1, 0.15)) if font is not None and \
            sexpr.find(font, "thickness") is not None else 0.15 * h
        js = sexpr.find(eff, "justify") if eff is not None else None
        justify = [str(v) for v in js[1:]] if js is not None else []
        ko = sexpr.find(node, "render_cache") is not None and False
        return Text(name, text, pos, norm_angle(r), _layer(node), (h, w), th, _hidden(node), justify, node, at,
                    knockout=ko)

    def _footprint(self, node) -> Footprint:
        at = sexpr.find(node, "at")
        x, y, r = _xy(at)
        lib = str(node[1]) if len(node) > 1 and not isinstance(node[1], list) else ""
        fp = Footprint("", "", lib, str(sexpr.value(sexpr.find(node, "uuid"), 1, "")), (x, y), norm_angle(r),
                       _layer(node) or "F.Cu", False, [], [], node, at)
        fp.locked = any(v == "locked" for v in node[1:3] if isinstance(v, str)) or \
            str(sexpr.value(sexpr.find(node, "locked"), 1, "no")) == "yes"
        a = sexpr.find(node, "attr")
        fp.attrs = [str(v) for v in a[1:]] if a is not None else []
        for c in node[1:]:
            if not isinstance(c, list):
                continue
            h = c.head
            if h == "property" and len(c) >= 3:
                name, val = str(c[1]), str(c[2])
                if name == "Reference":
                    fp.ref = val
                elif name == "Value":
                    fp.value = val
                if sexpr.find(c, "at") is not None:
                    t = self._text(c, name, val, fp)
                    if t:
                        fp.texts.append(t)
            elif h == "fp_text" and len(c) >= 3:
                kind = str(c[1])
                t = self._text(c, {"reference": "Reference", "value": "Value"}.get(kind, "user"), str(c[2]), fp)
                if t:
                    if kind == "reference" and not fp.ref:
                        fp.ref = t.text
                    if kind == "value" and not fp.value:
                        fp.value = t.text
                    fp.texts.append(t)
            elif h == "pad":
                pat = _xy(sexpr.find(c, "at"))
                sz = _xy(sexpr.find(c, "size"))
                drill = sexpr.find(c, "drill")
                dval = 0.0
                if drill is not None:
                    nums = [x for x in drill[1:] if isinstance(x, str) and re.match(r"^[\d.]+$", x)]
                    dval = float(nums[0]) if nums else 0.0
                fp.pads.append(Pad(str(c[1]) if len(c) > 1 else "", str(c[2]) if len(c) > 2 else "",
                                   str(c[3]) if len(c) > 3 else "", fp.to_abs(pat[:2]), (sz[0], sz[1] or sz[0]),
                                   norm_angle(pat[2]), _layers(c), self._net(c), pat[:2], dval))
            elif h in ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly"):
                lay = _layer(c)
                pts, segs = _shape_points(c)
                if lay.endswith(".CrtYd"):
                    fp.court_local += pts
                elif lay.endswith(".SilkS"):
                    fp.silk_local += segs
                    fp.body_local += pts
                elif lay.endswith(".Fab"):
                    fp.body_local += pts
        return fp

    # ------------------------------------------------------------ queries
    def outline_box(self) -> Optional[Box]:
        if not self.outline_segs:
            return None
        xs = [p[0] for s in self.outline_segs for p in s]
        ys = [p[1] for s in self.outline_segs for p in s]
        return Box(min(xs), min(ys), max(xs), max(ys))

    def by_ref(self) -> Dict[str, Footprint]:
        return {fp.ref: fp for fp in self.footprints}

    def routed_pads(self) -> set:
        """(footprint index, pad number) of pads that a track end or via touches."""
        ends: Dict[Tuple[int, int], List[Tuple[Pt, str]]] = {}

        def cell(p):
            return (int(math.floor(p[0])), int(math.floor(p[1])))
        for t in self.tracks:
            for p in (t.a, t.b):
                ends.setdefault(cell(p), []).append((p, t.layer))
        for v in self.vias:
            ends.setdefault(cell(v.pos), []).append((v.pos, "*"))
        out = set()
        for i, fp in enumerate(self.footprints):
            for pad in fp.pads:
                if not pad.net:
                    continue
                b = pad.box()
                cu = copper_layers_of(pad.layers, self.copper)
                hit = False
                for cx in range(int(math.floor(b.x1)) - 1, int(math.floor(b.x2)) + 2):
                    for cy in range(int(math.floor(b.y1)) - 1, int(math.floor(b.y2)) + 2):
                        for p, lay in ends.get((cx, cy), []):
                            if (lay == "*" or lay in cu) and b.x1 - 0.01 <= p[0] <= b.x2 + 0.01 and \
                                    b.y1 - 0.01 <= p[1] <= b.y2 + 0.01:
                                hit = True
                                break
                        if hit:
                            break
                    if hit:
                        break
                if hit:
                    out.add((i, pad.num))
        return out

    # ------------------------------------------------------------ writing
    def render(self) -> str:
        edits: List[Tuple[int, int, str]] = []
        for fp in self.footprints:
            if fp.pos != fp.orig and fp.at is not None:
                edits.append((fp.at.span[0], fp.at.span[1],
                              f"(at {sexpr.num(round(fp.pos[0], 4))} {sexpr.num(round(fp.pos[1], 4))}"
                              + (f" {sexpr.num(fp.rot)}" if fp.rot else "") + ")"))
            for t in fp.texts:
                if t.at is None:
                    continue
                moved = (t.pos[0] - t.orig[0][0], t.pos[1] - t.orig[0][1])
                fp_moved = (fp.pos[0] - fp.orig[0], fp.pos[1] - fp.orig[1])
                if abs(moved[0] - fp_moved[0]) < 1e-9 and abs(moved[1] - fp_moved[1]) < 1e-9 and t.rot == t.orig[1]:
                    continue                                    # moved along with the footprint (or not at all)
                loc = fp.to_local(t.pos)
                parts = [sexpr.num(round(loc[0], 4)), sexpr.num(round(loc[1], 4))]
                if t.rot:
                    parts.append(sexpr.num(t.rot))
                tail = " unlocked" if any(x == "unlocked" for x in t.at[1:]) else ""
                edits.append((t.at.span[0], t.at.span[1], f"(at {' '.join(parts)}{tail})"))
        text = self.text
        for s, e, rep in sorted(edits, key=lambda t: t[0], reverse=True):
            text = text[:s] + rep + text[e:]
        return text
