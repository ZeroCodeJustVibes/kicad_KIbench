"""KiCad footprint model, `.kicad_mod` writer and reader. Units: mm, Y down."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union

from .. import sexpr
from ..sexpr import num, q

FP_VERSION = "20241229"     # KiCad 9 format; KiCad 10 reads and upgrades it

SMD_LAYERS = ["F.Cu", "F.Paste", "F.Mask"]
THT_LAYERS = ["*.Cu", "*.Mask"]
NPTH_LAYERS = ["*.Cu", "*.Mask"]


@dataclass
class Pad:
    number: str
    kind: str = "smd"                 # smd | thru_hole | np_thru_hole | connect
    shape: str = "roundrect"          # rect | roundrect | circle | oval | custom
    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0
    rot: float = 0.0
    drill: Union[None, float, Tuple[float, float]] = None
    layers: List[str] = field(default_factory=lambda: list(SMD_LAYERS))
    rratio: float = 0.25
    polygon: Optional[List[Tuple[float, float]]] = None   # custom pad outline relative to pad center
    paste_ratio: Optional[float] = None
    drill_offset: Tuple[float, float] = (0.0, 0.0)

    def bbox(self) -> Tuple[float, float, float, float]:
        if self.polygon:
            pts = [_rot(px, py, self.rot) for px, py in self.polygon]
            xs = [self.x + p[0] for p in pts]
            ys = [self.y + p[1] for p in pts]
            return min(xs), min(ys), max(xs), max(ys)
        a = math.radians(self.rot)
        hw = abs(self.w / 2 * math.cos(a)) + abs(self.h / 2 * math.sin(a))
        hh = abs(self.w / 2 * math.sin(a)) + abs(self.h / 2 * math.cos(a))
        return self.x - hw, self.y - hh, self.x + hw, self.y + hh


def _rot(x: float, y: float, deg: float) -> Tuple[float, float]:
    if not deg:
        return x, y
    a = math.radians(-deg)   # KiCad rotation is CCW in a Y-down frame
    return x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)


@dataclass
class Line:
    x1: float
    y1: float
    x2: float
    y2: float
    layer: str = "F.SilkS"
    width: float = 0.12


@dataclass
class Rect:
    x1: float
    y1: float
    x2: float
    y2: float
    layer: str = "F.Fab"
    width: float = 0.1
    fill: bool = False


@dataclass
class Circle:
    cx: float
    cy: float
    r: float
    layer: str = "F.Fab"
    width: float = 0.1
    fill: bool = False


@dataclass
class Arc:
    start: Tuple[float, float]
    mid: Tuple[float, float]
    end: Tuple[float, float]
    layer: str = "F.SilkS"
    width: float = 0.12


@dataclass
class Poly:
    pts: List[Tuple[float, float]]
    layer: str = "F.Fab"
    width: float = 0.1
    fill: bool = False


@dataclass
class Text:
    text: str
    x: float
    y: float
    layer: str = "F.Fab"
    size: float = 1.0


@dataclass
class Model:
    path: str
    offset: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotate: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: Tuple[float, float, float] = (1.0, 1.0, 1.0)


@dataclass
class Footprint:
    name: str
    descr: str = ""
    tags: str = ""
    attr: str = "smd"                    # smd | through_hole
    pads: List[Pad] = field(default_factory=list)
    graphics: list = field(default_factory=list)
    models: List[Model] = field(default_factory=list)
    ref_y: Optional[float] = None
    value_y: Optional[float] = None

    def pad_bbox(self) -> Tuple[float, float, float, float]:
        if not self.pads:
            return (0, 0, 0, 0)
        bbs = [p.bbox() for p in self.pads]
        return (min(b[0] for b in bbs), min(b[1] for b in bbs), max(b[2] for b in bbs), max(b[3] for b in bbs))

    def layer_bbox(self, layers: Tuple[str, ...]) -> Optional[Tuple[float, float, float, float]]:
        pts = []
        for g in self.graphics:
            if getattr(g, "layer", "") in layers:
                pts += graphic_points(g)
        if not pts:
            return None
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))

    def numbered_pads(self) -> List[str]:
        return [p.number for p in self.pads if p.number and p.kind != "np_thru_hole"]

    def add_courtyard(self, margin: float = 0.25, grid: float = 0.01, replace: bool = True) -> None:
        if replace:
            self.graphics = [g for g in self.graphics if getattr(g, "layer", "") != "F.CrtYd"]
        x1, y1, x2, y2 = self.pad_bbox()
        fab = self.layer_bbox(("F.Fab",))
        if fab:
            x1, y1, x2, y2 = min(x1, fab[0]), min(y1, fab[1]), max(x2, fab[2]), max(y2, fab[3])

        def r_out(v, up):
            f = math.ceil if up else math.floor
            return round(f(round(v / grid, 6)) * grid, 4)
        self.graphics.append(Rect(r_out(x1 - margin, False), r_out(y1 - margin, False),
                                  r_out(x2 + margin, True), r_out(y2 + margin, True), "F.CrtYd", 0.05))


def graphic_points(g) -> List[Tuple[float, float]]:
    if isinstance(g, Line):
        return [(g.x1, g.y1), (g.x2, g.y2)]
    if isinstance(g, Rect):
        return [(g.x1, g.y1), (g.x2, g.y2)]
    if isinstance(g, Circle):
        return [(g.cx - g.r, g.cy - g.r), (g.cx + g.r, g.cy + g.r)]
    if isinstance(g, Arc):
        return [g.start, g.mid, g.end]
    if isinstance(g, Poly):
        return list(g.pts)
    if isinstance(g, Text):
        return [(g.x, g.y)]
    return []


# ------------------------------------------------------------------ writer

def _stroke(w: float) -> str:
    return f"(stroke (width {num(w)}) (type solid))"


def _fill(f: bool) -> str:
    return f"(fill {'yes' if f else 'no'})"


def _graphic(g) -> str:
    if isinstance(g, Line):
        return f"\t(fp_line (start {num(g.x1)} {num(g.y1)}) (end {num(g.x2)} {num(g.y2)}) {_stroke(g.width)} (layer {q(g.layer)}))\n"
    if isinstance(g, Rect):
        return (f"\t(fp_rect (start {num(g.x1)} {num(g.y1)}) (end {num(g.x2)} {num(g.y2)}) {_stroke(g.width)} "
                f"{_fill(g.fill)} (layer {q(g.layer)}))\n")
    if isinstance(g, Circle):
        return (f"\t(fp_circle (center {num(g.cx)} {num(g.cy)}) (end {num(g.cx + g.r)} {num(g.cy)}) "
                f"{_stroke(g.width)} {_fill(g.fill)} (layer {q(g.layer)}))\n")
    if isinstance(g, Arc):
        return (f"\t(fp_arc (start {num(g.start[0])} {num(g.start[1])}) (mid {num(g.mid[0])} {num(g.mid[1])}) "
                f"(end {num(g.end[0])} {num(g.end[1])}) {_stroke(g.width)} (layer {q(g.layer)}))\n")
    if isinstance(g, Poly):
        pts = " ".join(f"(xy {num(x)} {num(y)})" for x, y in g.pts)
        return f"\t(fp_poly (pts {pts}) {_stroke(g.width)} {_fill(g.fill)} (layer {q(g.layer)}))\n"
    if isinstance(g, Text):
        thick = round(g.size * 0.15, 3)
        return (f"\t(fp_text user {q(g.text)} (at {num(g.x)} {num(g.y)} 0) (layer {q(g.layer)}) "
                f"(effects (font (size {num(g.size)} {num(g.size)}) (thickness {num(thick)}))))\n")
    return ""


def _pad(p: Pad) -> str:
    at = f"(at {num(p.x)} {num(p.y)}" + (f" {num(p.rot)}" if p.rot else "") + ")"
    s = f"\t(pad {q(p.number)} {p.kind} {p.shape} {at} (size {num(p.w)} {num(p.h)})"
    if p.drill is not None:
        off = ""
        if p.drill_offset != (0.0, 0.0):
            off = f" (offset {num(p.drill_offset[0])} {num(p.drill_offset[1])})"
        if isinstance(p.drill, tuple):
            s += f" (drill oval {num(p.drill[0])} {num(p.drill[1])}{off})"
        else:
            s += f" (drill {num(p.drill)}{off})"
    s += " (layers " + " ".join(q(l) for l in p.layers) + ")"
    if p.shape == "roundrect":
        s += f" (roundrect_rratio {num(p.rratio)})"
    if p.paste_ratio is not None:
        s += f" (solder_paste_margin_ratio {num(p.paste_ratio)})"
    if p.shape == "custom" and p.polygon:
        pts = " ".join(f"(xy {num(x)} {num(y)})" for x, y in p.polygon)
        s += (" (options (clearance outline) (anchor rect))"
              f" (primitives (gr_poly (pts {pts}) (width 0) (fill yes)))")
    return s + ")\n"


def footprint_to_text(fp: Footprint) -> str:
    x1, y1, x2, y2 = fp.pad_bbox()
    crt = fp.layer_bbox(("F.CrtYd",)) or (x1, y1, x2, y2)
    ref_y = fp.ref_y if fp.ref_y is not None else round(crt[1] - 1.0, 2)
    val_y = fp.value_y if fp.value_y is not None else round(crt[3] + 1.0, 2)
    out = f"(footprint {q(fp.name)}\n\t(version {FP_VERSION})\n\t(generator \"kibench\")\n\t(generator_version \"0.1\")\n"
    out += "\t(layer \"F.Cu\")\n"
    if fp.descr:
        out += f"\t(descr {q(fp.descr)})\n"
    if fp.tags:
        out += f"\t(tags {q(fp.tags)})\n"
    eff = "(effects (font (size 1 1) (thickness 0.15)))"
    out += f"\t(property \"Reference\" \"REF**\" (at 0 {num(ref_y)} 0) (layer \"F.SilkS\") {eff})\n"
    out += f"\t(property \"Value\" {q(fp.name)} (at 0 {num(val_y)} 0) (layer \"F.Fab\") {eff})\n"
    out += (f"\t(property \"Datasheet\" \"\" (at 0 0 0) (layer \"F.Fab\") (hide yes) {eff})\n"
            f"\t(property \"Description\" {q(fp.descr)} (at 0 0 0) (layer \"F.Fab\") (hide yes) {eff})\n")
    attr = "smd" if fp.attr == "smd" else "through_hole"
    out += f"\t(attr {attr})\n"
    for g in fp.graphics:
        out += _graphic(g)
    if not any(isinstance(g, Text) and g.text == "${REFERENCE}" for g in fp.graphics):
        size = max(0.4, min(1.0, (crt[2] - crt[0]) / 5))
        out += _graphic(Text("${REFERENCE}", 0, 0, "F.Fab", round(size, 2)))
    for p in fp.pads:
        out += _pad(p)
    for m in fp.models:
        out += (f"\t(model {q(m.path)}\n\t\t(offset (xyz {' '.join(num(v) for v in m.offset)}))\n"
                f"\t\t(scale (xyz {' '.join(num(v) for v in m.scale)}))\n"
                f"\t\t(rotate (xyz {' '.join(num(v) for v in m.rotate)}))\n\t)\n")
    out += ")\n"
    return out


# ------------------------------------------------------------------ reader

def _xy(node, name: str, default=(0.0, 0.0)) -> Tuple[float, float]:
    n = sexpr.find(node, name)
    if not n or len(n) < 3:
        return default
    return float(n[1]), float(n[2])


def read_footprint(text: str) -> Footprint:
    root = sexpr.parse(text)
    fp = Footprint(name=str(root[1]))
    fp.descr = str(sexpr.value(sexpr.find(root, "descr"), 1, ""))
    fp.tags = str(sexpr.value(sexpr.find(root, "tags"), 1, ""))
    attr = sexpr.find(root, "attr")
    fp.attr = "through_hole" if attr and "through_hole" in attr else "smd"
    for p in sexpr.findall(root, "pad"):
        at = sexpr.find(p, "at")
        size = sexpr.find(p, "size")
        layers = sexpr.find(p, "layers")
        drill = sexpr.find(p, "drill")
        d = None
        if drill:
            vals = [x for x in drill[1:] if not isinstance(x, list)]
            if vals and vals[0] == "oval":
                d = (float(vals[1]), float(vals[2]) if len(vals) > 2 else float(vals[1]))
            elif vals:
                d = float(vals[0])
        fp.pads.append(Pad(
            number=str(p[1]), kind=str(p[2]), shape=str(p[3]),
            x=float(at[1]) if at else 0.0, y=float(at[2]) if at else 0.0,
            rot=float(at[3]) if at and len(at) > 3 else 0.0,
            w=float(size[1]) if size else 0.0, h=float(size[2]) if size else 0.0,
            drill=d, layers=[str(x) for x in layers[1:]] if layers else []))
    for name in ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly"):
        for g in sexpr.findall(root, name):
            layer = str(sexpr.value(sexpr.find(g, "layer"), 1, ""))
            st = sexpr.find(g, "stroke")
            w = float(sexpr.value(sexpr.find(st, "width"), 1, 0.1)) if st else float(sexpr.value(sexpr.find(g, "width"), 1, 0.1))
            if name == "fp_line":
                (a, b), (c, d2) = _xy(g, "start"), _xy(g, "end")
                fp.graphics.append(Line(a, b, c, d2, layer, w))
            elif name == "fp_rect":
                (a, b), (c, d2) = _xy(g, "start"), _xy(g, "end")
                fp.graphics.append(Rect(a, b, c, d2, layer, w))
            elif name == "fp_circle":
                (a, b), (c, d2) = _xy(g, "center"), _xy(g, "end")
                fp.graphics.append(Circle(a, b, math.hypot(c - a, d2 - b), layer, w))
            elif name == "fp_arc":
                fp.graphics.append(Arc(_xy(g, "start"), _xy(g, "mid"), _xy(g, "end"), layer, w))
            else:
                pts = sexpr.find(g, "pts")
                fp.graphics.append(Poly([(float(x[1]), float(x[2])) for x in sexpr.findall(pts, "xy")] if pts else [],
                                        layer, w))
    for m in sexpr.findall(root, "model"):
        off = sexpr.find(sexpr.find(m, "offset") or [], "xyz") if sexpr.find(m, "offset") else None
        rot = sexpr.find(sexpr.find(m, "rotate") or [], "xyz") if sexpr.find(m, "rotate") else None
        fp.models.append(Model(str(m[1]),
                               tuple(float(v) for v in off[1:4]) if off else (0, 0, 0),
                               tuple(float(v) for v in rot[1:4]) if rot else (0, 0, 0)))
    return fp


def set_model_path(text: str, new_path: str) -> str:
    """Replace (or add) the 3D model path in raw footprint text."""
    import re
    if "(model " in text:
        return re.sub(r'\(model\s+("(?:[^"\\]|\\.)*"|\S+)', lambda m: f"(model {q(new_path)}", text, count=1)
    idx = text.rstrip().rfind(")")
    block = (f"\t(model {q(new_path)}\n\t\t(offset (xyz 0 0 0))\n\t\t(scale (xyz 1 1 1))\n"
             f"\t\t(rotate (xyz 0 0 0))\n\t)\n")
    return text[:idx] + block + text[idx:]


def rename_footprint_raw(text: str, new_name: str) -> str:
    import re
    return re.sub(r'^\((footprint|module)\s+("(?:[^"\\]|\\.)*"|\S+)', lambda m: f"(footprint {q(new_name)}",
                  text.lstrip(), count=1)
