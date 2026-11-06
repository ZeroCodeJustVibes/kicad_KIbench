"""Schematic check: KiCad ERC, net label sanity, and part voltage ratings against net voltages.

Read only. Net voltages come from net names (+3V3, 12V, VBAT...), the design constraints (input range), and
user overrides stored in <project>/kibench-check.json. Part ratings come from symbol fields, capacitor values
("10u 16V"), and optionally distributor specs (`lookup`).

    python -m kibench.design.check path/to/project            # print the report
    python -m kibench.design.check path/to/project --json     # machine readable
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..kicad import cli
from .context import Constraints, find_root_schematic, rail_voltage

GND_RE = re.compile(r"^(A|D|P|SIG|CHASSIS|EARTH|POWER)?_?(GND|VSS|0V|COM)\w*$|^GND\w*$|^VSS\w*$", re.I)
UNNAMED_RE = re.compile(r"^(Net-\(|unconnected-\()")
VIN_RE = re.compile(r"^\+?(VIN|V_IN|VBUS|VSUPPLY|VSYS|VMAIN|VPWR|PWR_IN|VBAT)\b", re.I)

# symbol fields that hold a voltage rating, by rating kind
RATING_FIELDS = [
    (re.compile(r"^(voltage|rated voltage|voltage rating|voltage[ _-]?rated|v[ _]?rated|vr|vrated|"
                r"vmax|v max|max\.? ?voltage|voltage max|wv|working voltage)$", re.I), "across"),
    (re.compile(r"^(vds|vdss|v_?ds|drain.?source voltage)$", re.I), "across"),
    (re.compile(r"^(vce|vceo|v_?ce)$", re.I), "across"),
    (re.compile(r"^(vrrm|vr ?max|reverse voltage)$", re.I), "across"),
    (re.compile(r"^(vcc ?max|vdd ?max|supply voltage|vsupply|v ?supply|input voltage|vin ?max|vin)$", re.I), "supply"),
]
# distributor spec names (LCSC / DigiKey / Mouser) -> kind
SPEC_RATINGS = [
    (re.compile(r"voltage.{0,3}rated|rated voltage|voltage rating|voltage - rated", re.I), "across"),
    (re.compile(r"drain.{0,4}source voltage|vdss|vds", re.I), "across"),
    (re.compile(r"collector.{0,4}emitter.{0,12}voltage|vceo", re.I), "across"),
    (re.compile(r"reverse.{0,10}voltage|vrrm|voltage - dc reverse", re.I), "across"),
    (re.compile(r"supply voltage|voltage - supply|operating voltage|input voltage|voltage - input", re.I), "supply"),
]
VOLT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([kK]?)V(?![a-zA-Z])|(\d+)V(\d+)")


def parse_volts(text: str) -> Optional[Tuple[Optional[float], float]]:
    """'16V' -> (None, 16); '1.8V ~ 5.5V' -> (1.8, 5.5); '3V3' -> (None, 3.3)."""
    vals = []
    for m in VOLT_RE.finditer(text or ""):
        if m.group(3):
            vals.append(float(f"{m.group(3)}.{m.group(4)}"))
        else:
            v = float(m.group(1).replace(",", "."))
            vals.append(v * 1000 if m.group(2) else v)
    if not vals:
        return None
    return (min(vals) if len(vals) > 1 else None, max(vals))


@dataclass
class Issue:
    severity: str                 # error | warning | info
    category: str                 # ERC | Labels | Voltage | Interfaces | Setup
    message: str
    refs: List[str] = field(default_factory=list)
    nets: List[str] = field(default_factory=list)
    sheet: str = ""
    pos: Optional[Tuple[float, float]] = None
    detail: str = ""
    count: int = 1


@dataclass
class NetInfo:
    name: str
    volts: Optional[float] = None
    source: str = ""              # name | constraints | override | ground
    nodes: List[dict] = field(default_factory=list)      # {ref, pin, function, type}


@dataclass
class Rating:
    kind: str                     # across | supply
    vmax: float
    vmin: Optional[float] = None
    source: str = ""              # field 'Voltage' | value | LCSC spec ...


@dataclass
class PartInfo:
    ref: str
    value: str
    footprint: str = ""
    mpn: str = ""
    lcsc: str = ""
    description: str = ""
    ratings: List[Rating] = field(default_factory=list)
    pins: List[dict] = field(default_factory=list)       # {pin, function, type, net, volts}


@dataclass
class Report:
    project: str = ""
    sheet: str = ""
    issues: List[Issue] = field(default_factory=list)
    nets: List[NetInfo] = field(default_factory=list)
    parts: List[PartInfo] = field(default_factory=list)
    erc_ran: bool = False
    notes: List[str] = field(default_factory=list)

    def counts(self) -> Dict[str, int]:
        c = {"error": 0, "warning": 0, "info": 0}
        for i in self.issues:
            c[i.severity] = c.get(i.severity, 0) + 1
        return c

    def for_agent(self, max_parts: int = 120) -> dict:
        """Compact JSON for the chat agent: issues, net voltages, and each part's pins with nets and volts."""
        nets = [{"net": n.name, "volts": n.volts, "from": n.source, "pins": len(n.nodes)}
                for n in self.nets if not UNNAMED_RE.match(n.name) or n.volts is not None]
        parts = []
        for p in self.parts[:max_parts]:
            d = {"ref": p.ref, "value": p.value}
            if p.mpn:
                d["mpn"] = p.mpn
            if p.lcsc:
                d["lcsc"] = p.lcsc
            if p.ratings:
                d["ratings"] = [{"kind": r.kind, "max_v": r.vmax, **({"min_v": r.vmin} if r.vmin else {}),
                                 "from": r.source} for r in p.ratings]
            d["pins"] = [{k: v for k, v in pin.items() if v not in ("", None)} for pin in p.pins[:40]]
            parts.append(d)
        return {"project": self.project, "counts": self.counts(),
                "issues": [{k: v for k, v in asdict(i).items() if v not in ("", None, [])} for i in self.issues[:150]],
                "nets": nets[:300], "parts": parts,
                **({"notes": self.notes} if self.notes else {})}


# ---------------------------------------------------------------------- settings
def overrides_path(project_dir: Path) -> Path:
    return Path(project_dir) / "kibench-check.json"


def load_overrides(project_dir: Optional[Path]) -> Dict[str, float]:
    if not project_dir:
        return {}
    try:
        d = json.loads(overrides_path(project_dir).read_text())
        return {k: float(v) for k, v in (d.get("net_voltages") or {}).items() if v is not None and v != ""}
    except (OSError, ValueError, TypeError):
        return {}


def save_overrides(project_dir: Path, volts: Dict[str, Optional[float]]) -> None:
    p = overrides_path(project_dir)
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        d = {}
    d["net_voltages"] = {k: v for k, v in volts.items() if v is not None}
    p.write_text(json.dumps(d, indent=2))


# ---------------------------------------------------------------------- voltages
def base_name(net: str) -> str:
    return net.rsplit("/", 1)[-1]


def net_voltage(name: str, overrides: Dict[str, float], vin_max: Optional[float]) -> Tuple[Optional[float], str]:
    if name in overrides:
        return overrides[name], "override"
    b = base_name(name)
    if b in overrides:
        return overrides[b], "override"
    if UNNAMED_RE.match(name):
        return None, ""
    if GND_RE.match(b):
        return 0.0, "ground"
    v = rail_voltage(b)
    if v is not None and (re.match(r"^[+-]?(\d|V\d)", b, re.I) or re.search(r"\d+V\d*$", b, re.I)):
        return v, "name"
    if VIN_RE.match(b):
        if b.upper().lstrip("+").startswith("VBUS"):
            return (vin_max if vin_max is not None else 5.0), "name (USB VBUS)" if vin_max is None else "constraints"
        if vin_max is not None:
            return vin_max, "constraints"
    return None, ""


def part_ratings(ref: str, value: str, fields: Dict[str, str], specs: Optional[Dict[str, str]] = None) -> List[Rating]:
    out: List[Rating] = []
    for name, val in fields.items():
        for rx, kind in RATING_FIELDS:
            if rx.match(name.strip()):
                pv = parse_volts(val if re.search(r"[vV]", val) else val + "V")
                if pv:
                    out.append(Rating(kind, pv[1], pv[0], f"field '{name}'"))
                break
    prefix = re.match(r"^[A-Za-z]+", ref)
    if not out and prefix and prefix.group(0).upper() in ("C", "CP", "CE"):
        pv = parse_volts(value)
        if pv:
            out.append(Rating("across", pv[1], None, "value"))
    for name, val in (specs or {}).items():
        for rx, kind in SPEC_RATINGS:
            if rx.search(name):
                pv = parse_volts(str(val))
                if pv and not any(r.kind == kind for r in out):
                    out.append(Rating(kind, pv[1], pv[0], f"distributor spec '{name}'"))
                break
    return out


def _supply_pins(pins: List[dict]) -> List[dict]:
    pw = [p for p in pins if p.get("type") in ("power_in", "power_out") and (p.get("volts") or 0) > 0]
    if pw:
        return pw
    rx = re.compile(r"^(V(CC|DD|IN|BAT|S|SUP|IO|DDIO|CCIO)\w*|AVDD|DVDD|PVDD|VS\+?|V\+|IN)$", re.I)
    return [p for p in pins if rx.match(p.get("function") or "") and (p.get("volts") or 0) > 0]


# ---------------------------------------------------------------------- the check
def run_check(project_dir: Path, sheet: Optional[Path] = None,
              lookup: Optional[Callable[[str], Optional[Dict[str, str]]]] = None,
              overrides: Optional[Dict[str, float]] = None) -> Report:
    """`lookup(mpn_or_lcsc)` may return distributor specs for rating lookups (called once per distinct part)."""
    project_dir = Path(project_dir)
    sch = sheet or find_root_schematic(project_dir)
    rep = Report(project=project_dir.name, sheet=str(sch or ""))
    if not sch:
        rep.issues.append(Issue("error", "Setup", "No schematic found in the project folder"))
        return rep
    overrides = load_overrides(project_dir) if overrides is None else overrides
    cons = Constraints.load(project_dir)
    vin = parse_volts(cons.input_range) if cons.input_range else None
    vin_max = vin[1] if vin else None

    # --- ERC
    try:
        for v in cli.run_erc(sch):
            sev = v.severity if v.severity in ("error", "warning") else "info"
            refs = sorted({m.group(1) for it in v.items for m in [re.match(r"Symbol (\S+)", it.get("description", ""))]
                           if m})
            pos = None
            if v.items and v.items[0].get("pos"):
                pos = (v.items[0]["pos"].get("x", 0.0), v.items[0]["pos"].get("y", 0.0))
            rep.issues.append(Issue(sev, "ERC", v.description, refs, [], v.sheet, pos,
                                    "; ".join(it.get("description", "") for it in v.items)))
        rep.erc_ran = True
    except cli.CliError as e:
        rep.notes.append(f"ERC skipped: {e}")

    # --- netlist
    try:
        nl = cli.export_netlist(sch)
    except cli.CliError as e:
        rep.notes.append(f"netlist export failed: {e}")
        rep.issues.append(Issue("warning", "Setup", f"Could not read the netlist ({e}); voltage checks skipped"))
        return rep

    for name, nodes in sorted(nl.nets.items()):
        v, src = net_voltage(name, overrides, vin_max)
        rep.nets.append(NetInfo(name, v, src, [asdict(n) for n in nodes]))
    volts = {n.name: n.volts for n in rep.nets}
    pin_net: Dict[Tuple[str, str], str] = {}
    for n in rep.nets:
        for nd in n.nodes:
            pin_net[(nd["ref"], nd["pin"])] = n.name

    specs_cache: Dict[str, Optional[Dict[str, str]]] = {}
    for ref, c in sorted(nl.components.items(), key=lambda kv: _natkey(kv[0])):
        f = c.fields
        mpn = next((f[k] for k in ("MPN", "Manufacturer Part", "Manufacturer_Part_Number", "MFR Part", "PartNumber",
                                   "Mfr. No", "Part Number") if f.get(k)), "")
        lcsc = next((f[k] for k in ("LCSC", "LCSC Part", "JLCPCB Part", "LCSC Part #") if f.get(k)), "")
        specs = None
        if lookup and (mpn or lcsc):
            key = mpn or lcsc
            if key not in specs_cache:
                try:
                    specs_cache[key] = lookup(key)
                except Exception:
                    specs_cache[key] = None
            specs = specs_cache[key]
        pi = PartInfo(ref, c.value, c.footprint, mpn, lcsc, c.description,
                      part_ratings(ref, c.value, f, specs))
        for n in rep.nets:
            for nd in n.nodes:
                if nd["ref"] == ref:
                    pi.pins.append({"pin": nd["pin"], "function": nd.get("function", ""), "type": nd.get("type", ""),
                                    "net": n.name, "volts": n.volts})
        pi.pins.sort(key=lambda p: _natkey(p["pin"]))
        rep.parts.append(pi)

    _check_labels(rep, nl)
    _check_voltages(rep)
    _check_interfaces(rep, nl, volts)
    rep.issues = _group(rep.issues)
    order = {"error": 0, "warning": 1, "info": 2}
    rep.issues.sort(key=lambda i: (order.get(i.severity, 3), i.category, i.message))
    return rep


def _group(issues: List[Issue]) -> List[Issue]:
    """One row per repeated ERC message (e.g. the same library warning on twenty symbols)."""
    out: List[Issue] = []
    seen: Dict[Tuple[str, str, str], Issue] = {}
    for i in issues:
        if i.category != "ERC":
            out.append(i)
            continue
        k = (i.severity, i.category, i.message)
        g = seen.get(k)
        if g is None:
            g = seen[k] = Issue(i.severity, i.category, i.message, list(i.refs), list(i.nets), i.sheet, i.pos, i.detail)
            g.count = 1
            out.append(g)
        else:
            g.count += 1
            g.refs += [r for r in i.refs if r not in g.refs]
            if i.detail and len(g.detail) < 600:
                g.detail += "; " + i.detail
    for g in seen.values():
        if g.count > 1:
            g.message = f"{g.message} (×{g.count})"
    return out


def _natkey(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _norm(name: str) -> str:
    return re.sub(r"[\s_+\-./]", "", base_name(name).upper())


def _check_labels(rep: Report, nl: cli.Netlist) -> None:
    named = [n for n in nl.nets if not UNNAMED_RE.match(n)]
    # one-pin nets that carry a name: a label that goes nowhere, or a typo in the other label
    for n in named:
        nodes = nl.nets[n]
        if len(nodes) == 1:
            nd = nodes[0]
            rep.issues.append(Issue("warning", "Labels", f"Net '{base_name(n)}' reaches only one pin ({nd.ref} pin {nd.pin})",
                                    [nd.ref], [n], detail="Check the label spelling on the other end, or remove it."))
    # names that differ only in punctuation / case: +3V3 vs 3V3, SDA vs sda
    groups: Dict[str, List[str]] = {}
    for n in named:
        groups.setdefault(_norm(n), []).append(n)
    for k, ns in groups.items():
        bases = sorted({base_name(x) for x in ns})
        if len(bases) > 1:
            rep.issues.append(Issue("warning", "Labels", "Similar net names are separate nets: " + ", ".join(bases),
                                    [], ns, detail="Labels connect only when spelled exactly alike."))
    # same voltage, different rail names (3V3 and 3.3V)
    by_v: Dict[float, List[str]] = {}
    for n in rep.nets:
        if n.source == "name" and n.volts not in (None, 0.0):
            by_v.setdefault(n.volts, []).append(base_name(n.name))
    for v, ns in by_v.items():
        uniq = sorted(set(ns))
        if len(uniq) > 1 and len({_norm(x) for x in uniq}) > 1:
            plain = [x for x in uniq if re.fullmatch(r"\+?\d+V\d*|\+?\d+(\.\d+)?V", x, re.I)]
            if len(plain) > 1:
                rep.issues.append(Issue("info", "Labels", f"Several {v:g} V rail names: {', '.join(plain)}",
                                        [], plain, detail="If they are the same rail, use one name so they connect."))


def _check_voltages(rep: Report) -> None:
    for p in rep.parts:
        known = [pin for pin in p.pins if pin["volts"] is not None]
        for r in p.ratings:
            if r.kind == "across":
                if len(known) < 2 and not (len(known) == 1 and len(p.pins) == 1):
                    continue
                vs = [pin["volts"] for pin in known]
                span = max(vs) - min(vs)
                nets = sorted({pin["net"] for pin in known})
                if span > r.vmax + 1e-9:
                    rep.issues.append(Issue("error", "Voltage",
                                            f"{p.ref} ({p.value}) sees {span:g} V but is rated {r.vmax:g} V",
                                            [p.ref], nets, detail=f"rating from {r.source}"))
                elif p.ref[:1].upper() == "C" and span > 0.8 * r.vmax:
                    rep.issues.append(Issue("warning", "Voltage",
                                            f"{p.ref} ({p.value}) runs at {span / r.vmax:.0%} of its {r.vmax:g} V rating",
                                            [p.ref], nets,
                                            detail=f"ceramic capacitors lose capacitance near their rating (rating from {r.source})"))
            elif r.kind == "supply":
                sp = _supply_pins(p.pins)
                if not sp:
                    continue
                vmax = max(pin["volts"] for pin in sp)
                nets = sorted({pin["net"] for pin in sp})
                if vmax > r.vmax + 1e-9:
                    rep.issues.append(Issue("error", "Voltage", f"{p.ref} ({p.value}) supply {vmax:g} V is above its "
                                            f"{r.vmax:g} V maximum", [p.ref], nets, detail=f"rating from {r.source}"))
                elif r.vmin is not None and vmax < r.vmin - 1e-9:
                    rep.issues.append(Issue("warning", "Voltage", f"{p.ref} ({p.value}) supply {vmax:g} V is below its "
                                            f"{r.vmin:g} V minimum", [p.ref], nets, detail=f"rating from {r.source}"))
    unrated = [p.ref for p in rep.parts if not p.ratings and any(pin["volts"] for pin in p.pins)
               and p.ref[:1].upper() in ("C", "U", "Q", "D")]
    if unrated:
        rep.issues.append(Issue("info", "Voltage", f"No voltage rating known for {len(unrated)} part(s) on powered nets",
                                unrated[:30], detail="Add a 'Voltage' field or an MPN, use 'Look up ratings', "
                                                     "or ask the AI to check the datasheets."))


def _check_interfaces(rep: Report, nl: cli.Netlist, volts: Dict[str, Optional[float]]) -> None:
    """I2C lines need a pull-up resistor to a rail."""
    comps = nl.components
    for n, nodes in nl.nets.items():
        b = base_name(n).upper()
        if not re.search(r"(^|_)(SDA|SCL)\d*($|_)|I2C.*(SDA|SCL)", b):
            continue
        ok = False
        for nd in nodes:
            if not nd.ref[:1].upper() == "R" or nd.ref.upper().startswith("RN"):
                continue
            other = [x for x, xs in nl.nets.items() for y in xs if y.ref == nd.ref and y.pin != nd.pin]
            if any((volts.get(o) or 0) > 0 or re.match(r"^\+|VCC|VDD|3V3|5V", base_name(o), re.I) for o in other):
                ok = True
        if not ok and len(nodes) > 1:
            rep.issues.append(Issue("warning", "Interfaces", f"I2C line '{base_name(n)}' has no pull-up resistor",
                                    [x.ref for x in nodes][:8], [n],
                                    detail="Add a pull-up (typ. 2.2k-10k) to the bus voltage, unless a module provides it."))


# ---------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="kibench.design.check", description="Check a KiCad schematic")
    ap.add_argument("project", type=Path, help="project folder or .kicad_pro / .kicad_sch file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    p = args.project
    sheet = p if p.suffix == ".kicad_sch" else None
    proj = p.parent if p.is_file() else p
    rep = run_check(proj, sheet)
    if args.json:
        print(json.dumps(rep.for_agent(), indent=1))
        return 0
    for i in rep.issues:
        print(f"{i.severity.upper():7} [{i.category}] {i.message}" + (f"  ({', '.join(i.refs[:6])})" if i.refs else ""))
    rails = [n for n in rep.nets if n.volts is not None and n.source != "ground"]
    print(f"\n{len(rep.issues)} issue(s); {len(rep.nets)} nets, {len(rails)} with a known voltage; "
          f"{sum(1 for p in rep.parts if p.ratings)} of {len(rep.parts)} parts with a voltage rating")
    for n in rep.notes:
        print("note:", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
