"""kicad-cli wrapper: ERC report, netlist export and SVG plots of schematics."""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .. import sexpr


class CliError(RuntimeError):
    pass


def find_kicad_cli() -> Optional[str]:
    """$KIBENCH_KICAD_CLI, PATH, the running AppImage ($APPDIR), mounted KiCad AppImages, standard installs."""
    env = os.environ.get("KIBENCH_KICAD_CLI")
    if env and Path(env).exists():
        return env
    exe = "kicad-cli.exe" if sys.platform == "win32" else "kicad-cli"
    hit = shutil.which(exe)
    if hit:
        return hit
    cands: List[str] = []
    if os.environ.get("APPDIR"):
        cands.append(str(Path(os.environ["APPDIR"]) / "usr/bin" / exe))
        cands.append(str(Path(os.environ["APPDIR"]) / "bin" / exe))
    cands += sorted(glob.glob("/tmp/.mount_kicad*/bin/kicad-cli")) + sorted(glob.glob("/tmp/.mount_kicad*/usr/bin/kicad-cli"))
    cands += ["/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli",
              "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
              r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"]
    for c in cands:
        if Path(c).exists():
            return c
    return None


def _run(args: List[str], timeout: int = 120) -> str:
    cli = find_kicad_cli()
    if not cli:
        raise CliError("kicad-cli not found (set KIBENCH_KICAD_CLI to its path)")
    try:
        r = subprocess.run([cli] + args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CliError(f"kicad-cli failed: {e}") from e
    if r.returncode not in (0, 5):          # 5: ERC/DRC found violations (with --exit-code-violations)
        raise CliError((r.stderr or r.stdout or f"kicad-cli exit {r.returncode}").strip()[-400:])
    return r.stdout


# ---------------------------------------------------------------------- ERC
@dataclass
class ErcViolation:
    type: str
    severity: str            # error | warning | exclusion
    description: str
    items: List[dict]        # {"description", "pos": {"x","y"}, "uuid"}
    sheet: str = "/"


def run_erc(sch: Path) -> List[ErcViolation]:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "erc.json"
        _run(["sch", "erc", "--format", "json", "--severity-all", "-o", str(out), str(sch)])
        d = json.loads(out.read_text())
    res = []
    for s in d.get("sheets", []):
        for v in s.get("violations", []):
            res.append(ErcViolation(v.get("type", ""), v.get("severity", "warning"), v.get("description", ""),
                                    v.get("items", []), s.get("path", "/")))
    return res


# ---------------------------------------------------------------------- netlist
@dataclass
class NetNode:
    ref: str
    pin: str
    function: str = ""
    type: str = ""


@dataclass
class Component:
    ref: str
    value: str = ""
    footprint: str = ""
    lib: str = ""
    part: str = ""
    description: str = ""
    fields: Dict[str, str] = field(default_factory=dict)
    sheet: str = ""


@dataclass
class Netlist:
    components: Dict[str, Component] = field(default_factory=dict)
    nets: Dict[str, List[NetNode]] = field(default_factory=dict)

    def partition(self) -> set:
        """Connectivity as a set of frozensets of 'REF:PIN' (net names ignored), for before/after comparison."""
        return {frozenset(f"{n.ref}:{n.pin}" for n in nodes) for nodes in self.nets.values()}


def parse_netlist(text: str) -> Netlist:
    root = sexpr.parse(text)
    nl = Netlist()
    comps = sexpr.find(root, "components")
    for c in (sexpr.findall(comps, "comp") if comps is not None else []):
        ref = str(sexpr.value(sexpr.find(c, "ref"), 1, ""))
        comp = Component(ref, str(sexpr.value(sexpr.find(c, "value"), 1, "")),
                         str(sexpr.value(sexpr.find(c, "footprint"), 1, "")))
        ls = sexpr.find(c, "libsource")
        if ls is not None:
            comp.lib = str(sexpr.value(sexpr.find(ls, "lib"), 1, ""))
            comp.part = str(sexpr.value(sexpr.find(ls, "part"), 1, ""))
            comp.description = str(sexpr.value(sexpr.find(ls, "description"), 1, ""))
        flds = sexpr.find(c, "fields")
        for f in (sexpr.findall(flds, "field") if flds is not None else []):
            name = str(sexpr.value(sexpr.find(f, "name"), 1, ""))
            val = next((str(x) for x in f[1:] if not isinstance(x, list)), "")
            if name:
                comp.fields[name] = val
        for p in sexpr.findall(c, "property"):
            name = str(sexpr.value(sexpr.find(p, "name"), 1, ""))
            if name == "Sheetfile":
                comp.sheet = str(sexpr.value(sexpr.find(p, "value"), 1, ""))
        nl.components[ref] = comp
    nets = sexpr.find(root, "nets")
    for n in (sexpr.findall(nets, "net") if nets is not None else []):
        name = str(sexpr.value(sexpr.find(n, "name"), 1, ""))
        nodes = []
        for nd in sexpr.findall(n, "node"):
            nodes.append(NetNode(str(sexpr.value(sexpr.find(nd, "ref"), 1, "")),
                                 str(sexpr.value(sexpr.find(nd, "pin"), 1, "")),
                                 str(sexpr.value(sexpr.find(nd, "pinfunction"), 1, "")),
                                 str(sexpr.value(sexpr.find(nd, "pintype"), 1, ""))))
        nl.nets[name] = nodes
    return nl


def export_netlist(sch: Path) -> Netlist:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "n.net"
        _run(["sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(out), str(sch)])
        return parse_netlist(out.read_text(errors="replace"))


def netlist_of_text(sch_text: str, name: str, project_dir: Optional[Path] = None) -> Netlist:
    """Netlist of a schematic given as text (e.g. a cleaned version), exported from a temp copy.
    The project file is copied along so text variables and settings resolve the same way."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / name
        p.write_text(sch_text)
        if project_dir:
            for pro in Path(project_dir).glob("*.kicad_pro"):
                shutil.copy(pro, Path(td) / (Path(name).stem + ".kicad_pro"))
                break
        return export_netlist(p)


def export_svg(sch_text: str, name: str) -> bytes:
    """SVG plot (mm viewBox, no drawing sheet) of the first page of a schematic given as text."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / name
        p.write_text(sch_text)
        _run(["sch", "export", "svg", "-e", "-n", "--pages", "1", "-o", td, str(p)])
        svgs = sorted(Path(td).glob("*.svg"))
        if not svgs:
            raise CliError("kicad-cli wrote no SVG")
        return svgs[0].read_bytes()


# ---------------------------------------------------------------------- DRC / board plots
@dataclass
class DrcViolation:
    type: str
    severity: str            # error | warning | exclusion
    description: str
    items: List[dict]        # {"description", "pos": {"x","y"}, "uuid"}
    group: str = "drc"       # drc | unconnected | parity


def _board_env(pcb: Path, td: Path, text: Optional[str] = None) -> Path:
    """Copy a board (or the given board text) with its project and rule files into `td`."""
    pcb = Path(pcb)
    dst = td / pcb.name
    if text is None:
        shutil.copy(pcb, dst)
    else:
        dst.write_text(text)
    for ext in (".kicad_pro", ".kicad_dru"):
        src = pcb.with_suffix(ext)
        if src.exists():
            shutil.copy(src, dst.with_suffix(ext))
    return dst


def run_drc(pcb: Path, parity: bool = True, text: Optional[str] = None, refill: bool = False) -> List[DrcViolation]:
    """DRC of a board file (or of `text`, run in a temp copy next to the board's project/rule files).
    With `parity` and no `text`, the board is checked in place so the schematic is found."""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "drc.json"
        src = Path(pcb) if text is None else _board_env(Path(pcb), Path(td), text)
        args = ["pcb", "drc", "--format", "json", "--severity-all", "-o", str(out)]
        if parity and text is None:
            args.append("--schematic-parity")
        if refill:
            args.append("--refill-zones")
        _run(args + [str(src)], timeout=600)
        d = json.loads(out.read_text())
    res = []
    for grp, key in (("drc", "violations"), ("unconnected", "unconnected_items"), ("parity", "schematic_parity")):
        for v in d.get(key, []):
            res.append(DrcViolation(v.get("type", ""), v.get("severity", "warning"), v.get("description", ""),
                                    v.get("items", []), grp))
    return res


def export_board_svg(pcb_text: str, name: str, layers: str = "B.Cu,B.SilkS,F.Cu,F.SilkS,Edge.Cuts") -> bytes:
    """SVG plot (page mm viewBox, board coordinates) of a board given as text."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / name
        p.write_text(pcb_text)
        out = Path(td) / "board.svg"
        _run(["pcb", "export", "svg", "--mode-single", "--exclude-drawing-sheet", "--drill-shape-opt", "1",
              "-l", layers, "-o", str(out), str(p)])
        if not out.exists():
            raise CliError("kicad-cli wrote no SVG")
        return out.read_bytes()
