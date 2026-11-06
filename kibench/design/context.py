"""Design context from the open project: parsed read-only from .kicad_sch files.

Power rails (from power symbols and labels), existing parts, interfaces inferred
from net labels, user-edited design constraints, and the current selection.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from .. import sexpr

RAIL_RE = re.compile(r"^\+?-?(\d+)[Vv](\d*)$|^\+?-?(\d+(?:\.\d+)?)\s*V$|^[+-]?V(?:CC|DD|IN|BAT|BUS|SYS|MAIN|USB|IO)\w*$"
                     r"|^[+-]?\d+V\d*_\w+$|^V\d+V\d*$", re.I)
INTERFACES = {
    "I2C": [r"\bSDA\d*\b|_SDA\b|SDA_", r"\bSCL\d*\b|_SCL\b|SCL_"],
    "SPI": [r"MOSI|COPI|SDI\b", r"MISO|CIPO|SDO\b", r"SCK|SCLK"],
    "USB": [r"USB_?D[P+]|\bD\+|USB_?DP", r"USB_?D[MN-]|\bD-|USB_?DM"],
    "CAN": [r"CAN_?H", r"CAN_?L"],
    "UART": [r"\bTX\d?\b|_TX\b|TXD", r"\bRX\d?\b|_RX\b|RXD"],
    "SWD": [r"SWDIO", r"SWCLK"],
    "JTAG": [r"\bTDI\b", r"\bTDO\b", r"\bTCK\b", r"\bTMS\b"],
    "Ethernet": [r"ETH_|RMII|MDIO"],
    "I2S": [r"I2S|LRCLK|BCLK|\bWS\b"],
    "RS-485": [r"RS485|\bA485|\bB485"],
}


def rail_voltage(name: str) -> Optional[float]:
    n = name.strip().lstrip("+").upper()
    neg = n.startswith("-")
    n = n.lstrip("-")
    m = re.match(r"^(\d+)V(\d+)", n)                # 3V3, 1V8
    if m:
        v = float(f"{m.group(1)}.{m.group(2)}")
        return -v if neg else v
    m = re.match(r"^(\d+(?:\.\d+)?)V", n)            # 5V, 12V, 3.3V
    if m:
        v = float(m.group(1))
        return -v if neg else v
    m = re.search(r"(\d+)V(\d+)|(\d+(?:\.\d+)?)V\b", n)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}") if m.group(1) else float(m.group(3))
    return None


@dataclass
class PartInstance:
    ref: str
    value: str
    lib_id: str
    footprint: str = ""
    mpn: str = ""
    lcsc: str = ""
    sheet: str = ""


@dataclass
class Constraints:
    input_range: str = ""
    max_current: str = ""
    temperature: str = ""
    assembly: str = "JLC Basic preferred"
    size_limits: str = ""
    notes: str = ""

    @staticmethod
    def path(project_dir: Path) -> Path:
        return Path(project_dir) / "kibench-constraints.json"

    @classmethod
    def load(cls, project_dir: Optional[Path]) -> "Constraints":
        if not project_dir:
            return cls()
        try:
            d = json.loads(cls.path(project_dir).read_text())
            c = cls()
            for k, v in d.items():
                if hasattr(c, k):
                    setattr(c, k, v)
            return c
        except (OSError, ValueError):
            return cls()

    def save(self, project_dir: Path) -> None:
        self.path(project_dir).write_text(json.dumps(asdict(self), indent=2))


@dataclass
class DesignContext:
    project: str = ""
    rails: Dict[str, Optional[float]] = field(default_factory=dict)
    parts: List[PartInstance] = field(default_factory=list)
    labels: Set[str] = field(default_factory=set)
    interfaces: Dict[str, List[str]] = field(default_factory=dict)
    constraints: Constraints = field(default_factory=Constraints)
    selection: List[str] = field(default_factory=list)

    def summary(self, max_parts: int = 80) -> dict:
        """Compact summary for the chat agent."""
        by_val: Dict[str, List[str]] = {}
        for p in self.parts:
            if p.ref.startswith("#"):
                continue
            key = f"{p.value}|{p.footprint.split(':')[-1]}|{p.mpn or p.lcsc}"
            by_val.setdefault(key, []).append(p.ref)
        parts = []
        for key, refs in sorted(by_val.items(), key=lambda kv: kv[1][0])[:max_parts]:
            value, fp, mpn = key.split("|")
            parts.append({"refs": ",".join(sorted(refs, key=_natkey)[:12]) + ("…" if len(refs) > 12 else ""),
                          "value": value, "footprint": fp, **({"mpn": mpn} if mpn else {})})
        sel = [asdict(p) for p in self.parts if p.ref in self.selection]
        return {"project": self.project,
                "power_rails": [{"name": n, "volts": v} for n, v in sorted(self.rails.items())],
                "interfaces": {k: v[:8] for k, v in self.interfaces.items()},
                "parts": parts,
                "part_count": len([p for p in self.parts if not p.ref.startswith("#")]),
                "constraints": {k: v for k, v in asdict(self.constraints).items() if v},
                "selection": sel}


def _natkey(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def _sheet_files(root_sch: Path) -> List[Path]:
    """Root schematic plus all hierarchical sheets (recursively)."""
    seen: List[Path] = []
    stack = [root_sch]
    while stack:
        f = stack.pop()
        if f in seen or not f.exists():
            continue
        seen.append(f)
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for m in re.finditer(r'\(property\s+"Sheet ?[Ff]ile"\s+"([^"]+)"', text):
            stack.append((f.parent / m.group(1)).resolve())
    return seen


def find_root_schematic(project_dir: Path) -> Optional[Path]:
    pro = sorted(Path(project_dir).glob("*.kicad_pro"))
    if pro:
        sch = pro[0].with_suffix(".kicad_sch")
        if sch.exists():
            return sch
    schs = sorted(Path(project_dir).glob("*.kicad_sch"))
    return schs[0] if schs else None


def parse_schematic(path: Path) -> DesignContext:
    ctx = DesignContext()
    for f in _sheet_files(path):
        try:
            root = sexpr.parse(f.read_text(errors="replace"))
        except (OSError, ValueError):
            continue
        for sym in sexpr.findall(root, "symbol"):
            lib_id = str(sexpr.value(sexpr.find(sym, "lib_id"), 1, ""))
            props = sexpr.prop_map(sym)
            ref = props.get("Reference", "")
            # multi-instance designs store the real reference under (instances ...)
            inst_ref = next(iter(sexpr.walk(sym, "reference")), None)
            if inst_ref is not None:
                ref = str(inst_ref[1])
            value = props.get("Value", "")
            if lib_id.lower().startswith("power:") or ref.startswith("#PWR") or ref.startswith("#FLG"):
                if ref.startswith("#FLG"):
                    continue
                ctx.rails[value] = rail_voltage(value)
                continue
            mpn = next((props[k] for k in ("MPN", "Manufacturer Part", "MFR Part", "PartNumber", "Mfr. No") if props.get(k)), "")
            lcsc = next((props[k] for k in ("LCSC", "LCSC Part", "JLCPCB Part") if props.get(k)), "")
            ctx.parts.append(PartInstance(ref, value, lib_id, props.get("Footprint", ""), mpn, lcsc, f.name))
        for kind in ("label", "global_label", "hierarchical_label"):
            for lab in sexpr.findall(root, kind):
                name = str(lab[1])
                ctx.labels.add(name)
    for name in ctx.labels:
        if RAIL_RE.match(name) or rail_voltage(name) is not None and re.match(r"^[+-]?\d", name):
            ctx.rails.setdefault(name, rail_voltage(name))
    for iface, pats in INTERFACES.items():
        hits = sorted({l for l in ctx.labels for p in pats if re.search(p, l, re.I)})
        if len(hits) >= min(2, len(pats)):
            ctx.interfaces[iface] = hits
    return ctx


def load_context(project_dir: Optional[Path], selection: Optional[List[str]] = None) -> DesignContext:
    if not project_dir:
        return DesignContext()
    sch = find_root_schematic(Path(project_dir))
    ctx = parse_schematic(sch) if sch else DesignContext()
    ctx.project = Path(project_dir).name
    ctx.constraints = Constraints.load(project_dir)
    ctx.selection = selection or []
    return ctx
