"""Left column: search box, filters and merged results table."""
from __future__ import annotations

import time
from typing import List

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QTableView,
                               QToolButton, QVBoxLayout, QWidget, QCompleter)

from ..models import Part
from ..search.query import Filters

COLS = ["MPN", "Manufacturer", "Package", "Key specs", "Stock", "Price", "CAD", "DS"]


def key_specs(p: Part, n: int = 3) -> str:
    skip = ("Operating Temperature", "Mounting", "Package", "RoHS", "Packaging", "Series", "Part Status",
            "Base Product Number", "Supplier Device Package", "Mounting Type", "Package / Case")
    out = []
    for k, v in p.specs.items():
        if any(s.lower() in k.lower() for s in skip):
            continue
        out.append(v)
        if len(out) >= n:
            break
    return " · ".join(out) or p.description[:60]


class ResultsModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.parts: List[Part] = []

    def set_parts(self, parts: List[Part]):
        self.beginResetModel()
        self.parts = parts
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return len(self.parts)

    def columnCount(self, parent=QModelIndex()):
        return len(COLS)

    def headerData(self, s, o, role=Qt.DisplayRole):
        if o == Qt.Horizontal and role == Qt.DisplayRole:
            return COLS[s]
        return None

    def data(self, idx, role=Qt.DisplayRole):
        p = self.parts[idx.row()]
        c = idx.column()
        o = p.best_offer()
        if role == Qt.DisplayRole:
            if c == 0:
                return p.mpn
            if c == 1:
                return p.manufacturer
            if c == 2:
                return p.package
            if c == 3:
                return key_specs(p)
            if c == 4:
                return f"{p.total_stock:,}" if p.offers else ""
            if c == 5:
                if o and o.unit_price(1) is not None:
                    jlc = f"  {p.jlc_class}" if p.jlc_class else ""
                    return f"{o.unit_price(1):.4g} {o.currency} ({o.source}){jlc}"
                return ""
            if c == 6:
                badges = []
                if p.has_cad("symbol"):
                    badges.append("Sym")
                if p.has_cad("footprint"):
                    badges.append("Fp")
                if p.has_cad("model3d"):
                    badges.append("3D")
                src = ",".join(sorted({x.source.split("/")[0] for x in p.cad}))
                return (" ".join(badges) + (f" [{src}]" if src else "")) if badges else ""
            if c == 7:
                return "PDF" if p.datasheet_url else ""
        if role == Qt.ToolTipRole:
            lines = [f"<b>{p.mpn}</b> — {p.manufacturer}", p.description]
            for off in p.offers:
                lines.append(f"{off.source} {off.sku}: stock {off.stock}, "
                             + ", ".join(f"{q}+ {pr:.4g}" for q, pr in off.price_breaks[:4]))
            return "<br>".join(lines)
        if role == Qt.ForegroundRole and c == 4 and p.total_stock <= 0:
            return QColor(200, 80, 80)
        if role == Qt.TextAlignmentRole and c in (4,):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None


class ResultsPanel(QWidget):
    open_part = Signal(object)
    import_part = Signal(object)
    build_part = Signal(object)
    ask_chat = Signal(str, bool)          # text, send now

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        self.box = QLineEdit()
        self.box.setObjectName("searchBox")
        self.box.setPlaceholderText("Search parts: MPN, LCSC code (C12594) or '10k 0603 1%'   [Ctrl+K]")
        self.box.setClearButtonEnabled(True)
        self.box.returnPressed.connect(self.search)
        comp = QCompleter(self.state.settings.recent_queries)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        self.box.setCompleter(comp)
        v.addWidget(self.box)
        f1 = QHBoxLayout()
        self.cat = QLineEdit()
        self.cat.setPlaceholderText("category")
        self.pkg = QLineEdit()
        self.pkg.setPlaceholderText("package")
        self.jlc = QComboBox()
        self.jlc.addItems(["JLC: any", "Basic", "Basic+Preferred", "Extended"])
        for w in (self.cat, self.pkg, self.jlc):
            f1.addWidget(w)
        v.addLayout(f1)
        f2 = QHBoxLayout()
        self.stock = QCheckBox("In stock")
        self.cad = QCheckBox("Has CAD")
        self.param = QLineEdit()
        self.param.setPlaceholderText("parametric: Vds>=30V, Rds(on)<=10mΩ")
        f2.addWidget(self.stock)
        f2.addWidget(self.cad)
        f2.addWidget(self.param, 1)
        v.addLayout(f2)
        for w in (self.stock, self.cad):
            w.toggled.connect(self.search)
        self.jlc.currentIndexChanged.connect(self.search)
        for w in (self.cat, self.pkg, self.param):
            w.returnPressed.connect(self.search)
        self.table = QTableView()
        self.model = ResultsModel()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(False)
        for i, w in enumerate([130, 110, 90, 170, 70, 150, 90, 34]):
            self.table.setColumnWidth(i, w)
        self.table.selectionModel().currentRowChanged.connect(self._row_changed)
        self.table.doubleClicked.connect(self._double)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        v.addWidget(self.table, 1)
        self.info = QLabel("")
        self.info.setObjectName("muted")
        self.info.setWordWrap(True)
        v.addWidget(self.info)
        self._seq = 0

    def _menu(self, pos):
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QMenu
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        p = self.model.parts[idx.row()]
        m = QMenu(self)
        m.addAction("Import symbol + footprint…").triggered.connect(lambda: self.import_part.emit(p))
        m.addAction("Open in Builder").triggered.connect(lambda: self.build_part.emit(p))
        m.addSeparator()
        m.addAction("Ask chat about this part").triggered.connect(
            lambda: self.ask_chat.emit(f"About {p.mpn} ({p.manufacturer}): ", False))
        m.addAction("Find alternatives in chat").triggered.connect(
            lambda: self.ask_chat.emit(f"Find in-stock alternatives to {p.mpn} ({p.manufacturer}, {p.package}) "
                                       "for this design, and say what differs.", True))
        m.addSeparator()
        m.addAction("Copy MPN").triggered.connect(lambda: QGuiApplication.clipboard().setText(p.mpn))
        if p.lcsc:
            m.addAction(f"Copy LCSC code ({p.lcsc})").triggered.connect(
                lambda: QGuiApplication.clipboard().setText(p.lcsc))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def filters(self) -> Filters:
        j = self.jlc.currentText()
        return Filters(category=self.cat.text().strip(), package=self.pkg.text().strip(), in_stock=self.stock.isChecked(),
                       jlc="" if j.startswith("JLC") else j, has_cad=self.cad.isChecked(), parametric=self.param.text())

    def run_query(self, text: str) -> None:
        self.box.setText(text)
        self.search()

    def search(self) -> None:
        q = self.box.text().strip()
        if not q:
            return
        self._seq += 1
        seq = self._seq
        t0 = time.monotonic()
        self.info.setText("Searching…")
        rq = [x for x in self.state.settings.recent_queries if x != q]
        self.state.settings.recent_queries = ([q] + rq)[:50]

        def done(res):
            if seq != self._seq:
                return            # a newer search replaced this one
            self.model.set_parts(res.parts)
            dt = time.monotonic() - t0
            provs = ", ".join(p.label for p in self.state.engine.active_providers())
            msg = f"{len(res.parts)} parts in {dt:.2f} s{' (cache)' if res.from_cache else ''} · {provs}"
            if res.query.kind != "keyword":
                msg += f" · parsed as {res.query.kind}" + (f": {res.query.keyword}" if res.query.kind == "parametric" else "")
            if res.errors:
                msg += " · errors: " + "; ".join(f"{k}: {v}" for k, v in res.errors.items())
            self.info.setText(msg)
            if res.parts and len(res.parts) == 1:
                self.table.selectRow(0)
        self.state.bridge.run(self.state.engine.search(q, self.filters()), done,
                              lambda e: self.info.setText(f"Search failed: {e}"))

    def _row_changed(self, cur, _prev):
        if cur.isValid():
            self.open_part.emit(self.model.parts[cur.row()])

    def _double(self, idx):
        p = self.model.parts[idx.row()]
        if idx.column() == 7 and p.datasheet_url:
            QDesktopServices.openUrl(QUrl(p.datasheet_url))
