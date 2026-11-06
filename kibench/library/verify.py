"""Checks shown in the verification view before anything is written."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..kicad import footprint as F


@dataclass
class Check:
    ok: bool
    label: str
    detail: str = ""
    level: str = "error"          # error | warn | info


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)
    pitch: Optional[float] = None
    body: Optional[Tuple[float, float]] = None
    pad_span: Optional[Tuple[float, float]] = None

    @property
    def ok(self) -> bool:
        return all(c.ok or c.level != "error" for c in self.checks)


def estimate_pitch(fp: F.Footprint) -> Optional[float]:
    """Smallest center distance between same-size pads in a row or column."""
    pads = [p for p in fp.pads if p.kind != "np_thru_hole" and p.number]
    best = None
    for i, a in enumerate(pads):
        for b in pads[i + 1:]:
            if abs(a.w - b.w) > 1e-3 or abs(a.h - b.h) > 1e-3:
                continue
            dx, dy = abs(a.x - b.x), abs(a.y - b.y)
            if dx < 1e-3 and dy > 1e-3:
                d = dy
            elif dy < 1e-3 and dx > 1e-3:
                d = dx
            else:
                continue
            if best is None or d < best - 1e-4:
                best = d
    return round(best, 4) if best else None


def body_size(fp: F.Footprint) -> Optional[Tuple[float, float]]:
    bb = fp.layer_bbox(("F.Fab",))
    if not bb:
        return None
    return round(bb[2] - bb[0], 3), round(bb[3] - bb[1], 3)


def check_pins_pads(pins: List[Tuple[str, str, str]], fp: Optional[F.Footprint]) -> List[Check]:
    out: List[Check] = []
    numbers = [p[0] for p in pins]
    dup = [n for n, c in Counter(numbers).items() if c > 1]
    stacked_ok = all(any(p[0] == n and p[2] in ("power_in", "passive") for p in pins) for n in dup)
    out.append(Check(not dup or stacked_ok, "No duplicate pin numbers",
                     ", ".join(dup[:10]) if dup else "", "warn" if dup and stacked_ok else "error"))
    if fp is None:
        out.append(Check(False, "Footprint assigned", "no footprint", "warn"))
        return out
    pad_numbers = set(fp.numbered_pads())
    pin_set = set(numbers)
    out.append(Check(len(pin_set) == len(pad_numbers), "Pin count equals pad count",
                     f"{len(pin_set)} pins vs {len(pad_numbers)} numbered pads"))
    missing = sorted(pin_set - pad_numbers, key=_natkey)
    out.append(Check(not missing, "Every pin number exists on the footprint",
                     ("missing pads: " + ", ".join(missing[:12])) if missing else ""))
    extra = sorted(pad_numbers - pin_set, key=_natkey)
    out.append(Check(not extra, "Every pad has a pin", ("unconnected pads: " + ", ".join(extra[:12])) if extra else "",
                     "warn"))
    return out


def _natkey(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def step_extents(data: bytes) -> Optional[Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float]]]:
    """Rough model bounds from STEP CARTESIAN_POINTs (2nd-98th percentile)."""
    try:
        txt = data.decode("latin1")
    except Exception:
        return None
    pts = []
    for m in re.finditer(r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)", txt):
        v = m.group(1).split(",")
        if len(v) == 3:
            try:
                pts.append(tuple(float(x) for x in v))
            except ValueError:
                pass
    if len(pts) < 8:
        return None
    out = []
    for i in range(3):
        vals = sorted(p[i] for p in pts)
        lo, hi = vals[int(len(vals) * 0.02)], vals[int(len(vals) * 0.98) - 1]
        out.append((lo, hi))
    return tuple(out)  # type: ignore[return-value]


def check_model(fp: Optional[F.Footprint], data: Optional[bytes], offset, rotate) -> List[Check]:
    if not data:
        return [Check(False, "3D model", "none (optional)", "info")]
    ext = step_extents(data) if data[:20].lstrip().upper().startswith(b"ISO-10303") else None
    if not ext or fp is None:
        return [Check(True, "3D model", "present", "info")]
    (x0, x1), (y0, y1), (z0, z1) = ext
    rz = math.radians(rotate[2])
    corners = [(x, y) for x in (x0, x1) for y in (y0, y1)]
    # KiCad applies rotation then offset; 3D Y is up, footprint Y is down.
    rc = [(x * math.cos(-rz) - y * math.sin(-rz), x * math.sin(-rz) + y * math.cos(-rz)) for x, y in corners]
    mx0 = min(c[0] for c in rc) + offset[0]
    mx1 = max(c[0] for c in rc) + offset[0]
    my0 = min(-c[1] for c in rc) - offset[1]
    my1 = max(-c[1] for c in rc) - offset[1]
    px0, py0, px1, py1 = fp.layer_bbox(("F.Fab",)) or fp.pad_bbox()
    ix = max(0.0, min(mx1, px1) - max(mx0, px0))
    iy = max(0.0, min(my1, py1) - max(my0, py0))
    union = (mx1 - mx0) * (my1 - my0) + (px1 - px0) * (py1 - py0) - ix * iy
    iou = ix * iy / union if union > 0 else 0
    zb = z0 + offset[2]
    out = [Check(iou > 0.35, "3D model aligned with footprint", f"overlap {iou:.0%} (model vs body outline)", "warn")]
    if fp.attr == "smd":
        out.append(Check(abs(zb) < 0.3, "3D model sits on the board", f"model bottom at z = {zb:.2f} mm", "warn"))
    return out


def verify(prep) -> Report:
    fp = prep.footprint_model()
    rep = Report()
    rep.checks += check_pins_pads(prep.pins(), fp)
    if fp is not None:
        rep.pitch = estimate_pitch(fp)
        rep.body = body_size(fp)
        x0, y0, x1, y1 = fp.pad_bbox()
        rep.pad_span = (round(x1 - x0, 3), round(y1 - y0, 3))
        crt = fp.layer_bbox(("F.CrtYd",))
        rep.checks.append(Check(bool(crt), "Courtyard present", "", "warn"))
    rep.checks += check_model(fp, prep.model_bytes, prep.model_offset, prep.model_rotate)
    for w in prep.warnings:
        rep.checks.append(Check(False, w, "", "warn"))
    return rep
