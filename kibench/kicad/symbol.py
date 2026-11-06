"""KiCad symbol model, `.kicad_sym` writer and a light reader.

Coordinates are in mm with Y pointing up (KiCad symbol-library convention).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .. import sexpr
from ..sexpr import num, q

PIN_TYPES = ["input", "output", "bidirectional", "tri_state", "passive", "free", "unspecified",
             "power_in", "power_out", "open_collector", "open_emitter", "no_connect"]
SYM_LIB_VERSION = "20241209"     # KiCad 9 format; KiCad 10 reads and upgrades it


@dataclass
class Pin:
    number: str
    name: str
    etype: str = "passive"
    x: float = 0.0
    y: float = 0.0
    rot: int = 0                # direction from connection point toward the body
    length: float = 2.54
    shape: str = "line"
    hidden: bool = False


@dataclass
class SRect:
    x1: float
    y1: float
    x2: float
    y2: float
    width: float = 0.254
    fill: str = "background"


@dataclass
class SPoly:
    pts: List[Tuple[float, float]]
    width: float = 0.254
    fill: str = "none"


@dataclass
class SCircle:
    cx: float
    cy: float
    r: float
    width: float = 0.254
    fill: str = "none"


@dataclass
class SArc:
    start: Tuple[float, float]
    mid: Tuple[float, float]
    end: Tuple[float, float]
    width: float = 0.254


@dataclass
class SText:
    text: str
    x: float
    y: float
    size: float = 1.27


@dataclass
class Unit:
    pins: List[Pin] = field(default_factory=list)
    graphics: list = field(default_factory=list)
    name: str = ""


@dataclass
class Symbol:
    name: str
    reference: str = "U"
    value: str = ""
    footprint: str = ""
    datasheet: str = ""
    description: str = ""
    keywords: str = ""
    fp_filters: str = ""
    fields: Dict[str, str] = field(default_factory=dict)
    units: List[Unit] = field(default_factory=list)
    common: List = field(default_factory=list)        # graphics shared by all units
    pin_name_offset: float = 0.508
    hide_pin_numbers: bool = False
    hide_pin_names: bool = False
    power: bool = False

    @property
    def pins(self) -> List[Pin]:
        return [p for u in self.units for p in u.pins]

    def bbox(self) -> Tuple[float, float, float, float]:
        xs, ys = [], []
        for u in self.units:
            for g in u.graphics:
                for x, y in _graphic_points(g):
                    xs.append(x)
                    ys.append(y)
            for p in u.pins:
                xs.append(p.x)
                ys.append(p.y)
        if not xs:
            return (-2.54, -2.54, 2.54, 2.54)
        return (min(xs), min(ys), max(xs), max(ys))


def _graphic_points(g) -> List[Tuple[float, float]]:
    if isinstance(g, SRect):
        return [(g.x1, g.y1), (g.x2, g.y2)]
    if isinstance(g, SPoly):
        return list(g.pts)
    if isinstance(g, SCircle):
        return [(g.cx - g.r, g.cy - g.r), (g.cx + g.r, g.cy + g.r)]
    if isinstance(g, SArc):
        return [g.start, g.mid, g.end]
    if isinstance(g, SText):
        return [(g.x, g.y)]
    return []


# ------------------------------------------------------------------ writer

def _effects(size: float = 1.27, hide: bool = False, justify: str = "") -> str:
    s = f"(effects (font (size {num(size)} {num(size)}))"
    if justify:
        s += f" (justify {justify})"
    if hide:
        s += " (hide yes)"
    return s + ")"


def _stroke_fill(width: float, fill: str) -> str:
    return f"(stroke (width {num(width)}) (type default)) (fill (type {fill}))"


def _graphic(g, ind: str) -> str:
    if isinstance(g, SRect):
        return (f"{ind}(rectangle (start {num(g.x1)} {num(g.y1)}) (end {num(g.x2)} {num(g.y2)}) "
                f"{_stroke_fill(g.width, g.fill)})\n")
    if isinstance(g, SPoly):
        pts = " ".join(f"(xy {num(x)} {num(y)})" for x, y in g.pts)
        return f"{ind}(polyline (pts {pts}) {_stroke_fill(g.width, g.fill)})\n"
    if isinstance(g, SCircle):
        return (f"{ind}(circle (center {num(g.cx)} {num(g.cy)}) (radius {num(g.r)}) "
                f"{_stroke_fill(g.width, g.fill)})\n")
    if isinstance(g, SArc):
        return (f"{ind}(arc (start {num(g.start[0])} {num(g.start[1])}) (mid {num(g.mid[0])} {num(g.mid[1])}) "
                f"(end {num(g.end[0])} {num(g.end[1])}) {_stroke_fill(g.width, 'none')})\n")
    if isinstance(g, SText):
        return f"{ind}(text {q(g.text)} (at {num(g.x)} {num(g.y)} 0) {_effects(g.size)})\n"
    return ""


def _pin(p: Pin, ind: str) -> str:
    hide = " (hide yes)" if p.hidden else ""
    return (f"{ind}(pin {p.etype} {p.shape} (at {num(p.x)} {num(p.y)} {p.rot}) (length {num(p.length)}){hide}\n"
            f"{ind}\t(name {q(p.name)} {_effects()})\n"
            f"{ind}\t(number {q(p.number)} {_effects()})\n"
            f"{ind})\n")


def symbol_to_text(s: Symbol, indent: int = 1) -> str:
    i0 = "\t" * indent
    i1 = i0 + "\t"
    i2 = i1 + "\t"
    out = f"{i0}(symbol {q(s.name)}\n"
    if s.power:
        out += f"{i1}(power)\n"
    if s.hide_pin_numbers:
        out += f"{i1}(pin_numbers (hide yes))\n"
    out += f"{i1}(pin_names (offset {num(s.pin_name_offset)})" + (" (hide yes)" if s.hide_pin_names else "") + ")\n"
    out += f"{i1}(exclude_from_sim no)\n{i1}(in_bom yes)\n{i1}(on_board yes)\n"
    x1, y1, x2, y2 = s.bbox()
    props = [("Reference", s.reference, (x1, y2 + 1.27), False),
             ("Value", s.value or s.name, (x1, y1 - 1.27), False),
             ("Footprint", s.footprint, (0, y1 - 3.81), True),
             ("Datasheet", s.datasheet, (0, y1 - 6.35), True),
             ("Description", s.description, (0, 0), True)]
    if s.keywords:
        props.append(("ki_keywords", s.keywords, (0, 0), True))
    if s.fp_filters:
        props.append(("ki_fp_filters", s.fp_filters, (0, 0), True))
    for k, v in s.fields.items():
        props.append((k, v, (0, 0), True))
    for name, val, (px, py), hide in props:
        eff = _effects(hide=hide, justify="left" if name in ("Reference", "Value") else "")
        out += f"{i1}(property {q(name)} {q(val)} (at {num(px)} {num(py)} 0) {eff})\n"
    if s.common:
        out += f"{i1}(symbol {q(s.name + '_0_1')}\n"
        for g in s.common:
            out += _graphic(g, i2)
        out += f"{i1})\n"
    for n, u in enumerate(s.units, start=1):
        out += f"{i1}(symbol {q(f'{s.name}_{n}_1')}\n"
        if u.name:
            out += f"{i2}(unit_name {q(u.name)})\n"
        for g in u.graphics:
            out += _graphic(g, i2)
        for p in u.pins:
            out += _pin(p, i2)
        out += f"{i1})\n"
    out += f"{i0})\n"
    return out


def library_text(symbol_texts: List[str]) -> str:
    body = "".join(t if t.endswith("\n") else t + "\n" for t in symbol_texts)
    return (f"(kicad_symbol_lib\n\t(version {SYM_LIB_VERSION})\n\t(generator \"kibench\")\n"
            f"\t(generator_version \"0.1\")\n{body})\n")


# ------------------------------------------------------------------ reader

@dataclass
class LibSymbol:
    name: str
    raw: str
    properties: Dict[str, str]
    pins: List[Tuple[str, str, str]]          # (number, name, type)
    extends: str = ""
    units: int = 1


def read_library(text: str) -> List[LibSymbol]:
    """Parse a .kicad_sym file into top-level symbols (raw text preserved)."""
    root = sexpr.parse(text)
    out = []
    for node in sexpr.findall(root, "symbol"):
        s, e = node.span
        raw = text[s:e]
        pins = []
        units = set()
        for sub in sexpr.findall(node, "symbol"):
            nm = str(sub[1])
            parts = nm.rsplit("_", 2)
            if len(parts) == 3 and parts[1].isdigit() and parts[1] != "0":
                units.add(parts[1])
            for p in sexpr.findall(sub, "pin"):
                number = str(sexpr.value(sexpr.find(p, "number"), 1, ""))
                pname = str(sexpr.value(sexpr.find(p, "name"), 1, ""))
                pins.append((number, pname, str(p[1])))
        ext = sexpr.find(node, "extends")
        out.append(LibSymbol(str(node[1]), raw, sexpr.prop_map(node), pins,
                             str(ext[1]) if ext else "", max(1, len(units))))
    return out


def rename_symbol_raw(raw: str, old: str, new: str) -> str:
    """Rename a top-level symbol and its unit sub-symbols in raw text."""
    raw = raw.replace(f'(symbol "{old}"', f'(symbol "{new}"', 1)
    return raw.replace(f'(symbol "{old}_', f'(symbol "{new}_')


def set_properties_raw(raw: str, props: Dict[str, str]) -> str:
    """Set/insert properties on a raw top-level symbol, returning new raw text."""
    node = sexpr.parse(raw)
    base = node.span[0]
    existing = {str(p[1]): p for p in sexpr.findall(node, "property")}
    out = raw
    # Replace values right-to-left so offsets stay valid.
    edits = []
    for k, v in props.items():
        if k in existing:
            p = existing[k]
            s, e = p.span
            seg = raw[s - base:e - base]
            # value is the second quoted string in the segment
            start_name = seg.index('"')
            end_name = _end_of_string(seg, start_name)
            start_val = seg.index('"', end_name + 1)
            end_val = _end_of_string(seg, start_val)
            edits.append((s - base + start_val, s - base + end_val + 1, q(v)))
    for s, e, rep in sorted(edits, reverse=True):
        out = out[:s] + rep + out[e:]
    missing = [k for k in props if k not in existing]
    if missing:
        ins = "".join(f'\t\t(property {q(k)} {q(props[k])} (at 0 0 0) {_effects(hide=True)})\n' for k in missing)
        # insert after the last property
        node2 = sexpr.parse(out)
        last = sexpr.findall(node2, "property")
        if last:
            pos = last[-1].span[1] - node2.span[0]
            out = out[:pos] + "\n" + ins.rstrip("\n") + out[pos:]
        else:
            pos = out.index("\n") + 1
            out = out[:pos] + ins + out[pos:]
    return out


def _end_of_string(s: str, start: int) -> int:
    i = start + 1
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == '"':
            return i
        i += 1
    return len(s) - 1
