"""Middle column: selected part header and the workflow tabs (Datasheet, Sources, Footprint, Build)."""
from __future__ import annotations

import html
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..builder import symbols as SB
from ..kicad.footprint import read_footprint
from ..library import importer
from ..library.importer import PreparedImport
from ..library.project_lib import part_fields
from ..library.sourcing import SourceHit, find_sources
from ..library.watcher import DownloadWatcher
from ..models import Part
from .build_tab import BuildTab
from .datasheet_tab import DatasheetTab
from .preview import FootprintView, SymbolView, preview_symbol
from .view3d import Model3DView
from .theme import tokens
from .verify_dialog import VerifyDialog


class PartHeader(QLabel):
    def __init__(self):
        super().__init__()
        self.setWordWrap(True)
        self.setTextFormat(Qt.RichText)
        self.setOpenExternalLinks(True)
        self.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.setContentsMargins(4, 2, 4, 8)
        t = tokens()
        self.setText(f"<div style='color:{t.muted}; font-size:11pt'>Search for a part on the left, "
                     f"or ask the chat on the right.</div>")

    def show_part(self, p: Part):
        e, t = html.escape, tokens()
        chip = (lambda txt, fg=t.text, bg=t.raised:
                f"<span style='background:{bg}; color:{fg}'>&nbsp;{e(txt)}&nbsp;</span>&nbsp; ")
        chips = "".join(chip(x) for x in (p.package, p.category) if x)
        if p.jlc_class:
            chips += chip(f"JLC {p.jlc_class}", t.accent, t.accent_soft)
        stock = p.total_stock
        chips += chip(f"{stock:,} in stock" if stock > 0 else "out of stock",
                      t.ok if stock > 0 else t.danger, t.raised)
        specs = " &nbsp;&nbsp; ".join(f"<span style='color:{t.muted}'>{e(k)}</span> {e(v)}"
                                      for k, v in list(p.specs.items())[:8])
        offers = sorted(p.offers, key=lambda o: -(o.stock or 0))
        th = f"style='color:{t.muted}; font-weight:600' align=left"
        rows = [f"<tr><td {th}>Distributor</td><td {th}>SKU</td><td {th} align=right>Stock</td>"
                f"<td {th}>Price breaks</td></tr>"]
        for o in offers[:5]:
            pr = " &nbsp; ".join(f"<span style='color:{t.muted}'>{q}+</span> {v:.4g}" for q, v in o.price_breaks[:4])
            link = f"<a href='{e(o.url)}' style='color:{t.accent}'>{e(o.sku or o.source)}</a>" if o.url else e(o.sku)
            rows.append(f"<tr><td>{e(o.source)}</td><td>{link}</td><td align=right>{(o.stock or 0):,}</td>"
                        f"<td>{pr} <span style='color:{t.muted}'>{e(o.currency)}</span></td></tr>")
        if len(offers) > 5:
            rows.append(f"<tr><td colspan=4 style='color:{t.muted}'>+{len(offers) - 5} more offers</td></tr>")
        self.setText(
            f"<div><span style='font-size:17pt; font-weight:700'>{e(p.mpn)}</span>"
            f"&nbsp;&nbsp;<span style='font-size:11pt; color:{t.muted}'>{e(p.manufacturer)}</span></div>"
            f"<div style='margin-top:4px'>{chips}</div>"
            f"<div style='margin-top:6px'>{e(p.description[:220])}</div>"
            + (f"<div style='margin-top:4px; font-size:9pt'>{specs}</div>" if specs else "")
            + f"<table cellspacing=0 cellpadding=3 style='margin-top:8px'>{''.join(rows)}</table>")


ACTION_HINT = {"use_existing": "already in your library", "import_lcsc": "download + import",
               "open_browser": "download page", "stock_footprint": "KiCad stock footprint", "build": "builder"}


class SourcesTab(QWidget):
    """Every source for the part. Click one for Import / Open in Builder; the pane below previews it."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 6, 0, 0)
        split = QSplitter(Qt.Vertical)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["", "Source", "Sym", "Fp", "3D", "Detail"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setMouseTracking(True)
        for i, w in enumerate([26, 170, 40, 40, 40]):
            self.tree.setColumnWidth(i, w)
        self.tree.header().setStretchLastSection(True)
        self.tree.itemClicked.connect(self._clicked)
        split.addWidget(self.tree)
        # preview of the selected source
        pv = QWidget()
        pl = QVBoxLayout(pv)
        pl.setContentsMargins(0, 6, 0, 0)
        self.pv_title = QLabel("Preview")
        self.pv_title.setObjectName("sectionTitle")
        pl.addWidget(self.pv_title)
        views = QSplitter(Qt.Horizontal)
        self.symv = SymbolView()
        self.fpv = FootprintView()
        self.v3d = Model3DView()
        for w in (self.symv, self.fpv, self.v3d):
            views.addWidget(w)
        pl.addWidget(views, 1)
        row = QHBoxLayout()
        self.pv_info = QLabel("")
        self.pv_info.setObjectName("muted")
        self.pv_info.setWordWrap(True)
        row.addWidget(self.pv_info, 1)
        self.b_build = QPushButton("Open in Builder")
        self.b_build.clicked.connect(lambda: self.panel.open_in_builder(self.current))
        self.b_import = QPushButton("Import…")
        self.b_import.setToolTip("Check, then write symbol, footprint and 3D model to the library")
        self.b_import.clicked.connect(lambda: self.panel.import_hit(self.current))
        self.b_place = QPushButton("Import && place…")
        self.b_place.setProperty("primary", True)
        self.b_place.setToolTip("Import, then put the symbol on the clipboard: press Ctrl+V in the schematic "
                                "editor and click to place it")
        self.b_place.clicked.connect(lambda: self.panel.import_hit(self.current, place=True))
        row.addWidget(self.b_build)
        row.addWidget(self.b_import)
        row.addWidget(self.b_place)
        pl.addLayout(row)
        trow = QHBoxLayout()
        self.target = QLabel("")
        self.target.setObjectName("muted")
        change = QPushButton("Change…")
        change.setProperty("flat", True)
        change.clicked.connect(self.panel.settings_requested.emit)
        trow.addWidget(self.target, 1)
        trow.addWidget(change)
        pl.addLayout(trow)
        split.addWidget(pv)
        split.setSizes([200, 380])
        v.addWidget(split, 1)
        self.note = QLabel("")
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        v.addWidget(self.note)
        self.current: Optional[SourceHit] = None
        self.items = {}
        self.show_preview(None, "")

    def show_hits(self, hits: List[SourceHit], best: Optional[SourceHit] = None):
        self.tree.clear()
        self.items = {}
        for h in hits:
            mark = "★" if h is best else ""
            it = QTreeWidgetItem([mark, h.source, "✓" if h.symbol else "", "✓" if h.footprint else "",
                                  "✓" if h.model3d else "", h.detail])
            it.setToolTip(5, h.detail)
            it.setData(0, Qt.UserRole, h)
            if h.action == "none":
                it.setDisabled(True)
            for c in (2, 3, 4):
                it.setTextAlignment(c, Qt.AlignCenter)
            self.tree.addTopLevelItem(it)
            self.items[id(h)] = it

    def select(self, h: SourceHit):
        it = self.items.get(id(h))
        if it:
            self.tree.setCurrentItem(it)

    def _clicked(self, it: QTreeWidgetItem, _col: int):
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QMenu
        h: SourceHit = it.data(0, Qt.UserRole)
        if h is None or h.action == "none":
            return
        self.panel.preview_hit(h)
        m = QMenu(self)
        complete = h.symbol and h.footprint
        if h.action == "import_lcsc":
            a = m.addAction("Import symbol + footprint" + (" + 3D" if h.model3d else "") + "…")
            a.triggered.connect(lambda: self.panel.import_hit(h))
            m.addAction("Open in Builder (edit first)").triggered.connect(lambda: self.panel.open_in_builder(h))
        elif h.action == "use_existing":
            m.addAction("Copy lib id (place with Add Symbol)").triggered.connect(lambda: self.panel.run_hit(h))
            m.addAction("Open in Builder (make a variant)").triggered.connect(lambda: self.panel.open_in_builder(h))
        elif h.action == "stock_footprint":
            m.addAction("Import with generic symbol…").triggered.connect(lambda: self.panel.import_hit(h))
            m.addAction("Open in Builder (add a proper symbol)").triggered.connect(
                lambda: self.panel.open_in_builder(h))
            m.addAction("Compare stock footprints…").triggered.connect(lambda: self.panel.run_hit(h))
        elif h.action == "open_browser":
            m.addAction(f"Open {h.source} download page").triggered.connect(lambda: self.panel.run_hit(h))
        elif h.action == "build":
            m.addAction("Open Builder").triggered.connect(lambda: self.panel.open_in_builder(h))
        if complete and m.actions():
            m.setDefaultAction(m.actions()[0])
        m.exec(QCursor.pos())

    # ------------------------------------------------------------ preview pane
    def show_preview(self, prep: Optional[PreparedImport], status: str, hit: Optional[SourceHit] = None):
        self.current = hit
        self.symv.set_symbol(preview_symbol(prep))
        fp = prep.footprint_model() if prep else None
        self.fpv.set_footprint(fp, "" if fp else ("No footprint from this source" if prep else ""))
        self._load_3d(prep, fp)
        self.pv_title.setText(f"Preview — {hit.source}" if hit else "Preview")
        info = [status] if status else []
        if prep:
            info.append(f"{len(prep.pins())} pins" if prep.pins() else "no symbol")
            if fp:
                info.append(f"footprint {fp.name}, {len(fp.pads)} pads")
            if prep.model_bytes:
                info.append(f"3D model {prep.model_filename} ({len(prep.model_bytes) // 1024} kB)")
            elif fp:
                info.append("no 3D model")
            info += prep.warnings[:2]
        self.pv_info.setText(" · ".join(info) if info else "Click a source to preview it.")
        can = hit is not None and hit.action in ("import_lcsc", "stock_footprint", "use_existing")
        self.b_import.setVisible(can and hit.action != "use_existing")
        self.b_place.setVisible(can)
        self.b_place.setText("Place…" if hit is not None and hit.action == "use_existing" else "Import && place…")
        self.b_place.setEnabled(prep is not None or (hit is not None and hit.action == "use_existing"))
        self.target.setText(self.panel.target_text())
        self.b_build.setVisible(hit is not None and hit.action != "open_browser")
        self.b_import.setEnabled(prep is not None)
        self.b_build.setEnabled(prep is not None or (hit is not None and hit.action == "build"))

    def _load_3d(self, prep, fp):
        import asyncio
        from ..model3d import scene_for
        self._3d_for = prep
        if prep is None:
            self.v3d.set_scene(None, None, "")
            return
        self.v3d.set_scene(None, fp, "Loading 3D model…")

        def done(res):
            if self._3d_for is prep:
                self.v3d.set_scene(res[0], fp, res[1])
        self.panel.state.bridge.run(asyncio.to_thread(scene_for, prep), done,
                                    lambda e: self.v3d.set_scene(None, fp, f"3D preview failed: {e}"))

    def busy(self, text: str):
        self.tree.clear()
        self.note.setText(text)


class FootprintTab(QWidget):
    assign = Signal(str)          # stock lib id

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 4, 0, 0)
        h = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("Package, e.g. 'BGA-256, 1.0 mm, 17×17', 'LQFP-48_7x7x05P', 'SOT-23-5'")
        self.query.returnPressed.connect(self.run)
        b = QPushButton("Match")
        b.clicked.connect(self.run)
        h.addWidget(self.query, 1)
        h.addWidget(b)
        v.addLayout(h)
        split = QSplitter(Qt.Vertical)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._preview)
        split.addWidget(self.list)
        self.view = FootprintView()
        split.addWidget(self.view)
        split.setSizes([200, 300])
        v.addWidget(split, 1)
        bb = QHBoxLayout()
        self.status = QLabel("")
        bb.addWidget(self.status, 1)
        self.use = QPushButton("Assign to import")
        self.use.setToolTip("Use this KiCad stock footprint for the part being imported")
        self.use.clicked.connect(self._assign)
        bb.addWidget(self.use)
        v.addLayout(bb)

    def set_query(self, text: str, run: bool = True):
        self.query.setText(text)
        if run:
            self.run()

    def run(self):
        idx = self.state.fp_index
        if idx is None:
            self.status.setText("Footprint index not ready yet.")
            return
        from ..matcher import match, parse_query
        q = self.query.text().strip()
        ms = match(idx, q, 15)
        self.list.clear()
        for m in ms:
            it = QListWidgetItem(f"{m.lib_id}\n    {m.explain()}")
            it.setData(Qt.UserRole, m)
            self.list.addItem(it)
        spec = parse_query(q)
        self.status.setText(f"{len(ms)} matches · parsed: {spec.family or '?'} pins={spec.pins} pitch={spec.pitch} "
                            f"body={spec.body_x}×{spec.body_y} EP={'yes' if spec.has_ep else 'no'}")
        if ms:
            self.list.setCurrentRow(0)

    def _preview(self, cur, _prev):
        if not cur:
            return
        m = cur.data(Qt.UserRole)
        try:
            self.view.set_footprint(read_footprint(Path(m.path).read_text()))
        except Exception as e:
            self.view.set_footprint(None, str(e))

    def _assign(self):
        cur = self.list.currentItem()
        if cur:
            self.assign.emit(cur.data(Qt.UserRole).lib_id)


class PartPanel(QWidget):
    imported = Signal(str)
    settings_requested = Signal()

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.part: Optional[Part] = None
        self.hits: List[SourceHit] = []
        self.pending: Optional[PreparedImport] = None
        self.auto_import = False          # set by the chat card's "Get symbol/footprint"
        self.auto_build = False           # set by "Open in Builder" from a card or the results menu
        self._preps = {}                  # (action, ref) -> PreparedImport, so preview/import/build download once
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.header = PartHeader()
        v.addWidget(self.header)
        self.tabs = QTabWidget()
        self.ds = DatasheetTab(state)
        self.sources = SourcesTab(self)
        self.fptab = FootprintTab(state)
        self.build = BuildTab(state)
        self.tabs.addTab(self.ds, "Datasheet")
        self.tabs.addTab(self.sources, "Sources")
        self.tabs.addTab(self.fptab, "Footprint")
        self.tabs.addTab(self.build, "Build")
        v.addWidget(self.tabs, 1)
        self.ds.send_text.connect(lambda t: (self.tabs.setCurrentWidget(self.build), self.build.receive_text(t)))
        self.build.datasheet_fn = lambda: (self.ds.path, {b.title: b.page for b in self.ds.bookmarks})
        self.fptab.assign.connect(self.assign_stock)
        self.build.save_requested.connect(self.save_built)
        self.build.target_fn = self.target_text
        self.watcher = DownloadWatcher(state.downloads(), self._zip_arrived)

    # ------------------------------------------------------------ part selection
    def show_part(self, part: Part):
        self.part = part
        self.pending = None
        self.hits = []
        self._preps = {}
        self.header.show_part(part)
        self.ds.load_part(part)
        self.refresh_sources()
        if part.package:
            self.fptab.set_query(part.package, run=self.state.fp_index is not None)
        self.build.prefill(part, part.package)

    def refresh_sources(self):
        part = self.part
        if not part:
            return
        self.sources.busy("Looking up symbol/footprint sources…")

        def ok(hits):
            if self.part is not part:
                return
            self.hits = hits
            best = self.best_hit()
            self.sources.show_hits(hits, best)
            self.sources.note.setText("★ = best source. Click any source for Import / Open in Builder.")
            lc = next((h for h in hits if h.action == "import_lcsc"), None)
            if lc and lc.extra.get("package"):
                self.fptab.set_query(lc.extra["package"], run=self.state.fp_index is not None)
                self.build.prefill(part, lc.extra["package"])
            if self.auto_import:
                self.auto_import = False
                self.start_import()
            elif self.auto_build:
                self.auto_build = False
                self.open_in_builder(best)
            elif best is not None:
                self.preview_hit(best, load_builder=True)     # download + show the best source right away
        self.state.bridge.run(find_sources(part, self.state.engine, self.state.project_dir, self.state.fp_index), ok,
                              lambda e: self.sources.note.setText(f"Source lookup failed: {e}"))

    def best_hit(self) -> Optional[SourceHit]:
        """Most complete usable source (symbol + footprint + 3D); the builder only if nothing else."""
        order = {"use_existing": 0, "import_lcsc": 1, "stock_footprint": 2, "build": 3}
        usable = [h for h in self.hits if h.action in order]
        # most complete first (symbol + footprint + 3D); on a tie, what you already have wins
        return min(usable, key=lambda h: (h.action == "build", -(h.symbol + h.footprint + h.model3d),
                                          order[h.action])) if usable else None

    def start_import(self):
        """Import from the best source (chat card / results menu 'Import')."""
        self.tabs.setCurrentWidget(self.sources)
        h = self.best_hit()
        if h is None:
            return
        if h.action == "use_existing":
            self.run_hit(h)
        elif h.action == "build":
            self.open_in_builder(h)
        else:
            self.import_hit(h)

    # ------------------------------------------------------------ prepare / preview / import / build
    def _prepare(self, h: SourceHit, done):
        """Download a source once (symbol, footprint, 3D) and call done(prep) on the GUI thread."""
        key = (h.action, h.ref)
        if key in self._preps:
            done(self._preps[key])
            return
        part = self.part
        if h.action == "import_lcsc":
            prov = self.state.engine.provider("lcsc")

            async def work():
                from ..library import easyeda
                cad = await prov.fetch_cad(h.ref)
                _, model = easyeda.convert_footprint(cad)
                step, err = None, ""
                if model:
                    try:
                        step = await prov.fetch_step(model.uuid)
                    except Exception as e:
                        err = f"3D model download failed: {e}"
                prep = importer.prepare_lcsc(cad, part, step)
                if err:
                    prep.warnings.append(err)
                return prep
        elif h.action == "stock_footprint":
            async def work():
                return self._stock_prep(h.ref, part)
        elif h.action == "use_existing":
            async def work():
                return self._existing_prep(h, part)
        else:
            done(None)
            return

        def ok(prep):
            self._preps[key] = prep
            if self.part is part:
                done(prep)
        self.state.bridge.run(work(), ok, lambda e: self.sources.show_preview(None, f"Download failed: {e}", h))

    def preview_hit(self, h: SourceHit, load_builder: bool = False):
        if h.action in ("open_browser", "build", "none"):
            self.sources.show_preview(None, h.detail if h.action == "open_browser" else "", h)
            return
        self.sources.select(h)
        self.sources.show_preview(None, "Downloading symbol, footprint and 3D model…", h)

        def done(prep):
            if self.sources.current is h:
                self.sources.show_preview(prep, "", h)
            # give the builder the downloaded data unless the user is already editing there
            if load_builder and prep is not None and self.tabs.currentWidget() is not self.build:
                self.build.load_source(prep, self.part)
        self._prepare(h, done)

    def target_text(self) -> str:
        try:
            lib = self.state.library()
        except ValueError as e:
            return f"⚠ {e}"
        where = "this project" if lib.project_dir else str(lib.base)
        return f"Saves to library “{lib.name}” in {where}"

    def import_hit(self, h: Optional[SourceHit], place: bool = False):
        if h is None:
            return
        if h.action == "use_existing":
            if place:
                self.place_existing(h)
            else:
                self.run_hit(h)
            return
        self.sources.note.setText(f"Preparing {h.source}…")
        self._prepare(h, lambda prep: prep and self._verify_and_commit(prep, place))

    def place_existing(self, h: SourceHit):
        lib_nick, name = h.ref.split(":", 1)
        path = Path(h.extra.get("file", ""))
        self._to_clipboard(path, name, lib_nick)

    def _to_clipboard(self, sym_file: Path, name: str, nickname: str) -> None:
        from ..kicad.place import snippet
        try:
            QGuiApplication.clipboard().setText(snippet(sym_file.read_text(), name, nickname))
        except (OSError, ValueError) as e:
            QGuiApplication.clipboard().setText(f"{nickname}:{name}")
            QMessageBox.information(self, "Place symbol", f"{e}\n\nThe lib id {nickname}:{name} was copied instead.")
            return
        QMessageBox.information(self, "Place symbol",
                                f"{nickname}:{name} is on the clipboard.\n\nIn the KiCad schematic editor press "
                                "Ctrl+V, then click to place it.")

    def open_in_builder(self, h: Optional[SourceHit]):
        self.tabs.setCurrentWidget(self.build)
        if h is None or h.action == "build":
            self.build.clear_source()
            self.build.prefill(self.part, (h.extra.get("package", "") if h else "") or (self.part.package if self.part else ""))
            return

        def done(prep):
            if prep is not None:
                self.build.load_source(prep, self.part)
        self._prepare(h, done)

    def _stock_prep(self, lib_id: str, part: Optional[Part]) -> PreparedImport:
        """Stock footprint + the LCSC symbol when one was downloaded, else a generic pad-number symbol."""
        idx = self.state.fp_index
        path = idx.path_of(lib_id) if idx else None
        fp = read_footprint(path.read_text()) if path else None
        part = part or Part(mpn=lib_id.split(":")[-1])
        lc = next((p for (a, _r), p in self._preps.items() if a == "import_lcsc" and p and p.symbol), None)
        if lc is not None:
            sym = lc.symbol
            note = "LCSC symbol with KiCad stock footprint"
        else:
            nums = sorted({p.number for p in fp.pads if p.number}, key=SB._natkey) if fp else []
            sym = SB.build_symbol(part.mpn, [SB.PinDef(n, n, "passive") for n in nums], "U")
            note = "generic symbol"
        sym.datasheet = part.datasheet_url
        prep = PreparedImport(source=f"KiCad stock footprint + {note}", symbol_name=part.mpn or sym.name, symbol=sym)
        prep.assign_stock_footprint(lib_id, fp)
        prep.fields = part_fields(part.mpn, part.manufacturer, part.datasheet_url, part.lcsc, part.sku("Mouser"),
                                  part.sku("DigiKey"), f"KiCad stock footprint {lib_id}; {note}")
        if lc is None:
            prep.warnings.append("Generic symbol: pin names are pad numbers. Open in Builder for a proper symbol.")
        return prep

    def _existing_prep(self, h: SourceHit, part: Optional[Part]) -> PreparedImport:
        from ..kicad.symbol import read_library
        path = Path(h.extra.get("file", "")) if h.extra.get("file") else None
        name = h.ref.split(":", 1)[-1]
        raw = ""
        if path and path.exists():
            for ls in read_library(path.read_text(errors="replace")):
                if ls.name == name:
                    raw = ls.raw
        prep = PreparedImport(source=f"Library {h.ref}", symbol_name=name, symbol_raw=raw)
        fpid = h.extra.get("footprint", "")
        from ..library.existing import footprint_file
        idx = self.state.fp_index
        fpath = (idx.path_of(fpid) if idx and fpid else None) or (footprint_file(fpid, self.state.project_dir)
                                                                     if fpid else None)
        if fpath:
            prep.assign_stock_footprint(fpid, read_footprint(fpath.read_text()))
        return prep

    def run_hit(self, h: SourceHit):
        if h.action == "use_existing":
            QGuiApplication.clipboard().setText(h.ref)
            QMessageBox.information(self, "Already in your libraries",
                                    f"{h.ref} is already available.\nThe lib id was copied; place it with KiCad's "
                                    "Add Symbol (A) chooser.")
        elif h.action == "import_lcsc":
            self.import_hit(h)
        elif h.action == "open_browser":
            QDesktopServices.openUrl(QUrl(h.url))
            self.watcher.folder = self.state.downloads()
            self.watcher.arm(self.part.mpn if self.part else "")
            self.sources.note.setText(f"Watching {self.watcher.folder} for a {h.source} ZIP of {self.part.mpn}. "
                                      "Download the KiCad format; it will be unpacked and shown for verification.")
        elif h.action == "stock_footprint":
            self.tabs.setCurrentWidget(self.fptab)
            self.fptab.set_query(h.extra.get("query", ""))
        elif h.action == "build":
            self.open_in_builder(h)

    # ------------------------------------------------------------ import flows
    def assign_stock(self, lib_id: str):
        if self.pending is not None:
            idx = self.state.fp_index
            path = idx.path_of(lib_id) if idx else None
            self.pending.assign_stock_footprint(lib_id, read_footprint(path.read_text()) if path else None)
            self._verify_and_commit(self.pending)
            return
        self._verify_and_commit(self._stock_prep(lib_id, self.part))

    def save_built(self, sym, fp, place: bool = False):
        part = self.part or Part(mpn=sym.name)
        sym.datasheet = part.datasheet_url
        sym.description = sym.description or part.description
        src = self.build.source_prep
        prep = PreparedImport(source="KiBench build helper", symbol_name=sym.name, symbol=sym, footprint=fp,
                              footprint_name=fp.name if fp else "")
        origin = "KiBench build helper (IPC-7351B rules)"
        if src is not None and self.build.uses_source_fp:
            # keep the downloaded footprint's 3D model, stock reference and source attribution
            prep.stock_footprint = src.stock_footprint
            prep.footprint_name = src.footprint_name or prep.footprint_name
            if src.footprint_raw and not src.footprint:      # vendor file: write it byte for byte
                prep.footprint, prep.footprint_raw = None, src.footprint_raw
            prep.model_bytes, prep.model_filename = src.model_bytes, src.model_filename
            prep.model_offset, prep.model_rotate = src.model_offset, src.model_rotate
            origin = f"{src.fields.get('Source') or src.source}; edited in KiBench builder"
            prep.source = f"Builder, footprint from {src.source}"
        prep.fields = part_fields(part.mpn or sym.name, part.manufacturer, part.datasheet_url, part.lcsc,
                                  part.sku("Mouser"), part.sku("DigiKey"), origin)
        self._verify_and_commit(prep)

    def _zip_arrived(self, path: Path, matches: bool):
        # called from the watcher thread
        def on_gui():
            if not matches:
                r = QMessageBox.question(self, "New ZIP in Downloads",
                                         f"{path.name} does not mention {self.part.mpn if self.part else 'the part'}. Import it anyway?")
                if r != QMessageBox.Yes:
                    return
            try:
                prep = importer.prepare_zip(path, self.part.mpn if self.part else "", self.part)
            except Exception as e:
                QMessageBox.warning(self, "ZIP import", str(e))
                return
            self.watcher.disarm()
            self._verify_and_commit(prep, place)
        self.state.bridge.call_soon(on_gui)

    def _verify_and_commit(self, prep: PreparedImport, place: bool = False):
        self.pending = prep
        try:
            lib = self.state.library()
        except ValueError as e:
            QMessageBox.warning(self, "No library target", str(e))
            return
        page = self.ds.page_of("Package outline")
        if page is None:
            page = self.ds.page_of("Land pattern")
        dlg = VerifyDialog(prep, lib, self.ds.path, page, self)
        accepted = dlg.exec()
        dlg.deleteLater()          # free it now; a parked dialog crashes Qt at shutdown
        if not accepted:
            self.sources.note.setText("Import cancelled. Pick a stock footprint in the Footprint tab to replace it, "
                                      "or edit it in the Build tab.")
            return
        try:
            rec = importer.commit(prep, lib)
        except Exception as e:
            QMessageBox.critical(self, "Write failed", f"{e.__class__.__name__}: {e}")
            return
        self.pending = None
        msg = "\n".join(rec.messages)
        self.sources.note.setText(msg + f"\nPlace it in KiCad with Add Symbol (A) → {rec.lib_id}.")
        QGuiApplication.clipboard().setText(rec.lib_id)
        self.imported.emit(rec.lib_id)
        self.state.library_changed.emit()
        if place:
            self._to_clipboard(lib.sym_path, rec.lib_id.split(":", 1)[1], lib.name)
