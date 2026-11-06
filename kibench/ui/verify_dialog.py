"""Verification view: footprint next to the datasheet package drawing, checks, then confirm to write."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QScrollArea, QSplitter, QTabWidget, QVBoxLayout, QWidget)

from .. import datasheet as ds
from ..library.importer import PreparedImport
from ..library.verify import verify
from .preview import FootprintView, SymbolView, preview_symbol


class VerifyDialog(QDialog):
    def __init__(self, prep: PreparedImport, lib, datasheet: Optional[Path] = None, package_page: Optional[int] = None,
                 parent=None):
        super().__init__(parent)
        self.prep = prep
        self.setWindowTitle(f"Verify import — {prep.fields.get('MPN') or prep.symbol_name}")
        self.resize(1200, 720)
        v = QVBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        left = QSplitter(Qt.Vertical)          # symbol and footprint together, so neither gets missed
        self.symv = SymbolView()
        self.symv.set_symbol(preview_symbol(prep))
        self.fpv = FootprintView()
        self.fpv.set_footprint(prep.footprint_model(), "Stock footprint " + prep.stock_footprint if prep.stock_footprint else "")
        from ..model3d import scene_for
        from .view3d import Model3DView
        self.v3d = Model3DView()
        mesh, msg = scene_for(prep)           # STEP meshes are cached, so this is quick after the preview
        self.v3d.set_scene(mesh, prep.footprint_model(), msg)
        bottom = QSplitter(Qt.Horizontal)
        bottom.addWidget(self.fpv)
        bottom.addWidget(self.v3d)
        for title, w in (("Symbol", self.symv), ("Footprint · 3D model", bottom)):
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            lab = QLabel(title)
            lab.setObjectName("sectionTitle")
            bl.addWidget(lab)
            bl.addWidget(w, 1)
            left.addWidget(box)
        split.addWidget(left)
        # datasheet package drawing
        pdf = QScrollArea()
        pdf.setWidgetResizable(True)
        lab = QLabel("No package drawing page detected in the datasheet.")
        lab.setAlignment(Qt.AlignCenter)
        if datasheet and package_page is not None:
            try:
                img = QImage.fromData(ds.render_page_png(datasheet, package_page, 1.6))
                lab = QLabel()
                lab.setPixmap(QPixmap.fromImage(img))
            except Exception as e:
                lab.setText(f"Could not render the datasheet page: {e}")
        pdf.setWidget(lab)
        split.addWidget(pdf)
        has_drawing = bool(datasheet and package_page is not None)
        pdf.setVisible(has_drawing)
        # checks
        right = QWidget()
        rv = QVBoxLayout(right)
        rep = verify(prep)
        self.checks = QListWidget()
        for c in rep.checks:
            icon = "✓" if c.ok else ("✗" if c.level == "error" else "⚠" if c.level == "warn" else "ℹ")
            it = QListWidgetItem(f"{icon}  {c.label}" + (f" — {c.detail}" if c.detail else ""))
            self.checks.addItem(it)
        rv.addWidget(QLabel("<b>Checks</b>"))
        rv.addWidget(self.checks, 1)
        facts = []
        if rep.pitch:
            facts.append(f"Pitch: {rep.pitch} mm")
        if rep.body:
            facts.append(f"Fab outline: {rep.body[0]} × {rep.body[1]} mm")
        if rep.pad_span:
            facts.append(f"Pad span: {rep.pad_span[0]} × {rep.pad_span[1]} mm")
        if prep.model_bytes:
            facts.append(f"3D model: {prep.model_filename} offset {prep.model_offset} rot {prep.model_rotate}")
        facts.append(f"Source: {prep.source}")
        try:
            facts.append(f"Target: {lib.sym_path.name} / {lib.fp_dir.name} in {lib.base}")
        except Exception:
            pass
        info = QLabel("<br>".join(facts))
        info.setWordWrap(True)
        info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        rv.addWidget(info)
        fields = QLabel("<br>".join(f"<b>{k}</b>: {v}" for k, v in prep.fields.items() if v))
        fields.setWordWrap(True)
        rv.addWidget(fields)
        split.addWidget(right)
        split.setSizes([420, 480, 340] if has_drawing else [700, 0, 480])
        v.addWidget(split, 1)
        bb = QHBoxLayout()
        self.force = QCheckBox("Write even though checks failed")
        self.force.setVisible(not rep.ok)
        bb.addWidget(self.force)
        self.buttons = QDialogButtonBox()
        self.ok_btn = self.buttons.addButton("Confirm and write", QDialogButtonBox.AcceptRole)
        self.ok_btn.setProperty("primary", True)
        self.buttons.addButton(QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.ok_btn.setEnabled(rep.ok)
        self.force.toggled.connect(lambda on: self.ok_btn.setEnabled(rep.ok or on))
        bb.addWidget(self.buttons)
        v.addLayout(bb)
