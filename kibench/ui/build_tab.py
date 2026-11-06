"""Build tab: parametric footprint builder + pin-table symbol builder with live previews and checks."""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
                               QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget, QDialog,
                               QDialogButtonBox, QMessageBox)

from ..builder import footprints as FB
from ..builder import symbols as SB
from ..kicad.symbol import PIN_TYPES
from ..library.verify import check_pins_pads
from .preview import FootprintView, SymbolView
from .view3d import Model3DView


def _scrollable(w: QWidget) -> QScrollArea:
    """Let a builder half shrink below its natural width (scrolls instead of forcing the window wide)."""
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QScrollArea.NoFrame)
    sa.setWidget(w)
    sa.setMinimumWidth(240)
    return sa


class PasteDialog(QDialog):
    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Paste pin table")
        self.resize(560, 420)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Paste pin rows (from the datasheet viewer, a CSV, or a vendor pin file). "
                           "Formats like '1 VDD', '1\tVDD\tP', 'A1, GND' are recognised."))
        self.edit = QPlainTextEdit(text)
        v.addWidget(self.edit, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)


def _natkey(text: str):
    import re
    return [(0, int(t), "") if t.isdigit() else (1, 0, t.lower()) for t in re.split(r"(\d+)", text) if t]


class NatItem(QTableWidgetItem):
    """Sorts 'A2' < 'A10' and '2' < '10' (pin numbers, ball names)."""

    def __lt__(self, other):
        return _natkey(self.text()) < _natkey(other.text())

    def clone(self):
        return NatItem()


class DrawingDialog(QDialog):
    """Non-modal view of datasheet pages (package drawing / land pattern) next to the builder form."""

    def __init__(self, path, page: int, parent=None):
        super().__init__(parent)
        from .. import datasheet as ds
        self.ds, self.path = ds, path
        self.setWindowTitle(f"Datasheet — {path.name}")
        self.resize(760, 900)
        v = QVBoxLayout(self)
        h = QHBoxLayout()
        self.page = QSpinBox()
        self.page.setRange(1, max(1, len(ds.cached_texts(path))))
        self.page.setValue(page + 1)
        self.page.valueChanged.connect(self._render)
        self.zoom = QComboBox()
        self.zoom.addItems(["1.5×", "2×", "3×"])
        self.zoom.setCurrentIndex(1)
        self.zoom.currentIndexChanged.connect(self._render)
        h.addWidget(QLabel("Page"))
        h.addWidget(self.page)
        h.addWidget(QLabel("Zoom"))
        h.addWidget(self.zoom)
        h.addStretch(1)
        v.addLayout(h)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.img = QLabel()
        self.img.setAlignment(Qt.AlignCenter)
        self.area.setWidget(self.img)
        v.addWidget(self.area, 1)
        self._render()

    def _render(self, *_):
        from PySide6.QtGui import QImage, QPixmap
        z = (1.5, 2.0, 3.0)[self.zoom.currentIndex()]
        try:
            img = QImage.fromData(self.ds.render_page_png(self.path, self.page.value() - 1, z))
            self.img.setPixmap(QPixmap.fromImage(img))
        except Exception as e:
            self.img.setText(f"Could not render page: {e}")


class BuildTab(QWidget):
    save_requested = Signal(object, object, bool)  # Symbol, Footprint, place afterwards
    ask_chat = Signal(str)                        # question for the chat agent

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.part = None
        self.fp = None
        self.sym = None
        self.source_prep = None          # PreparedImport loaded from a source (LCSC, vendor ZIP, ...)
        self.source_fp = None
        self.source_sym = None
        self.datasheet_fn = lambda: (None, {})     # set by PartPanel: (pdf path, {bookmark title: page})
        self._drawing_dlg = None
        self._editors: Dict[str, QWidget] = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 0)
        # narrow column: a Footprint | Symbol switch shows one half at a time
        self.half_bar = QWidget()
        hb = QHBoxLayout(self.half_bar)
        hb.setContentsMargins(0, 0, 0, 4)
        self.half_btns = []
        for i, label in enumerate(("Footprint", "Symbol")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.setMinimumWidth(b.fontMetrics().horizontalAdvance(label) + 44)    # room for the bold checked state
            b.clicked.connect(lambda _=False, k=i: self._show_half(k))
            hb.addWidget(b)
            self.half_btns.append(b)
        hb.addStretch(1)
        outer.addWidget(self.half_bar)
        split = QSplitter(Qt.Horizontal)
        self.halves = split
        self._narrow = None
        outer.addWidget(split, 1)

        # ---------------- footprint builder
        fpw = QWidget()
        fv = QVBoxLayout(fpw)
        top = QHBoxLayout()
        self.tpl = QComboBox()
        for k, t in FB.TEMPLATES.items():
            self.tpl.addItem(t.label, k)
        self.tpl.currentIndexChanged.connect(self._template_changed)
        self.density = QComboBox()
        self.density.addItems(["A (most)", "B (nominal)", "C (least)"])
        self.density.setCurrentIndex("ABC".index(self.state.settings.ipc_density or "B"))
        self.density.currentIndexChanged.connect(self._schedule)
        top.addWidget(QLabel("Template"))
        top.addWidget(self.tpl, 1)
        fv.addLayout(top)
        top2 = QHBoxLayout()
        top2.addWidget(QLabel("IPC-7351B density"))
        top2.addWidget(self.density)
        top2.addStretch(1)
        fv.addLayout(top2)
        dsrow = QHBoxLayout()
        b_draw = QPushButton("Package drawing")
        b_draw.setToolTip("Show the datasheet's package outline / land pattern page next to this form")
        b_draw.clicked.connect(self._show_drawing)
        b_dims = QPushButton("Ask chat for dimensions")
        b_dims.setToolTip("The chat reads the dimension table from the datasheet and cites the page")
        b_dims.clicked.connect(self._ask_dims)
        dsrow.addWidget(b_draw)
        dsrow.addWidget(b_dims)
        dsrow.addStretch(1)
        fv.addLayout(dsrow)
        self.drawing = QLabel()
        self.drawing.setWordWrap(True)
        self.drawing.setStyleSheet("color: gray")
        fv.addWidget(self.drawing)
        self.form_area = QScrollArea()
        self.form_area.setWidgetResizable(True)
        fv.addWidget(self.form_area, 2)
        self.fpview = FootprintView()
        self.view3d = Model3DView()
        self.fp_views = QTabWidget()
        self.fp_views.addTab(self.fpview, "2D")
        self.fp_views.addTab(self.view3d, "3D")
        self.fp_views.currentChanged.connect(lambda _i: self._refresh_3d())
        fv.addWidget(self.fp_views, 3)
        split.addWidget(_scrollable(fpw))

        # ---------------- symbol builder
        sw = QWidget()
        sv = QVBoxLayout(sw)
        nm = QHBoxLayout()
        self.sym_name = QLineEdit()
        self.sym_name.setPlaceholderText("Symbol name (MPN)")
        self.ref = QLineEdit("U")
        self.ref.setMaximumWidth(50)
        nm.addWidget(QLabel("Name"))
        nm.addWidget(self.sym_name, 1)
        nm.addWidget(QLabel("Ref"))
        nm.addWidget(self.ref)
        sv.addLayout(nm)
        mode = QHBoxLayout()
        self.sym_mode = QComboBox()
        self.sym_mode.addItem("Generate from pin table", "generated")
        self.sym_mode.currentIndexChanged.connect(self._schedule)
        mode.addWidget(QLabel("Symbol"))
        mode.addWidget(self.sym_mode, 1)
        sv.addLayout(mode)
        btns = QGridLayout()
        for i, (label, fn) in enumerate((("Pins from datasheet", self._pins_from_ds), ("Paste…", self._paste),
                                         ("CSV / pin file…", self._load_csv), ("From pads", self._from_pads),
                                         ("+ row", self._add_row), ("− row", self._del_row))):
            b = QPushButton(label)
            b.clicked.connect(fn)
            btns.addWidget(b, i // 3, i % 3)
        sv.addLayout(btns)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Pin #", "Pin name", "Type", "Side", "Group / bank"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().hide()                 # only the pin number column counts
        self.table.setItemPrototype(NatItem())
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setToolTip("Click a column header to sort. Select a pin to highlight it in the footprint and symbol.")
        self.table.itemSelectionChanged.connect(self._pin_selected)
        self.table.itemChanged.connect(self._pins_edited)
        sv.addWidget(self.table, 2)
        units = QHBoxLayout()
        self.split = QComboBox()
        self.split.addItems(["auto", "group", "none"])
        self.maxpins = QSpinBox()
        self.maxpins.setRange(0, 1000)
        self.maxpins.setValue(0)
        self.maxpins.setSpecialValueText("no limit")
        self.unit_sel = QSpinBox()
        self.unit_sel.setMinimum(1)
        self.unit_sel.valueChanged.connect(lambda v: self.symview.set_symbol(self.sym, v - 1))
        for w in (self.split, self.maxpins):
            (w.currentIndexChanged if isinstance(w, QComboBox) else w.valueChanged).connect(self._schedule)
        units.addWidget(QLabel("Units"))
        units.addWidget(self.split)
        units.addWidget(QLabel("max pins"))
        units.addWidget(self.maxpins)
        units.addWidget(QLabel("show"))
        units.addWidget(self.unit_sel)
        sv.addLayout(units)
        self.symview = SymbolView()
        sv.addWidget(self.symview, 3)
        split.addWidget(_scrollable(sw))
        split.setSizes([500, 500])

        bottom = QHBoxLayout()
        self.checks = QLabel("")
        self.checks.setWordWrap(True)
        bottom.addWidget(self.checks, 1)
        self.save_btn = QPushButton("Save to library…")
        self.save_btn.clicked.connect(self._save)
        self.place_btn = QPushButton("Save && place…")
        self.place_btn.setProperty("primary", True)
        self.place_btn.setToolTip("Save, then put the symbol on the clipboard: Ctrl+V in the schematic editor places it")
        self.place_btn.clicked.connect(lambda: self._save(place=True))
        bottom.addWidget(self.save_btn)
        bottom.addWidget(self.place_btn)
        outer.addLayout(bottom)
        self.target_lbl = QLabel("")
        self.target_lbl.setObjectName("muted")
        outer.addWidget(self.target_lbl)
        self.target_fn = lambda: ""

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self.rebuild)
        self._template_changed()

    def resizeEvent(self, e):
        narrow = e.size().width() < 900          # side by side only when both halves have room
        if narrow != self._narrow:
            self._narrow = narrow
            self.half_bar.setVisible(narrow)
            if narrow:
                self._show_half(0 if self.half_btns[0].isChecked() else 1)
            else:
                for i in range(2):
                    self.halves.widget(i).setVisible(True)
        super().resizeEvent(e)

    def _show_half(self, k: int):
        for i, b in enumerate(self.half_btns):
            b.setChecked(i == k)
        if self._narrow:
            for i in range(2):
                self.halves.widget(i).setVisible(i == k)

    # ------------------------------------------------------------ footprint form
    def _template_changed(self, *_):
        key = self.tpl.currentData()
        if key == "__source__":
            src = self.source_prep.source if self.source_prep else "the source"
            self.drawing.setText(f"Footprint as downloaded from {src}. Pick a template above to rebuild it from "
                                 "datasheet dimensions instead.")
            self._editors = {}
            self.form_area.setWidget(QLabel(""))
            self.form_area.setVisible(False)        # nothing to edit: give the preview the room
            self._schedule()
            return
        self.form_area.setVisible(True)
        t = FB.TEMPLATES[key]
        self.drawing.setText(t.drawing)
        w = QWidget()
        lay = QVBoxLayout(w)
        groups: Dict[str, QFormLayout] = {}
        self._editors = {}
        for p in t.params:
            if p.group not in groups:
                gb = QGroupBox(p.group)
                groups[p.group] = QFormLayout(gb)
                lay.addWidget(gb)
            ed = self._editor(p)
            if p.help:
                ed.setToolTip(p.help)
            groups[p.group].addRow(p.label, ed)
            self._editors[p.key] = ed
        lay.addStretch(1)
        self.form_area.setWidget(w)
        self._schedule()

    def _editor(self, p):
        if p.kind == "float":
            e = QDoubleSpinBox()
            e.setDecimals(3)
            e.setRange(-1000, 1000)
            e.setSingleStep(0.05)
            e.setValue(float(p.default))
            e.setSuffix(" mm" if p.unit == "mm" else "")
            e.valueChanged.connect(self._schedule)
        elif p.kind == "int":
            e = QSpinBox()
            e.setRange(0, 2000)
            e.setValue(int(p.default))
            e.valueChanged.connect(self._schedule)
        elif p.kind == "choice":
            e = QComboBox()
            e.addItems(list(p.choices))
            e.setCurrentText(str(p.default))
            e.currentIndexChanged.connect(self._schedule)
        else:
            e = QLineEdit(str(p.default))
            e.textChanged.connect(self._schedule)
        return e

    def values(self) -> Dict:
        out = {}
        for k, e in self._editors.items():
            if isinstance(e, QDoubleSpinBox):
                out[k] = e.value()
            elif isinstance(e, QSpinBox):
                out[k] = e.value()
            elif isinstance(e, QComboBox):
                out[k] = e.currentText()
            else:
                out[k] = e.text().strip()
        return out

    def prefill(self, part=None, package_hint: str = "") -> None:
        """Pick a template from the package text and set the name from the part."""
        if self.source_prep is not None and part is self.part:
            return                       # a downloaded source is loaded for this part; keep it
        if part is not self.part:
            self.clear_source()
        self.part = part
        if part:
            self.sym_name.setText(part.mpn)
        from ..matcher.parse import parse_query
        spec = parse_query(package_hint or (part.package if part else ""))
        key = {"QFN": "qfn", "DFN": "qfn", "SOIC": "dual", "SSOP": "dual", "BGA": "bga", "QFP": "qfp",
               "SOT": "sot", "CHIP": "chip", "HEADER": "header"}.get(spec.group)
        if key:
            self.tpl.setCurrentIndex(self.tpl.findData(key))
            ed = self._editors
            def setv(k, v):
                if v is not None and k in ed:
                    w = ed[k]
                    (w.setValue(v) if hasattr(w, "setValue") else w.setText(str(v)))
            if spec.pitch:
                setv("pitch", spec.pitch)
            if key == "dual" and spec.pins:
                setv("pins", spec.pins)
                if spec.body_x and spec.body_y:
                    setv("E1", min(spec.body_x, spec.body_y)); setv("D", max(spec.body_x, spec.body_y))
                if spec.lead_span:
                    setv("E_min", spec.lead_span - 0.2); setv("E_max", spec.lead_span + 0.2)
            if key == "sot" and spec.pins:
                setv("pins_left", (spec.pins + 1) // 2); setv("pins_right", spec.pins // 2)
                setv("name", "")
            if key == "qfn" and spec.pins:
                side = spec.pins // 4
                setv("pins_x", side); setv("pins_y", spec.pins // 2 - side)
                if spec.body_x:
                    setv("D_min", spec.body_x - 0.1); setv("D_max", spec.body_x + 0.1)
                    setv("E_min", spec.body_y - 0.1); setv("E_max", spec.body_y + 0.1)
                if spec.ep_x:
                    setv("D2", spec.ep_x); setv("E2", spec.ep_y)
            if key == "qfp" and spec.pins:
                setv("pins_x", spec.pins // 4); setv("pins_y", spec.pins // 4)
                if spec.body_x:
                    setv("D1", spec.body_x); setv("E1", spec.body_y)
            if key == "bga" and spec.pins:
                import math
                n = spec.rows or int(round(math.sqrt(spec.pins)))
                setv("rows", n); setv("cols", spec.cols or n)
                if spec.body_x:
                    setv("D", spec.body_x); setv("E", spec.body_y)
        self._schedule()

    # ------------------------------------------------------------ pin table
    def pins(self) -> List[SB.PinDef]:
        out = []
        for r in range(self.table.rowCount()):
            def cell(c):
                it = self.table.item(r, c)
                return it.text().strip() if it else ""
            if cell(0) or cell(1):
                t = cell(2)
                out.append(SB.PinDef(cell(0), cell(1), t if t in PIN_TYPES else "", cell(3).lower(), cell(4)))
        return out

    def set_pins(self, pins: List[SB.PinDef]) -> None:
        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)          # or rows reshuffle while they are filled
        self.table.setRowCount(0)
        for p in pins:
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, v in enumerate([p.number, p.name, p.etype or p.resolved_type(), p.side, p.group]):
                self.table.setItem(r, c, NatItem(v))
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.AscendingOrder)
        self.table.blockSignals(False)
        self._schedule()

    def receive_text(self, text: str) -> None:
        dlg = PasteDialog(text, self)
        if dlg.exec():
            pins = SB.parse_pin_text(dlg.edit.toPlainText())
            if pins:
                self.set_pins(pins)

    def _paste(self):
        self.receive_text(QGuiApplication.clipboard().text())

    def _load_csv(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Pin file", "", "Pin tables (*.csv *.tsv *.txt);;All files (*)")
        if fn:
            with open(fn, encoding="utf-8", errors="replace") as f:
                pins = SB.parse_csv(f.read())
            self.set_pins(pins)

    def _from_pads(self):
        if not self.fp:
            return
        nums = sorted({p.number for p in self.fp.pads if p.number}, key=lambda s: [int(t) if t.isdigit() else t for t in __import__("re").split(r"(\d+)", s)])
        self.set_pins([SB.PinDef(n, n, "passive") for n in nums])

    def _add_row(self):
        self.table.setSortingEnabled(False)
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c in range(self.table.columnCount()):
            self.table.setItem(r, c, NatItem(""))
        self.table.setSortingEnabled(True)
        self.table.editItem(self.table.item(r, 0))

    def _del_row(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True) or [self.table.rowCount() - 1]
        for r in rows:
            if r >= 0:
                self.table.removeRow(r)
        self._schedule()

    def _clear(self):
        self.table.setRowCount(0)
        self._schedule()

    # ------------------------------------------------------------ rebuild + checks
    def _schedule(self, *_):
        self._timer.start()

    def rebuild(self):
        dens = "ABC"[self.density.currentIndex()]
        if self.tpl.currentData() == "__source__":
            self.fp = self.source_fp
            self.fpview.set_footprint(self.fp, "" if self.fp else "The source has no footprint")
        else:
            try:
                self.fp = FB.build(self.tpl.currentData(), self.values(), dens)
                self.fpview.set_footprint(self.fp)
            except Exception as e:
                self.fp = None
                self.fpview.set_footprint(None, f"Cannot build: {e}")
        pins = self.pins()
        name = self.sym_name.text().strip() or "NewPart"
        if self.sym_mode.currentData() == "source" and self.source_sym is not None:
            import dataclasses
            self.sym = dataclasses.replace(self.source_sym, name=name)
            self.unit_sel.setMaximum(max(1, len(self.sym.units)))
            self.symview.set_symbol(self.sym, self.unit_sel.value() - 1)
        elif pins:
            name = self.sym_name.text().strip() or "NewPart"
            self.sym = SB.build_symbol(name, pins, self.ref.text().strip() or "U", self.maxpins.value(),
                                       self.split.currentText())
            self.unit_sel.setMaximum(max(1, len(self.sym.units)))
            self.symview.set_symbol(self.sym, self.unit_sel.value() - 1)
        else:
            self.sym = None
            self.symview.set_symbol(None)
        self._update_checks()
        self._refresh_3d()

    def _update_checks(self):
        if not self.sym:
            self.checks.setText("Add pins to build a symbol (paste from the datasheet, load a CSV, or 'From pads').")
            return
        res = check_pins_pads([(p.number, p.name, p.etype) for p in self.sym.pins], self.fp)
        parts = [("✓ " if c.ok else ("⚠ " if c.level == "warn" else "✗ ")) + c.label + (f" ({c.detail})" if c.detail else "")
                 for c in res]
        self.checks.setText("   ".join(parts))
        ok = all(c.ok or c.level != "error" for c in res)
        self.save_btn.setEnabled(ok)
        self.place_btn.setEnabled(ok)

    def _save(self, place: bool = False):
        if self.sym:
            self.save_requested.emit(self.sym, self.fp, place)

    def showEvent(self, e):
        super().showEvent(e)
        self.target_lbl.setText(self.target_fn())

    # ------------------------------------------------------------ downloaded sources
    @property
    def uses_source_fp(self) -> bool:
        return self.tpl.currentData() == "__source__" and self.source_fp is not None

    def clear_source(self) -> None:
        self.source_prep = self.source_fp = self.source_sym = None
        i = self.tpl.findData("__source__")
        if i >= 0:
            self.tpl.removeItem(i)
        j = self.sym_mode.findData("source")
        if j >= 0:
            self.sym_mode.removeItem(j)

    def load_source(self, prep, part=None) -> None:
        """Put a downloaded symbol + footprint (+ 3D model) into the builder to edit or complete."""
        self.clear_source()
        self.part = part or self.part
        self.source_prep = prep
        self.source_fp = prep.footprint_model()
        self.source_sym = prep.symbol
        self.sym_name.setText(prep.symbol_name or (part.mpn if part else ""))
        if self.source_fp is not None:
            self.tpl.blockSignals(True)
            self.tpl.insertItem(0, f"From source: {self.source_fp.name}", "__source__")
            self.tpl.setCurrentIndex(0)
            self.tpl.blockSignals(False)
            self._template_changed()
        if self.source_sym is not None:
            self.sym_mode.insertItem(0, f"From source ({prep.source.split(' (')[0]})", "source")
            self.sym_mode.setCurrentIndex(0)
        if self.source_sym is not None and len(self.source_sym.units) > 1:
            pins = [SB.PinDef(p.number, p.name, p.etype, "", f"Unit {i + 1}")
                    for i, u in enumerate(self.source_sym.units) for p in u.pins]
        else:
            pins = [SB.PinDef(n, nm, t) for n, nm, t in prep.pins()]
        self.set_pins(pins)
        self.rebuild()

    def _pin_selected(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        nums = {self.table.item(r, 0).text().strip() for r in rows if self.table.item(r, 0)}
        nums.discard("")
        self.fpview.set_highlight(nums)
        self.view3d.set_highlight(nums)
        # show the unit that holds the pin
        if self.sym and nums and len(self.sym.units) > 1:
            for i, u in enumerate(self.sym.units):
                if any(p.number in nums for p in u.pins):
                    self.unit_sel.setValue(i + 1)
                    break
        self.symview.set_highlight(nums)

    def _refresh_3d(self):
        """Load the 3D model for the current footprint when the 3D tab is shown."""
        if self.fp_views.currentWidget() is not self.view3d:
            return
        from ..library.importer import PreparedImport
        from ..model3d import scene_for
        fp = self.fp
        if fp is None:
            self.view3d.set_scene(None, None, "No footprint")
            return
        prep = self.source_prep if self.uses_source_fp else PreparedImport(source="", symbol_name="", footprint=fp)
        key = (id(prep), id(fp))
        if getattr(self, "_3d_key", None) == key:
            return
        self._3d_key = key
        self.view3d.set_scene(None, fp, "Loading 3D model…")
        import asyncio

        def done(res):
            if getattr(self, "_3d_key", None) == key:
                mesh, msg = res
                if mesh is None and not self.uses_source_fp:
                    msg = "Generated footprint: no 3D model yet (pads shown)."
                self.view3d.set_scene(mesh, fp, msg)
        self.state.bridge.run(asyncio.to_thread(scene_for, prep), done,
                              lambda e: self.view3d.set_scene(None, fp, f"3D preview failed: {e}"))

    def _pins_edited(self, *_):
        # editing the pin table means the user wants a generated symbol
        if self.sym_mode.currentData() == "source":
            self.sym_mode.setCurrentIndex(self.sym_mode.findData("generated"))
        self._schedule()

    # ------------------------------------------------------------ datasheet helpers
    def _datasheet(self):
        path, marks = self.datasheet_fn()
        if not path:
            QMessageBox.information(self, "Datasheet", "The datasheet isn't loaded yet (see the Datasheet tab).")
        return path, marks

    def _pins_from_ds(self):
        path, marks = self._datasheet()
        if not path:
            return
        from .. import datasheet as ds
        page = marks.get("Pinout")
        if page is None:
            QMessageBox.information(self, "Pins from datasheet",
                                    "No pinout page was detected. In the Datasheet tab, go to the pin table and use "
                                    "'Page text → pin table', or paste the rows here.")
            return
        texts = ds.cached_texts(path)
        text = "\n".join(texts[i] for i in (page, page + 1) if i < len(texts))
        self.receive_text(text)

    def _show_drawing(self):
        path, marks = self._datasheet()
        if not path:
            return
        page = next((marks[k] for k in ("Package outline", "Land pattern") if marks.get(k) is not None), 0)
        if self._drawing_dlg is not None:
            self._drawing_dlg.close()
            self._drawing_dlg.deleteLater()
        self._drawing_dlg = DrawingDialog(path, page, self)
        self._drawing_dlg.show()

    def _ask_dims(self):
        mpn = self.part.mpn if self.part else self.sym_name.text().strip()
        if not mpn:
            return
        pkg = (self.part.package if self.part else "") or self.tpl.currentText()
        self.ask_chat.emit(
            f"Read the package dimensions of {mpn} ({pkg}) from its datasheet package outline page. "
            f"List pitch e, body D/E (or D/E1), lead span E, lead width b, foot length L, height A and any exposed "
            f"pad D2/E2 in mm as min/nom/max, with the page number. Template: {self.tpl.currentText()}.")
