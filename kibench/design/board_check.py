"""Board check: KiCad DRC (+ schematic parity), voltage clearances (IPC-2221B), silkscreen references and
decoupling-capacitor placement. Read only.

Net voltages come from the same place as the schematic check: net names, the design constraints and the
overrides in <project>/kibench-check.json.

    python -m kibench.design.board_check path/to/project            # print the report
    python -m kibench.design.board_check path/to/project --json     # machine readable
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..kicad import cli
from ..kicad.board import BoardModel, Footprint, copper_layers_of, poly_overlap, seg_dist
from ..kicad.schematic import Box
from .check import GND_RE, UNNAMED_RE, Issue, _natkey, load_overrides, net_voltage, parse_volts
from .context import Constraints

# IPC-2221B table 6-1, minimum conductor spacing in mm: (upper volts, B1 internal, B2 external uncoated)
IPC2221 = [(15, 0.05, 0.1), (30, 0.05, 0.1), (50, 0.1, 0.6), (100, 0.1, 0.6), (150, 0.2, 0.6),
           (170, 0.2, 1.25), (250, 0.2, 1.25), (300, 0.2, 1.25), (500, 0.25, 2.5)]
DECOUPLE_MM = 5.0
REF_FAR_MM = 2.0
MIN_TEXT_H = 0.8
MIN_TEXT_T = 0.15

DRC_LABEL = {"unconnected_items": "Not routed yet", "clearance": "Clearance too small",
             "shorting_items": "Short between nets", "tracks_crossing": "Tracks cross",
             "courtyards_overlap": "Courtyards overlap", "silk_over_copper": "Silkscreen over copper",
             "silk_overlap": "Silkscreen texts overlap", "silk_edge_clearance": "Silkscreen clipped by board edge",
             "copper_edge_clearance": "Copper too close to board edge", "hole_clearance": "Hole too close",
             "lib_footprint_mismatch": "Footprint differs from library",
             "lib_footprint_issues": "Footprint not found in library",
             "footprint_symbol_mismatch": "Footprint differs from the one in the schematic",
             "missing_footprint": "Part missing on the board", "extra_footprint": "Part not in the schematic",
             "net_conflict": "Pad net differs from schematic", "text_height": "Text too small",
             "text_thickness": "Text line too thin", "starved_thermal": "Thermal relief starved",
             "track_width": "Track too narrow", "annular_width": "Annular ring too small",
             "drill_out_of_range": "Drill size out of range", "via_dangling": "Via not connected",
             "track_dangling": "Track end not connected", "isolated_copper": "Isolated copper",
             "malformed_courtyard": "Courtyard malformed", "missing_courtyard": "No courtyard",
             "solder_mask_bridge": "Solder mask bridge"}
DRC_CATEGORY = {"drc": "DRC", "unconnected": "Unrouted", "parity": "Parity"}


def required_spacing(dv: float, external: bool) -> float:
    dv = abs(dv)
    for vmax, b1, b2 in IPC2221:
        if dv <= vmax:
            return b2 if external else b1
    return dv * (0.005 if external else 0.0025)


def cap_farads(value: str) -> Optional[float]:
    """'100n' / '0.1uF' / '4u7' / '10µF 16V' -> farads."""
    v = (value or "").replace("µ", "u").replace(",", ".")
    m = re.search(r"(\d+)([pnum])(\d+)", v)
    if m:
        num = float(f"{m.group(1)}.{m.group(3)}")
        unit = m.group(2)
    else:
        m = re.search(r"(\d+(?:\.\d+)?)\s*([pnum]?)F?\b", v, re.I)
        if not m:
            return None
        num, unit = float(m.group(1)), m.group(2).lower()
    return num * {"p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1.0}[unit]


@dataclass
class BoardNet:
    name: str
    volts: Optional[float] = None
    source: str = ""
    pads: int = 0
    min_width: Optional[float] = None
    max_width: Optional[float] = None
    length: float = 0.0
    vias: int = 0


@dataclass
class BoardReport:
    project: str = ""
    board: str = ""
    issues: List[Issue] = field(default_factory=list)
    nets: List[BoardNet] = field(default_factory=list)
    footprints: int = 0
    size: Optional[Tuple[float, float]] = None
    layers: int = 2
    drc_ran: bool = False
    notes: List[str] = field(default_factory=list)
    parts: List[dict] = field(default_factory=list)

    def counts(self) -> Dict[str, int]:
        c = {"error": 0, "warning": 0, "info": 0}
        for i in self.issues:
            c[i.severity] = c.get(i.severity, 0) + 1
        return c

    def for_agent(self, max_parts: int = 200) -> dict:
        nets = [{k: v for k, v in asdict(n).items() if v not in (None, "", 0, 0.0) or k == "volts"}
                for n in self.nets if not UNNAMED_RE.match(n.name) or n.volts is not None]
        return {"board": Path(self.board).name, "counts": self.counts(), "footprints": self.footprints,
                "copper_layers": self.layers,
                **({"size_mm": [round(self.size[0], 2), round(self.size[1], 2)]} if self.size else {}),
                "issues": [{k: v for k, v in asdict(i).items() if v not in ("", None, [])} for i in self.issues[:150]],
                "nets": nets[:300], "parts": self.parts[:max_parts],
                **({"notes": self.notes} if self.notes else {})}


def find_board(project_dir: Path) -> Optional[Path]:
    project_dir = Path(project_dir)
    pros = list(project_dir.glob("*.kicad_pro"))
    for pro in pros:
        b = pro.with_suffix(".kicad_pcb")
        if b.exists():
            return b
    boards = sorted(project_dir.glob("*.kicad_pcb"))
    return boards[0] if boards else None


# ---------------------------------------------------------------------- the check
def run_board_check(project_dir: Path, board: Optional[Path] = None, overrides: Optional[Dict[str, float]] = None,
                    drc: bool = True) -> BoardReport:
    project_dir = Path(project_dir)
    pcb = board or find_board(project_dir)
    rep = BoardReport(project=project_dir.name, board=str(pcb or ""))
    if not pcb:
        rep.issues.append(Issue("error", "Setup", "No board (.kicad_pcb) found in the project folder"))
        return rep
    m = BoardModel(Path(pcb).read_text())
    rep.footprints = len(m.footprints)
    rep.layers = len(m.copper)
    if m.outline:
        rep.size = (m.outline.w, m.outline.h)
    else:
        rep.issues.append(Issue("error", "Placement", "The board has no outline on Edge.Cuts"))

    if drc:
        try:
            _drc_issues(rep, cli.run_drc(Path(pcb), parity=True))
            rep.drc_ran = True
        except cli.CliError as e:
            rep.notes.append(f"DRC skipped: {e}")

    overrides = load_overrides(project_dir) if overrides is None else overrides
    cons = Constraints.load(project_dir)
    vin = parse_volts(cons.input_range) if cons.input_range else None
    vin_max = vin[1] if vin else None
    volts: Dict[str, Optional[float]] = {}
    nets: Dict[str, BoardNet] = {}
    for fp in m.footprints:
        for p in fp.pads:
            if p.net:
                n = nets.get(p.net)
                if n is None:
                    v, src = net_voltage(p.net, overrides, vin_max)
                    n = nets[p.net] = BoardNet(p.net, v, src)
                n.pads += 1
    for t in m.tracks:
        n = nets.get(t.net)
        if n is None:
            continue
        n.min_width = t.width if n.min_width is None else min(n.min_width, t.width)
        n.max_width = t.width if n.max_width is None else max(n.max_width, t.width)
        n.length += math.hypot(t.b[0] - t.a[0], t.b[1] - t.a[1])
    for v in m.vias:
        if v.net in nets:
            nets[v.net].vias += 1
    for n in nets.values():
        n.length = round(n.length, 2)
        volts[n.name] = n.volts
    rep.nets = sorted(nets.values(), key=lambda n: (n.volts is None, -(n.volts or 0), _natkey(n.name)))
    for fp in sorted(m.footprints, key=lambda f: _natkey(f.ref)):
        if fp.ref and fp.pads:
            rep.parts.append({"ref": fp.ref, "value": fp.value, "footprint": fp.lib.split(":")[-1], "side": fp.side,
                              "x": round(fp.pos[0], 3), "y": round(fp.pos[1], 3), "rot": fp.rot})

    _check_clearance(rep, m, volts)
    _check_refs(rep, m)
    _check_placement(rep, m)
    _check_decoupling(rep, m, volts)
    order = {"error": 0, "warning": 1, "info": 2}
    rep.issues.sort(key=lambda i: (order.get(i.severity, 3), i.category, i.message))
    return rep


# ---------------------------------------------------------------------- DRC
REF_RXS = [re.compile(r"\bof (\S+?)(?: on |$)"), re.compile(r"^Footprint (\S+)"), re.compile(r"field of (\S+)")]


def _item_refs(desc: str) -> List[str]:
    out = []
    for rx in REF_RXS:
        for m in rx.finditer(desc or ""):
            r = m.group(1).rstrip(",")
            if r and r not in out:
                out.append(r)
    return out


def _drc_issues(rep: BoardReport, viols: List[cli.DrcViolation]) -> None:
    groups: Dict[Tuple[str, str, str], Issue] = {}
    for v in viols:
        sev = v.severity if v.severity in ("error", "warning") else "info"
        cat = DRC_CATEGORY.get(v.group, "DRC")
        refs: List[str] = []
        nets: List[str] = []
        for it in v.items:
            d = it.get("description", "")
            refs += [r for r in _item_refs(d) if r not in refs]
            nets += [n for n in re.findall(r"\[([^\]]+)\]", d) if n not in nets and not n.startswith("<")]
        pos = None
        if v.items and v.items[0].get("pos"):
            pos = (v.items[0]["pos"].get("x", 0.0), v.items[0]["pos"].get("y", 0.0))
        k = (sev, cat, v.type)
        g = groups.get(k)
        detail = "; ".join(it.get("description", "") for it in v.items)
        if g is None:
            label = DRC_LABEL.get(v.type) or re.sub(r"\s*\(.*\)$", "", v.description) or v.type
            g = groups[k] = Issue(sev, cat, label, refs, nets, "", pos, f"{v.description}: {detail}")
            g.count = 1
            rep.issues.append(g)
        else:
            g.count += 1
            g.refs += [r for r in refs if r not in g.refs]
            g.nets += [n for n in nets if n not in g.nets]
            if len(g.detail) < 700:
                g.detail += f" | {v.description}: {detail}"
    for g in groups.values():
        if g.count > 1:
            g.message = f"{g.message} (×{g.count})"


# ---------------------------------------------------------------------- voltage clearance
class _Grid:
    def __init__(self, cell: float = 2.5):
        self.cell = cell
        self.d: Dict[Tuple[int, int], list] = {}

    def _cells(self, b: Box):
        c = self.cell
        for x in range(int(math.floor(b.x1 / c)), int(math.floor(b.x2 / c)) + 1):
            for y in range(int(math.floor(b.y1 / c)), int(math.floor(b.y2 / c)) + 1):
                yield (x, y)

    def add(self, b: Box, item) -> None:
        for k in self._cells(b):
            self.d.setdefault(k, []).append(item)

    def query(self, b: Box):
        seen = set()
        for k in self._cells(b):
            for it in self.d.get(k, ()):
                if id(it) not in seen:
                    seen.add(id(it))
                    yield it


def _copper_items(m: BoardModel, volts: Dict[str, Optional[float]]):
    """(layer, a, b, radius, net, label) for copper of nets with a known voltage."""
    out = []
    for fp in m.footprints:
        for p in fp.pads:
            if volts.get(p.net) is None:
                continue
            a, b, r = p.capsule()
            for lay in copper_layers_of(p.layers, m.copper):
                out.append((lay, a, b, r, p.net, f"pad {p.num} of {fp.ref}", fp.ref))
    for t in m.tracks:
        if volts.get(t.net) is not None:
            out.append((t.layer, t.a, t.b, t.width / 2, t.net, "track", ""))
    for v in m.vias:
        if volts.get(v.net) is not None:
            lays = m.copper if len(v.layers) == 2 and set(v.layers) == {"F.Cu", "B.Cu"} else \
                [l for l in v.layers if l.endswith(".Cu")]
            for lay in lays:
                out.append((lay, v.pos, v.pos, v.size / 2, v.net, "via", ""))
    return out


def _check_clearance(rep: BoardReport, m: BoardModel, volts: Dict[str, Optional[float]]) -> None:
    known = {n: v for n, v in volts.items() if v is not None}
    if len(known) < 2:
        return
    vmin, vmax = min(known.values()), max(known.values())
    if vmax - vmin <= 30:
        return                                   # below 30 V every IPC-2221 spacing is <= 0.1 mm (DRC covers it)
    items = _copper_items(m, volts)
    grid = _Grid()
    for it in items:
        lay, a, b, r, net, *_ = it
        grid.add(Box(min(a[0], b[0]) - r, min(a[1], b[1]) - r, max(a[0], b[0]) + r, max(a[1], b[1]) + r), it)
    worst: Dict[Tuple[str, str], tuple] = {}
    outer = {"F.Cu", "B.Cu"}
    for it in items:
        lay, a, b, r, net, label, ref = it
        v = known[net]
        if max(abs(v - vmin), abs(v - vmax)) <= 30:
            continue
        ext = lay in outer
        need_max = required_spacing(max(abs(v - vmin), abs(v - vmax)), ext)
        q = Box(min(a[0], b[0]) - r - need_max - 1, min(a[1], b[1]) - r - need_max - 1,
                max(a[0], b[0]) + r + need_max + 1, max(a[1], b[1]) + r + need_max + 1)
        for o in grid.query(q):
            if o is it or o[0] != lay or o[4] == net or (ref and ref == o[6]):
                continue                         # pads of one part: the package sets their spacing
            dv = abs(v - known[o[4]])
            if dv <= 30:
                continue
            need = required_spacing(dv, ext)
            gap = seg_dist(a, b, o[1], o[2]) - r - o[3]
            if gap < need - 1e-6:
                k = tuple(sorted((net, o[4])))
                if k not in worst or gap < worst[k][0]:
                    worst[k] = (gap, need, dv, lay, ((a[0] + o[1][0]) / 2, (a[1] + o[1][1]) / 2),
                                sorted({x for x in (ref, o[6]) if x}), f"{label} ↔ {o[5]}")
    for (n1, n2), (gap, need, dv, lay, pos, refs, what) in sorted(worst.items(), key=lambda kv: kv[1][0] - kv[1][1]):
        kind = "outer layer, uncoated (B2)" if lay in outer else "inner layer (B1)"
        rep.issues.append(Issue(
            "error", "Clearance",
            f"{n1} and {n2} ({dv:g} V apart) are {max(gap, 0):.2f} mm apart on {lay}; IPC-2221B asks ≥ {need:g} mm",
            refs, [n1, n2], "", pos,
            f"Closest: {what}. Spacing rule: IPC-2221B table 6-1, {kind}. Zones are not included; a conformal "
            f"coating allows less. Set a net class clearance so DRC keeps checking it."))


# ---------------------------------------------------------------------- silkscreen references
def _side_pads(m: BoardModel, side: str):
    lay = f"{side}.Cu"
    for fp in m.footprints:
        for p in fp.pads:
            cu = copper_layers_of(p.layers, m.copper)
            if lay in cu or p.kind in ("thru_hole", "np_thru_hole"):
                yield fp, p


def _check_refs(rep: BoardReport, m: BoardModel) -> None:
    missing: List[str] = []
    small: Dict[Tuple[float, float], List[str]] = {}
    pads_by_side = {s: [(fp, p, p.box()) for fp, p in _side_pads(m, s)] for s in ("F", "B")}
    grid_pads = {s: _Grid(2.0) for s in ("F", "B")}
    for s, lst in pads_by_side.items():
        for it in lst:
            grid_pads[s].add(it[2], it)
    refs_boxes: Dict[str, List[Tuple[str, Box]]] = {"F": [], "B": []}
    for fp in m.footprints:
        if not fp.pads or not fp.ref or fp.ref.startswith("#") or "REF" in fp.ref:
            continue
        t = fp.reference
        if t is None or t.hidden or not t.on_silk:
            missing.append(fp.ref)
            continue
        side = "B" if t.layer.startswith("B.") else "F"
        tb = t.box()
        tc = t.corners()
        refs_boxes[side].append((fp.ref, tb, tc))
        d = tb.distance(fp.body())
        if d > REF_FAR_MM:
            rep.issues.append(Issue("warning", "Silkscreen", f"{fp.ref} reference is {d:.1f} mm away from its part",
                                    [fp.ref], [], "", t.pos, "Hard to tell which part it labels after assembly."))
        hit = [f"{o.ref} pad {p.num}" for o, p, b in grid_pads[side].query(tb)
               if b.overlaps(tb, -0.05) and poly_overlap(p.corners(), tc, -0.05)]
        if hit:
            rep.issues.append(Issue("warning", "Silkscreen", f"{fp.ref} reference lies on pads",
                                    [fp.ref], [], "", t.pos, "Over " + ", ".join(hit[:6]) +
                                    ". The fab clips silkscreen on pads, so it may become unreadable."))
        if t.size[0] < MIN_TEXT_H - 1e-6 or t.thickness < MIN_TEXT_T - 1e-6:
            small.setdefault((round(t.size[0], 3), round(t.thickness, 3)), []).append(fp.ref)
    for side, lst in refs_boxes.items():
        g = _Grid(3.0)
        for it in lst:
            g.add(it[1], it)
        seen = set()
        for ref, b, c in lst:
            for oref, ob, oc in g.query(b):
                if oref != ref and b.overlaps(ob, -0.05) and (oref, ref) not in seen and poly_overlap(c, oc, -0.05):
                    seen.add((ref, oref))
                    rep.issues.append(Issue("warning", "Silkscreen", f"References {ref} and {oref} overlap",
                                            [ref, oref], [], "", (b.cx, b.cy)))
    if missing:
        rep.issues.append(Issue("info", "Silkscreen", f"{len(missing)} parts have no visible reference on the "
                                f"silkscreen", sorted(missing, key=_natkey), count=len(missing)))
    for (h, th), refs in small.items():
        rep.issues.append(Issue("warning", "Silkscreen", f"{len(refs)} references are smaller than most fabs print "
                                f"({h:g} mm high, {th:g} mm line)", sorted(refs, key=_natkey), count=len(refs),
                                detail=f"Typical minimum: {MIN_TEXT_H} mm high, {MIN_TEXT_T} mm line width."))


# ---------------------------------------------------------------------- placement
def _check_placement(rep: BoardReport, m: BoardModel) -> None:
    ob = m.outline
    if ob is None:
        return
    out = []
    for fp in m.footprints:
        if not fp.pads:
            continue
        cx = sum(p.pos[0] for p in fp.pads) / len(fp.pads)
        cy = sum(p.pos[1] for p in fp.pads) / len(fp.pads)
        if not (ob.x1 <= cx <= ob.x2 and ob.y1 <= cy <= ob.y2):
            out.append(fp.ref)
    if out:
        rep.issues.append(Issue("error", "Placement", f"{len(out)} parts are outside the board outline",
                                sorted(out, key=_natkey), count=len(out)))


def _is_ground(net: str, volts: Dict[str, Optional[float]]) -> bool:
    return bool(GND_RE.match(net.rsplit("/", 1)[-1])) or volts.get(net) == 0.0


def _check_decoupling(rep: BoardReport, m: BoardModel, volts: Dict[str, Optional[float]]) -> None:
    ics = [fp for fp in m.footprints if len(fp.pads) >= 6 and re.match(r"^(U|IC)\d", fp.ref or "")]
    if not ics:
        return
    caps: List[Tuple[Footprint, str]] = []
    for fp in m.footprints:
        if not re.match(r"^C\d", fp.ref or "") or len(fp.pads) != 2:
            continue
        n1, n2 = fp.pads[0].net, fp.pads[1].net
        if not n1 or not n2:
            continue
        f = cap_farads(fp.value)
        if f is None or f > 1.1e-6:
            continue
        if _is_ground(n1, volts) and not _is_ground(n2, volts) and (volts.get(n2) or 0) > 0:
            caps.append((fp, n2))
        elif _is_ground(n2, volts) and not _is_ground(n1, volts) and (volts.get(n1) or 0) > 0:
            caps.append((fp, n1))
    supply_pins: Dict[str, List[Tuple[Footprint, object]]] = {}
    for ic in ics:
        for p in ic.pads:
            if p.net and not _is_ground(p.net, volts) and (volts.get(p.net) or 0) > 0:
                supply_pins.setdefault(p.net, []).append((ic, p))

    def dist(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])
    covered = set()
    for cap, net in caps:
        cp = next(p for p in cap.pads if p.net == net)
        pins = supply_pins.get(net, [])
        if not pins:
            continue
        ic, pin = min(pins, key=lambda ip: dist(cp.pos, ip[1].pos))
        d = dist(cp.pos, pin.pos)
        if d > DECOUPLE_MM:
            rep.issues.append(Issue("warning", "Decoupling",
                                    f"{cap.ref} ({cap.value} on {net}) is {d:.1f} mm from {ic.ref} pin {pin.num}",
                                    [cap.ref, ic.ref], [net], "", cap.pos,
                                    f"Decoupling capacitors work best within a few mm of the supply pin "
                                    f"(this check warns above {DECOUPLE_MM:g} mm)."))
        for ic2, pin2 in pins:
            if dist(cp.pos, pin2.pos) <= DECOUPLE_MM:
                covered.add((ic2.ref, net))
    for net, pins in supply_pins.items():
        if not any(n == net for _c, n in caps):
            continue                     # no small capacitors on this rail at all: nothing to compare with
        for ic in sorted({ic.ref for ic, _p in pins}, key=_natkey):
            if (ic, net) not in covered:
                rep.issues.append(Issue("info", "Decoupling",
                                        f"{ic} has no small capacitor within {DECOUPLE_MM:g} mm of its {net} pin(s)",
                                        [ic], [net]))


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="kibench.design.board_check", description="Check a KiCad board")
    ap.add_argument("project", type=Path)
    ap.add_argument("--board", type=Path)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-drc", action="store_true")
    args = ap.parse_args(argv)
    rep = run_board_check(args.project, args.board, drc=not args.no_drc)
    if args.json:
        print(json.dumps(rep.for_agent(), indent=1, default=str))
        return 0
    for i in rep.issues:
        print(f"[{i.severity:7}] {i.category:11} {i.message}" + (f"  ({', '.join(i.refs[:8])})" if i.refs else ""))
    c = rep.counts()
    print(f"{c['error']} errors, {c['warning']} warnings, {c['info']} info; "
          f"{sum(1 for n in rep.nets if n.volts is not None)} nets with a voltage")
    for n in rep.notes:
        print("note:", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
