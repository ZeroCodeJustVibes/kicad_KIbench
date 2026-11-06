"""Right column: design-aware chat with part cards, design constraints and a compare table."""
from __future__ import annotations

import html
import re
from typing import List

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget, QHeaderView)

from ..agent.agent import ChatAgent
from ..agent.tools import Card, ToolBox
from ..design.context import Constraints
from .theme import tokens

TOOL_LABELS = {"search_parts": "Searching", "get_part": "Reading part", "get_design_context": "Reading design",
               "get_datasheet_text": "Reading datasheet", "match_footprint": "Matching footprint",
               "check_cad_availability": "Checking CAD sources", "recommend_parts": "Preparing cards",
               "check_schematic": "Checking schematic", "check_board": "Checking board"}


class CardWidget(QFrame):
    def __init__(self, card: Card, panel, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        t = tokens()
        p = card.part
        e = html.escape
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        o = p.best_offer()
        price = f"{o.unit_price(1):.4g} {o.currency}" if o and o.unit_price(1) is not None else "no price"
        badges = " ".join(b for b, ok in (("Sym", p.has_cad("symbol")), ("Fp", p.has_cad("footprint")),
                                           ("3D", p.has_cad("model3d"))) if ok)
        reason = e(card.reason)
        for bad in card.unverified:
            reason = reason.replace(e(bad), f"<span style='background:{t.warn};color:black' "
                                             f"title='not found in tool data'>{e(bad)}</span>")
        warn = []
        if card.unverified:
            warn.append("highlighted values were not found in tool data")
        if card.uncited:
            warn.append("limit claim without datasheet citation")
        cites = " &nbsp; ".join(f"<a href='page:{c.get('page')}' style='color:{t.accent}'>p.{c.get('page')}</a> "
                                f"{e(c.get('claim', ''))}" for c in card.citations)
        stock = p.total_stock
        stock_s = (f"<span style='color:{t.ok}'>{stock:,} in stock</span>" if stock > 0
                   else f"<span style='color:{t.danger}'>out of stock</span>")
        meta = " &nbsp;·&nbsp; ".join(x for x in (stock_s, e(price), f"JLC {e(p.jlc_class)}" if p.jlc_class else "",
                                                 badges) if x)
        lab = QLabel(f"<span style='font-size:11pt; font-weight:700'>{e(p.mpn)}</span>&nbsp; "
                     f"<span style='color:{t.muted}'>{e(p.manufacturer)}{' · ' + e(p.package) if p.package else ''}"
                     f"</span><div style='margin-top:4px'>{reason}</div>"
                     + (f"<div style='margin-top:3px; font-size:9pt'>{cites}</div>" if cites else "")
                     + (f"<div style='margin-top:3px; font-size:9pt; color:{t.warn}'>⚠ {' · '.join(warn)}</div>"
                        if warn else "")
                     + f"<div style='margin-top:4px; font-size:9pt; color:{t.muted}'>{meta}</div>")
        lab.setWordWrap(True)
        lab.setTextFormat(Qt.RichText)
        lab.setCursor(Qt.PointingHandCursor)
        lab.setToolTip("Click to open this part")
        lab.linkActivated.connect(lambda href: panel.open_datasheet.emit(p, int(href.split(":")[1]) - 1))
        v.addWidget(lab)
        grid = QGridLayout()
        grid.setSpacing(4)
        for i, (text, tip, sig, primary) in enumerate((
                ("Import", "Download symbol, footprint and 3D model from the best source, check, then write",
                 lambda: panel.get_cad.emit(p), True),
                ("Build", "Open in the builder with the downloaded symbol/footprint to edit or complete",
                 lambda: panel.open_build.emit(p), False),
                ("Datasheet", "Open the datasheet", lambda: panel.open_datasheet.emit(p, -1), False),
                ("Search", "Show in the results list", lambda: panel.show_in_search.emit(p.mpn), False),
                ("Compare", "Add to the comparison table", lambda: panel.add_compare(p), False))):
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.setProperty("primary" if primary else "flat", True)
            btn.clicked.connect(sig)
            grid.addWidget(btn, i // 3, i % 3)
        v.addLayout(grid)
        lab.mousePressEvent = lambda _e: panel.find_part.emit(p)


class ChatInput(QPlainTextEdit):
    """Enter sends; Shift+Enter inserts a new line."""
    submit = Signal()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter) and not (ev.modifiers() & Qt.ShiftModifier):
            self.submit.emit()
            return
        super().keyPressEvent(ev)


class CompareDialog(QDialog):
    def __init__(self, parts, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Compare parts")
        self.resize(900, 520)
        v = QVBoxLayout(self)
        keys = ["Manufacturer", "Package", "Stock", "Price (1)", "JLC class", "CAD"]
        spec_keys: List[str] = []
        for p in parts:
            for k in p.specs:
                if k not in spec_keys:
                    spec_keys.append(k)
        rows = keys + spec_keys
        t = QTableWidget(len(rows), len(parts))
        t.setHorizontalHeaderLabels([p.mpn for p in parts])
        t.setVerticalHeaderLabels(rows)
        for c, p in enumerate(parts):
            o = p.best_offer()
            vals = [p.manufacturer, p.package, f"{p.total_stock:,}",
                    f"{o.unit_price(1):.4g} {o.currency}" if o and o.unit_price(1) is not None else "",
                    p.jlc_class, ", ".join(x.source for x in p.cad)] + [p.specs.get(k, "") for k in spec_keys]
            for r, val in enumerate(vals):
                t.setItem(r, c, QTableWidgetItem(val))
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        v.addWidget(t)


class ChatPanel(QWidget):
    find_part = Signal(object)
    show_in_search = Signal(str)
    get_cad = Signal(object)
    open_build = Signal(object)
    open_datasheet = Signal(object, int)

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.compare: List = []
        self.toolbox = ToolBox(state.engine, lambda: state.design_context().summary(),
                               None, lambda: state.project_dir)
        self.agent = ChatAgent(state.settings, self.toolbox)
        self.busy = False
        self._thinking = None
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        top = QHBoxLayout()
        self.model_lbl = QLabel()
        self.model_lbl.setObjectName("muted")
        new = QPushButton("New chat")
        new.setProperty("flat", True)
        new.clicked.connect(self.new_chat)
        self.cmp_btn = QPushButton("Compare (0)")
        self.cmp_btn.setProperty("flat", True)
        self.cmp_btn.clicked.connect(self.show_compare)
        title = QLabel("Chat")
        title.setObjectName("sectionTitle")
        top.addWidget(title)
        top.addWidget(self.model_lbl, 1)
        top.addWidget(self.cmp_btn)
        top.addWidget(new)
        v.addLayout(top)
        # design constraints (folded away until needed)
        self.ctoggle = QPushButton("▸ Design context && constraints")
        self.ctoggle.setProperty("flat", True)
        self.ctoggle.setCheckable(True)
        self.ctoggle.setStyleSheet("text-align: left")
        self.cbox = QFrame()
        self.cbox.setObjectName("card")
        cf = QFormLayout(self.cbox)
        cf.setContentsMargins(10, 8, 10, 8)
        self.cfields = {}
        for key, label, ph in (("input_range", "Input range", "e.g. 9-14 V"), ("max_current", "Max current", "e.g. 2 A"),
                               ("temperature", "Temperature", "e.g. -40..85 °C"),
                               ("assembly", "Assembly house", "JLC Basic preferred"),
                               ("size_limits", "Size limits", "e.g. 0402 min, height < 2 mm"),
                               ("notes", "Notes", "anything else")):
            e = QLineEdit()
            e.setPlaceholderText(ph)
            e.editingFinished.connect(self._save_constraints)
            cf.addRow(label, e)
            self.cfields[key] = e
        # what the agent checks its picks against
        dh = QHBoxLayout()
        self.parts_title = QLabel("Parts in this design")
        self.parts_title.setObjectName("sectionTitle")
        rb = QPushButton("↻")
        rb.setFixedWidth(32)
        rb.setToolTip("Re-read the schematic (it is also re-read whenever you save it)")
        rb.clicked.connect(lambda: self.refresh_design(force=True))
        dh.addWidget(self.parts_title, 1)
        dh.addWidget(rb)
        cf.addRow(dh)
        self.rails_lbl = QLabel("")
        self.rails_lbl.setWordWrap(True)
        self.rails_lbl.setObjectName("muted")
        cf.addRow(self.rails_lbl)
        self.parts_tbl = QTableWidget(0, 4)
        self.parts_tbl.setHorizontalHeaderLabels(["Refs", "Value", "Footprint", "MPN"])
        self.parts_tbl.verticalHeader().hide()
        self.parts_tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.parts_tbl.setSelectionBehavior(QTableWidget.SelectRows)
        self.parts_tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.parts_tbl.horizontalHeader().setStretchLastSection(True)
        self.parts_tbl.setMinimumHeight(150)
        self.parts_tbl.setToolTip("The chat checks recommendations against these parts, rails and interfaces. "
                                  "Double-click a row to ask about that part.")
        self.parts_tbl.cellDoubleClicked.connect(self._ask_about_row)
        cf.addRow(self.parts_tbl)
        self.cbox.setVisible(False)
        self.ctoggle.toggled.connect(lambda on: (self.cbox.setVisible(on),
                                                 self.ctoggle.setText(("▾" if on else "▸") + " Design context && constraints"),
                                                 on and self.refresh_design()))
        v.addWidget(self.ctoggle, 0, Qt.AlignLeft)
        v.addWidget(self.cbox)
        # messages
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.msgs = QWidget()
        self.msg_layout = QVBoxLayout(self.msgs)
        self.msg_layout.setContentsMargins(2, 2, 8, 2)
        self.msg_layout.setSpacing(10)
        self.msg_layout.addStretch(1)
        self.scroll.setWidget(self.msgs)
        v.addWidget(self.scroll, 1)
        composer = QFrame()
        composer.setObjectName("composer")
        h = QHBoxLayout(composer)
        h.setContentsMargins(6, 4, 6, 4)
        self.input = ChatInput()
        self.input.setPlaceholderText("Ask for a part, e.g. low-side MOSFET for 12 V, logic-level from 3.3 V")
        self.input.setFixedHeight(64)
        self.input.submit.connect(self.send)
        self.send_btn = QPushButton("Send")
        self.send_btn.setProperty("primary", True)
        self.send_btn.clicked.connect(self.send)
        h.addWidget(self.input, 1)
        h.addWidget(self.send_btn, 0, Qt.AlignBottom)
        v.addWidget(composer)
        hint = QLabel("Enter to send · Shift+Enter for a new line")
        hint.setObjectName("toolNote")
        v.addWidget(hint)
        self.reload_settings()
        state.project_changed.connect(lambda _p: (self.load_constraints(), self.refresh_design()))
        self.load_constraints()
        self._add_note("Ask for parts in plain language. Recommendations come only from real search results; "
                       "importing always runs the deterministic pipeline.")

    def reload_settings(self):
        s = self.state.settings
        model = {"anthropic": s.llm_model, "openrouter": s.openrouter_model, "local": s.local_model,
                 "claude-code": s.claude_code_model or "your plan's default"}.get(s.llm_provider, "")
        self.model_lbl.setText(f"{s.llm_provider}: {model}")
        self.agent.reset()
        if s.llm_provider == "claude-code":
            from ..agent.claude_code import ClaudeCodeAgent
            self.agent = ClaudeCodeAgent(s, self.toolbox, lambda: self.state.project_dir)
        else:
            self.agent = ChatAgent(s, self.toolbox)

    def set_index(self, idx):
        self.toolbox.fp_index = idx

    # ------------------------------------------------------------ constraints
    def load_constraints(self):
        c = Constraints.load(self.state.project_dir)
        for k, e in self.cfields.items():
            e.setText(getattr(c, k))

    def refresh_design(self, force: bool = False):
        """Show the parts, rails and interfaces the agent sees through get_design_context."""
        if not self.cbox.isVisible() and not force:
            return
        try:
            ctx = self.state.design_context(refresh=force).summary(max_parts=500)
        except Exception as e:
            self.parts_title.setText("Parts in this design")
            self.rails_lbl.setText(f"Could not read the schematic: {e}")
            return
        parts = ctx.get("parts", [])
        self.parts_title.setText(f"Parts in this design ({ctx.get('part_count', 0)})")
        rails = ", ".join(f"{r['name']}" + (f" = {r['volts']:g} V" if r.get("volts") is not None else "")
                          for r in ctx.get("power_rails", []))
        ifs = ", ".join(f"{k} ({len(v)})" for k, v in ctx.get("interfaces", {}).items())
        info = []
        if rails:
            info.append(f"Rails: {rails}")
        if ifs:
            info.append(f"Interfaces: {ifs}")
        if not self.state.project_dir:
            info.append("No project open.")
        elif not parts:
            info.append("No parts in the schematic yet (or it isn't saved).")
        self.rails_lbl.setText("  ·  ".join(info))
        self.parts_tbl.setRowCount(len(parts))
        for r, p in enumerate(parts):
            for c, k in enumerate(("refs", "value", "footprint", "mpn")):
                self.parts_tbl.setItem(r, c, QTableWidgetItem(str(p.get(k, ""))))

    def _ask_about_row(self, row: int, _col: int):
        get = lambda c: self.parts_tbl.item(row, c).text() if self.parts_tbl.item(row, c) else ""
        what = get(3) or f"{get(1)} ({get(2)})"
        self.ask(f"For {get(0)} = {what} in my design: ", send=False)

    def _save_constraints(self):
        if not self.state.project_dir:
            return
        c = Constraints(**{k: e.text().strip() for k, e in self.cfields.items()})
        try:
            c.save(self.state.project_dir)
        except OSError:
            pass
        self.state._ctx = None

    # ------------------------------------------------------------ messages
    def _add(self, w: QWidget):
        self.msg_layout.insertWidget(self.msg_layout.count() - 1, w)
        self.state.bridge.call_soon(lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))

    def _add_text(self, text: str, who: str):
        lab = QLabel()
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        lab.setOpenExternalLinks(True)
        lab.setTextFormat(Qt.PlainText if who == "user" else Qt.MarkdownText)
        lab.setText(text)
        lab.setObjectName("bubbleUser" if who == "user" else "bubbleAssistant")
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        if who == "user":                     # right-aligned, at most ~85 % wide
            h.addStretch(1)
            h.addWidget(lab, 6)
            if lab.fontMetrics().horizontalAdvance(text) + 30 < self.scroll.viewport().width() * 0.8 and "\n" not in text:
                lab.setWordWrap(False)
                h.setStretch(1, 0)
        else:
            h.addWidget(lab, 1)
        self._add(row)

    def _add_note(self, text: str, kind: str = "muted"):
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setObjectName({"error": "errorNote", "tool": "toolNote"}.get(kind, "toolNote"))
        self._add(lab)
        return lab

    def _set_thinking(self, on: bool):
        if on and self._thinking is None:
            self._thinking = self._add_note("Thinking…", "tool")
        elif not on and self._thinking is not None:
            self._thinking.deleteLater()
            self._thinking = None

    def new_chat(self):
        self.agent.reset()
        self._thinking = None
        while self.msg_layout.count() > 1:
            w = self.msg_layout.takeAt(0).widget()
            if w:
                w.deleteLater()

    def ask(self, text: str, send: bool = True):
        """Put text in the chat box (from another panel) and send it, or leave it for the user to finish."""
        self.input.setPlainText(text)
        self.input.setFocus()
        cur = self.input.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        self.input.setTextCursor(cur)
        if send:
            self.send()

    def send(self):
        text = self.input.toPlainText().strip()
        if not text or self.busy:
            return
        self.input.clear()
        self._add_text(text, "user")
        self.busy = True
        self.send_btn.setEnabled(False)
        self._set_thinking(True)

        def on_event(kind, payload):           # background thread -> GUI thread
            self.state.bridge.call_soon(lambda: self._event(kind, payload))
        self.state.bridge.run(self.agent.ask(text, on_event))

    def _event(self, kind, payload):
        self._set_thinking(False)
        if kind == "text":
            self._add_text(payload, "assistant")
        elif kind == "tool":
            name, args = payload
            detail = args.get("query") or args.get("mpn") or args.get("package") or ""
            self._add_note(f"↻ {TOOL_LABELS.get(name, name)}" + (f" “{detail}”" if detail else "") + "…", "tool")
        elif kind == "cards":
            for c in payload:
                self._add(CardWidget(c, self))
        elif kind == "error":
            self._add_note(f"⚠ {payload}", "error")
        elif kind == "done":
            self.busy = False
            self.send_btn.setEnabled(True)
            self.input.setFocus()
            return
        if self.busy:
            self._set_thinking(True)          # keep the indicator at the bottom while the agent works

    # ------------------------------------------------------------ compare
    def add_compare(self, part):
        if all(p.key != part.key for p in self.compare):
            self.compare.append(part)
        self.cmp_btn.setText(f"Compare ({len(self.compare)})")

    def show_compare(self):
        if self.compare:
            CompareDialog(self.compare, self).exec()
