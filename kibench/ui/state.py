"""Shared application state for all panels."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QObject, Signal

from ..cache import get_cache
from ..config import Settings
from ..design.context import DesignContext, load_context
from ..kicad_ipc import KiCadBridge
from ..library.project_lib import KiCadLibrary
from ..matcher import FootprintIndex
from ..models import Part
from ..paths import downloads_dir, stock_dir
from ..search.engine import SearchEngine
from .bridge import AsyncBridge

log = logging.getLogger(__name__)


class AppState(QObject):
    part_opened = Signal(object)            # Part
    project_changed = Signal(object)        # Path | None
    library_changed = Signal()
    status = Signal(str)
    index_ready = Signal(int)

    def __init__(self, project_dir: Optional[Path] = None):
        super().__init__()
        self.settings = Settings.load()
        self.cache = get_cache()
        self.bridge = AsyncBridge(self)
        self.engine = SearchEngine(self.settings)
        self.kicad = KiCadBridge()
        self.project_dir: Optional[Path] = Path(project_dir) if project_dir else None
        self.current: Optional[Part] = None
        self.fp_index: Optional[FootprintIndex] = None
        self._ctx: Optional[DesignContext] = None
        self._ctx_mtime = 0.0

    # ---------------------------------------------------------------- project
    def set_project(self, path: Optional[Path]) -> None:
        if path and path.is_file():
            path = path.parent
        self.project_dir = path
        self._ctx = None
        self.project_changed.emit(path)

    def library(self) -> KiCadLibrary:
        return KiCadLibrary.for_settings(self.settings, self.project_dir)

    def design_context(self, refresh: bool = False) -> DesignContext:
        """Parsed lazily, refreshed when a schematic file changes on disk (save)."""
        mt = 0.0
        if self.project_dir:
            try:
                mt = max((p.stat().st_mtime for p in self.project_dir.glob("*.kicad_sch")), default=0.0)
            except OSError:
                pass
        if self._ctx is None or refresh or mt != self._ctx_mtime:
            sel: List[str] = []
            try:
                sel = self.kicad.selection_refs()
            except Exception:
                pass
            self._ctx = load_context(self.project_dir, sel)
            self._ctx_mtime = mt
        else:
            try:
                self._ctx.selection = self.kicad.selection_refs()
            except Exception:
                pass
        return self._ctx

    def downloads(self) -> Path:
        return Path(self.settings.downloads_dir) if self.settings.downloads_dir else downloads_dir()

    # ---------------------------------------------------------------- footprint index
    def start_index(self) -> None:
        root = stock_dir("footprints", self.settings.stock_footprint_dir)
        if not root:
            self.status.emit("KiCad stock footprints not found; set the folder in Settings for the matcher.")
            return
        idx = FootprintIndex(stock=root)
        if idx.ready():
            self.fp_index = idx
            self.index_ready.emit(idx.count())
            return
        self.status.emit("Indexing KiCad stock footprints (first run, ~15 s)...")
        import asyncio

        def done(n):
            self.fp_index = idx          # only expose the index once it is complete
            self.index_ready.emit(n)
            self.status.emit(f"Footprint index ready: {n} footprints")
        self.bridge.run(asyncio.to_thread(idx.build), done, lambda e: self.status.emit(f"Footprint index failed: {e}"))

    def open_part(self, part: Part) -> None:
        self.current = part
        self.part_opened.emit(part)
