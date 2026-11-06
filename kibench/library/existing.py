"""Find parts already present in project and global symbol libraries (by MPN / LCSC field)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .. import sexpr
from ..cache import get_cache
from ..models import mpn_key
from ..paths import expand_kicad_vars, kicad_config_dir, stock_dir

MPN_FIELDS = ("MPN", "Manufacturer Part", "Manufacturer_Part_Number", "MFR Part", "Mfr. No", "PartNumber",
              "Part Number", "MANUFACTURER_PART_NUMBER", "Value")
LCSC_FIELDS = ("LCSC", "LCSC Part", "LCSC Part #", "JLCPCB Part", "Supplier Part", "JLC")


@dataclass
class LibHit:
    lib: str
    symbol: str
    footprint: str
    matched: str          # which field matched
    path: str


def _read_table(path: Path, project_dir: Optional[Path], depth: int = 0) -> List[Tuple[str, Path]]:
    out = []
    if not path.exists() or depth > 3:
        return out
    try:
        root = sexpr.parse(path.read_text())
    except (OSError, ValueError):
        return out
    for lib in sexpr.findall(root, "lib"):
        name = str(sexpr.value(sexpr.find(lib, "name"), 1, ""))
        uri = expand_kicad_vars(str(sexpr.value(sexpr.find(lib, "uri"), 1, "")), project_dir)
        typ = str(sexpr.value(sexpr.find(lib, "type"), 1, ""))
        if typ == "Table":
            out += _read_table(Path(uri), project_dir, depth + 1)
        elif uri:
            out.append((name, Path(uri)))
    return out


def symbol_libraries(project_dir: Optional[Path]) -> List[Tuple[str, Path]]:
    libs = []
    if project_dir:
        libs += _read_table(Path(project_dir) / "sym-lib-table", Path(project_dir))
    libs += _read_table(kicad_config_dir() / "sym-lib-table", project_dir)
    return libs


def footprint_libraries(project_dir: Optional[Path]) -> List[Tuple[str, Path]]:
    libs = []
    if project_dir:
        libs += _read_table(Path(project_dir) / "fp-lib-table", Path(project_dir))
    libs += _read_table(kicad_config_dir() / "fp-lib-table", project_dir)
    return libs


def footprint_file(lib_id: str, project_dir: Optional[Path]) -> Optional[Path]:
    """Resolve 'Lib:Name' through the project and global fp-lib-tables to its .kicad_mod file."""
    if ":" not in lib_id:
        return None
    lib, name = lib_id.split(":", 1)
    for nick, path in footprint_libraries(project_dir):
        if nick == lib:
            f = Path(path) / f"{name}.kicad_mod"
            if f.exists():
                return f
    return None


def _lib_files(path: Path) -> Iterable[Path]:
    if path.is_dir():                      # KiCad 10 .kicad_symdir
        yield from sorted(path.glob("*.kicad_sym"))
    elif path.exists():
        yield path


_PROP_RE = re.compile(r'\(property\s+"((?:[^"\\]|\\.)*)"\s+"((?:[^"\\]|\\.)*)"')
_SYM_RE = re.compile(r'^\s*\(symbol\s+"((?:[^"\\]|\\.)*)"', re.M)


def _index_file(f: Path) -> List[dict]:
    """Fast regex scan: [{'name', 'props'}] for each top-level symbol."""
    text = f.read_text(errors="replace")
    entries = []
    starts = [(m.start(), m.group(1)) for m in _SYM_RE.finditer(text)]
    # top-level symbols are indented one level; unit sub-symbols are named NAME_<u>_<b>
    names = [(pos, n) for pos, n in starts if not re.search(r"_\d+_\d+$", n)]
    for i, (pos, name) in enumerate(names):
        end = names[i + 1][0] if i + 1 < len(names) else len(text)
        props = dict(_PROP_RE.findall(text[pos:end]))
        entries.append({"name": name, "props": props})
    return entries


def find_in_libraries(mpn: str, lcsc: str, project_dir: Optional[Path]) -> List[LibHit]:
    cache = get_cache()
    key = mpn_key(mpn)
    hits: List[LibHit] = []
    stock = stock_dir("symbols")
    for lib, path in symbol_libraries(project_dir):
        # KiCad's stock symbols carry no MPN/LCSC fields; scanning their ~23k files takes minutes.
        if stock and _is_within(path, stock):
            continue
        files = list(_lib_files(path))
        try:
            stamp = max((f.stat().st_mtime_ns for f in files), default=0)
        except OSError:
            continue
        ck = f"{path}|{len(files)}|{stamp}"
        idx = cache.get("libidx2", ck, ttl=None)        # one cache entry per library
        if idx is None:
            idx = []
            for f in files:
                try:
                    idx += [dict(e, file=str(f)) for e in _index_file(f)]
                except OSError:
                    continue
            cache.put("libidx2", ck, idx)
        for e in idx:
            matched = _match(e, key, lcsc)
            if matched:
                hits.append(LibHit(lib, e["name"], e["props"].get("Footprint", ""), matched,
                                   e.get("file", str(path))))
    return hits


def _match(entry: dict, key: str, lcsc: str) -> str:
    props: Dict[str, str] = entry["props"]
    if lcsc:
        for fld in LCSC_FIELDS:
            if props.get(fld, "").upper() == lcsc.upper():
                return fld
    if key:
        for fld in MPN_FIELDS:
            v = props.get(fld, "")
            if v and mpn_key(v) == key and (fld != "Value" or len(key) > 5):
                return fld
        if mpn_key(entry["name"]) == key:
            return "name"
    return ""


def _is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False
