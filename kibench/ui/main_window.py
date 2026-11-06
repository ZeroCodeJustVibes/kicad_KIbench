"""The KiBench window: results | part detail | chat."""
from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import QFileDialog, QFrame, QLabel, QMainWindow, QSplitter, QToolBar, QVBoxLayout

from .. import APP_NAME, __version__
from .chat import ChatPanel
from .part_view import PartPanel
from .results import ResultsPanel
from .settings_dialog import SettingsDialog
from .state import AppState
from .theme import tokens


class MainWindow(QMainWindow):
    def __init__(self, state: AppState):
        super().__init__()
        self.state = state
        self.resize(1600, 950)
        self.results = ResultsPanel(state)
        self.part = PartPanel(state)
        self.chat = ChatPanel(state)
        self.split = QSplitter(Qt.Horizontal)
        self.split.setContentsMargins(8, 8, 8, 8)
        self.split.setHandleWidth(8)
        self.columns = []
        for w in (self.results, self.part, self.chat):
            col = QFrame()
            col.setObjectName("column")
            lay = QVBoxLayout(col)
            lay.setContentsMargins(8, 8, 8, 8)
            lay.addWidget(w)
            self.columns.append(col)
            self.split.addWidget(col)
        self.split.setSizes([520, 680, 400])
        self.split.setStretchFactor(1, 1)
        self.setCentralWidget(self.split)

        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)
        act_open = QAction("Open project…", self)
        act_open.triggered.connect(self.choose_project)
        tb.addAction(act_open)
        self.act_chat = QAction("Chat", self, checkable=True)
        self.act_chat.setChecked(state.settings.chat_visible)
        self.act_chat.setShortcut(QKeySequence("Ctrl+J"))
        self.act_chat.toggled.connect(self.toggle_chat)
        tb.addAction(self.act_chat)
        act_check = QAction("Check && Clean", self)
        act_check.setToolTip("Check and tidy up the schematic and the board: ERC/DRC, labels, voltage ratings and clearances, reference texts (Ctrl+Shift+C)")
        act_check.setShortcut(QKeySequence("Ctrl+Shift+C"))
        act_check.triggered.connect(self.open_check)
        tb.addAction(act_check)
        act_set = QAction("Settings", self)
        act_set.triggered.connect(self.open_settings)
        tb.addAction(act_set)
        self.proj_lbl = QLabel()
        self.proj_lbl.setObjectName("projectLabel")
        tb.addSeparator()
        tb.addWidget(self.proj_lbl)

        QShortcut(QKeySequence("Ctrl+K"), self, activated=self.focus_search, context=Qt.ApplicationShortcut)

        self.results.open_part.connect(state.open_part)
        state.part_opened.connect(self.part.show_part)
        state.status.connect(lambda m: self.statusBar().showMessage(m, 8000))
        state.project_changed.connect(lambda _p: self._update_title())
        state.index_ready.connect(self._index_ready)
        self.chat.find_part.connect(state.open_part)
        self.chat.show_in_search.connect(self.results.run_query)
        self.chat.get_cad.connect(self._get_cad)
        self.chat.open_build.connect(self._build)
        self.part.settings_requested.connect(self.open_settings)
        self.results.import_part.connect(self._get_cad)
        self.results.build_part.connect(self._build)
        self.results.ask_chat.connect(self._ask_chat)
        self.part.build.ask_chat.connect(self._ask_chat)
        self.chat.open_datasheet.connect(self._open_datasheet)
        self.part.imported.connect(lambda lid: self.statusBar().showMessage(
            f"Imported {lid} (lib id copied). In KiCad: Add Symbol (A) → {lid}", 15000))
        self.toggle_chat(state.settings.chat_visible)
        self._update_title()
        state.start_index()

    def _update_title(self):
        p = self.state.project_dir
        self.setWindowTitle(f"{APP_NAME} {__version__}" + (f" — {p.name}" if p else ""))
        t = tokens()
        dot = (f"<span style='color:{t.ok}'>●</span> KiCad connected" if self.state.kicad.connected
               else f"<span style='color:{t.muted}'>○</span> KiCad not connected")
        name = f"<b>{html.escape(p.name)}</b>" if p else "no project (imports need a project or a global library)"
        self.proj_lbl.setText(f"{name} &nbsp;·&nbsp; {dot}")
        self.proj_lbl.setToolTip(str(p) if p else "")

    def showEvent(self, e):
        super().showEvent(e)
        if not getattr(self, "_sized", False):
            self._sized = True
            w = self.width()
            self.split.setSizes([int(w * 0.32), int(w * 0.43), int(w * 0.25)])

    def _index_ready(self, n: int):
        self.chat.set_index(self.state.fp_index)
        self.statusBar().showMessage(f"Footprint index ready: {n} stock footprints", 5000)
        if self.state.current:
            self.part.refresh_sources()           # adds the stock-footprint step
            if self.state.current.package:
                self.part.fptab.set_query(self.state.current.package)

    def focus_search(self):
        self.results.box.setFocus()
        self.results.box.selectAll()

    def toggle_chat(self, on: bool):
        self.columns[2].setVisible(on)
        self.state.settings.chat_visible = on

    def choose_project(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Open KiCad project", str(self.state.project_dir or Path.home()),
                                            "KiCad project (*.kicad_pro)")
        if fn:
            self.state.set_project(Path(fn).parent)

    def open_settings(self):
        if SettingsDialog(self.state.settings, self).exec():
            self.chat.reload_settings()
            self.state.start_index()

    def open_check(self, tab: str = ""):
        if getattr(self, "check_win", None) is None:
            from .check_window import CheckWindow
            self.check_win = CheckWindow(self.state)
            self.check_win.ask_chat.connect(self._ask_chat)
        if tab:
            self.check_win.show_tab(tab)
        self.check_win.show()
        self.check_win.raise_()
        self.check_win.activateWindow()

    def _ask_chat(self, text: str, send: bool = True):
        self.act_chat.setChecked(True)
        self.chat.ask(text, send)

    def _same(self, part) -> bool:
        cur = self.state.current
        return cur is not None and cur.key == part.key and bool(self.part.hits) and self.part.part is cur

    def _get_cad(self, part):
        if self._same(part):                       # sources already known: go straight to import
            self.part.start_import()
            return
        self.part.auto_import = True               # runs once the source lookup finishes
        self.part.tabs.setCurrentWidget(self.part.sources)
        self.state.open_part(part)

    def _build(self, part):
        if self._same(part):
            self.part.open_in_builder(self.part.best_hit())
            return
        self.part.auto_build = True
        self.state.open_part(part)
        self.part.tabs.setCurrentWidget(self.part.build)

    def _open_datasheet(self, part, page: int):
        if not self.state.current or self.state.current.key != part.key:
            self.state.open_part(part)
        self.part.tabs.setCurrentWidget(self.part.ds)
        if page >= 0:
            if self.part.ds.path:
                self.part.ds.goto(page)
            else:
                self.part.ds.loaded.connect(lambda _p, pg=page: self.part.ds.goto(pg), Qt.SingleShotConnection)

    def closeEvent(self, e):
        if getattr(self, "check_win", None) is not None:
            self.check_win.close()
        self.state.settings.save()
        self.part.watcher.stop()
        super().closeEvent(e)
