"""Pin-table symbol builder: parse pins, classify, auto-layout, split into units. Fixed rules, no AI."""
from __future__ import annotations

import csv
import io
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..kicad import symbol as S

GRID = 2.54
PIN_LEN = 2.54

POWER_POS = re.compile(r"^(V(CC|DD|IN|BAT|BUS|IO|REF|CORE|AUX|CCIO|CCINT|CCAUX|CCO|DDIO|DDA|DDQ|PP|S|EE)[\w+-]*|AVDD\w*|DVDD\w*|"
                       r"AVCC\w*|PVDD\w*|PVIN\w*|V\+|\+?\d+V\d*|VP|VDD_\w+|VCC_\w+|VSUP\w*)$", re.I)
POWER_NEG = re.compile(r"^(GND\w*|VSS\w*|V(CC|DD|S)-|AGND\w*|DGND\w*|PGND\w*|AVSS\w*|DVSS\w*|V-|VEE\w*|EP|EPAD|PAD|SGND|GNDA|GNDD|"
                       r"THERMAL_?PAD|TAB)$", re.I)
NC_RE = re.compile(r"^(N\.?C\.?|DNC|NIC|RSVD|RESERVED)\d*$", re.I)
OUT_RE = re.compile(r"(^|_)(OUT[\w+-]*|TXD?\d*|DO\d*|MISO|SDO|CIPO|Q\d*|IRQ\w*|INT\w*|PGOOD|PG|FAULT\w*|ALERT\w*|"
                    r"SW\d*|LX\d*|CLKOUT\w*)$", re.I)
IN_RE = re.compile(r"(^|_)(IN[\w+-]*|RXD?\d*|DI\d*|MOSI|SDI|COPI|SCK|SCLK|CLK\w*|CS\w*|SS\w*|N?RST\w*|N?RESET\w*|EN\w*|"
                   r"SHDN\w*|CE\w*|WE\w*|OE\w*|BOOT\w*|MODE\w*|SEL\w*|ADDR\w*|A\d+|FB|SET|SYNC|TRIG\w*|XTAL_?IN|"
                   r"XI|OSC_?IN)$", re.I)
TYPE_WORDS = {
    "i": "input", "in": "input", "input": "input", "di": "input", "ai": "input",
    "o": "output", "out": "output", "output": "output", "do": "output", "ao": "output",
    "io": "bidirectional", "i/o": "bidirectional", "bidir": "bidirectional", "bidirectional": "bidirectional",
    "inout": "bidirectional", "b": "bidirectional",
    "p": "power_in", "pwr": "power_in", "power": "power_in", "s": "power_in", "supply": "power_in",
    "g": "power_in", "gnd": "power_in", "ground": "power_in", "power_in": "power_in", "power_out": "power_out",
    "a": "passive", "analog": "passive", "passive": "passive", "nc": "no_connect", "no_connect": "no_connect",
    "od": "open_collector", "oc": "open_collector", "open_drain": "open_collector", "open_collector": "open_collector",
    "z": "tri_state", "tri_state": "tri_state", "t": "tri_state",
}


@dataclass
class PinDef:
    number: str
    name: str
    etype: str = ""            # blank = classify from the name
    side: str = ""             # left | right | top | bottom | blank = auto
    group: str = ""            # bank / function group (for multi-unit split)
    unit: int = 0              # 0 = auto

    def resolved_type(self) -> str:
        return self.etype or classify(self.name)


def classify(name: str) -> str:
    n = name.strip().lstrip("~/!").replace("{", "").replace("}", "")
    if NC_RE.match(n):
        return "no_connect"
    if POWER_POS.match(n) or POWER_NEG.match(n):
        return "power_in"
    if OUT_RE.search(n):
        return "output"
    if IN_RE.search(n):
        return "input"
    if re.match(r"^(P[A-Z]\d+|GPIO\w*|IO\w*|D\d+|PIO\w*|SDA\w*|SCL\w*|USB_?D[PN+-]?\w*|D[+-]|DP|DM|SWDIO|"
                r"CAN\w*|[A-Z]+\d+_\w+)$", n, re.I):
        return "bidirectional"
    return "passive"


def is_ground(name: str) -> bool:
    return bool(POWER_NEG.match(name.strip()))


def _natkey(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _is_pin_number(tok: str) -> bool:
    return bool(re.fullmatch(r"\d{1,4}|[A-HJ-NPR-Y]{1,2}\d{1,2}|EP|PAD|TAB|MP", tok.strip(), re.I))


# ---------------------------------------------------------------- parsing

def parse_pin_text(text: str) -> List[PinDef]:
    """Parse pasted text (datasheet table, CSV, TSV, 'num name' lines)."""
    text = text.replace("\r", "")
    lines = [l for l in text.split("\n") if l.strip()]
    if not lines:
        return []
    sample = "\n".join(lines[:20])
    if sample.count("\t") >= len(lines[:20]) or sample.count(",") >= len(lines[:20]) or sample.count(";") >= len(lines[:20]):
        pins = parse_csv(text)
        if pins:
            return pins
    structured = [bool(re.search(r"\s{2,}|\t|\|", l.strip())) for l in lines]
    rows = [re.split(r"\s{2,}|\t|\s*\|\s*", l.strip()) if st else l.split() for l, st in zip(lines, structured)]
    pins: List[PinDef] = []
    if all(len(r) >= 2 for r in rows[:max(1, len(rows) // 2)]):
        for r, st in zip(rows, structured):
            r = [c for c in r if c]
            if len(r) < 2:
                continue
            if _is_pin_number(r[0]) and not _is_pin_number(r[1]):
                num, name, rest = r[0], r[1], r[2:]
            elif _is_pin_number(r[1]) and not _is_pin_number(r[0]):
                num, name, rest = r[1], r[0], r[2:]
            elif _is_pin_number(r[0]):
                num, name, rest = r[0], r[1], r[2:]
            else:
                continue
            etype = ""
            # a type column only in tabular rows, or as the single trailing word ("1 VIN P")
            if rest and rest[0].lower() in TYPE_WORDS and (st or len(rest) == 1):
                etype = TYPE_WORDS[rest[0].lower()]
            # "1, 9  GND" style: several numbers for one name
            for n in re.split(r"[,/]\s*", num):
                if n:
                    pins.append(PinDef(n.strip(), name.strip(), etype))
        if pins:
            return _dedupe(pins)
    # token stream: alternating number / name (PDF column copy)
    toks = text.split()
    i = 0
    while i + 1 < len(toks):
        if _is_pin_number(toks[i]) and not _is_pin_number(toks[i + 1]):
            pins.append(PinDef(toks[i], toks[i + 1]))
            i += 2
        else:
            i += 1
    return _dedupe(pins)


def _dedupe(pins: List[PinDef]) -> List[PinDef]:
    seen = OrderedDict()
    for p in pins:
        seen.setdefault(p.number, p)
    return list(seen.values())


HEADER_ALIASES = {
    "number": ["pin", "pin number", "pin #", "pin no", "number", "ball", "ball id", "pad", "no", "#", "pin_number"],
    "name": ["name", "pin name", "signal", "signal name", "function", "pin_name", "net"],
    "type": ["type", "pin type", "i/o", "io", "direction", "electrical type", "i/o type", "dir"],
    "group": ["bank", "group", "port", "unit", "i/o bank", "domain"],
    "side": ["side"],
}


def parse_csv(text: str) -> List[PinDef]:
    """CSV/TSV with or without header. Also reads vendor pin files (Xilinx/Intel/Lattice style)."""
    lines = [l for l in text.replace("\r", "").split("\n") if l.strip() and not l.lstrip().startswith(("#", "--"))]
    if not lines:
        return []
    head_idx = 0
    for i, l in enumerate(lines[:30]):
        low = l.lower()
        if re.search(r"\bpin\b|\bball\b", low) and re.search(r"name|signal|function", low):
            head_idx = i
            break
    body = "\n".join(lines[head_idx:])
    try:
        dialect = csv.Sniffer().sniff(body[:4000], delimiters=",;\t|")
        reader = list(csv.reader(io.StringIO(body), dialect))
    except csv.Error:
        reader = [re.split(r"\s{2,}|\t", l.strip()) for l in lines[head_idx:]]
    if reader and len(reader[0]) == 1:
        reader = [re.split(r"\s{2,}|\t|\s+", r[0].strip()) for r in reader]
    if not reader:
        return []
    header = [c.strip().lower() for c in reader[0]]
    cols: Dict[str, int] = {}
    for key, aliases in HEADER_ALIASES.items():
        for i, h in enumerate(header):
            if h in aliases and key not in cols:
                cols[key] = i
    # Xilinx: "Pin" then "Pin Name"
    if "number" not in cols and header and header[0] in ("pin", "ball"):
        cols["number"] = 0
    rows = reader[1:] if "number" in cols else reader
    if "number" not in cols:
        cols = {"number": 0, "name": 1}
        if reader and len(reader[0]) > 2 and reader[0][2].strip().lower() in TYPE_WORDS:
            cols["type"] = 2
    if "name" not in cols:
        cols["name"] = 1 if cols["number"] != 1 else 0
    pins = []
    for r in rows:
        if len(r) <= max(cols.values()):
            continue
        num = r[cols["number"]].strip()
        name = r[cols["name"]].strip()
        if not num or not name or not _is_pin_number(num) and not re.fullmatch(r"[A-Z]{0,3}\d{1,4}", num):
            continue
        t = r[cols["type"]].strip().lower() if "type" in cols else ""
        grp = r[cols["group"]].strip() if "group" in cols else ""
        if grp.upper() in ("NA", "N/A", "-"):
            grp = ""
        side = r[cols["side"]].strip().lower() if "side" in cols else ""
        pins.append(PinDef(num, name, TYPE_WORDS.get(t, ""), side if side in ("left", "right", "top", "bottom") else "",
                           grp))
    return _dedupe(pins)


# ---------------------------------------------------------------- layout

def _prefix(name: str) -> str:
    m = re.match(r"^([A-Za-z_]+?)_?(\d+)(?:_\w+)?$", name)
    if m:
        return m.group(1).upper()
    m = re.match(r"^([A-Za-z]+)", name)
    return m.group(1).upper() if m else name.upper()


def _auto_side(p: PinDef) -> str:
    if p.side:
        return p.side
    t = p.resolved_type()
    if t in ("power_in", "power_out"):
        return "bottom" if is_ground(p.name) else "top"
    if t == "input":
        return "left"
    if t in ("output", "open_collector", "tri_state"):
        return "right"
    if t == "no_connect":
        return "right"
    return ""          # bidirectional/passive: balanced later


def _text_w(name: str) -> float:
    return len(re.sub(r"[~{}]", "", name)) * 1.27 * 0.72


def layout_unit(pins: List[PinDef], name: str = "") -> S.Unit:
    sides: Dict[str, List[PinDef]] = {"left": [], "right": [], "top": [], "bottom": []}
    floating: List[PinDef] = []
    for p in pins:
        s = _auto_side(p)
        (sides[s] if s else floating).append(p)
    # keep groups (same prefix) together, balance left/right
    groups: "OrderedDict[str, List[PinDef]]" = OrderedDict()
    for p in sorted(floating, key=lambda q: _natkey(q.name)):
        groups.setdefault(_prefix(p.name), []).append(p)
    big = [g for g in groups.values() if len(g) > max(8, len(floating) // 2)]
    for g in big:            # split one dominant group across both sides
        key = next(k for k, v in groups.items() if v is g)
        half = (len(g) + 1) // 2
        groups[key] = g[:half]
        groups[key + "\u0000b"] = g[half:]
    for g in sorted(groups.values(), key=len, reverse=True):
        tgt = "left" if len(sides["left"]) <= len(sides["right"]) else "right"
        if sides[tgt]:
            sides[tgt].append(None)  # type: ignore[arg-type]   # spacer between groups
        sides[tgt].extend(g)
    for k in ("left", "right"):
        while sides[k] and sides[k][-1] is None:
            sides[k].pop()
    for k in ("top", "bottom"):
        # merge identical names (several GND pins) next to each other
        sides[k].sort(key=lambda q: (_natkey(q.name), _natkey(q.number)))
    for k in ("left", "right"):
        if not any(p is None for p in sides[k]):
            sides[k].sort(key=lambda q: _natkey(q.name))
    lw = max([_text_w(p.name) for p in sides["left"] if p] + [0])
    rw = max([_text_w(p.name) for p in sides["right"] if p] + [0])
    vt = max([_text_w(p.name) for p in sides["top"] + sides["bottom"] if p] + [0])
    width = max(lw + rw + 2.54, (max(len(sides["top"]), len(sides["bottom"])) + 1) * GRID, 7.62)
    width = GRID * 2 * round(width / (2 * GRID) + 0.49)
    nrows = max(len(sides["left"]), len(sides["right"]), 1)
    height = max((nrows + 1) * GRID + (vt + 1.27 if sides["top"] or sides["bottom"] else 0), 5.08)
    height = GRID * 2 * round(height / (2 * GRID) + 0.49)
    unit = S.Unit(name=name)
    unit.graphics.append(S.SRect(-width / 2, height / 2, width / 2, -height / 2, 0.254, "background"))
    top_y = height / 2 - GRID - (vt + 1.27 if sides["top"] else 0)
    top_y = GRID * round(top_y / GRID)

    def add(p: PinDef, x, y, rot):
        unit.pins.append(S.Pin(p.number, p.name, p.resolved_type(), round(x, 4), round(y, 4), rot, PIN_LEN))
    for i, p in enumerate(sides["left"]):
        if p:
            add(p, -width / 2 - PIN_LEN, top_y - i * GRID, 0)
    for i, p in enumerate(sides["right"]):
        if p:
            add(p, width / 2 + PIN_LEN, top_y - i * GRID, 180)
    for k, y, rot in (("top", height / 2 + PIN_LEN, 270), ("bottom", -height / 2 - PIN_LEN, 90)):
        n = len(sides[k])
        for i, p in enumerate(sides[k]):
            add(p, (i - (n - 1) / 2) * GRID, y, rot)     # on the 1.27 mm schematic grid
    _dedupe_positions(unit)
    return unit


def _dedupe_positions(unit: S.Unit) -> None:
    """Shift top/bottom pins that ended on the same grid point."""
    used = set()
    for p in unit.pins:
        key = (p.x, p.y)
        while key in used:
            p.x = round(p.x + GRID, 4)
            key = (p.x, p.y)
        used.add(key)


def split_units(pins: List[PinDef], max_per_unit: int = 0, by: str = "auto") -> List[Tuple[str, List[PinDef]]]:
    """Return [(unit name, pins)]. by: auto | group | none."""
    if by == "none" or (by == "auto" and (not max_per_unit or len(pins) <= max_per_unit)
                        and not any(p.unit for p in pins)):
        return [("", pins)]
    if any(p.unit for p in pins):
        units: "OrderedDict[int, List[PinDef]]" = OrderedDict()
        for p in pins:
            units.setdefault(p.unit or 1, []).append(p)
        return [(f"Unit {k}", v) for k, v in sorted(units.items())]
    power = [p for p in pins if p.resolved_type() in ("power_in", "power_out")]
    rest = [p for p in pins if p not in power]
    groups: "OrderedDict[str, List[PinDef]]" = OrderedDict()
    use_group = any(p.group for p in rest)
    for p in sorted(rest, key=lambda q: (_natkey(q.group), _natkey(q.name))):
        groups.setdefault((p.group if use_group else _prefix(p.name)) or "Other", []).append(p)
    out: List[Tuple[str, List[PinDef]]] = []
    cur_name, cur = "", []
    limit = max_per_unit or 60
    for g, ps in groups.items():
        if use_group:
            for i in range(0, len(ps), limit):
                out.append((f"Bank {g}" if g.isdigit() else g, ps[i:i + limit]))
            continue
        if cur and len(cur) + len(ps) > limit:
            out.append((cur_name, cur))
            cur_name, cur = "", []
        cur_name = (cur_name + "/" + g) if cur_name else g
        cur += ps
    if cur:
        out.append((cur_name, cur))
    for i in range(0, len(power), max(limit, 1)):
        out.append(("Power", power[i:i + limit]))
    return out


def build_symbol(name: str, pins: List[PinDef], reference: str = "U", max_per_unit: int = 0,
                 split: str = "auto", footprint: str = "", datasheet: str = "", description: str = "") -> S.Symbol:
    sym = S.Symbol(name=name, reference=reference, value=name, footprint=footprint, datasheet=datasheet,
                   description=description)
    for uname, ps in split_units(pins, max_per_unit, split):
        sym.units.append(layout_unit(ps, uname))
    return sym
