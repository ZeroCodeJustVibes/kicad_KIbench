"""IPC-7351B land pattern calculations (density levels A/B/C).

All inputs in mm with (min, max) tolerances. Results are rounded the IPC way:
outer extents (Z, X) up and inner extents (G) down to a 0.01 mm grid.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Tuple

FAB_TOL = 0.05        # F: PCB fabrication tolerance
PLACE_TOL = 0.025     # P: placement tolerance
GRID = 0.01

# (toe, heel, side) fillet goals and courtyard excess per density level.
J = {
    "gullwing":        {"A": (0.55, 0.45, 0.05), "B": (0.35, 0.35, 0.03), "C": (0.15, 0.25, 0.01)},
    "gullwing_fine":   {"A": (0.55, 0.45, 0.01), "B": (0.35, 0.35, -0.02), "C": (0.15, 0.25, -0.04)},
    "flat_no_lead":    {"A": (0.40, 0.00, -0.04), "B": (0.30, 0.00, -0.04), "C": (0.20, 0.00, -0.04)},
    "chip":            {"A": (0.55, 0.00, 0.05), "B": (0.35, 0.00, 0.00), "C": (0.15, 0.00, -0.05)},
    "chip_small":      {"A": (0.30, 0.00, 0.05), "B": (0.20, 0.00, 0.00), "C": (0.10, 0.00, -0.05)},
    "j_lead":          {"A": (0.10, 0.55, 0.05), "B": (0.00, 0.35, 0.03), "C": (-0.10, 0.15, 0.01)},
}
COURTYARD = {"A": 0.5, "B": 0.25, "C": 0.1}
BGA_LAND_FACTOR = {"A": 0.85, "B": 0.80, "C": 0.75}     # NSMD land = nominal ball x factor


def _ceil(v: float) -> float:
    return round(math.ceil(round(v / GRID, 6)) * GRID, 4)


def _floor(v: float) -> float:
    return round(math.floor(round(v / GRID, 6)) * GRID, 4)


@dataclass
class Land:
    """A pair of opposing lands: pad size and center distance."""
    pad_length: float     # along the lead (toe-heel direction)
    pad_width: float      # across the lead
    center: float         # distance of each pad center from the package center
    z: float
    g: float
    x: float

    @property
    def span(self) -> float:
        return 2 * self.center


def lands(lead_span: Tuple[float, float], term_len: Tuple[float, float], term_width: Tuple[float, float],
          kind: str = "gullwing", density: str = "B", pitch: float = 1.27) -> Land:
    """lead_span: toe-to-toe (L min,max); term_len: foot length (T); term_width: lead width (W)."""
    if kind == "gullwing" and pitch <= 0.625:
        kind = "gullwing_fine"
    toe, heel, side = J[kind][density]
    Lmin, Lmax = lead_span
    Tmin, Tmax = term_len
    Wmin, Wmax = term_width
    Ltol, Ttol, Wtol = Lmax - Lmin, Tmax - Tmin, Wmax - Wmin
    Smin, Smax = Lmin - 2 * Tmax, Lmax - 2 * Tmin
    Stol = Smax - Smin
    Stol_rms = math.sqrt(Ltol ** 2 + 2 * Ttol ** 2)
    Smax_rms = Smax - (Stol - Stol_rms) / 2
    Z = _ceil(Lmin + 2 * toe + math.sqrt(Ltol ** 2 + FAB_TOL ** 2 + PLACE_TOL ** 2))
    G = _floor(Smax_rms - 2 * heel - math.sqrt(Stol_rms ** 2 + FAB_TOL ** 2 + PLACE_TOL ** 2))
    X = _ceil(Wmin + 2 * side + math.sqrt(Wtol ** 2 + FAB_TOL ** 2 + PLACE_TOL ** 2))
    G = max(G, 0.0)
    plen = round((Z - G) / 2, 4)
    center = round((Z + G) / 4, 4)
    return Land(plen, X, center, Z, G, X)


def limit_width_for_pitch(width: float, pitch: float, min_gap: float = 0.2) -> float:
    """Keep at least `min_gap` copper-to-copper between neighbouring pads."""
    return round(min(width, pitch - min_gap), 4)


def bga_land(ball: float, density: str = "B") -> float:
    return _ceil(ball * BGA_LAND_FACTOR[density])
