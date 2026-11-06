"""Engineering value parsing: '10k', '4.7uF', '10 mΩ', '3V3', '6V~36V'."""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0,
          "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}

UNIT_ALIASES = {
    "ohm": "Ω", "ohms": "Ω", "Ω": "Ω", "r": "Ω", "R": "Ω",
    "f": "F", "F": "F", "h": "H", "H": "H", "v": "V", "V": "V", "a": "A", "A": "A",
    "w": "W", "W": "W", "hz": "Hz", "Hz": "Hz", "s": "s", "°c": "°C", "℃": "°C", "°C": "°C",
}

_NUM = r"[-+]?\d+(?:[.,]\d+)?"
VALUE_RE = re.compile(
    rf"^\s*(?P<num>{_NUM})\s*(?P<pre>[pnuµμmkKMG]?)\s*(?P<unit>ohms?|Ω|[RrFfHhVvAaWw]|[Hh]z|°C|℃|s)?\s*$")
# KiCad/European style: 4k7, 3V3, 2R2, 1u0
INFIX_RE = re.compile(r"^\s*(?P<a>\d+)(?P<pre>[pnuµμmkKMGRVF])(?P<b>\d+)\s*(?P<unit>Ω|ohms?|F|V|H)?\s*$")


def parse_value(text: str, unit_hint: str = "") -> Optional[Tuple[float, str]]:
    """Return (value in base units, unit symbol) or None."""
    if text is None:
        return None
    t = text.strip().replace("Ohm", "ohm")
    m = INFIX_RE.match(t)
    if m:
        pre = m.group("pre")
        unit = m.group("unit") or ""
        if pre in "RVF":
            unit = {"R": "Ω", "V": "V", "F": "F"}[pre]
            pre = ""
        val = float(f"{m.group('a')}.{m.group('b')}") * PREFIX.get(pre, 1.0)
        if not unit and not unit_hint:
            # 4k7 / 1M5 are resistances, 2u2 / 4n7 / 1p5 capacitances by convention
            unit = "Ω" if pre in ("k", "K", "M", "R", "Ω") else "F" if pre in ("u", "µ", "μ", "n", "p") else ""
        return val, UNIT_ALIASES.get(unit, unit) or unit_hint
    m = VALUE_RE.match(t)
    if not m:
        return None
    pre, unit = m.group("pre"), m.group("unit") or ""
    # "5m" alone is milli, "5M" mega; "2m" with unit hint Ω etc.
    if pre == "m" and unit == "" and unit_hint == "":
        pass
    num = float(m.group("num").replace(",", "."))
    return num * PREFIX.get(pre, 1.0), UNIT_ALIASES.get(unit, unit) or unit_hint


def find_values(text: str) -> List[Tuple[float, str]]:
    """All values with units found in a free text (e.g. JLC 'describe' strings)."""
    out = []
    for m in re.finditer(rf"({_NUM})\s*([pnuµμmkKMG]?)(Ω|ohm|F|H|V|A|W|Hz|°C|℃)(?![a-zA-Z])", text):
        num = float(m.group(1).replace(",", "."))
        out.append((num * PREFIX.get(m.group(2), 1.0), UNIT_ALIASES.get(m.group(3), m.group(3))))
    return out


def parse_range(text: str) -> Optional[Tuple[float, float, str]]:
    """'6V~36V' / '-40℃~+85℃' / '1.8V to 5.5V' -> (lo, hi, unit)."""
    parts = re.split(r"\s*(?:~|to|…|\.\.\.)\s*", text.strip())
    if len(parts) != 2:
        return None
    a, b = parse_value(parts[0]), parse_value(parts[1])
    if not a or not b:
        return None
    unit = a[1] or b[1]
    return min(a[0], b[0]), max(a[0], b[0]), unit


def format_value(v: float, unit: str = "") -> str:
    if v == 0:
        return f"0{unit}"
    for p, f in (("G", 1e9), ("M", 1e6), ("k", 1e3), ("", 1), ("m", 1e-3), ("µ", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        if abs(v) >= f:
            s = f"{v / f:.3g}"
            return f"{s}{p}{unit}"
    return f"{v:g}{unit}"
