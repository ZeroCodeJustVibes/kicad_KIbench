"""Deterministic import pipeline: prepare (in memory) -> verify -> commit (write files)."""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..kicad import footprint as F
from ..kicad import symbol as S
from ..models import Part, mpn_key
from . import easyeda
from ..providers.lcsc import EASYEDA_ATTRIBUTION
from .project_lib import ImportRecord, KiCadLibrary, part_fields, safe_filename, unique_name


@dataclass
class PreparedImport:
    source: str
    symbol_name: str
    symbol: Optional[S.Symbol] = None          # generated symbols
    symbol_raw: str = ""                       # raw text from vendor files
    extra_symbols_raw: List[Tuple[str, str]] = field(default_factory=list)   # parents for derived symbols
    footprint: Optional[F.Footprint] = None
    footprint_raw: str = ""
    footprint_name: str = ""
    stock_footprint: str = ""                  # "Lib:Name" when a KiCad stock footprint is assigned
    model_bytes: Optional[bytes] = None
    model_filename: str = ""
    model_offset: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    model_rotate: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    fields: Dict[str, str] = field(default_factory=dict)
    package_hint: str = ""
    warnings: List[str] = field(default_factory=list)

    # ---- views used by the verification step
    def pins(self) -> List[Tuple[str, str, str]]:
        if self.symbol:
            return [(p.number, p.name, p.etype) for p in self.symbol.pins]
        if self.symbol_raw:
            libs = S.read_library("(kicad_symbol_lib\n" + self.symbol_raw + "\n)")
            return libs[0].pins if libs else []
        return []

    def footprint_model(self) -> Optional[F.Footprint]:
        if self.footprint:
            return self.footprint
        if self.footprint_raw:
            try:
                return F.read_footprint(self.footprint_raw)
            except ValueError:
                return None
        return None

    def assign_stock_footprint(self, lib_id: str, fp: Optional[F.Footprint] = None) -> None:
        self.stock_footprint = lib_id
        self.footprint = fp
        self.footprint_raw = ""
        self.footprint_name = lib_id.split(":", 1)[-1]
        self.model_bytes = None
        self.model_filename = ""


# ---------------------------------------------------------------- LCSC / EasyEDA

def prepare_lcsc(cad: dict, part: Optional[Part] = None, step: Optional[bytes] = None) -> PreparedImport:
    sym = easyeda.convert_symbol(cad)
    fp, model = easyeda.convert_footprint(cad)
    info = easyeda.cad_fields(cad)
    mpn = (part.mpn if part else "") or info["mpn"] or sym.name
    manufacturer = (part.manufacturer if part else "") or info["manufacturer"]
    datasheet = (part.datasheet_url if part else "") or ""
    sym.datasheet = datasheet
    sym.description = (part.description if part else "") or cad.get("description", "") or ""
    sym.value = mpn
    prep = PreparedImport(source="LCSC/EasyEDA", symbol_name=sym.name, symbol=sym, footprint=fp,
                          footprint_name=fp.name, package_hint=easyeda.package_hint(cad))
    prep.fields = part_fields(
        mpn=mpn, manufacturer=manufacturer, datasheet=datasheet,
        lcsc=(part.lcsc if part else "") or info["lcsc"],
        mouser=part.sku("Mouser") if part else "", digikey=part.sku("DigiKey") if part else "",
        source=EASYEDA_ATTRIBUTION)
    if model:
        prep.model_offset, prep.model_rotate = model.offset, model.rotate
        if "[origin reset" in model.title:
            prep.warnings.append("3D model origin in the source data was invalid and was reset to the footprint center.")
        if step:
            prep.model_bytes = step
            prep.model_filename = safe_filename(fp.name) + ".step"
    return prep


# ---------------------------------------------------------------- vendor ZIP (SnapMagic / UL / SamacSys)

MODEL_EXT = (".step", ".stp", ".wrl")


def prepare_zip(path: Path, mpn: str = "", part: Optional[Part] = None, source: str = "") -> PreparedImport:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        # nested zips (SamacSys sometimes nests the KiCad package)
        for n in list(names):
            if n.lower().endswith(".zip") and "kicad" in n.lower():
                inner = zipfile.ZipFile(io.BytesIO(z.read(n)))
                return _prepare_from_members({m: inner.read(m) for m in inner.namelist()}, mpn, part, source, path)
        return _prepare_from_members({n: z.read(n) for n in names if not n.endswith("/")}, mpn, part, source, path)


def guess_zip_source(path: Path, members: List[str]) -> str:
    s = (path.name + " " + " ".join(members[:20])).lower()
    if "snapeda" in s or "snapmagic" in s:
        return "SnapMagic"
    if "ul_" in s or "ultralibrarian" in s or "/kicad/" in s and "ul" in path.name.lower():
        return "Ultra Librarian"
    if path.name.upper().startswith("LIB_") or "samacsys" in s:
        return "SamacSys"
    return "Vendor ZIP"


def _prepare_from_members(files: Dict[str, bytes], mpn: str, part: Optional[Part], source: str,
                          path: Path) -> PreparedImport:
    source = source or guess_zip_source(path, list(files))
    sym_files = [n for n in files if n.lower().endswith(".kicad_sym")]
    fp_files = [n for n in files if n.lower().endswith(".kicad_mod")]
    model_files = [n for n in files if n.lower().endswith(MODEL_EXT)]
    legacy = [n for n in files if n.lower().endswith((".lib", ".mod"))]
    prep = PreparedImport(source=source, symbol_name="")
    if not sym_files and not fp_files:
        if legacy:
            raise ValueError("This ZIP only contains KiCad 5 files (.lib/.mod). Download the KiCad 6+ format instead.")
        raise ValueError("No KiCad symbol or footprint found in the ZIP.")
    key = mpn_key(mpn)
    if sym_files:
        syms: List[S.LibSymbol] = []
        for n in sym_files:
            syms += S.read_library(files[n].decode("utf-8", "replace"))
        chosen = None
        for s in syms:
            if key and (mpn_key(s.name) == key or mpn_key(s.properties.get("Value", "")) == key
                        or any(mpn_key(v) == key for v in s.properties.values())):
                chosen = s
                break
        chosen = chosen or next((s for s in syms if not s.extends), syms[0] if syms else None)
        if chosen:
            prep.symbol_name = chosen.name
            prep.symbol_raw = chosen.raw
            if chosen.extends:
                parent = next((s for s in syms if s.name == chosen.extends), None)
                if parent:
                    prep.extra_symbols_raw.append((parent.name, parent.raw))
    if fp_files:
        # prefer a footprint named by the symbol's Footprint property
        want = ""
        if prep.symbol_raw:
            m = re.search(r'\(property\s+"Footprint"\s+"([^"]*)"', prep.symbol_raw)
            want = m.group(1).split(":")[-1] if m else ""
        fpn = next((n for n in fp_files if Path(n).stem == want), fp_files[0])
        prep.footprint_raw = files[fpn].decode("utf-8", "replace")
        prep.footprint_name = Path(fpn).stem
        if len(fp_files) > 1:
            prep.warnings.append(f"ZIP has {len(fp_files)} footprints; using {prep.footprint_name}.")
    if model_files:
        mf = next((n for n in model_files if n.lower().endswith((".step", ".stp"))), model_files[0])
        prep.model_bytes = files[mf]
        prep.model_filename = safe_filename(Path(mf).name)
    if not prep.symbol_name:
        prep.symbol_name = mpn or prep.footprint_name
        prep.warnings.append("ZIP has no symbol; use the symbol builder or a generic symbol.")
    prep.fields = part_fields(
        mpn=(part.mpn if part else mpn), manufacturer=part.manufacturer if part else "",
        datasheet=part.datasheet_url if part else "", lcsc=part.lcsc if part else "",
        mouser=part.sku("Mouser") if part else "", digikey=part.sku("DigiKey") if part else "",
        source=f"{source} ({path.name})")
    return prep


# ---------------------------------------------------------------- commit

def commit(prep: PreparedImport, lib: KiCadLibrary, replace_same_mpn: bool = True) -> ImportRecord:
    rec = ImportRecord(lib_name=lib.name)
    rec.messages += lib.register()
    existing = {s.name: s for s in lib.existing_symbols()}
    name = prep.symbol_name
    if name in existing:
        same = mpn_key(existing[name].properties.get("MPN", "")) == mpn_key(prep.fields.get("MPN", ""))
        if not (replace_same_mpn and same):
            name = unique_name(name, set(existing))
    rec.symbol_name = name

    # footprint + 3D model
    fp_lib_id = ""
    if prep.stock_footprint:
        fp_lib_id = prep.stock_footprint
        rec.footprint_name = prep.stock_footprint
    elif prep.footprint or prep.footprint_raw:
        fpname = safe_filename(prep.footprint_name or name)
        model_uri = ""
        if prep.model_bytes and prep.model_filename:
            lib.write_model(prep.model_filename, prep.model_bytes)
            model_uri = lib.model_uri(prep.model_filename)
            rec.model_file = str(lib.model_dir / prep.model_filename)
        if prep.footprint is not None:
            fp = prep.footprint
            fp.name = fpname
            if model_uri:
                fp.models = [F.Model(model_uri, prep.model_offset, prep.model_rotate)]
            lib.write_footprint(fp)
        else:
            text = F.rename_footprint_raw(prep.footprint_raw, fpname)
            if model_uri:
                text = F.set_model_path(text, model_uri)
            lib.write_footprint_text(fpname, text)
        fp_lib_id = f"{lib.name}:{fpname}"
        rec.footprint_name = fp_lib_id

    props = dict(prep.fields)
    props["Footprint"] = fp_lib_id
    if prep.fields.get("MPN"):
        props["Value"] = prep.fields["MPN"]
    if prep.symbol is not None:
        sym = prep.symbol
        sym.name = name
        sym.footprint = fp_lib_id
        sym.value = props.get("Value", sym.value)
        sym.fields = {k: v for k, v in props.items() if k not in ("Footprint", "Value")}
        lib.write_symbol(sym)
    elif prep.symbol_raw:
        for pname, praw in prep.extra_symbols_raw:
            if pname not in existing:
                lib.write_symbol_text(pname, praw)
        raw = S.rename_symbol_raw(prep.symbol_raw, prep.symbol_name, name)
        if prep.fields.get("Datasheet"):
            props["Datasheet"] = prep.fields["Datasheet"]
        lib.write_symbol_text(name, S.set_properties_raw(raw, props))
    rec.messages.append(f"Wrote {lib.name}:{name}" + (f" with footprint {fp_lib_id}" if fp_lib_id else ""))
    return rec
