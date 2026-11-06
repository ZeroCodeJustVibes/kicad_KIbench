"""SQLite index of KiCad stock footprints, keyed by parsed package dimensions."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import List, Optional

from ..kicad.footprint import read_footprint
from ..library.verify import estimate_pitch
from ..paths import cache_dir, stock_dir
from .parse import PackageSpec, parse_kicad_name

SCHEMA = """
CREATE TABLE IF NOT EXISTS fp (
  lib TEXT, name TEXT, family TEXT, grp TEXT, pins INTEGER, body_x REAL, body_y REAL,
  pitch REAL, pitch2 REAL, ep_x REAL, ep_y REAL, has_ep INTEGER, rows INTEGER, cols INTEGER,
  lead_span REAL, chip TEXT, variants TEXT, path TEXT, PRIMARY KEY (lib, name));
CREATE INDEX IF NOT EXISTS fp_grp ON fp (grp, pins);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""
# Libraries that only hold logos, mounting holes, test points etc. are skipped.
SKIP_LIBS = ("Symbol", "MountingHole", "TestPoint", "Fiducial", "Jumper", "NetTie", "Mounting_Wuerth",
             "Kicad_Logos", "Footprint_Wizard")


class FootprintIndex:
    def __init__(self, db_path: Optional[Path] = None, stock: Optional[Path] = None):
        self.db_path = Path(db_path or cache_dir() / "footprint_index.sqlite")
        self.stock = stock
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.db.executescript(SCHEMA)

    def _stamp(self, root: Path) -> str:
        libs = sorted(root.glob("*.pretty"))
        return f"{root}|{len(libs)}|{max((p.stat().st_mtime_ns for p in libs), default=0)}"

    def ready(self) -> bool:
        root = self.stock or stock_dir("footprints")
        if not root:
            return False
        row = self.db.execute("SELECT v FROM meta WHERE k='stamp'").fetchone()
        return bool(row and row[0] == self._stamp(root))

    def build(self, progress=None, force: bool = False) -> int:
        root = self.stock or stock_dir("footprints")
        if not root:
            raise FileNotFoundError("KiCad stock footprint folder not found (set it in Settings).")
        if not force and self.ready():
            return self.count()
        rows = []
        libs = sorted(root.glob("*.pretty"))
        for i, lib in enumerate(libs):
            if lib.stem.startswith(SKIP_LIBS):
                continue
            for f in lib.glob("*.kicad_mod"):
                spec = parse_kicad_name(f.stem)
                if spec.pins is None or (spec.group not in ("CHIP", "HEADER")
                                         and (spec.pitch is None or spec.body_x is None)):
                    spec = _fill_from_file(f, spec)
                r = spec.to_row()
                rows.append((lib.stem, f.stem, r["family"], r["group"], r["pins"], r["body_x"], r["body_y"],
                             r["pitch"], r["pitch2"], r["ep_x"], r["ep_y"], int(r["has_ep"]), r["rows"], r["cols"],
                             r["lead_span"], r["chip"], r["variants"], str(f)))
            if progress:
                progress(i + 1, len(libs))
        with self._lock:
            self.db.execute("DELETE FROM fp")
            self.db.executemany("INSERT OR REPLACE INTO fp VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('stamp', ?)", (self._stamp(root),))
            self.db.commit()
        return len(rows)

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM fp").fetchone()[0]

    def candidates(self, group: str, pins: Optional[int]) -> List[dict]:
        cur = self.db.cursor()
        if pins:
            cur.execute("SELECT * FROM fp WHERE grp=? AND pins=?", (group, pins))
        else:
            cur.execute("SELECT * FROM fp WHERE grp=?", (group,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def path_of(self, lib_id: str) -> Optional[Path]:
        lib, _, name = lib_id.partition(":")
        r = self.db.execute("SELECT path FROM fp WHERE lib=? AND name=?", (lib, name)).fetchone()
        return Path(r[0]) if r else None


def _fill_from_file(f: Path, spec: PackageSpec) -> PackageSpec:
    try:
        fp = read_footprint(f.read_text())
    except (OSError, ValueError, IndexError):
        return spec
    nums = {p.number for p in fp.pads if p.number and p.kind != "np_thru_hole"}
    if spec.pins is None and nums:
        spec.pins = len(nums)
    if spec.pitch is None:
        spec.pitch = estimate_pitch(fp)
    if spec.body_x is None:
        bb = fp.layer_bbox(("F.Fab",))
        if bb:
            spec.body_x = round(round((bb[2] - bb[0]) / 0.05) * 0.05, 2)
            spec.body_y = round(round((bb[3] - bb[1]) / 0.05) * 0.05, 2)
    return spec
