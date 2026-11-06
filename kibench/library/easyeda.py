"""Deterministic EasyEDA (LCSC) -> KiCad conversion: symbol, footprint and 3D model placement.

Clean-room implementation of the EasyEDA "dataStr" shape format (the same
format easyeda2kicad and JLCImport read). Units: EasyEDA uses 10 mil
(0.254 mm) per unit in both schematic and PCB documents.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..kicad import footprint as F
from ..kicad import symbol as S

U = 0.254   # mm per EasyEDA unit

PIN_TYPES = {"0": "unspecified", "1": "input", "2": "output", "3": "bidirectional", "4": "power_in"}
FP_LAYERS = {"1": "F.Cu", "2": "B.Cu", "3": "F.SilkS", "4": "B.SilkS", "5": "F.Paste", "6": "B.Paste",
             "7": "F.Mask", "8": "B.Mask", "10": "Edge.Cuts", "12": "Cmts.User", "13": "F.Fab",
             "14": "B.Fab", "15": "Dwgs.User", "99": "F.Fab", "101": "F.Fab"}


def _f(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _safe_name(s: str) -> str:
    s = re.sub(r"[^\w.\-+()]", "_", s.strip())
    return re.sub(r"_+", "_", s).strip("_") or "part"


# ----------------------------------------------------------------- SVG paths

def _path_tokens(d: str) -> List[str]:
    return re.findall(r"[MmLlHhVvAaZzCcQqSsTt]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", d)


def path_to_polylines(d: str, arc_steps: int = 12) -> List[List[Tuple[float, float]]]:
    """Flatten an SVG path (M/L/H/V/A/C/Q/Z, abs and rel) into polylines."""
    toks = _path_tokens(d)
    polys: List[List[Tuple[float, float]]] = []
    cur: List[Tuple[float, float]] = []
    x = y = sx = sy = 0.0
    i = 0
    cmd = ""

    def nums(n):
        nonlocal i
        vals = [float(t) for t in toks[i:i + n]]
        i += n
        return vals
    while i < len(toks):
        t = toks[i]
        if re.match(r"[A-Za-z]", t):
            cmd = t
            i += 1
            if cmd in "Zz":
                if cur:
                    cur.append((sx, sy))
                    polys.append(cur)
                    cur = []
                x, y = sx, sy
                continue
        if i >= len(toks) or re.match(r"[A-Za-z]", toks[i]):
            continue
        rel = cmd.islower()
        c = cmd.upper()
        if c == "M":
            nx, ny = nums(2)
            if rel:
                nx, ny = x + nx, y + ny
            if cur and len(cur) > 1:
                polys.append(cur)
            x, y, sx, sy = nx, ny, nx, ny
            cur = [(x, y)]
            cmd = "l" if rel else "L"
        elif c == "L":
            nx, ny = nums(2)
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            cur.append((x, y))
        elif c == "H":
            (nx,) = nums(1)
            x = x + nx if rel else nx
            cur.append((x, y))
        elif c == "V":
            (ny,) = nums(1)
            y = y + ny if rel else ny
            cur.append((x, y))
        elif c == "A":
            rx, ry, rot, large, sweep, nx, ny = nums(7)
            if rel:
                nx, ny = x + nx, y + ny
            cur += _arc_points(x, y, rx, ry, rot, int(large), int(sweep), nx, ny, arc_steps)[1:]
            x, y = nx, ny
        elif c in "CQST":
            n = {"C": 6, "Q": 4, "S": 4, "T": 2}[c]
            vals = nums(n)
            nx, ny = vals[-2], vals[-1]
            if rel:
                nx, ny = x + nx, y + ny
            x, y = nx, ny
            cur.append((x, y))
        else:
            i += 1
    if cur and len(cur) > 1:
        polys.append(cur)
    return polys


def _arc_center(x1, y1, rx, ry, phi_deg, fa, fs, x2, y2):
    """SVG endpoint -> center parameterisation (circular arcs assumed when rx==ry)."""
    if rx == 0 or ry == 0:
        return None
    phi = math.radians(phi_deg)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p = math.cos(phi) * dx + math.sin(phi) * dy
    y1p = -math.sin(phi) * dx + math.cos(phi) * dy
    rx, ry = abs(rx), abs(ry)
    lam = (x1p ** 2) / rx ** 2 + (y1p ** 2) / ry ** 2
    if lam > 1:
        rx *= math.sqrt(lam)
        ry *= math.sqrt(lam)
    num = rx ** 2 * ry ** 2 - rx ** 2 * y1p ** 2 - ry ** 2 * x1p ** 2
    den = rx ** 2 * y1p ** 2 + ry ** 2 * x1p ** 2
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if fa == fs:
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = math.cos(phi) * cxp - math.sin(phi) * cyp + (x1 + x2) / 2
    cy = math.sin(phi) * cxp + math.cos(phi) * cyp + (y1 + y2) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a
    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not fs and dt > 0:
        dt -= 2 * math.pi
    elif fs and dt < 0:
        dt += 2 * math.pi
    return cx, cy, rx, ry, phi, t1, dt


def _arc_points(x1, y1, rx, ry, rot, fa, fs, x2, y2, steps):
    c = _arc_center(x1, y1, rx, ry, rot, fa, fs, x2, y2)
    if not c:
        return [(x1, y1), (x2, y2)]
    cx, cy, rx, ry, phi, t1, dt = c
    pts = []
    for k in range(steps + 1):
        t = t1 + dt * k / steps
        px = cx + rx * math.cos(t) * math.cos(phi) - ry * math.sin(t) * math.sin(phi)
        py = cy + rx * math.cos(t) * math.sin(phi) + ry * math.sin(t) * math.cos(phi)
        pts.append((px, py))
    return pts


def arc_three_points(d: str):
    """First 'M x y A ...' arc in a path -> (start, mid, end) in source units."""
    toks = _path_tokens(d)
    try:
        mi = toks.index("M") if "M" in toks else toks.index("m")
        x1, y1 = float(toks[mi + 1]), float(toks[mi + 2])
        ai = next(k for k, t in enumerate(toks) if t in ("A", "a"))
        rx, ry, rot, fa, fs, x2, y2 = [float(t) for t in toks[ai + 1:ai + 8]]
        if toks[ai] == "a":
            x2, y2 = x1 + x2, y1 + y2
    except (ValueError, StopIteration, IndexError):
        return None
    pts = _arc_points(x1, y1, rx, ry, rot, int(fa), int(fs), x2, y2, 2)
    return pts[0], pts[1], pts[2]


# ----------------------------------------------------------------- symbol

def _sym_xy(x, y, ox, oy) -> Tuple[float, float]:
    return round((_f(x) - ox) * U, 4), round(-(_f(y) - oy) * U, 4)


def _snap(v: float, grid: float = 1.27) -> float:
    return round(round(v / grid) * grid, 4)


def _convert_sym_shapes(shapes: List[str], ox: float, oy: float) -> S.Unit:
    unit = S.Unit()
    for s in shapes:
        kind = s.split("~", 1)[0]
        try:
            if kind == "P":
                unit.pins.append(_sym_pin(s, ox, oy))
            elif kind == "R":
                f = s.split("~")
                x, y = _sym_xy(f[1], f[2], ox, oy)
                w, h = _f(f[5]) * U, _f(f[6]) * U
                fill = "background" if f[10] not in ("none", "") else "none"
                unit.graphics.append(S.SRect(x, y, round(x + w, 4), round(y - h, 4), 0.254, fill))
            elif kind == "E":
                f = s.split("~")
                cx, cy = _sym_xy(f[1], f[2], ox, oy)
                rx, ry = _f(f[3]) * U, _f(f[4]) * U
                fill = "background" if f[8] not in ("none", "") else "none"
                if abs(rx - ry) < 1e-6:
                    unit.graphics.append(S.SCircle(cx, cy, round(rx, 4), 0.254, fill))
                else:
                    pts = [(round(cx + rx * math.cos(t * math.pi / 12), 4), round(cy + ry * math.sin(t * math.pi / 12), 4))
                           for t in range(25)]
                    unit.graphics.append(S.SPoly(pts, 0.254, fill))
            elif kind == "C":
                f = s.split("~")
                cx, cy = _sym_xy(f[1], f[2], ox, oy)
                unit.graphics.append(S.SCircle(cx, cy, round(_f(f[3]) * U, 4)))
            elif kind in ("PL", "PG"):
                f = s.split("~")
                nums_ = [float(v) for v in f[1].split()]
                pts = [_sym_xy(nums_[k], nums_[k + 1], ox, oy) for k in range(0, len(nums_) - 1, 2)]
                fill = "none"
                if kind == "PG":
                    pts.append(pts[0])
                    fill = "outline" if f[5] not in ("none", "") else "none"
                unit.graphics.append(S.SPoly(pts, 0.254, fill))
            elif kind == "A":
                f = s.split("~")
                tp = arc_three_points(f[1])
                if tp:
                    a, m, b = (_sym_xy(p[0], p[1], ox, oy) for p in tp)
                    unit.graphics.append(S.SArc(a, m, b))
            elif kind == "PT":
                f = s.split("~")
                fill = "outline" if len(f) > 5 and f[5] not in ("none", "") else "none"
                for poly in path_to_polylines(f[1]):
                    unit.graphics.append(S.SPoly([_sym_xy(x, y, ox, oy) for x, y in poly], 0.254, fill))
            elif kind == "T":
                f = s.split("~")
                if len(f) > 12 and f[12] and f[13] != "0":
                    x, y = _sym_xy(f[2], f[3], ox, oy)
                    unit.graphics.append(S.SText(f[12], x, y, 1.27))
        except (IndexError, ValueError):
            continue    # skip malformed shapes, never fail the whole symbol
    return unit


def _sym_pin(s: str, ox: float, oy: float) -> S.Pin:
    segs = s.split("^^")
    f = segs[0].split("~")
    etype = PIN_TYPES.get(f[2], "unspecified")
    x, y = _sym_xy(f[4], f[5], ox, oy)
    rot = int(_f(f[6])) % 360
    path = segs[2].split("~")[0] if len(segs) > 2 else ""
    m = re.findall(r"[hv]\s*([-+]?\d+\.?\d*)", path)
    if m:
        length = abs(float(m[0])) * U
    else:
        pts = [float(v) for v in re.findall(r"[-+]?\d+\.?\d*", path)]
        length = math.hypot(pts[2] - pts[0], pts[3] - pts[1]) * U if len(pts) >= 4 else 2.54
    name_seg = segs[3].split("~") if len(segs) > 3 else []
    num_seg = segs[4].split("~") if len(segs) > 4 else []
    name = name_seg[4] if len(name_seg) > 4 else ""
    number = (num_seg[4] if len(num_seg) > 4 and num_seg[4] else f[3]).strip()
    dot = len(segs) > 5 and segs[5].split("~")[0] == "1"
    clock = len(segs) > 6 and segs[6].split("~")[0] == "1"
    shape = "inverted_clock" if dot and clock else "inverted" if dot else "clock" if clock else "line"
    pin = S.Pin(number=number, name=name, etype=etype, x=x, y=y, rot=(rot + 180) % 360,
                length=round(length, 4), shape=shape, hidden=(f[1] == "none"))
    pin._name_visible = (name_seg[0] != "0") if name_seg else True        # type: ignore[attr-defined]
    pin._num_visible = (num_seg[0] != "0") if num_seg else True           # type: ignore[attr-defined]
    return pin


def convert_symbol(cad: dict, name: str = "") -> S.Symbol:
    ds = cad["dataStr"]
    head = ds["head"]
    cp = head.get("c_para", {})
    name = _safe_name(name or cp.get("name") or cad.get("title", "part"))
    sym = S.Symbol(name=name, reference=(cp.get("pre") or "U?").rstrip("?").rstrip("_") or "U", value=name)
    subparts = cad.get("subparts") or []
    if subparts:
        for sp in subparts:
            sds = sp["dataStr"]
            u = _convert_sym_shapes(sds["shape"], _f(sds["head"]["x"]), _f(sds["head"]["y"]))
            sym.units.append(u)
    else:
        sym.units.append(_convert_sym_shapes(ds["shape"], _f(head["x"]), _f(head["y"])))
    pins = sym.pins
    if pins and not any(getattr(p, "_name_visible", True) for p in pins):
        sym.hide_pin_names = True
    if pins and not any(getattr(p, "_num_visible", True) for p in pins):
        sym.hide_pin_numbers = True
    if pins and all(p.name in ("", p.number) for p in pins):
        sym.hide_pin_names = True
    _center_symbol(sym)
    return sym


def _center_symbol(sym: S.Symbol) -> None:
    """Shift so the body is near the origin and pin ends land on the 1.27 mm grid."""
    pins = sym.pins
    if not pins:
        return
    ref = pins[0]
    x1, y1, x2, y2 = sym.bbox()
    dx = _snap(-(x1 + x2) / 2) + (_snap(ref.x) - ref.x)
    dy = _snap(-(y1 + y2) / 2) + (_snap(ref.y) - ref.y)
    for u in sym.units:
        for p in u.pins:
            p.x, p.y = round(p.x + dx, 4), round(p.y + dy, 4)
        for g in u.graphics:
            _shift_graphic(g, dx, dy)


def _shift_graphic(g, dx, dy):
    if isinstance(g, S.SRect):
        g.x1, g.y1, g.x2, g.y2 = g.x1 + dx, g.y1 + dy, g.x2 + dx, g.y2 + dy
    elif isinstance(g, S.SPoly):
        g.pts = [(x + dx, y + dy) for x, y in g.pts]
    elif isinstance(g, (S.SCircle,)):
        g.cx, g.cy = g.cx + dx, g.cy + dy
    elif isinstance(g, S.SArc):
        g.start, g.mid, g.end = [(p[0] + dx, p[1] + dy) for p in (g.start, g.mid, g.end)]
    elif isinstance(g, S.SText):
        g.x, g.y = g.x + dx, g.y + dy


# ----------------------------------------------------------------- footprint

@dataclass
class Model3D:
    uuid: str
    title: str
    offset: Tuple[float, float, float]
    rotate: Tuple[float, float, float]


def _fp_xy(x, y, ox, oy) -> Tuple[float, float]:
    return round((_f(x) - ox) * U, 4), round((_f(y) - oy) * U, 4)


def convert_footprint(cad: dict, name: str = "") -> Tuple[F.Footprint, Optional[Model3D]]:
    pk = cad["packageDetail"]
    ds = pk["dataStr"]
    head = ds["head"]
    ox, oy = _f(head["x"]), _f(head["y"])
    name = _safe_name(name or pk.get("title") or head.get("c_para", {}).get("package", "footprint"))
    fp = F.Footprint(name=name, descr=f"{pk.get('title', '')} (converted from EasyEDA)", tags="")
    model: Optional[Model3D] = None
    tht = False
    for s in ds["shape"]:
        kind = s.split("~", 1)[0]
        try:
            if kind == "PAD":
                pad = _fp_pad(s, ox, oy)
                if pad.kind == "thru_hole":
                    tht = True
                fp.pads.append(pad)
            elif kind == "TRACK":
                f = s.split("~")
                layer = FP_LAYERS.get(f[2])
                if not layer:
                    continue
                w = round(_f(f[1]) * U, 4)
                v = [float(t) for t in f[4].split()]
                pts = [_fp_xy(v[k], v[k + 1], ox, oy) for k in range(0, len(v) - 1, 2)]
                for a, b in zip(pts, pts[1:]):
                    fp.graphics.append(F.Line(a[0], a[1], b[0], b[1], layer, w))
            elif kind == "CIRCLE":
                f = s.split("~")
                layer = FP_LAYERS.get(f[5])
                if not layer:
                    continue
                cx, cy = _fp_xy(f[1], f[2], ox, oy)
                fp.graphics.append(F.Circle(cx, cy, round(_f(f[3]) * U, 4), layer, round(max(_f(f[4]) * U, 0.05), 4)))
            elif kind == "ARC":
                f = s.split("~")
                layer = FP_LAYERS.get(f[2])
                tp = arc_three_points(f[4]) if layer else None
                if tp:
                    a, m, b = (_fp_xy(p[0], p[1], ox, oy) for p in tp)
                    fp.graphics.append(F.Arc(a, m, b, layer, round(_f(f[1]) * U, 4)))
            elif kind == "RECT":
                f = s.split("~")
                layer = FP_LAYERS.get(f[5])
                if not layer:
                    continue
                x, y = _fp_xy(f[1], f[2], ox, oy)
                w = round(_f(f[8]) * U, 4) if len(f) > 8 and _f(f[8]) else 0.1
                fp.graphics.append(F.Rect(x, y, round(x + _f(f[3]) * U, 4), round(y + _f(f[4]) * U, 4), layer, w))
            elif kind == "HOLE":
                f = s.split("~")
                x, y = _fp_xy(f[1], f[2], ox, oy)
                d = round(_f(f[3]) * 2 * U, 4)
                fp.pads.append(F.Pad("", "np_thru_hole", "circle", x, y, d, d, drill=d, layers=list(F.NPTH_LAYERS)))
            elif kind == "SOLIDREGION":
                f = s.split("~")
                layer = FP_LAYERS.get(f[1])
                if not layer or f[1] in ("99", "100"):
                    continue
                for poly in path_to_polylines(f[3]):
                    pts = [_fp_xy(x, y, ox, oy) for x, y in poly]
                    if len(pts) >= 3:
                        fp.graphics.append(F.Poly(pts, layer, 0.0 if layer.endswith("Cu") else 0.1,
                                                  fill=f[4] == "solid"))
            elif kind == "SVGNODE":
                node = json.loads(s.split("~", 1)[1])
                attrs = node.get("attrs", {})
                outline = []
                for ch in node.get("childNodes") or []:
                    v = [float(t) for t in (ch.get("attrs", {}).get("points") or "").split()]
                    pts = [_fp_xy(v[k], v[k + 1], ox, oy) for k in range(0, len(v) - 1, 2)]
                    if len(pts) >= 2:
                        outline.append(pts)
                if outline:
                    fp._model_outline = outline          # type: ignore[attr-defined]
                if attrs.get("uuid"):
                    cx, cy = [_f(v) for v in attrs.get("c_origin", "0,0").split(",")]
                    rx, ry, rz = ([_f(v) for v in attrs.get("c_rotation", "0,0,0").split(",")] + [0, 0, 0])[:3]
                    z = _f(attrs.get("z"))
                    model = Model3D(attrs["uuid"], attrs.get("title", name),
                                    (round((cx - ox) * U, 3), round(-(cy - oy) * U, 3), round(-z * U, 3)),
                                    ((360 - rx) % 360, (360 - ry) % 360, (360 - rz) % 360))
        except (IndexError, ValueError, json.JSONDecodeError):
            continue
    fp.attr = "through_hole" if tht else "smd"
    if model and tht:
        model.offset = (model.offset[0], model.offset[1], 0.0)
    if model and fp.pads:
        # Some older EasyEDA entries carry a model origin in a different frame.
        x1, y1, x2, y2 = fp.pad_bbox()
        size = max(x2 - x1, y2 - y1, 1.0)
        if math.hypot(model.offset[0], model.offset[1]) > 2 * size + 5:
            model.offset = (round((x1 + x2) / 2, 3), round(-(y1 + y2) / 2, 3), model.offset[2])
            model.title += " [origin reset: source offset was outside the footprint]"
    outline = getattr(fp, "_model_outline", None)
    if outline and fp.pads:
        # The 3D outline projection is drawn on F.Fab, unless it sits in a stale coordinate frame.
        xs = [x for poly in outline for x, _ in poly]
        ys = [y for poly in outline for _, y in poly]
        x1, y1, x2, y2 = fp.pad_bbox()
        size = max(x2 - x1, y2 - y1, 1.0)
        if math.hypot((min(xs) + max(xs) - x1 - x2) / 2, (min(ys) + max(ys) - y1 - y2) / 2) < size:
            for poly in outline:
                fp.graphics.append(F.Poly(_simplify(poly), "F.Fab", 0.1))
    _add_fab_outline(fp)
    fp.add_courtyard(0.25)
    return fp, model


def _simplify(pts, tol: float = 0.01):
    """Drop collinear/duplicate points from dense EasyEDA outline polylines."""
    out = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - out[-1][0]) < tol and abs(p[1] - out[-1][1]) < tol:
            continue
        if len(out) >= 2:
            (ax, ay), (bx, by) = out[-2], out[-1]
            if abs((bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)) < tol * 0.05:
                out[-1] = p
                continue
        out.append(p)
    return out


def _fp_pad(s: str, ox: float, oy: float) -> F.Pad:
    f = s.split("~")
    shape, layer = f[1], f[6]
    x, y = _fp_xy(f[2], f[3], ox, oy)
    w, h = round(_f(f[4]) * U, 4), round(_f(f[5]) * U, 4)
    number = f[8]
    hole_r = _f(f[9])
    rot = _f(f[11]) % 360 if len(f) > 11 else 0.0
    hole_len = _f(f[13]) if len(f) > 13 else 0.0
    kind = "thru_hole" if layer == "11" else "smd"
    layers = list(F.THT_LAYERS) if kind == "thru_hole" else (
        ["B.Cu", "B.Paste", "B.Mask"] if layer == "2" else list(F.SMD_LAYERS))
    drill = None
    if kind == "thru_hole" and hole_r > 0:
        d = round(hole_r * 2 * U, 4)
        if hole_len > 0:
            ln = round(max(hole_len * U, d), 4)
            drill = (ln, d) if w >= h else (d, ln)
        else:
            drill = d
    kshape = {"ELLIPSE": "circle", "OVAL": "oval", "RECT": "rect", "POLYGON": "custom"}.get(shape, "rect")
    if kshape == "circle" and abs(w - h) > 1e-4:
        kshape = "oval"
    pad = F.Pad(number=number, kind=kind, shape=kshape, x=x, y=y, w=w, h=h, drill=drill, layers=layers)
    if kshape == "custom":
        v = [float(t) for t in f[10].split()]
        pts = [_fp_xy(v[k], v[k + 1], ox, oy) for k in range(0, len(v) - 1, 2)]
        pad.polygon = [(round(px - x, 4), round(py - y, 4)) for px, py in pts]
        a = round(max(0.05, min(w, h) * 0.5), 4) if w and h else 0.1
        pad.w = pad.h = a
        pad.shape = "custom"
        return pad
    # EasyEDA rotates clockwise in a Y-down frame; KiCad angles are counter-clockwise.
    r = (360 - rot) % 360
    if abs(r - 90) < 1e-6 or abs(r - 270) < 1e-6:
        pad.w, pad.h = pad.h, pad.w
        if isinstance(pad.drill, tuple):
            pad.drill = (pad.drill[1], pad.drill[0])
        r = 0.0
    elif abs(r - 180) < 1e-6:
        r = 0.0
    pad.rot = round(r, 3)
    return pad


def _add_fab_outline(fp: F.Footprint) -> None:
    """Add a F.Fab body outline if the source had none or only tiny marks (from silkscreen extents)."""
    fab = fp.layer_bbox(("F.Fab",))
    px1, py1, px2, py2 = fp.pad_bbox()
    pad_area = max((px2 - px1) * (py2 - py1), 1e-6)
    if fab and (fab[2] - fab[0]) * (fab[3] - fab[1]) > 0.25 * pad_area:
        return
    bb = fp.layer_bbox(("F.SilkS",)) or fp.pad_bbox()
    x1, y1, x2, y2 = bb
    fp.graphics.append(F.Rect(round(x1, 3), round(y1, 3), round(x2, 3), round(y2, 3), "F.Fab", 0.1))


def package_hint(cad: dict) -> str:
    """Package text used by the footprint matcher (e.g. 'SOIC-14_L8.7-W3.9-P1.27-LS6.0-BL')."""
    return (cad.get("packageDetail", {}).get("title")
            or cad.get("dataStr", {}).get("head", {}).get("c_para", {}).get("package", ""))


def cad_fields(cad: dict) -> dict:
    cp = cad.get("dataStr", {}).get("head", {}).get("c_para", {})
    return {"mpn": cp.get("Manufacturer Part", "") or cp.get("name", ""),
            "manufacturer": re.sub(r"\(.*?\)", "", cp.get("Manufacturer", "")).strip(),
            "lcsc": cp.get("Supplier Part", "") or (cad.get("lcsc") or {}).get("number", ""),
            "jlc_class": cp.get("JLCPCB Part Class", "")}
