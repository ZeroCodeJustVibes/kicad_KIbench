"""Check & Clean window.

Check: KiCad ERC + label checks + part voltage ratings vs. net voltages, with editable net voltages and an
"Ask AI to review" hand-off to the chat.
Clean: proposed tidy-up steps in a checkable table, a before/after preview, and Apply (backup + netlist check).
"""
from __future__ import annotations

import asyncio
import html
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QPainter, QPen
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel,
                               QMainWindow, QMessageBox, QPushButton, QRadioButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..design import check as chk
from ..design import board_tidy, clean
from ..design import board_check as bchk
from ..design.context import _sheet_files, find_root_schematic
from .theme import tokens

SEV_ORDER = {"error": 0, "warning": 1, "info": 2}
SEV_LABEL = {"error": "Error", "warning": "Warning", "info": "Info"}


def _sev_color(sev: str) -> QColor:
    t = tokens()
    return QColor({"error": t.danger, "warning": t.warn}.get(sev, t.muted))


# ====================================================================== sheet preview
class SheetView(QWidget):
    """SVG plot of a sheet (mm viewBox) with change overlays. Wheel zooms at the cursor, drag pans."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 240)
        self.renderer: Optional[QSvgRenderer] = None
        self.page = QRectF(0, 0, 297, 210)
        self.center = QPointF(148, 105)
        self.scale = 2.0
        self.changes: List[clean.Change] = []
        self.checked: set = set()
        self.current: Optional[str] = None
        self.show_after = True
        self.message = "Find changes to see a preview"
        self.background = QColor(250, 250, 247)
        self.legend: Optional[str] = None
        self.marker: Optional[Tuple[float, float]] = None
        self._drag = None

    def set_svg(self, data: Optional[bytes], refit: bool = False) -> None:
        self.renderer = QSvgRenderer(QByteArray(data)) if data else None
        if self.renderer is not None and self.renderer.isValid():
            vb = self.renderer.viewBoxF()
            if vb.width() > 0:
                if refit or vb != self.page:
                    self.page = vb
                    self.fit()
        self.update()

    def fit(self) -> None:
        self.center = self.page.center()
        self.scale = min(self.width() / max(1, self.page.width()), self.height() / max(1, self.page.height())) * 0.96
        self.update()

    def focus(self, ch: clean.Change) -> None:
        self.current = ch.key
        xs, ys = [ch.where[0]], [ch.where[1]]
        for a, b in ch.old_segs + ch.new_segs:
            xs += [a[0], b[0]]
            ys += [a[1], b[1]]
        for bx in ch.old_boxes + ch.new_boxes:
            xs += [bx.x1, bx.x2]
            ys += [bx.y1, bx.y2]
        w, h = max(xs) - min(xs) + 25, max(ys) - min(ys) + 20
        self.center = QPointF((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2)
        self.scale = max(2.0, min(self.width() / w, self.height() / h, 40.0))
        self.update()

    def focus_point(self, x: float, y: float, span: float = 12.0) -> None:
        self.marker = (x, y)
        self.center = QPointF(x, y)
        self.scale = max(2.0, min(self.width() / span, self.height() / span, 60.0))
        self.update()

    def _to_px(self, x: float, y: float) -> QPointF:
        return QPointF((x - self.center.x()) * self.scale + self.width() / 2,
                       (y - self.center.y()) * self.scale + self.height() / 2)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), self.background)
        if self.renderer is None or not self.renderer.isValid():
            p.setPen(QColor(120, 120, 120))
            p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, self.message)
            return
        tl = self._to_px(self.page.left(), self.page.top())
        br = self._to_px(self.page.right(), self.page.bottom())
        self.renderer.render(p, QRectF(tl, br))
        for ch in self.changes:
            on = ch.key in self.checked
            cur = ch.key == self.current
            if not (on or cur):
                continue
            wpx = 3.0 if cur else 1.6
            old = QColor(220, 60, 60, 220 if cur else 120)
            new = QColor(30, 160, 80, 230 if cur else 140)
            p.setBrush(Qt.NoBrush)
            if not self.show_after or cur:
                p.setPen(QPen(old, wpx, Qt.DashLine))
                for a, b in ch.old_segs:
                    p.drawLine(self._to_px(*a), self._to_px(*b))
                for bx in ch.old_boxes:
                    p.drawRect(QRectF(self._to_px(bx.x1, bx.y1), self._to_px(bx.x2, bx.y2)))
            if self.show_after or cur:
                p.setPen(QPen(new, wpx))
                for a, b in ch.new_segs:
                    p.drawLine(self._to_px(*a), self._to_px(*b))
                for bx in ch.new_boxes:
                    p.drawRect(QRectF(self._to_px(bx.x1, bx.y1), self._to_px(bx.x2, bx.y2)))
            if cur:
                c = self._to_px(*ch.where)
                p.setPen(QPen(QColor(47, 111, 237), 2))
                p.drawEllipse(c, 14, 14)
        if self.marker is not None:
            c = self._to_px(*self.marker)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(255, 170, 0), 3))
            p.drawEllipse(c, 16, 16)
            p.drawLine(c + QPointF(-24, 0), c + QPointF(-10, 0))
            p.drawLine(c + QPointF(10, 0), c + QPointF(24, 0))
        p.setPen(QColor(150, 150, 150) if self.background.lightness() < 128 else QColor(110, 110, 110))
        p.drawText(8, self.height() - 8, self.legend if self.legend is not None else (
            ("After" if self.show_after else "Before") +
            "  ·  red dashed = old, green = new  ·  wheel: zoom, drag: pan, double-click: whole view"))

    def wheelEvent(self, e):
        f = 1.2 if e.angleDelta().y() > 0 else 1 / 1.2
        pos = e.position()
        before = QPointF((pos.x() - self.width() / 2) / self.scale + self.center.x(),
                         (pos.y() - self.height() / 2) / self.scale + self.center.y())
        self.scale = max(0.3, min(self.scale * f, 80))
        self.center = QPointF(before.x() - (pos.x() - self.width() / 2) / self.scale,
                              before.y() - (pos.y() - self.height() / 2) / self.scale)
        self.update()

    def mousePressEvent(self, e):
        self._drag = e.position()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            d = e.position() - self._drag
            self._drag = e.position()
            self.center -= QPointF(d.x() / self.scale, d.y() / self.scale)
            self.update()

    def mouseReleaseEvent(self, e):
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        self.fit()


# ====================================================================== check tab
class CheckTab(QWidget):
    ask_chat = Signal(str, bool)

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.report: Optional[chk.Report] = None
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.run_btn = QPushButton("Run check")
        self.run_btn.setProperty("primary", True)
        self.run_btn.clicked.connect(self.run)
        self.lookup = QCheckBox("Look up ratings online")
        self.lookup.setToolTip("Fetch voltage ratings from distributor data for parts with an MPN or LCSC number "
                               "(slower; uses the enabled distributors)")
        self.ai_btn = QPushButton("Ask AI to review")
        self.ai_btn.setToolTip("Send the check to the chat: the agent compares datasheet limits with the "
                               "voltages on every part's pins")
        self.ai_btn.clicked.connect(self.ask_ai)
        self.summary = QLabel("Run the check to see ERC results, label problems and voltage ratings.")
        self.summary.setObjectName("muted")
        bar.addWidget(self.run_btn)
        bar.addWidget(self.lookup)
        bar.addWidget(self.ai_btn)
        bar.addWidget(self.summary, 1)
        lay.addLayout(bar)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        frow = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.addItems(["All issues", "Errors", "Warnings", "Info", "ERC", "Labels", "Voltage", "Interfaces"])
        self.filter.currentIndexChanged.connect(self._fill_issues)
        frow.addWidget(QLabel("Show"))
        frow.addWidget(self.filter)
        frow.addStretch(1)
        ll.addLayout(frow)
        self.issues = QTreeWidget()
        self.issues.setHeaderLabels(["", "Check", "Issue", "Parts"])
        self.issues.setRootIsDecorated(False)
        self.issues.setAlternatingRowColors(True)
        self.issues.header().setSectionResizeMode(2, QHeaderView.Stretch)
        self.issues.currentItemChanged.connect(self._issue_selected)
        self.issues.itemDoubleClicked.connect(self._copy_refs)
        ll.addWidget(self.issues, 1)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail.setObjectName("muted")
        ll.addWidget(self.detail)
        split.addWidget(left)

        right = QTabWidget()
        nets = QWidget()
        nl = QVBoxLayout(nets)
        nl.setContentsMargins(0, 4, 0, 0)
        nrow = QHBoxLayout()
        self.show_unnamed = QCheckBox("Show unnamed nets")
        self.show_unnamed.toggled.connect(self._fill_nets)
        nh = QLabel("Type a voltage to set it for a net; the next check uses it.")
        nh.setObjectName("muted")
        nrow.addWidget(nh, 1)
        nrow.addWidget(self.show_unnamed)
        nl.addLayout(nrow)
        self.nets = QTableWidget(0, 4)
        self.nets.setHorizontalHeaderLabels(["Net", "Volts", "From", "Pins"])
        self.nets.verticalHeader().hide()
        self.nets.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.nets.itemChanged.connect(self._volts_edited)
        nl.addWidget(self.nets)
        right.addTab(nets, "Net voltages")
        self.parts = QTableWidget(0, 5)
        self.parts.setHorizontalHeaderLabels(["Ref", "Value", "MPN / LCSC", "Rating", "Highest V on pins"])
        self.parts.verticalHeader().hide()
        self.parts.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.parts.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        right.addTab(self.parts, "Parts")
        split.addWidget(right)
        split.setSizes([620, 420])
        lay.addWidget(split, 1)
        self._filling = False

    # ------------------------------------------------------------ run
    def run(self):
        proj = self.state.project_dir
        if not proj:
            self.summary.setText("Open a KiCad project first.")
            return
        self.run_btn.setEnabled(False)
        self.summary.setText("Checking… (ERC and netlist via kicad-cli)")
        lookup = self._lookup if self.lookup.isChecked() else None

        async def job():
            return await asyncio.to_thread(chk.run_check, Path(proj), None, lookup)
        self.state.bridge.run(job(), self._done, self._failed)

    def _lookup(self, key: str):
        """Runs on a worker thread: ask the search engine (on the async loop) for the part's specs."""
        from ..runner import get_runner
        try:
            p = get_runner().run(self.state.engine.get(key), timeout=25)
        except Exception:
            return None
        return p.specs if p else None

    def _failed(self, e):
        self.run_btn.setEnabled(True)
        self.summary.setText(f"Check failed: {e}")

    def _done(self, rep: chk.Report):
        self.run_btn.setEnabled(True)
        self.report = rep
        c = rep.counts()
        known = len([n for n in rep.nets if n.volts is not None and n.source != "ground"])
        t = tokens()
        self.summary.setText(
            f"<span style='color:{t.danger}'><b>{c['error']}</b> errors</span> · "
            f"<span style='color:{t.warn}'><b>{c['warning']}</b> warnings</span> · {c['info']} info · "
            f"{known} nets with a voltage · {sum(1 for p in rep.parts if p.ratings)}/{len(rep.parts)} parts rated"
            + (f" · {html.escape(rep.notes[0])}" if rep.notes else ""))
        self._fill_issues()
        self._fill_nets()
        self._fill_parts()

    # ------------------------------------------------------------ tables
    def _fill_issues(self):
        self.issues.clear()
        if not self.report:
            return
        f = self.filter.currentText()
        for i in self.report.issues:
            if f == "Errors" and i.severity != "error" or f == "Warnings" and i.severity != "warning" or \
                    f == "Info" and i.severity != "info" or f in ("ERC", "Labels", "Voltage", "Interfaces") and i.category != f:
                continue
            it = QTreeWidgetItem(["●", i.category, i.message, ", ".join(i.refs[:8]) + ("…" if len(i.refs) > 8 else "")])
            it.setForeground(0, QBrush(_sev_color(i.severity)))
            it.setToolTip(0, SEV_LABEL.get(i.severity, i.severity))
            it.setToolTip(2, i.message)
            it.setData(0, Qt.UserRole, i)
            self.issues.addTopLevelItem(it)
        for c in (0, 1, 3):
            self.issues.resizeColumnToContents(c)
        if self.issues.topLevelItemCount() == 0:
            self.detail.setText("No issues found." if self.report.issues == [] else "Nothing matches this filter.")

    def _issue_selected(self, cur, _prev):
        if cur is None:
            return
        i: chk.Issue = cur.data(0, Qt.UserRole)
        bits = [f"<b>{html.escape(i.message)}</b>"]
        if i.detail:
            bits.append(html.escape(i.detail))
        if i.nets:
            bits.append("Nets: " + html.escape(", ".join(i.nets[:10])))
        if i.pos:
            bits.append(f"At ({i.pos[0]:.2f}, {i.pos[1]:.2f}) mm on sheet {html.escape(i.sheet or '/')}")
        if i.refs:
            bits.append("Double-click to copy the references (paste into KiCad's Find, Ctrl+F).")
        self.detail.setText("<br>".join(bits))

    def _copy_refs(self, item, _col):
        i: chk.Issue = item.data(0, Qt.UserRole)
        if i.refs:
            QGuiApplication.clipboard().setText(" ".join(i.refs))
            self.state.status.emit(f"Copied {' '.join(i.refs[:6])}: in KiCad press Ctrl+F and paste to find it")

    def _fill_nets(self):
        if not self.report:
            return
        self._filling = True
        rows = [n for n in self.report.nets if self.show_unnamed.isChecked() or not chk.UNNAMED_RE.match(n.name)
                or n.volts is not None]
        rows.sort(key=lambda n: (n.volts is None, -(n.volts or 0), n.name))
        self.nets.setRowCount(len(rows))
        t = tokens()
        for r, n in enumerate(rows):
            a = QTableWidgetItem(n.name)
            a.setFlags(a.flags() & ~Qt.ItemIsEditable)
            v = QTableWidgetItem("" if n.volts is None else f"{n.volts:g}")
            v.setData(Qt.UserRole, n.name)
            if n.source == "override":
                v.setForeground(QBrush(QColor(t.accent)))
            s = QTableWidgetItem(n.source or "unknown")
            s.setFlags(s.flags() & ~Qt.ItemIsEditable)
            c = QTableWidgetItem(str(len(n.nodes)))
            c.setFlags(c.flags() & ~Qt.ItemIsEditable)
            for col, it in enumerate((a, v, s, c)):
                self.nets.setItem(r, col, it)
        self.nets.resizeColumnsToContents()
        self.nets.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._filling = False

    def _volts_edited(self, item: QTableWidgetItem):
        if self._filling or item.column() != 1 or not self.state.project_dir:
            return
        name = item.data(Qt.UserRole)
        txt = item.text().strip().replace(",", ".").rstrip("vV").strip()
        ov = chk.load_overrides(self.state.project_dir)
        if txt == "":
            ov.pop(name, None)
        else:
            try:
                ov[name] = float(txt)
            except ValueError:
                self.state.status.emit(f"'{item.text()}' is not a voltage")
                return
        chk.save_overrides(self.state.project_dir, ov)
        self.summary.setText(f"Saved {name} = {txt or 'auto'} V. Run the check again to use it.")

    def _fill_parts(self):
        rep = self.report
        self.parts.setRowCount(len(rep.parts))
        for r, p in enumerate(rep.parts):
            vmax = max((pin["volts"] for pin in p.pins if pin["volts"] is not None), default=None)
            rating = "; ".join(f"{'≤' if x.kind == 'across' else 'supply ≤'}{x.vmax:g} V" for x in p.ratings)
            for col, txt in enumerate((p.ref, p.value, p.mpn or p.lcsc, rating, "" if vmax is None else f"{vmax:g} V")):
                self.parts.setItem(r, col, QTableWidgetItem(txt))
        self.parts.resizeColumnsToContents()
        self.parts.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)

    def ask_ai(self):
        prompt = ("Review my schematic. Call check_schematic, then compare each part's limits (datasheet absolute "
                  "maximum ratings, voltage ratings, supply range) with the voltages on its pins. Tell me the "
                  "problems first (most severe first, cite datasheet pages), then the parts and nets you could not "
                  "check because the MPN or the net voltage is unknown.")
        self.ask_chat.emit(prompt, True)


# ====================================================================== board check tab
class BoardCheckTab(QWidget):
    ask_chat = Signal(str, bool)
    CATS = ["DRC", "Unrouted", "Parity", "Clearance", "Silkscreen", "Placement", "Decoupling"]

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.report: Optional[bchk.BoardReport] = None
        self.svg: Optional[bytes] = None
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.run_btn = QPushButton("Run board check")
        self.run_btn.setProperty("primary", True)
        self.run_btn.clicked.connect(self.run)
        self.ai_btn = QPushButton("Ask AI to review")
        self.ai_btn.setToolTip("Send the board check to the chat: DRC in plain words, track widths vs. current, "
                               "high-voltage spacing, decoupling")
        self.ai_btn.clicked.connect(self.ask_ai)
        self.summary = QLabel("Checks the saved board file: KiCad DRC and schematic parity, voltage clearances, "
                              "silkscreen references and decoupling capacitors.")
        self.summary.setObjectName("muted")
        self.summary.setWordWrap(True)
        bar.addWidget(self.run_btn)
        bar.addWidget(self.ai_btn)
        bar.addWidget(self.summary, 1)
        lay.addLayout(bar)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        frow = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.addItems(["All issues", "Errors", "Warnings", "Info"] + self.CATS)
        self.filter.currentIndexChanged.connect(self._fill_issues)
        frow.addWidget(QLabel("Show"))
        frow.addWidget(self.filter)
        frow.addStretch(1)
        ll.addLayout(frow)
        self.issues = QTreeWidget()
        self.issues.setHeaderLabels(["", "Check", "Issue", "Parts"])
        self.issues.setRootIsDecorated(False)
        self.issues.setAlternatingRowColors(True)
        self.issues.header().setSectionResizeMode(2, QHeaderView.Stretch)
        self.issues.currentItemChanged.connect(self._issue_selected)
        self.issues.itemDoubleClicked.connect(self._copy_refs)
        ll.addWidget(self.issues, 1)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail.setObjectName("muted")
        ll.addWidget(self.detail)
        split.addWidget(left)

        right = QTabWidget()
        self.view = SheetView()
        self.view.background = QColor(18, 20, 28)
        self.view.message = "Run the check to see the board"
        self.view.legend = "Click an issue to find it  ·  wheel: zoom, drag: pan, double-click: whole board"
        right.addTab(self.view, "Board")
        nets = QWidget()
        nl = QVBoxLayout(nets)
        nl.setContentsMargins(0, 4, 0, 0)
        nh = QLabel("Type a voltage to set it for a net (shared with the schematic check).")
        nh.setObjectName("muted")
        nl.addWidget(nh)
        self.nets = QTableWidget(0, 6)
        self.nets.setHorizontalHeaderLabels(["Net", "Volts", "From", "Pads", "Track width", "Length"])
        self.nets.verticalHeader().hide()
        self.nets.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.nets.itemChanged.connect(self._volts_edited)
        nl.addWidget(self.nets)
        right.addTab(nets, "Nets")
        split.addWidget(right)
        split.setSizes([600, 520])
        lay.addWidget(split, 1)
        self._filling = False

    def run(self):
        proj = self.state.project_dir
        if not proj:
            self.summary.setText("Open a KiCad project first.")
            return
        self.run_btn.setEnabled(False)
        self.summary.setText("Checking… (DRC via kicad-cli; large boards take a minute)")

        async def job():
            rep = await asyncio.to_thread(bchk.run_board_check, Path(proj))
            svg = None
            if rep.board:
                from ..kicad import cli
                try:
                    svg = await asyncio.to_thread(cli.export_board_svg, Path(rep.board).read_text(),
                                                  Path(rep.board).name)
                except (cli.CliError, OSError):
                    svg = None
            return rep, svg
        self.state.bridge.run(job(), self._done, self._failed)

    def _failed(self, e):
        self.run_btn.setEnabled(True)
        self.summary.setText(f"Check failed: {e}")

    def _done(self, res):
        rep, svg = res
        self.run_btn.setEnabled(True)
        self.report, self.svg = rep, svg
        c = rep.counts()
        t = tokens()
        size = f"{rep.size[0]:.1f} × {rep.size[1]:.1f} mm · " if rep.size else ""
        self.summary.setText(
            f"<span style='color:{t.danger}'><b>{c['error']}</b> errors</span> · "
            f"<span style='color:{t.warn}'><b>{c['warning']}</b> warnings</span> · {c['info']} info · "
            f"{html.escape(Path(rep.board).name) if rep.board else ''} · {size}{rep.layers} copper layers · "
            f"{rep.footprints} parts" + (f" · {html.escape(rep.notes[0])}" if rep.notes else ""))
        self.view.message = "No board preview (kicad-cli not found)"
        self.view.set_svg(svg, refit=True)
        self._fill_issues()
        self._fill_nets()

    def _fill_issues(self):
        self.issues.clear()
        if not self.report:
            return
        f = self.filter.currentText()
        for i in self.report.issues:
            if f == "Errors" and i.severity != "error" or f == "Warnings" and i.severity != "warning" or \
                    f == "Info" and i.severity != "info" or f in self.CATS and i.category != f:
                continue
            it = QTreeWidgetItem(["●", i.category, i.message, ", ".join(i.refs[:8]) + ("…" if len(i.refs) > 8 else "")])
            it.setForeground(0, QBrush(_sev_color(i.severity)))
            it.setToolTip(0, SEV_LABEL.get(i.severity, i.severity))
            it.setToolTip(2, i.message)
            it.setData(0, Qt.UserRole, i)
            self.issues.addTopLevelItem(it)
        for c in (0, 1, 3):
            self.issues.resizeColumnToContents(c)
        if self.issues.topLevelItemCount() == 0:
            self.detail.setText("No issues found." if not self.report.issues else "Nothing matches this filter.")

    def _issue_selected(self, cur, _prev):
        if cur is None:
            return
        i: chk.Issue = cur.data(0, Qt.UserRole)
        bits = [f"<b>{html.escape(i.message)}</b>"]
        if i.detail:
            bits.append(html.escape(i.detail[:900]))
        if i.nets:
            bits.append("Nets: " + html.escape(", ".join(i.nets[:10])))
        if i.pos:
            bits.append(f"At ({i.pos[0]:.2f}, {i.pos[1]:.2f}) mm")
            self.view.focus_point(*i.pos, 35.0)
        else:
            self.view.marker = None
            self.view.update()
        if i.refs:
            bits.append("Double-click to copy the references (paste into KiCad's Find, Ctrl+F).")
        self.detail.setText("<br>".join(bits))

    def _copy_refs(self, item, _col):
        i: chk.Issue = item.data(0, Qt.UserRole)
        if i.refs:
            QGuiApplication.clipboard().setText(" ".join(i.refs))
            self.state.status.emit(f"Copied {' '.join(i.refs[:6])}: in KiCad press Ctrl+F and paste to find it")

    def _fill_nets(self):
        if not self.report:
            return
        self._filling = True
        rows = [n for n in self.report.nets if not chk.UNNAMED_RE.match(n.name) or n.volts is not None]
        self.nets.setRowCount(len(rows))
        for r, n in enumerate(rows):
            w = "" if n.min_width is None else (f"{n.min_width:g}" if n.min_width == n.max_width
                                                 else f"{n.min_width:g} – {n.max_width:g}") + " mm"
            vals = (n.name, "" if n.volts is None else f"{n.volts:g}", n.source or "unknown", str(n.pads), w,
                    f"{n.length:g} mm" if n.length else "")
            for col, txt in enumerate(vals):
                it = QTableWidgetItem(txt)
                if col == 1:
                    it.setData(Qt.UserRole, n.name)
                else:
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.nets.setItem(r, col, it)
        self.nets.resizeColumnsToContents()
        self.nets.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._filling = False

    def _volts_edited(self, item: QTableWidgetItem):
        if self._filling or item.column() != 1 or not self.state.project_dir:
            return
        name = item.data(Qt.UserRole)
        txt = item.text().strip().replace(",", ".").rstrip("vV").strip()
        ov = chk.load_overrides(self.state.project_dir)
        if txt == "":
            ov.pop(name, None)
        else:
            try:
                ov[name] = float(txt)
            except ValueError:
                self.state.status.emit(f"'{item.text()}' is not a voltage")
                return
        chk.save_overrides(self.state.project_dir, ov)
        self.summary.setText(f"Saved {name} = {txt or 'auto'} V. Run the check again to use it.")

    def ask_ai(self):
        prompt = ("Review my PCB layout. Call check_board (and check_schematic if you need part ratings). Explain "
                  "the DRC and clearance problems in plain words, check power track widths against the current the "
                  "parts draw (say which current you assumed and use IPC-2152), check spacing on high-voltage nets, "
                  "and point out decoupling capacitors that are far from their IC. Most severe first.")
        self.ask_chat.emit(prompt, True)


# ====================================================================== tidy back-ends
class SheetBackend:
    """Schematic sheet tidy-up (rewrites the file; KiCad 10 has no schematic editing API)."""
    file_label = "Sheet"
    dark = False
    kind_label = clean.KIND_LABEL
    skip_reason = "they would change a connection or overlap something"
    apply_note = "(checking the netlist first)"
    options = (("wires", "Straighten wires", "Diagonal or crooked wires become horizontal/vertical"),
               ("merge", "Merge wire pieces", "Join collinear wire segments, drop zero-length wires"),
               ("junctions", "Junctions", "Remove junction dots that join fewer than three things"),
               ("align", "Align parts", "Line up parts that are almost in a row or column "
                                        "(moves by whole grid steps; wires follow)"),
               ("fields", "Tidy texts", "Reference / value texts next to the part, readable and evenly spaced"))

    def files(self, proj: Path) -> List[Path]:
        root = find_root_schematic(proj)
        return list(_sheet_files(root)) if root else []

    def make_options(self, d):
        return clean.CleanOptions(**d)

    def plan(self, text, opts, exclude=None):
        return clean.plan(text, opts, exclude)

    def svg(self, text, name):
        from ..kicad import cli
        return cli.export_svg(text, name)

    def plan_note(self, p) -> str:
        return ""

    def confirm_open(self, parent, path: Path) -> Optional[bool]:
        """None = cancelled. Returns whether KiCad will be reloaded live (never for sheets)."""
        if clean.is_open_in_kicad(path):
            r = QMessageBox.question(
                parent, "Sheet is open in KiCad",
                f"{path.name} is open in KiCad's schematic editor.\n\nSave your work there first. After "
                "applying, use File → Revert in the schematic editor to load the tidied sheet (unsaved edits in "
                "KiCad would be lost).\n\nApply now?")
            if r != QMessageBox.Yes:
                return None
        return False

    def apply(self, state, text, opts, ex, path: Path, proj, live: bool):
        p = clean.plan(text, opts, ex)
        ok, msg = clean.verify(text, p.text, path.name, proj)
        if not ok:
            return p, ok, msg, None, ""
        bak = clean.write(path, p.text, proj)
        return p, ok, msg, bak, "In KiCad: File → Revert to reload the sheet."

    def after_undo(self, state, live: bool) -> str:
        return "In KiCad: File → Revert."


class BoardBackend:
    """Board tidy-up. When the board is open in KiCad's PCB editor, KiBench saves it there first and
    reloads it after writing (through KiCad's API)."""
    file_label = "Board"
    dark = True
    kind_label = board_tidy.KIND_LABEL
    skip_reason = "the part would overlap another one, get too close to copper or leave the board"
    apply_note = "(running DRC before and after)"
    options = (("refs", "Tidy references", "Reference texts next to their part: off pads, other texts, "
                                           "silkscreen lines and the board edge. Texts that look fine stay."),
               ("align", "Align parts", "Line up identical parts that are almost in a row or column "
                                        "(shifts up to 0.5 mm; only parts that are not routed yet)"),
               ("spacing", "Even spacing", "Equal gaps in rows of three or more identical parts "
                                           "(only parts that are not routed yet)"))

    def files(self, proj: Path) -> List[Path]:
        from ..design.board_check import find_board
        main = find_board(proj)
        rest = [b for b in sorted(proj.glob("*.kicad_pcb")) if b != main]
        return ([main] if main else []) + rest

    def make_options(self, d):
        return board_tidy.BoardTidyOptions(**d)

    def plan(self, text, opts, exclude=None):
        return board_tidy.plan(text, opts, exclude)

    def svg(self, text, name):
        from ..kicad import cli
        return cli.export_board_svg(text, name)

    def plan_note(self, p) -> str:
        m = p.model
        if m is None:
            return ""
        routed = getattr(p, "fixed_parts", 0)
        return f" Routed or locked parts are never moved ({routed} of {len(m.footprints)})." if routed else ""

    @staticmethod
    def _kicad_has(state, path: Path) -> bool:
        k = getattr(state, "kicad", None)
        if k is None:
            return False
        op = k.open_board_path()
        try:
            return op is not None and Path(op).resolve() == path.resolve()
        except OSError:
            return False

    def confirm_open(self, parent, path: Path) -> Optional[bool]:
        state = parent.state
        if self._kicad_has(state, path):
            r = QMessageBox.question(
                parent, "Board is open in KiCad",
                f"{path.name} is open in KiCad's PCB editor.\n\nKiBench will save the board there, apply the "
                "changes, and reload it in KiCad. KiCad's Ctrl+Z can't undo this; use 'Undo last apply' here."
                "\n\nApply now?")
            return True if r == QMessageBox.Yes else None
        if clean.is_open_in_kicad(path):
            r = QMessageBox.question(
                parent, "Board is open in KiCad",
                f"{path.name} seems to be open in KiCad, but KiBench can't reach it (is the API server on? "
                "Preferences → Plugins).\n\nSave your work there first. After applying, use File → Revert in the "
                "PCB editor (unsaved edits in KiCad would be lost).\n\nApply now?")
            if r != QMessageBox.Yes:
                return None
        return False

    def apply(self, state, text, opts, ex, path: Path, proj, live: bool):
        k = getattr(state, "kicad", None)
        if live and k is not None:
            if not k.save_board():
                return None, False, f"KiCad did not save the board ({k.error})", None, ""
            text = path.read_text()            # may include edits made in KiCad since Find changes
        p, ok, msg, dropped = board_tidy.plan_verified(text, path, opts, ex)
        if dropped:
            msg += f"; {len(dropped)} change(s) near new DRC problems were left out"
        if not ok:
            return p, False, msg, None, ""
        if not p.changed:
            return p, False, "nothing left to change", None, ""
        bak = board_tidy.write(path, p.text, proj)
        if live and k is not None:
            note = "KiCad reloaded the board." if k.revert_board() else \
                "KiCad did not reload it: use File → Revert in the PCB editor."
        else:
            note = "If the board is open in KiCad: File → Revert to reload it."
        return p, ok, msg, bak, note

    def after_undo(self, state, live: bool) -> str:
        k = getattr(state, "kicad", None)
        if live and k is not None and k.revert_board():
            return "KiCad reloaded the board."
        return "If the board is open in KiCad: File → Revert."


# ====================================================================== clean tab
class CleanTab(QWidget):
    """Proposed tidy-up steps in a checkable table, preview, Apply. `backend` decides schematic or board."""

    def __init__(self, state, backend=None, parent=None):
        super().__init__(parent)
        self.state = state
        self.be = backend or SheetBackend()
        self.live = False
        self.sheet: Optional[Path] = None
        self.text = ""
        self.mtime = 0.0
        self.plan: Optional[clean.Plan] = None
        self.svg_before: Optional[bytes] = None
        self.svg_after: Optional[bytes] = None
        self.last_backup: Optional[Path] = None
        self._busy = False
        self._preview_timer = QTimer(self, singleShot=True, interval=500, timeout=self._refresh_after)

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.sheets = QComboBox()
        self.sheets.setMinimumWidth(220)
        top.addWidget(QLabel(self.be.file_label))
        top.addWidget(self.sheets)
        self.opt = {}
        for k, label, tip in self.be.options:
            cb = QCheckBox(label)
            cb.setChecked(True)
            cb.setToolTip(tip)
            self.opt[k] = cb
            top.addWidget(cb)
        self.find_btn = QPushButton("Find changes")
        self.find_btn.setProperty("primary", True)
        self.find_btn.clicked.connect(self.find)
        top.addWidget(self.find_btn)
        top.addStretch(1)
        lay.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Change", "Details"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.Interactive)
        self.tree.header().setStretchLastSection(True)
        self.tree.setColumnWidth(0, 260)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.currentItemChanged.connect(self._current)
        ll.addWidget(self.tree, 1)
        sel = QHBoxLayout()
        for label, on in (("Select all", True), ("Select none", False)):
            b = QPushButton(label)
            b.setProperty("flat", True)
            b.clicked.connect(lambda _=False, on=on: self._select_all(on))
            sel.addWidget(b)
        sel.addStretch(1)
        self.count = QLabel()
        self.count.setObjectName("muted")
        sel.addWidget(self.count)
        ll.addLayout(sel)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        tog = QHBoxLayout()
        self.r_after = QRadioButton("After")
        self.r_before = QRadioButton("Before")
        self.r_after.setChecked(True)
        self.r_after.toggled.connect(self._toggle_view)
        tog.addWidget(self.r_before)
        tog.addWidget(self.r_after)
        tog.addStretch(1)
        self.preview_note = QLabel()
        self.preview_note.setObjectName("muted")
        tog.addWidget(self.preview_note)
        rl.addLayout(tog)
        self.view = SheetView()
        if self.be.dark:
            self.view.background = QColor(18, 20, 28)
        rl.addWidget(self.view, 1)
        split.addWidget(right)
        split.setSizes([440, 700])
        lay.addWidget(split, 1)

        bottom = QHBoxLayout()
        self.status = QLabel(f"Choose a {self.be.file_label.lower()} and press Find changes. Nothing is written "
                             "until you press Apply.")
        self.status.setWordWrap(True)
        self.status.setObjectName("muted")
        bottom.addWidget(self.status, 1)
        self.undo_btn = QPushButton("Undo last apply")
        self.undo_btn.setVisible(False)
        self.undo_btn.clicked.connect(self.undo)
        bottom.addWidget(self.undo_btn)
        self.apply_btn = QPushButton("Apply selected changes")
        self.apply_btn.setProperty("primary", True)
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self.apply)
        bottom.addWidget(self.apply_btn)
        lay.addLayout(bottom)

    # ------------------------------------------------------------ sheets
    def reload_sheets(self):
        self.sheets.clear()
        proj = self.state.project_dir
        files = self.be.files(Path(proj)) if proj else []
        if not files:
            self.status.setText("Open a KiCad project first." if not proj else
                                f"No {self.be.file_label.lower()} found in the project.")
            return
        for f in files:
            try:
                label = str(f.relative_to(proj))
            except ValueError:
                label = f.name
            self.sheets.addItem(label, str(f))

    def _options(self):
        return self.be.make_options({k: cb.isChecked() for k, cb in self.opt.items()})

    def _excluded(self) -> set:
        out = set()
        for i in range(self.tree.topLevelItemCount()):
            grp = self.tree.topLevelItem(i)
            for j in range(grp.childCount()):
                it = grp.child(j)
                if it.checkState(0) != Qt.Checked:
                    out.add(it.data(0, Qt.UserRole).key)
        return out

    # ------------------------------------------------------------ find
    def find(self):
        if self._busy or self.sheets.currentData() is None:
            return
        self.sheet = Path(self.sheets.currentData())
        try:
            self.text = self.sheet.read_text()
            self.mtime = self.sheet.stat().st_mtime
        except OSError as e:
            self.status.setText(f"Cannot read {self.sheet.name}: {e}")
            return
        self._set_busy(True, "Looking for changes…")
        text, opts, name, be = self.text, self._options(), self.sheet.name, self.be

        async def job():
            p = await asyncio.to_thread(be.plan, text, opts)
            from ..kicad import cli
            before = after = None
            note = ""
            try:
                before = await asyncio.to_thread(be.svg, text, name)
                after = await asyncio.to_thread(be.svg, p.text, name) if p.changed else before
            except cli.CliError as e:
                note = f"No preview: {e}"
            return p, before, after, note
        self.state.bridge.run(job(), self._found, self._failed)

    def _found(self, res):
        p, before, after, note = res
        self._set_busy(False)
        self.plan, self.svg_before, self.svg_after = p, before, after
        self.tree.blockSignals(True)
        self.tree.clear()
        groups: Dict[str, QTreeWidgetItem] = {}
        for ch in p.changes:
            g = groups.get(ch.kind)
            if g is None:
                g = QTreeWidgetItem([self.be.kind_label.get(ch.kind, ch.kind), ""])
                g.setFlags(g.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
                g.setCheckState(0, Qt.Checked)
                self.tree.addTopLevelItem(g)
                groups[ch.kind] = g
            it = QTreeWidgetItem([ch.title, ch.detail])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(0, Qt.Checked)
            it.setData(0, Qt.UserRole, ch)
            it.setToolTip(0, ch.title)
            it.setToolTip(1, ch.detail)
            g.addChild(it)
        for k, g in groups.items():
            g.setText(1, f"{g.childCount()} change{'s' if g.childCount() != 1 else ''}")
            g.setExpanded(g.childCount() <= 30)
        self.tree.blockSignals(False)
        self.view.changes = list(p.changes)
        self.view.checked = {c.key for c in p.changes}
        self.view.message = note or "No preview"
        self.view.set_svg(self.svg_after if self.r_after.isChecked() else self.svg_before, refit=True)
        self.preview_note.setText(note)
        self._update_count()
        if not p.changes:
            self.status.setText(f"{self.sheet.name} looks tidy: nothing to change"
                                + (f" ({len(p.skipped)} possible steps skipped: {self.be.skip_reason})"
                                   if p.skipped else ".") + self.be.plan_note(p))
        else:
            self.status.setText(f"{len(p.changes)} proposed changes. Untick what you don't want, then Apply. "
                                + (f"{len(p.skipped)} other steps were skipped: {self.be.skip_reason}. "
                                   if p.skipped else "") + self.be.plan_note(p))

    def _failed(self, e):
        self._set_busy(False)
        self.status.setText(f"Failed: {e}")

    def _set_busy(self, on: bool, msg: str = ""):
        self._busy = on
        self.find_btn.setEnabled(not on)
        self.apply_btn.setEnabled(not on and bool(self.plan and self.plan.changes))
        if msg:
            self.status.setText(msg)

    # ------------------------------------------------------------ selection
    def _item_changed(self, item, col):
        if col != 0:
            return
        self.view.checked = {c.key for c in (self.plan.changes if self.plan else [])} - self._excluded()
        self.view.update()
        self._update_count()
        self._preview_timer.start()

    def _select_all(self, on: bool):
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setCheckState(0, Qt.Checked if on else Qt.Unchecked)

    def _update_count(self):
        n = len(self.plan.changes) if self.plan else 0
        k = n - len(self._excluded())
        self.count.setText(f"{k} of {n} selected")
        self.apply_btn.setEnabled(k > 0 and not self._busy)
        self.apply_btn.setText(f"Apply {k} change{'s' if k != 1 else ''}" if n else "Apply selected changes")

    def _current(self, cur, _prev):
        ch = cur.data(0, Qt.UserRole) if cur is not None else None
        if isinstance(ch, clean.Change):
            self.view.focus(ch)

    def _toggle_view(self, after: bool):
        self.view.show_after = after
        self.view.set_svg(self.svg_after if after else self.svg_before)

    def _refresh_after(self):
        """Re-render the 'after' preview for the current selection (in the background)."""
        if not self.plan or self._busy:
            return
        text, opts, ex, name, be = self.text, self._options(), self._excluded(), self.sheet.name, self.be

        async def job():
            p = await asyncio.to_thread(be.plan, text, opts, ex)
            from ..kicad import cli
            try:
                return await asyncio.to_thread(be.svg, p.text, name)
            except cli.CliError:
                return None
        self.preview_note.setText("updating preview…")

        def done(svg):
            self.preview_note.setText("")
            if svg:
                self.svg_after = svg
                if self.r_after.isChecked():
                    self.view.set_svg(svg)
        self.state.bridge.run(job(), done, lambda e: self.preview_note.setText(f"preview failed: {e}"))

    # ------------------------------------------------------------ apply
    def apply(self):
        if not self.plan or not self.sheet:
            return
        try:
            if self.sheet.stat().st_mtime != self.mtime:
                self.status.setText(f"{self.sheet.name} changed on disk since the changes were found. "
                                    "Press Find changes again.")
                return
        except OSError:
            pass
        live = self.be.confirm_open(self, self.sheet)
        if live is None:
            return
        self.live = live
        text, opts, ex, sheet, be = self.text, self._options(), self._excluded(), self.sheet, self.be
        proj = self.state.project_dir
        n_sel = len(self.plan.changes) - len(ex)
        self._set_busy(True, "Applying… " + be.apply_note)

        async def job():
            return await asyncio.to_thread(be.apply, self.state, text, opts, ex, sheet, proj, live)
        self.state.bridge.run(job(), lambda r: self._applied(r, n_sel), self._failed)

    def _applied(self, res, n_sel: int):
        p, ok, msg, bak, note = res
        self._set_busy(False)
        if not ok:
            self.status.setText(f"Not applied: {msg}. Nothing was written. Untick the changes near the problem "
                                "and try again.")
            return
        self.last_backup = bak
        self.undo_btn.setVisible(True)
        extra = "" if len(p.changes) == n_sel else \
            f" ({n_sel - len(p.changes)} selected change(s) were left out: they depended on unticked ones or " \
            f"caused new problems)"
        self.status.setText(f"Applied {len(p.changes)} changes to {self.sheet.name}{extra}. {msg}. Backup: "
                            f"{bak.name} in kibench-backups. {note}")
        self.state.status.emit(f"Tidied {self.sheet.name}: {len(p.changes)} changes (backup {bak.name})")
        self.plan = None
        self.tree.clear()
        self.view.changes = []
        self._update_count()
        try:
            self.text = self.sheet.read_text()
            self.mtime = self.sheet.stat().st_mtime
        except OSError:
            pass

    def undo(self):
        if not self.last_backup or not self.sheet:
            return
        try:
            if self.sheet.stat().st_mtime != self.mtime:
                r = QMessageBox.question(self, "Undo", f"{self.sheet.name} was changed after the tidy-up (probably "
                                         "saved in KiCad). Restore the backup anyway and lose those edits?")
                if r != QMessageBox.Yes:
                    return
            shutil.copy2(self.last_backup, self.sheet)
        except OSError as e:
            self.status.setText(f"Undo failed: {e}")
            return
        self.status.setText(f"Restored {self.sheet.name} from {self.last_backup.name}. "
                            + self.be.after_undo(self.state, self.live))
        self.undo_btn.setVisible(False)
        self.last_backup = None


# ====================================================================== window
class CheckWindow(QMainWindow):
    ask_chat = Signal(str, bool)

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.setWindowTitle("KiBench — Check & Clean (schematic and board)")
        self.resize(1250, 800)
        self.tabs = QTabWidget()
        self.check = CheckTab(state)
        self.clean = CleanTab(state)
        self.board_check = BoardCheckTab(state)
        self.board_clean = CleanTab(state, BoardBackend())
        self.tabs.addTab(self.check, "Schematic check")
        self.tabs.addTab(self.clean, "Schematic clean-up")
        self.tabs.addTab(self.board_check, "Board check")
        self.tabs.addTab(self.board_clean, "Board tidy-up")
        self.setCentralWidget(self.tabs)
        self.check.ask_chat.connect(self.ask_chat)
        self.board_check.ask_chat.connect(self.ask_chat)
        state.project_changed.connect(lambda _p: self._reload())
        self._reload()

    def _reload(self):
        self.clean.reload_sheets()
        self.board_clean.reload_sheets()

    def show_tab(self, name: str) -> None:
        tab = {"check": self.check, "clean": self.clean, "board": self.board_check,
               "board-tidy": self.board_clean}.get(name)
        if tab is not None:
            self.tabs.setCurrentWidget(tab)

    def showEvent(self, e):
        super().showEvent(e)
        if self.clean.sheets.count() == 0 or self.board_clean.sheets.count() == 0:
            self._reload()
