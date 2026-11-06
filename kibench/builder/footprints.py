"""Parametric footprint templates. Fixed rules only: IPC-7351B lands + KiCad library (KLC) layer conventions.

The design called for KicadModTree; this is a self-contained generator with the same
conventions (F.Fab body with pin-1 chamfer, 0.12 mm silk kept 0.2 mm off copper,
0.05 mm courtyard on a 0.01 mm grid), so no extra dependency is needed inside KiCad's venv.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..kicad import footprint as F
from . import ipc7351 as ipc

SILK_W = 0.12
SILK_PAD_CLEAR = 0.2
FAB_W = 0.1
BGA_SKIP_LETTERS = set("IOQSXZ")


@dataclass
class Param:
    key: str
    label: str
    default: object
    unit: str = "mm"
    kind: str = "float"            # float | int | str | bool | choice
    choices: Sequence[str] = ()
    help: str = ""
    group: str = "Dimensions"


@dataclass
class Template:
    key: str
    label: str
    params: List[Param]
    build: Callable[[Dict, str], F.Footprint]
    drawing: str = ""              # what the letters mean (shown next to the form)

    def defaults(self) -> Dict:
        return {p.key: p.default for p in self.params}


def _fmt(v: float) -> str:
    return f"{v:g}"


def _mm(v) -> float:
    return float(v)


# ------------------------------------------------------------------ drawing helpers

def _keepouts(fp: F.Footprint, clear: float) -> List[Tuple[float, float, float, float]]:
    out = []
    for p in fp.pads:
        x1, y1, x2, y2 = p.bbox()
        out.append((x1 - clear, y1 - clear, x2 + clear, y2 + clear))
    return out


def _clip_segment(a, b, keepouts) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """Remove the parts of an axis-aligned segment that run through keepout boxes."""
    (x1, y1), (x2, y2) = a, b
    horiz = abs(y1 - y2) < 1e-9
    lo, hi = (min(x1, x2), max(x1, x2)) if horiz else (min(y1, y2), max(y1, y2))
    fixed = y1 if horiz else x1
    cuts = []
    for kx1, ky1, kx2, ky2 in keepouts:
        if horiz and ky1 < fixed < ky2:
            cuts.append((kx1, kx2))
        elif not horiz and kx1 < fixed < kx2:
            cuts.append((ky1, ky2))
    segs = [(lo, hi)]
    for c1, c2 in cuts:
        nxt = []
        for s1, s2 in segs:
            if c2 <= s1 or c1 >= s2:
                nxt.append((s1, s2))
                continue
            if c1 > s1:
                nxt.append((s1, c1))
            if c2 < s2:
                nxt.append((c2, s2))
        segs = nxt
    out = []
    for s1, s2 in segs:
        if s2 - s1 >= 0.25:
            out.append(((s1, fixed), (s2, fixed)) if horiz else ((fixed, s1), (fixed, s2)))
    return out


def silk_rect(fp: F.Footprint, x1, y1, x2, y2, pin1: Optional[Tuple[float, float]] = None) -> None:
    """Body outline on F.SilkS offset outward, clipped away from copper."""
    off = SILK_W / 2 + 0.01
    x1, y1, x2, y2 = x1 - off, y1 - off, x2 + off, y2 + off
    ko = _keepouts(fp, SILK_PAD_CLEAR + SILK_W / 2)
    for a, b in (((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)), ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))):
        for (sa, sb) in _clip_segment(a, b, ko):
            fp.graphics.append(F.Line(round(sa[0], 4), round(sa[1], 4), round(sb[0], 4), round(sb[1], 4),
                                      "F.SilkS", SILK_W))
    if pin1:
        px, py = pin1
        s = 0.3
        fp.graphics.append(F.Poly([(round(px, 4), round(py, 4)), (round(px - s, 4), round(py - s * 0.8, 4)),
                                   (round(px - s, 4), round(py + s * 0.8, 4)), (round(px, 4), round(py, 4))],
                                  "F.SilkS", SILK_W, fill=True))


def fab_body(fp: F.Footprint, w: float, h: float, chamfer: Optional[float] = None) -> None:
    x1, y1, x2, y2 = -w / 2, -h / 2, w / 2, h / 2
    c = chamfer if chamfer is not None else min(1.0, 0.25 * min(w, h))
    pts = [(x1 + c, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1 + c), (x1 + c, y1)]
    fp.graphics.append(F.Poly([(round(x, 4), round(y, 4)) for x, y in pts], "F.Fab", FAB_W))


def finish(fp: F.Footprint, density: str) -> F.Footprint:
    fp.add_courtyard(ipc.COURTYARD[density])
    return fp


def _row(n: int, pitch: float) -> List[float]:
    return [round((i - (n - 1) / 2) * pitch, 4) for i in range(n)]


def _ep_pads(fp: F.Footprint, number: str, w: float, h: float, paste_cov: float = 0.6) -> None:
    """Exposed pad: copper+mask pad plus a grid of paste apertures (~60% coverage)."""
    fp.pads.append(F.Pad(number, "smd", "roundrect", 0, 0, w, h, layers=["F.Cu", "F.Mask"], rratio=0.04))
    n = max(1, min(4, int(round(max(w, h) / 1.6))))
    nx = n if w >= h else max(1, int(round(n * w / h)))
    ny = n if h > w else max(1, int(round(n * h / w)))
    s = math.sqrt(paste_cov)
    pw, ph = w * s / nx, h * s / ny
    for x in _row(nx, w / nx):
        for y in _row(ny, h / ny):
            fp.pads.append(F.Pad("", "smd", "roundrect", x, y, round(pw, 3), round(ph, 3), layers=["F.Paste"], rratio=0.25))


# ------------------------------------------------------------------ dual gull-wing (SOIC/TSSOP/MSOP/SSOP)

def build_dual_gullwing(v: Dict, density: str) -> F.Footprint:
    n = int(v["pins"])
    e = _mm(v["pitch"])
    land = ipc.lands((_mm(v["E_min"]), _mm(v["E_max"])), (_mm(v["L_min"]), _mm(v["L_max"])),
                     (_mm(v["b_min"]), _mm(v["b_max"])), "gullwing", density, e)
    pw = ipc.limit_width_for_pitch(land.pad_width, e)
    fam = str(v.get("family") or "SOIC")
    has_ep = _mm(v.get("D2", 0) or 0) > 0 and _mm(v.get("E2", 0) or 0) > 0
    name = v.get("name") or (f"{fam}-{n}{'-1EP' if has_ep else ''}_{_fmt(_mm(v['E1']))}x{_fmt(_mm(v['D']))}mm_P{_fmt(e)}mm"
                             + (f"_EP{_fmt(_mm(v['E2']))}x{_fmt(_mm(v['D2']))}mm" if has_ep else ""))
    fp = F.Footprint(name, descr=f"{fam}, {n} pin, generated by KiBench (IPC-7351B density {density})",
                     tags=f"{fam} SO")
    half = n // 2
    ys = _row(half, e)
    for i in range(half):
        fp.pads.append(F.Pad(str(i + 1), "smd", "roundrect", -land.center, ys[i], land.pad_length, pw))
    for i in range(half):
        fp.pads.append(F.Pad(str(half + i + 1), "smd", "roundrect", land.center, ys[half - 1 - i], land.pad_length, pw))
    if has_ep:
        _ep_pads(fp, str(n + 1), _mm(v["E2"]), _mm(v["D2"]))
    bw, bh = _mm(v["E1"]), _mm(v["D"])
    fab_body(fp, bw, bh)
    silk_rect(fp, -bw / 2, -bh / 2, bw / 2, bh / 2)
    # pin-1 marker line above the first pad (KLC style)
    p1 = fp.pads[0]
    fp.graphics.append(F.Line(round(p1.x - land.pad_length / 2, 4), round(-bh / 2 - SILK_W / 2 - 0.01, 4),
                              round(-bw / 2 - 0.07, 4), round(-bh / 2 - SILK_W / 2 - 0.01, 4), "F.SilkS", SILK_W))
    _reclip_silk(fp)
    return finish(fp, density)


def _reclip_silk(fp: F.Footprint) -> None:
    ko = _keepouts(fp, SILK_PAD_CLEAR + SILK_W / 2)
    keep = []
    for g in fp.graphics:
        if isinstance(g, F.Line) and g.layer == "F.SilkS" and (abs(g.x1 - g.x2) < 1e-9 or abs(g.y1 - g.y2) < 1e-9):
            for a, b in _clip_segment((g.x1, g.y1), (g.x2, g.y2), ko):
                keep.append(F.Line(round(a[0], 4), round(a[1], 4), round(b[0], 4), round(b[1], 4), "F.SilkS", SILK_W))
        else:
            keep.append(g)
    fp.graphics = keep


DUAL_PARAMS = [
    Param("family", "Family", "SOIC", "", "choice", ["SOIC", "TSSOP", "SSOP", "MSOP", "SOP", "HTSSOP"], group="Package"),
    Param("pins", "Pin count", 8, "", "int", group="Package"),
    Param("pitch", "e  pitch", 1.27),
    Param("D", "D  body length", 4.9),
    Param("E1", "E1 body width", 3.9),
    Param("E_min", "E  lead span min", 5.8), Param("E_max", "E  lead span max", 6.2),
    Param("b_min", "b  lead width min", 0.31), Param("b_max", "b  lead width max", 0.51),
    Param("L_min", "L  foot length min", 0.4), Param("L_max", "L  foot length max", 1.27),
    Param("D2", "D2 exposed pad length (0 = none)", 0.0, group="Exposed pad"),
    Param("E2", "E2 exposed pad width", 0.0, group="Exposed pad"),
    Param("name", "Footprint name (blank = auto)", "", "", "str", group="Package"),
]


# ------------------------------------------------------------------ SOT (unequal sides)

def _positions(m: int, n: int) -> List[int]:
    """Spread m pins over n slots symmetrically (SOT-23-5: right side uses slots 0 and 2)."""
    if m >= n:
        return list(range(n))
    if m == 1:
        return [n // 2]
    return [round(i * (n - 1) / (m - 1)) for i in range(m)]


def build_sot(v: Dict, density: str) -> F.Footprint:
    nl, nr = int(v["pins_left"]), int(v["pins_right"])
    e = _mm(v["pitch"])
    land = ipc.lands((_mm(v["E_min"]), _mm(v["E_max"])), (_mm(v["L_min"]), _mm(v["L_max"])),
                     (_mm(v["b_min"]), _mm(v["b_max"])), "gullwing", density, e)
    pw = ipc.limit_width_for_pitch(land.pad_width, e)
    slots = max(nl, nr)
    ys = _row(slots, e)
    name = v.get("name") or f"SOT-{nl + nr}_{_fmt(_mm(v['E1']))}x{_fmt(_mm(v['D']))}mm_P{_fmt(e)}mm"
    fp = F.Footprint(name, descr=f"SOT, {nl + nr} pin, generated by KiBench (IPC-7351B density {density})", tags="SOT")
    num = 1
    for s in _positions(nl, slots):
        fp.pads.append(F.Pad(str(num), "smd", "roundrect", -land.center, ys[s], land.pad_length, pw))
        num += 1
    for s in reversed(_positions(nr, slots)):
        fp.pads.append(F.Pad(str(num), "smd", "roundrect", land.center, ys[s], land.pad_length, pw))
        num += 1
    bw, bh = _mm(v["E1"]), _mm(v["D"])
    fab_body(fp, bw, bh, chamfer=min(0.4, 0.25 * min(bw, bh)))
    silk_rect(fp, -bw / 2, -bh / 2, bw / 2, bh / 2)
    return finish(fp, density)


SOT_PARAMS = [
    Param("pins_left", "Pins on left side", 3, "", "int", group="Package"),
    Param("pins_right", "Pins on right side", 2, "", "int", group="Package"),
    Param("pitch", "e  pitch", 0.95),
    Param("D", "D  body length", 2.9), Param("E1", "E1 body width", 1.6),
    Param("E_min", "E  lead span min", 2.6), Param("E_max", "E  lead span max", 3.0),
    Param("b_min", "b  lead width min", 0.3), Param("b_max", "b  lead width max", 0.5),
    Param("L_min", "L  foot length min", 0.3), Param("L_max", "L  foot length max", 0.6),
    Param("name", "Footprint name (blank = auto)", "SOT-23-5", "", "str", group="Package"),
]


# ------------------------------------------------------------------ QFP (quad gull-wing) and QFN/DFN

def _quad_pads(fp: F.Footprint, nx: int, ny: int, e: float, cx: float, cy: float,
               plx: float, ply: float, pwx: float, pwy: float) -> None:
    """Counter-clockwise from pin 1 at the top of the left side.
    nx = pins on top/bottom sides, ny = pins on left/right sides."""
    num = 1
    for y in _row(ny, e):                                    # left, top -> bottom
        fp.pads.append(F.Pad(str(num), "smd", "roundrect", -cx, y, plx, pwy)); num += 1
    for x in _row(nx, e):                                    # bottom, left -> right
        fp.pads.append(F.Pad(str(num), "smd", "roundrect", x, cy, pwx, ply)); num += 1
    for y in reversed(_row(ny, e)):                          # right, bottom -> top
        fp.pads.append(F.Pad(str(num), "smd", "roundrect", cx, y, plx, pwy)); num += 1
    for x in reversed(_row(nx, e)):                          # top, right -> left
        fp.pads.append(F.Pad(str(num), "smd", "roundrect", x, -cy, pwx, ply)); num += 1


def build_qfp(v: Dict, density: str) -> F.Footprint:
    nx, ny = int(v["pins_x"]), int(v["pins_y"])
    e = _mm(v["pitch"])
    lx = ipc.lands((_mm(v["Dl_min"]), _mm(v["Dl_max"])), (_mm(v["L_min"]), _mm(v["L_max"])),
                   (_mm(v["b_min"]), _mm(v["b_max"])), "gullwing", density, e)
    ly = ipc.lands((_mm(v["El_min"]), _mm(v["El_max"])), (_mm(v["L_min"]), _mm(v["L_max"])),
                   (_mm(v["b_min"]), _mm(v["b_max"])), "gullwing", density, e)
    pw = ipc.limit_width_for_pitch(lx.pad_width, e)
    n = 2 * (nx + ny)
    fam = v.get("family") or "LQFP"
    has_ep = _mm(v.get("D2", 0) or 0) > 0
    bw, bh = _mm(v["D1"]), _mm(v["E1"])
    name = v.get("name") or (f"{fam}-{n}{'-1EP' if has_ep else ''}_{_fmt(bw)}x{_fmt(bh)}mm_P{_fmt(e)}mm"
                             + (f"_EP{_fmt(_mm(v['D2']))}x{_fmt(_mm(v['E2']))}mm" if has_ep else ""))
    fp = F.Footprint(name, descr=f"{fam}, {n} pin, generated by KiBench (IPC-7351B density {density})", tags="QFP")
    # left/right pads come from the X lead span, top/bottom from Y
    _quad_pads(fp, nx, ny, e, lx.center, ly.center, lx.pad_length, ly.pad_length, pw, pw)
    if has_ep:
        _ep_pads(fp, str(n + 1), _mm(v["D2"]), _mm(v["E2"]))
    fab_body(fp, bw, bh)
    silk_rect(fp, -bw / 2, -bh / 2, bw / 2, bh / 2)
    return finish(fp, density)


QFP_PARAMS = [
    Param("family", "Family", "LQFP", "", "choice", ["LQFP", "TQFP", "QFP", "HTQFP"], group="Package"),
    Param("pins_x", "Pins per top/bottom side", 12, "", "int", group="Package"),
    Param("pins_y", "Pins per left/right side", 12, "", "int", group="Package"),
    Param("pitch", "e  pitch", 0.5),
    Param("D1", "D1 body X", 7.0), Param("E1", "E1 body Y", 7.0),
    Param("Dl_min", "D  lead span X min", 8.8), Param("Dl_max", "D  lead span X max", 9.2),
    Param("El_min", "E  lead span Y min", 8.8), Param("El_max", "E  lead span Y max", 9.2),
    Param("b_min", "b  lead width min", 0.17), Param("b_max", "b  lead width max", 0.27),
    Param("L_min", "L  foot length min", 0.45), Param("L_max", "L  foot length max", 0.75),
    Param("D2", "D2 exposed pad X (0 = none)", 0.0, group="Exposed pad"),
    Param("E2", "E2 exposed pad Y", 0.0, group="Exposed pad"),
    Param("name", "Footprint name (blank = auto)", "", "", "str", group="Package"),
]


def build_qfn(v: Dict, density: str) -> F.Footprint:
    nx, ny = int(v["pins_x"]), int(v["pins_y"])
    e = _mm(v["pitch"])
    Dmin, Dmax, Emin, Emax = _mm(v["D_min"]), _mm(v["D_max"]), _mm(v["E_min"]), _mm(v["E_max"])
    lx = ipc.lands((Dmin, Dmax), (_mm(v["L_min"]), _mm(v["L_max"])), (_mm(v["b_min"]), _mm(v["b_max"])),
                   "flat_no_lead", density, e)
    ly = ipc.lands((Emin, Emax), (_mm(v["L_min"]), _mm(v["L_max"])), (_mm(v["b_min"]), _mm(v["b_max"])),
                   "flat_no_lead", density, e)
    pw = ipc.limit_width_for_pitch(lx.pad_width, e)
    n = 2 * (nx + ny)
    dfn = nx == 0
    fam = v.get("family") or ("DFN" if dfn else "QFN")
    D2, E2 = _mm(v.get("D2", 0) or 0), _mm(v.get("E2", 0) or 0)
    has_ep = D2 > 0 and E2 > 0
    bw, bh = (Dmin + Dmax) / 2, (Emin + Emax) / 2
    name = v.get("name") or (f"{fam}-{n}{'-1EP' if has_ep else ''}_{_fmt(round(bw, 2))}x{_fmt(round(bh, 2))}mm_P{_fmt(e)}mm"
                             + (f"_EP{_fmt(D2)}x{_fmt(E2)}mm" if has_ep else ""))
    fp = F.Footprint(name, descr=f"{fam}, {n} pin, generated by KiBench (IPC-7351B density {density})",
                     tags=f"{fam} NoLead")
    _quad_pads(fp, nx, ny, e, lx.center, ly.center, lx.pad_length, ly.pad_length, pw, pw)
    if has_ep:
        # keep 0.2 mm between EP and the inner end of the signal pads
        inner_x = lx.center - lx.pad_length / 2 - 0.2
        inner_y = (ly.center - ly.pad_length / 2 - 0.2) if not dfn else bh
        _ep_pads(fp, str(n + 1), round(min(D2, 2 * inner_x), 3), round(min(E2, 2 * inner_y), 3))
    fab_body(fp, bw, bh)
    # QFN silk: corner marks only (clipping does this automatically)
    silk_rect(fp, -bw / 2, -bh / 2, bw / 2, bh / 2)
    return finish(fp, density)


QFN_PARAMS = [
    Param("family", "Family", "QFN", "", "choice", ["QFN", "DFN", "VQFN", "WQFN", "SON"], group="Package"),
    Param("pins_x", "Pins per top/bottom side (0 = DFN)", 8, "", "int", group="Package"),
    Param("pins_y", "Pins per left/right side", 8, "", "int", group="Package"),
    Param("pitch", "e  pitch", 0.5),
    Param("D_min", "D  body X min", 4.9), Param("D_max", "D  body X max", 5.1),
    Param("E_min", "E  body Y min", 4.9), Param("E_max", "E  body Y max", 5.1),
    Param("b_min", "b  terminal width min", 0.18), Param("b_max", "b  terminal width max", 0.30),
    Param("L_min", "L  terminal length min", 0.30), Param("L_max", "L  terminal length max", 0.50),
    Param("D2", "D2 exposed pad X", 3.45, group="Exposed pad"),
    Param("E2", "E2 exposed pad Y", 3.45, group="Exposed pad"),
    Param("name", "Footprint name (blank = auto)", "", "", "str", group="Package"),
]


# ------------------------------------------------------------------ BGA

def bga_row_names(n: int) -> List[str]:
    letters = [c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if c not in BGA_SKIP_LETTERS]
    out = []
    i = 0
    while len(out) < n:
        if i < len(letters):
            out.append(letters[i])
        else:
            k = i - len(letters)
            out.append(letters[k // len(letters)] + letters[k % len(letters)])
        i += 1
    return out


def parse_depop(spec: str, rows: int, cols: int) -> set:
    """'A1,B2,C3-C5' or 'center 4x4' or 'perimeter 3' -> set of (row_idx, col_idx) removed."""
    spec = (spec or "").strip()
    names = bga_row_names(rows)
    out = set()
    if not spec:
        return out
    m = re.match(r"center\s+(\d+)\s*x\s*(\d+)", spec, re.I)
    if m:
        h, w = int(m.group(1)), int(m.group(2))
        r0, c0 = (rows - h) // 2, (cols - w) // 2
        return {(r, c) for r in range(r0, r0 + h) for c in range(c0, c0 + w)}
    m = re.match(r"perimeter\s+(\d+)", spec, re.I)
    if m:
        k = int(m.group(1))
        return {(r, c) for r in range(rows) for c in range(cols)
                if k <= r < rows - k and k <= c < cols - k}
    for tok in re.split(r"[,\s;]+", spec):
        if not tok:
            continue
        rng = tok.split("-")
        cells = []
        for t in rng:
            mm = re.match(r"([A-Z]+)(\d+)$", t.upper())
            if mm and mm.group(1) in names:
                cells.append((names.index(mm.group(1)), int(mm.group(2)) - 1))
        if len(cells) == 1:
            out.add(cells[0])
        elif len(cells) == 2:
            (r1, c1), (r2, c2) = cells
            for r in range(min(r1, r2), max(r1, r2) + 1):
                for c in range(min(c1, c2), max(c1, c2) + 1):
                    out.add((r, c))
    return out


def build_bga(v: Dict, density: str) -> F.Footprint:
    rows, cols = int(v["rows"]), int(v["cols"])
    px = _mm(v["pitch"])
    py = _mm(v.get("pitch_y") or px) or px
    ball = _mm(v["ball"])
    land = ipc.bga_land(ball, density)
    removed = parse_depop(str(v.get("depop", "")), rows, cols)
    names = bga_row_names(rows)
    bw, bh = _mm(v["D"]), _mm(v["E"])
    n = rows * cols - len(removed)
    pitch_txt = f"P{_fmt(px)}mm" + (f"P{_fmt(py)}mm" if py != px else "")
    name = v.get("name") or f"BGA-{n}_{_fmt(bw)}x{_fmt(bh)}mm_Layout{cols}x{rows}_{pitch_txt}"
    fp = F.Footprint(name, descr=f"BGA-{n}, {cols}x{rows} grid, {px} mm pitch, generated by KiBench "
                     f"(NSMD land {land} mm, density {density})", tags="BGA")
    for r, ry in enumerate(_row(rows, py)):
        for c, cx in enumerate(_row(cols, px)):
            if (r, c) in removed:
                continue
            fp.pads.append(F.Pad(f"{names[r]}{c + 1}", "smd", "circle", cx, ry, land, land,
                                 layers=list(F.SMD_LAYERS)))
    fab_body(fp, bw, bh, chamfer=min(1.0, 0.1 * min(bw, bh)))
    # silk: corner marks plus A1 marker
    off = SILK_W / 2 + 0.01
    x1, y1, x2, y2 = -bw / 2 - off, -bh / 2 - off, bw / 2 + off, bh / 2 + off
    L = min(1.0, 0.2 * min(bw, bh))
    for cxp, cyp, dx, dy in ((x1, y1, 1, 1), (x2, y1, -1, 1), (x2, y2, -1, -1), (x1, y2, 1, -1)):
        fp.graphics.append(F.Line(round(cxp, 4), round(cyp, 4), round(cxp + dx * L, 4), round(cyp, 4), "F.SilkS", SILK_W))
        fp.graphics.append(F.Line(round(cxp, 4), round(cyp, 4), round(cxp, 4), round(cyp + dy * L, 4), "F.SilkS", SILK_W))
    fp.graphics.append(F.Line(round(x1 - 0.3, 4), round(y1 + L, 4), round(x1 - 0.3, 4), round(y1 - 0.3, 4), "F.SilkS", SILK_W))
    fp.graphics.append(F.Line(round(x1 - 0.3, 4), round(y1 - 0.3, 4), round(x1 + L, 4), round(y1 - 0.3, 4), "F.SilkS", SILK_W))
    return finish(fp, density)


BGA_PARAMS = [
    Param("rows", "Rows (A, B, ...)", 16, "", "int", group="Grid"),
    Param("cols", "Columns (1, 2, ...)", 16, "", "int", group="Grid"),
    Param("pitch", "e  ball pitch", 1.0, group="Grid"),
    Param("pitch_y", "e  row pitch (blank = same)", 0.0, group="Grid"),
    Param("D", "D  body X", 17.0), Param("E", "E  body Y", 17.0),
    Param("ball", "b  ball diameter (nominal)", 0.5),
    Param("depop", "Depopulated balls", "", "", "str",
          help="List 'A1,B2,C3-C5', or 'center 4x4', or 'perimeter 3' (keep outer 3 rings)", group="Grid"),
    Param("name", "Footprint name (blank = auto)", "", "", "str", group="Grid"),
]


# ------------------------------------------------------------------ two-terminal chip

def build_chip(v: Dict, density: str) -> F.Footprint:
    Lmin, Lmax = _mm(v["L_min"]), _mm(v["L_max"])
    Wmin, Wmax = _mm(v["W_min"]), _mm(v["W_max"])
    Tmin, Tmax = _mm(v["T_min"]), _mm(v["T_max"])
    kind = "chip" if Lmax >= 1.5 else "chip_small"
    land = ipc.lands((Lmin, Lmax), (Tmin, Tmax), (Wmin, Wmax), kind, density)
    prefix = v.get("prefix") or "R"
    code = str(v.get("size_code") or "").strip()
    bl, bw = (Lmin + Lmax) / 2, (Wmin + Wmax) / 2
    if code:
        metric = f"{int(round(bl * 10)):02d}{int(round(bw * 10)):02d}"
        name = f"{prefix}_{code}_{metric}Metric"
    else:
        name = f"{prefix}_{_fmt(round(bl, 2))}x{_fmt(round(bw, 2))}mm"
    name = v.get("name") or name
    fp = F.Footprint(name, descr=f"Chip {prefix}, {bl:.2f}x{bw:.2f} mm, generated by KiBench (IPC-7351B density {density})",
                     tags=f"{prefix} chip")
    fp.pads.append(F.Pad("1", "smd", "roundrect", -land.center, 0, land.pad_length, land.pad_width))
    fp.pads.append(F.Pad("2", "smd", "roundrect", land.center, 0, land.pad_length, land.pad_width))
    fp.graphics.append(F.Rect(round(-bl / 2, 4), round(-bw / 2, 4), round(bl / 2, 4), round(bw / 2, 4), "F.Fab", FAB_W))
    # silk: two lines above and below the body between the pads (KLC style)
    gap = land.center - land.pad_length / 2 - SILK_PAD_CLEAR - SILK_W / 2
    y = max(bw / 2, land.pad_width / 2) + SILK_W / 2 + 0.01
    if gap > 0.15:
        for s in (-1, 1):
            fp.graphics.append(F.Line(round(-gap, 4), round(s * y, 4), round(gap, 4), round(s * y, 4), "F.SilkS", SILK_W))
    return finish(fp, density)


CHIP_PARAMS = [
    Param("prefix", "Type", "R", "", "choice", ["R", "C", "L", "D", "LED", "Fuse"], group="Package"),
    Param("size_code", "Imperial code (e.g. 0603, blank = none)", "0603", "", "str", group="Package"),
    Param("L_min", "L  body length min", 1.45), Param("L_max", "L  body length max", 1.75),
    Param("W_min", "W  body width min", 0.65), Param("W_max", "W  body width max", 0.95),
    Param("T_min", "T  terminal length min", 0.15), Param("T_max", "T  terminal length max", 0.45),
    Param("name", "Footprint name (blank = auto)", "", "", "str", group="Package"),
]


# ------------------------------------------------------------------ THT header

def build_header(v: Dict, density: str) -> F.Footprint:
    rows, cols = int(v["rows"]), int(v["cols"])
    p = _mm(v["pitch"])
    rp = _mm(v.get("row_pitch") or p) or p
    drill, pad = _mm(v["drill"]), _mm(v["pad"])
    kind = v.get("kind") or "PinHeader"
    name = v.get("name") or f"{kind}_{rows}x{cols:02d}_P{_fmt(p)}mm_Vertical"
    fp = F.Footprint(name, descr=f"Through hole {kind.lower()}, {rows}x{cols}, {p} mm pitch, generated by KiBench",
                     tags=f"Through hole {kind.lower()} THT {rows}x{cols:02d} {p}mm", attr="through_hole")
    num = 1
    for c in range(cols):
        for r in range(rows):
            shape = "rect" if num == 1 else "circle"
            fp.pads.append(F.Pad(str(num), "thru_hole", shape, round(r * rp, 4), round(c * p, 4), pad, pad,
                                 drill=drill, layers=list(F.THT_LAYERS)))
            num += 1
    x1, y1 = -p / 2, -p / 2
    x2, y2 = (rows - 1) * rp + p / 2, (cols - 1) * p + p / 2
    fp.graphics.append(F.Poly([(round(x1 + p / 4, 4), round(y1, 4)), (round(x2, 4), round(y1, 4)), (round(x2, 4), round(y2, 4)),
                               (round(x1, 4), round(y2, 4)), (round(x1, 4), round(y1 + p / 4, 4)),
                               (round(x1 + p / 4, 4), round(y1, 4))], "F.Fab", FAB_W))
    silk_rect(fp, x1, y1, x2, y2)
    fp.graphics.append(F.Line(round(x1 - 0.2, 4), round(y1 - 0.2, 4), round(x1 - 0.2, 4), round(y1 + p / 2, 4), "F.SilkS", SILK_W))
    return finish(fp, density)


HEADER_PARAMS = [
    Param("kind", "Kind", "PinHeader", "", "choice", ["PinHeader", "PinSocket"], group="Package"),
    Param("rows", "Rows", 1, "", "int", group="Package"),
    Param("cols", "Pins per row", 4, "", "int", group="Package"),
    Param("pitch", "Pitch", 2.54), Param("row_pitch", "Row pitch", 2.54),
    Param("drill", "Drill", 1.0), Param("pad", "Pad diameter", 1.7),
    Param("name", "Footprint name (blank = auto)", "", "", "str", group="Package"),
]


# ------------------------------------------------------------------ generic connector

def build_connector(v: Dict, density: str) -> F.Footprint:
    n = int(v["pins"])
    p = _mm(v["pitch"])
    tht = v.get("mount") == "THT"
    pw, ph = _mm(v["pad_w"]), _mm(v["pad_h"])
    name = v.get("name") or f"Connector_1x{n:02d}_P{_fmt(p)}mm_{'THT' if tht else 'SMD'}"
    fp = F.Footprint(name, descr=f"Generic connector, {n} pins, {p} mm pitch, generated by KiBench",
                     tags="connector", attr="through_hole" if tht else "smd")
    for i, x in enumerate(_row(n, p)):
        if tht:
            fp.pads.append(F.Pad(str(i + 1), "thru_hole", "rect" if i == 0 else "oval", x, 0, pw, ph,
                                 drill=_mm(v["drill"]), layers=list(F.THT_LAYERS)))
        else:
            fp.pads.append(F.Pad(str(i + 1), "smd", "roundrect", x, 0, pw, ph))
    mc = int(v.get("mount_pads") or 0)
    if mc:
        mw, mh = _mm(v["mp_w"]), _mm(v["mp_h"])
        mx, my = _mm(v["mp_x"]), _mm(v["mp_y"])
        for s in (-1, 1):
            fp.pads.append(F.Pad("MP", "smd", "roundrect", s * mx, my, mw, mh))
    bw, bh = _mm(v["body_w"]), _mm(v["body_h"])
    by = _mm(v.get("body_y") or 0)
    if bw > 0 and bh > 0:
        fp.graphics.append(F.Rect(round(-bw / 2, 4), round(by - bh / 2, 4), round(bw / 2, 4), round(by + bh / 2, 4), "F.Fab", FAB_W))
        silk_rect(fp, -bw / 2, by - bh / 2, bw / 2, by + bh / 2)
    p1 = fp.pads[0]
    fp.graphics.append(F.Line(round(p1.x - pw / 2, 4), round(-ph / 2 - 0.4, 4), round(p1.x + pw / 2, 4),
                              round(-ph / 2 - 0.4, 4), "F.SilkS", SILK_W))
    _reclip_silk(fp)
    return finish(fp, density)


CONNECTOR_PARAMS = [
    Param("mount", "Mounting", "SMD", "", "choice", ["SMD", "THT"], group="Package"),
    Param("pins", "Pins", 4, "", "int", group="Package"),
    Param("pitch", "Pitch", 2.0),
    Param("pad_w", "Pad width (X)", 1.0), Param("pad_h", "Pad height (Y)", 3.5),
    Param("drill", "Drill (THT)", 0.8),
    Param("mount_pads", "Mounting pads (0 or 2)", 2, "", "int", group="Mounting pads"),
    Param("mp_w", "Mounting pad width", 1.6, group="Mounting pads"),
    Param("mp_h", "Mounting pad height", 3.0, group="Mounting pads"),
    Param("mp_x", "Mounting pad X (from center)", 4.35, group="Mounting pads"),
    Param("mp_y", "Mounting pad Y", -4.9, group="Mounting pads"),
    Param("body_w", "Body width", 9.9, group="Body"), Param("body_h", "Body height", 6.0, group="Body"),
    Param("body_y", "Body center Y", -2.5, group="Body"),
    Param("name", "Footprint name (blank = auto)", "", "", "str", group="Package"),
]


TEMPLATES: Dict[str, Template] = {t.key: t for t in [
    Template("qfn", "QFN / DFN (flat no-lead)", QFN_PARAMS, build_qfn,
             "D×E body · e pitch · b terminal width · L terminal length · D2×E2 exposed pad"),
    Template("dual", "SOIC / TSSOP / MSOP (dual gull-wing)", DUAL_PARAMS, build_dual_gullwing,
             "D body length · E1 body width · E toe-to-toe span · e pitch · b lead width · L foot length"),
    Template("bga", "BGA", BGA_PARAMS, build_bga, "rows × cols grid · e pitch · b ball · D×E body"),
    Template("qfp", "QFP / LQFP / TQFP (quad gull-wing)", QFP_PARAMS, build_qfp,
             "D1×E1 body · D/E toe-to-toe span · e pitch · b lead width · L foot length"),
    Template("sot", "SOT (gull-wing, unequal sides)", SOT_PARAMS, build_sot,
             "D body length · E1 body width · E span · e pitch"),
    Template("chip", "Two-terminal chip (R/C/L/D)", CHIP_PARAMS, build_chip,
             "L body length · W body width · T terminal length"),
    Template("header", "Through-hole header", HEADER_PARAMS, build_header, "rows × pins · pitch · drill · pad"),
    Template("connector", "Generic connector", CONNECTOR_PARAMS, build_connector,
             "row of signal pads plus optional mounting pads"),
]}


def build(template: str, values: Dict, density: str = "B") -> F.Footprint:
    t = TEMPLATES[template]
    v = t.defaults()
    v.update({k: val for k, val in values.items() if val not in (None,)})
    return t.build(v, density)
