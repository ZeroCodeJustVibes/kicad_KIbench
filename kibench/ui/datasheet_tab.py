"""Datasheet viewer tab: cached PDF, page search, detected bookmarks, copy page text."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPointF, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtPdf import QPdfDocument, QPdfSearchModel
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton, QToolButton, QVBoxLayout, QWidget)

from .. import datasheet as ds


class DatasheetTab(QWidget):
    send_text = Signal(str)          # page text -> symbol builder
    loaded = Signal(object)          # Path

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.path: Optional[Path] = None
        self.bookmarks = []
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 4, 0, 0)
        bar = QHBoxLayout()
        self.find = QLineEdit()
        self.find.setPlaceholderText("Search in datasheet")
        self.find.returnPressed.connect(self._search)
        self.prev = QToolButton(text="◀")
        self.next = QToolButton(text="▶")
        self.prev.clicked.connect(lambda: self._step(-1))
        self.next.clicked.connect(lambda: self._step(1))
        self.hits = QLabel("")
        bar.addWidget(self.find, 1)
        bar.addWidget(self.prev)
        bar.addWidget(self.next)
        bar.addWidget(self.hits)
        self.copy_btn = QPushButton("Page text → pin table")
        self.copy_btn.setToolTip("Send the current page's text to the symbol builder's pin table")
        self.copy_btn.clicked.connect(self._copy_page)
        self.ext = QPushButton("Open externally")
        self.ext.clicked.connect(lambda: self.path and QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path))))
        bar.addWidget(self.copy_btn)
        bar.addWidget(self.ext)
        v.addLayout(bar)
        self.bm_bar = QHBoxLayout()
        self.bm_bar.addWidget(QLabel("Jump to:"))
        self.bm_bar.addStretch(1)
        v.addLayout(self.bm_bar)
        self.doc = QPdfDocument(self)
        self.view = QPdfView(self)
        self.view.setDocument(self.doc)
        self.view.setPageMode(QPdfView.PageMode.MultiPage)
        self.view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self.search_model = QPdfSearchModel(self)
        self.search_model.setDocument(self.doc)
        self.view.setSearchModel(self.search_model)
        v.addWidget(self.view, 1)
        self.status = QLabel("Select a part to load its datasheet.")
        v.addWidget(self.status)

    def clear(self):
        self.doc.close()
        self.path = None
        self._set_bookmarks([])

    def load_part(self, part) -> None:
        self.clear()
        if not part.datasheet_url:
            self.status.setText("No datasheet URL for this part.")
            return
        self.status.setText("Downloading datasheet…")
        mpn = part.mpn

        async def work():
            p = await ds.fetch_datasheet(self.state.engine.client, mpn, part.datasheet_url)
            texts = await asyncio.to_thread(ds.cached_texts, p)
            return p, ds.detect_bookmarks(texts)

        def ok(res):
            if not self.state.current or self.state.current.mpn != mpn:
                return
            p, bms = res
            self.path = p
            self.doc.load(str(p))
            self._set_bookmarks(bms)
            self.status.setText(f"{p.name} · {self.doc.pageCount()} pages (cached)")
            self.loaded.emit(p)
        self.state.bridge.run(work(), ok, lambda e: self.status.setText(f"Datasheet failed: {e}  ({part.datasheet_url})"))

    def _set_bookmarks(self, bms):
        self.bookmarks = bms
        while self.bm_bar.count() > 2:
            w = self.bm_bar.takeAt(1).widget()
            if w:
                w.deleteLater()
        for b in bms:
            btn = QToolButton(text=f"{b.title} (p{b.page + 1})")
            btn.clicked.connect(lambda _=False, pg=b.page: self.goto(pg))
            self.bm_bar.insertWidget(self.bm_bar.count() - 1, btn)

    def page_of(self, title: str) -> Optional[int]:
        return next((b.page for b in self.bookmarks if b.title == title), None)

    def goto(self, page: int) -> None:
        self.view.pageNavigator().jump(page, QPointF(0, 0), self.view.zoomFactor())

    def _search(self):
        self.search_model.setSearchString(self.find.text())
        self._idx = -1
        self.state.bridge.call_soon(lambda: self._step(1))

    def _step(self, d: int):
        n = self.search_model.rowCount(self.search_model.index(0, 0).parent())
        self.hits.setText(f"{n} hits" if n else "no hits")
        if not n:
            return
        self._idx = (getattr(self, "_idx", -1) + d) % n
        self.view.setCurrentSearchResultIndex(self._idx)
        link = self.search_model.resultAtIndex(self._idx)
        self.view.pageNavigator().jump(link)
        self.hits.setText(f"{self._idx + 1}/{n}")

    def current_page(self) -> int:
        return self.view.pageNavigator().currentPage()

    def _copy_page(self):
        if not self.path:
            return
        texts = ds.cached_texts(self.path)
        pg = self.current_page()
        if 0 <= pg < len(texts):
            self.send_text.emit(texts[pg])
