"""'Import & place': a schematic clipboard snippet for one symbol.

KiCad's schematic editor pastes its own s-expression clipboard format (lib_symbols + items, no file
header). Pasting it with Ctrl+V attaches the symbol to the cursor, so the user places it with a click.
The KiCad 10 IPC API cannot start an interactive symbol placement, so this is the placement path.
"""
from __future__ import annotations

import re
import uuid
from typing import List

from .. import sexpr
from .symbol import read_library


def _node_text(text: str, node) -> str:
    s, e = node.span
    return text[s:e]


def snippet(lib_text: str, name: str, nickname: str) -> str:
    """Clipboard text that places `nickname:name` (from the library file text `lib_text`)."""
    libs = {ls.name: ls for ls in read_library(lib_text)}
    if name not in libs:
        raise ValueError(f"{name} not found in the library")
    ls = libs[name]
    if ls.extends:
        raise ValueError(f"{name} is a derived symbol ({ls.extends}); place it from KiCad's Add Symbol dialog")
    raw = ls.raw
    # schematics embed the definition as "Lib:Name"; unit sub-symbols keep the bare "Name_u_b"
    embedded = raw.replace(f'(symbol "{name}"', f'(symbol "{nickname}:{name}"', 1)

    node = sexpr.parse(raw)
    props: List[str] = []
    ref = "U"
    for p in sexpr.findall(node, "property"):
        key, val = str(p[1]), str(p[2])
        at = sexpr.find(p, "at")
        x, y, rot = (float(at[1]), float(at[2]), float(at[3]) if len(at) > 3 else 0.0) if at else (0.0, 0.0, 0.0)
        eff = sexpr.find(p, "effects")
        eff_txt = _node_text(raw, eff) if eff is not None else "(effects (font (size 1.27 1.27)))"
        if key == "Reference":
            ref = val.rstrip("?") or "U"
            val = ref + "?"
        props.append(f'\t\t(property {sexpr.q(key)} {sexpr.q(val)}\n\t\t\t(at {x:g} {(-y) or 0.0:g} {rot:g})\n\t\t\t{eff_txt}\n\t\t)')
    pins = sorted({str(sexpr.value(sexpr.find(p, "number"), 1, "")) for sub in sexpr.findall(node, "symbol")
                   for p in sexpr.findall(sub, "pin")} - {""}, key=lambda s: [int(t) if t.isdigit() else t
                                                                         for t in re.split(r"(\d+)", s)])
    pin_txt = "\n".join(f'\t\t(pin {sexpr.q(n)}\n\t\t\t(uuid "{uuid.uuid4()}")\n\t\t)' for n in pins)
    inst = (f'\t(symbol\n\t\t(lib_id {sexpr.q(nickname + ":" + name)})\n\t\t(at 0 0 0)\n\t\t(unit 1)\n'
            f'\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(dnp no)\n'
            f'\t\t(uuid "{uuid.uuid4()}")\n' + "\n".join(props) + "\n" + pin_txt + "\n\t)")
    return f"(lib_symbols\n{embedded}\n)\n{inst}\n"
