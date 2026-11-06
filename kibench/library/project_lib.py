"""Write imported parts into a KiCad library (project-local by default).

Layout: <dir>/<Name>.kicad_sym, <Name>.pretty/, <Name>.3dshapes/, registered in the
project's sym-lib-table and fp-lib-table (or the global tables when configured).
"""
from __future__ import annotations

import datetime as _dt
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .. import sexpr
from ..kicad import footprint as F
from ..kicad import symbol as S
from ..paths import kicad_config_dir


def _table_entry(name: str, uri: str, descr: str) -> str:
    return f'  (lib (name {sexpr.q(name)})(type "KiCad")(uri {sexpr.q(uri)})(options "")(descr {sexpr.q(descr)}))\n'


def ensure_lib_table(table: Path, kind: str, name: str, uri: str, descr: str = "KiBench imported parts") -> bool:
    """Add a library entry to sym-lib-table / fp-lib-table. Returns True if changed."""
    head = "sym_lib_table" if kind == "sym" else "fp_lib_table"
    if table.exists():
        text = table.read_text()
        try:
            root = sexpr.parse(text)
            for lib in sexpr.findall(root, "lib"):
                if str(sexpr.value(sexpr.find(lib, "name"), 1, "")) == name:
                    return False
        except ValueError:
            pass
        idx = text.rstrip().rfind(")")
        text = text[:idx].rstrip("\n") + "\n" + _table_entry(name, uri, descr) + ")\n"
    else:
        text = f"({head}\n  (version 7)\n" + _table_entry(name, uri, descr) + ")\n"
    table.parent.mkdir(parents=True, exist_ok=True)
    tmp = table.with_suffix(table.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(table)
    return True


@dataclass
class ImportRecord:
    symbol_name: str = ""
    footprint_name: str = ""
    model_file: str = ""
    lib_name: str = ""
    messages: List[str] = field(default_factory=list)

    @property
    def lib_id(self) -> str:
        return f"{self.lib_name}:{self.symbol_name}"


class KiCadLibrary:
    def __init__(self, base_dir: Path, name: str = "KiBench", project_dir: Optional[Path] = None):
        self.base = Path(base_dir)
        self.name = name
        self.project_dir = Path(project_dir) if project_dir else None

    @classmethod
    def for_settings(cls, settings, project_dir: Optional[Path]) -> "KiCadLibrary":
        if settings.use_global_library and settings.global_library_dir:
            return cls(Path(settings.global_library_dir), settings.library_name, None)
        if not project_dir:
            raise ValueError("No KiCad project open; set a global library in Settings.")
        return cls(Path(project_dir), settings.library_name, Path(project_dir))

    @property
    def sym_path(self) -> Path:
        return self.base / f"{self.name}.kicad_sym"

    @property
    def fp_dir(self) -> Path:
        return self.base / f"{self.name}.pretty"

    @property
    def model_dir(self) -> Path:
        return self.base / f"{self.name}.3dshapes"

    def _uri(self, p: Path) -> str:
        if self.project_dir:
            return "${KIPRJMOD}/" + p.relative_to(self.project_dir).as_posix()
        return p.as_posix()

    def model_uri(self, filename: str) -> str:
        return self._uri(self.model_dir / filename)

    def register(self) -> List[str]:
        msgs = []
        tdir = self.project_dir if self.project_dir else kicad_config_dir()
        if ensure_lib_table(tdir / "sym-lib-table", "sym", self.name, self._uri(self.sym_path)):
            msgs.append(f"Registered symbol library '{self.name}' in {tdir / 'sym-lib-table'}")
        if ensure_lib_table(tdir / "fp-lib-table", "fp", self.name, self._uri(self.fp_dir)):
            msgs.append(f"Registered footprint library '{self.name}' in {tdir / 'fp-lib-table'}")
        if msgs and not self.project_dir:
            msgs.append("Restart KiCad (or reopen the library manager) to see the global library.")
        return msgs

    # ------------------------------------------------------------ symbols
    def existing_symbols(self) -> List[S.LibSymbol]:
        if not self.sym_path.exists():
            return []
        return S.read_library(self.sym_path.read_text())

    def has_symbol(self, name: str) -> bool:
        return any(s.name == name for s in self.existing_symbols())

    def write_symbol_text(self, name: str, raw: str) -> None:
        """Insert or replace a top-level symbol given its raw s-expression text."""
        syms = [s for s in self.existing_symbols() if s.name != name]
        texts = ["\t" + s.raw.strip() for s in syms] + ["\t" + raw.strip()]
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.sym_path.with_suffix(".tmp")
        tmp.write_text(S.library_text(texts))
        tmp.replace(self.sym_path)

    def write_symbol(self, sym: S.Symbol) -> None:
        self.write_symbol_text(sym.name, S.symbol_to_text(sym, indent=1))

    # ------------------------------------------------------------ footprints
    def write_footprint_text(self, name: str, text: str) -> Path:
        self.fp_dir.mkdir(parents=True, exist_ok=True)
        p = self.fp_dir / f"{name}.kicad_mod"
        p.write_text(text)
        return p

    def write_footprint(self, fp: F.Footprint) -> Path:
        return self.write_footprint_text(fp.name, F.footprint_to_text(fp))

    def write_model(self, filename: str, data: bytes) -> Path:
        self.model_dir.mkdir(parents=True, exist_ok=True)
        p = self.model_dir / filename
        p.write_bytes(data)
        return p

    def copy_model(self, src: Path, filename: str = "") -> Path:
        self.model_dir.mkdir(parents=True, exist_ok=True)
        dst = self.model_dir / (filename or src.name)
        shutil.copyfile(src, dst)
        return dst


def part_fields(mpn: str = "", manufacturer: str = "", datasheet: str = "", lcsc: str = "",
                mouser: str = "", digikey: str = "", source: str = "") -> Dict[str, str]:
    f = {"MPN": mpn, "Manufacturer": manufacturer}
    if lcsc:
        f["LCSC"] = lcsc
    if mouser:
        f["Mouser"] = mouser
    if digikey:
        f["DigiKey"] = digikey
    f["Source"] = source
    f["Fetch date"] = _dt.date.today().isoformat()
    return {k: v for k, v in f.items() if v is not None}


def unique_name(name: str, taken: set) -> str:
    if name not in taken:
        return name
    i = 2
    while f"{name}_{i}" in taken:
        i += 1
    return f"{name}_{i}"


def safe_filename(s: str) -> str:
    return re.sub(r"[^\w.\-+()]", "_", s).strip("_")
