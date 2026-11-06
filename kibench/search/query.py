"""Query parsing (LCSC codes, MPNs, parametric phrases) and result filters."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..models import Part
from .units import find_values, parse_range, parse_value

LCSC_RE = re.compile(r"^C\d{3,9}$", re.I)
CHIP_SIZES = {"01005", "0201", "0402", "0603", "0805", "1206", "1210", "1812", "2010", "2512", "2220"}
PACKAGE_RE = re.compile(
    r"^(SOT-?\d+(-\d+)?|SOD-?\d+\w*|SOIC-?\d+|SOP-?\d+|TSSOP-?\d+|MSOP-?\d+|QFN-?\d+|DFN-?\d+|"
    r"LQFP-?\d+|TQFP-?\d+|QFP-?\d+|BGA-?\d+|DIP-?\d+|TO-?\d+\w*|SMA|SMB|SMC|DPAK|D2PAK|"
    r"PowerPAK\S*|WSON-?\d+|VSON-?\d+|SON-?\d+|LGA-?\d+|WLCSP-?\d+)$", re.I)
TOL_RE = re.compile(r"^±?(\d+(?:\.\d+)?)%$")


@dataclass
class ParsedQuery:
    raw: str
    kind: str = "keyword"          # lcsc | mpn | parametric | keyword
    lcsc: str = ""
    keyword: str = ""              # what to send to text-search providers
    category: str = ""             # resistor | capacitor | inductor | ...
    value: Optional[Tuple[float, str]] = None
    package: str = ""
    tolerance: Optional[float] = None
    voltage: Optional[float] = None
    extra_terms: List[str] = field(default_factory=list)


def looks_like_mpn(tok: str) -> bool:
    return (len(tok) >= 4 and bool(re.search(r"\d", tok)) and bool(re.search(r"[A-Za-z]", tok))
            and parse_value(tok) is None and not PACKAGE_RE.match(tok) and tok not in CHIP_SIZES)


def parse_query(q: str) -> ParsedQuery:
    raw = q.strip()
    pq = ParsedQuery(raw=raw, keyword=raw)
    if LCSC_RE.match(raw):
        pq.kind, pq.lcsc = "lcsc", raw.upper()
        return pq
    toks = raw.split()
    if len(toks) == 1 and looks_like_mpn(raw):
        pq.kind = "mpn"
        return pq
    parametric = False
    for t in toks:
        tl = t.lower()
        if t in CHIP_SIZES or PACKAGE_RE.match(t):
            pq.package = t.upper()
            parametric = True
            continue
        m = TOL_RE.match(t)
        if m:
            pq.tolerance = float(m.group(1))
            parametric = True
            continue
        v = parse_value(t)
        if v and v[1]:
            unit = v[1]
            if unit == "Ω":
                pq.category, pq.value = "resistor", v
            elif unit == "F":
                pq.category, pq.value = "capacitor", v
            elif unit == "H":
                pq.category, pq.value = "inductor", v
            elif unit == "V":
                pq.voltage = v[0]
                pq.extra_terms.append(t)
                parametric = True
                continue
            else:
                pq.extra_terms.append(t)
            parametric = True
            continue
        if v and not v[1] and re.match(r"^\d+(\.\d+)?[kKM]$", t):
            # bare "10k" -> resistor
            pq.category, pq.value = "resistor", (v[0], "Ω")
            parametric = True
            continue
        if tl in ("resistor", "res", "capacitor", "cap", "inductor", "mlcc"):
            pq.category = {"res": "resistor", "cap": "capacitor", "mlcc": "capacitor"}.get(tl, tl)
            parametric = True
            continue
        pq.extra_terms.append(t)
    if parametric:
        pq.kind = "parametric"
        pq.keyword = build_keyword(pq)
    return pq


def build_keyword(pq: ParsedQuery) -> str:
    from .units import format_value
    parts = []
    if pq.value:
        val, unit = pq.value
        unit_txt = {"Ω": "Ω", "F": "F", "H": "H"}.get(unit, unit)
        parts.append(format_value(val, unit_txt).replace("µ", "u"))
    if pq.package:
        parts.append(pq.package)
    if pq.tolerance is not None:
        parts.append(f"{pq.tolerance:g}%")
    parts += pq.extra_terms
    if pq.category and not pq.value:
        parts.insert(0, pq.category)
    return " ".join(parts) or pq.raw


# ---------------------------------------------------------------- filters

COND_RE = re.compile(r"^\s*(?P<name>[^<>=≤≥]+?)\s*(?P<op><=|>=|≤|≥|<|>|=)\s*(?P<val>.+?)\s*$")


@dataclass
class Condition:
    name: str
    op: str
    value: float
    unit: str

    def test(self, x: float) -> bool:
        tol = abs(self.value) * 1e-6
        return {"<": x < self.value, "<=": x <= self.value + tol, "≤": x <= self.value + tol,
                ">": x > self.value, ">=": x >= self.value - tol, "≥": x >= self.value - tol,
                "=": abs(x - self.value) <= max(tol, abs(self.value) * 0.001)}[self.op]


def parse_conditions(text: str) -> List[Condition]:
    """'Vds >= 30V, Rds(on) <= 10mΩ' -> conditions."""
    out = []
    for chunk in re.split(r"[,;]", text or ""):
        m = COND_RE.match(chunk)
        if not m:
            continue
        v = parse_value(m.group("val"))
        if not v:
            continue
        out.append(Condition(m.group("name").strip(), m.group("op"), v[0], v[1]))
    return out


SPEC_ALIASES = {
    "vds": ["drain source voltage", "vdss", "vds", "drain to source voltage"],
    "rds(on)": ["rds(on)", "rdson", "drain source on resistance", "on-resistance", "rds on"],
    "vgs(th)": ["vgs(th)", "gate threshold voltage", "vgs th"],
    "id": ["continuous drain current", "drain current", "id"],
    "vin": ["input voltage", "supply voltage", "operating voltage"],
    "vout": ["output voltage"],
    "iout": ["output current"],
    "tolerance": ["tolerance"],
    "power": ["power", "power dissipation"],
}


def _spec_values(part: Part, name: str, unit: str) -> List[float]:
    n = name.lower().strip()
    aliases = SPEC_ALIASES.get(n, [n])
    vals: List[float] = []
    for k, v in part.specs.items():
        kl = k.lower()
        if any(a in kl for a in aliases):
            rng = parse_range(v)
            if rng and (not unit or rng[2] in ("", unit)):
                vals += [rng[0], rng[1]]
                continue
            pv = parse_value(v.split("@")[0])
            if pv and (not unit or pv[1] in ("", unit)):
                vals.append(pv[0])
            else:
                vals += [x for x, u in find_values(v) if not unit or u == unit][:1]
    return vals


@dataclass
class Filters:
    category: str = ""
    package: str = ""
    in_stock: bool = False
    jlc: str = ""                     # "" | "Basic" | "Extended" | "Basic+Preferred"
    has_cad: bool = False
    parametric: str = ""

    def is_empty(self) -> bool:
        return not (self.category or self.package or self.in_stock or self.jlc or self.has_cad or self.parametric)

    def match(self, p: Part) -> Tuple[bool, str]:
        if self.category and self.category.lower() not in (p.category + " " + p.description).lower():
            return False, "category"
        if self.package:
            pk = re.sub(r"[\s_-]", "", self.package.lower())
            hay = re.sub(r"[\s_-]", "", (p.package + " " + p.description).lower())
            if pk not in hay:
                return False, "package"
        if self.in_stock and p.total_stock <= 0:
            return False, "stock"
        if self.jlc:
            cls = p.jlc_class
            allowed = {"Basic": {"Basic"}, "Extended": {"Extended"},
                       "Basic+Preferred": {"Basic", "Preferred"}}.get(self.jlc, {self.jlc})
            if cls not in allowed:
                return False, "jlc"
        if self.has_cad and not p.has_cad():
            return False, "cad"
        for c in parse_conditions(self.parametric):
            vals = _spec_values(p, c.name, c.unit)
            if not vals or not any(c.test(v) for v in vals):
                return False, f"param:{c.name}"
        return True, ""


def matches_parsed(pq: ParsedQuery, p: Part) -> bool:
    """Extra client-side check for parametric queries (value/tolerance/package)."""
    if pq.kind != "parametric":
        return True
    text = " ".join([p.description, p.package, " ".join(p.specs.values())])
    if pq.package:
        pk = pq.package.lower().replace("-", "")
        if pk not in text.lower().replace("-", ""):
            return False
    if pq.value:
        val, unit = pq.value
        found = [x for x, u in find_values(text) if u == unit]
        if found and not any(abs(x - val) <= abs(val) * 0.001 for x in found):
            return False
    if pq.tolerance is not None:
        tols = [float(m) for m in re.findall(r"±?(\d+(?:\.\d+)?)\s*%", text)]
        if tols and pq.tolerance not in tols:
            return False
    return True
