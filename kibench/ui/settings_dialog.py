"""Settings: providers and keys (OS keyring), chat model, library target, folders."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QCheckBox, QMessageBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget)

from ..config import SECRET_NAMES, get_secret, set_secret
from ..providers import REGISTRY


def _dir_row(edit: QLineEdit, parent) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.addWidget(edit, 1)
    b = QPushButton("…")
    b.clicked.connect(lambda: (lambda d: d and edit.setText(d))(QFileDialog.getExistingDirectory(parent, "Folder", edit.text())))
    h.addWidget(b)
    return w


PROVIDERS = [("claude-code", "Claude Code (your Claude Pro/Max plan, no key)"),
             ("anthropic", "Anthropic API (API key)"),
             ("openrouter", "OpenRouter (API key)"),
             ("local", "Local model (Ollama / LM Studio)")]
ANTHROPIC_MODELS = [("claude-opus-5-5", "Claude Opus 5.5 (best)"), ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
                    ("claude-haiku-5-5", "Claude Haiku 5.5 (fastest, cheapest)"), ("claude-fable-5-1", "Claude Fable 5.1")]
CLAUDE_CODE_MODELS = [("", "Plan default"), ("opus", "Opus"), ("sonnet", "Sonnet (uses less of your plan)"),
                      ("haiku", "Haiku")]
OPENROUTER_MODELS = [("anthropic/claude-opus-5.5", "Claude Opus 5.5"), ("anthropic/claude-sonnet-5.5", "Claude Sonnet 5.5"),
                     ("anthropic/claude-haiku-5.5", "Claude Haiku 5.5")]
LOCAL_URLS = ["http://localhost:11434/v1", "http://localhost:1234/v1"]
PROVIDER_HINTS = {
    "claude-code": "Runs your installed Claude Code in the background (run `claude` once in a terminal to sign in). "
                   "It can only use KiBench's read-only part tools.",
    "anthropic": "Key from console.anthropic.com, billed per use (separate from a Claude subscription).",
    "openrouter": "Key from openrouter.ai. Any model with tool use works; type its id to use one not listed.",
    "local": "Ollama: http://localhost:11434/v1 · LM Studio: http://localhost:1234/v1. Press ↻ to list the server's "
             "models. Tool use quality depends on the model.",
}


def _model_combo(choices, current: str) -> QComboBox:
    """Dropdown of known models; editable so any other model id can still be typed."""
    cb = QComboBox()
    cb.setEditable(True)
    for c in choices:
        key, label = c if isinstance(c, tuple) else (c, c)
        cb.addItem(label, key)
    i = cb.findData(current)
    if i >= 0:
        cb.setCurrentIndex(i)
    else:
        cb.setCurrentText(current)
    return cb


def _model_value(cb: QComboBox) -> str:
    i = cb.findText(cb.currentText())
    return cb.itemData(i) if i >= 0 and cb.itemData(i) is not None else cb.currentText().strip()


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.setWindowTitle("KiBench settings")
        self.resize(620, 520)
        v = QVBoxLayout(self)
        tabs = QTabWidget()
        v.addWidget(tabs)

        # providers
        pw = QWidget()
        pf = QFormLayout(pw)
        self.prov = {}
        for name, cls in REGISTRY.items():
            cb = QCheckBox(cls.label)
            cb.setChecked(settings.providers.get(name, False))
            pf.addRow(cb)
            self.prov[name] = cb
        self.secrets = {}
        for key in ("mouser_api_key", "digikey_client_id", "digikey_client_secret", "nexar_client_id",
                    "nexar_client_secret", "snapmagic_api_key"):
            e = QLineEdit(get_secret(key))
            e.setEchoMode(QLineEdit.PasswordEchoOnEdit)
            pf.addRow(SECRET_NAMES[key], e)
            self.secrets[key] = e
        pf.addRow(QLabel("<small>LCSC/JLCPCB needs no key. Keys are stored in the OS keyring. "
                         "SnapMagic, Ultra Librarian and SamacSys work without keys via the browser and the "
                         "Downloads watcher.</small>"))
        self.timeout = QLineEdit(str(settings.search_timeout_s))
        pf.addRow("Provider timeout (s)", self.timeout)
        tabs.addTab(pw, "Search providers")

        # chat: dropdowns everywhere except keys and URLs; rows follow the chosen provider
        cw = QWidget()
        cf = QFormLayout(cw)
        self.chat_form = cf
        self.llm = QComboBox()
        for key, label in PROVIDERS:
            self.llm.addItem(label, key)
        self.llm.setCurrentIndex(max(0, self.llm.findData(settings.llm_provider)))
        cf.addRow("Provider", self.llm)
        self.model = _model_combo(ANTHROPIC_MODELS, settings.llm_model)
        cf.addRow("Claude model", self.model)
        self.cc_model = _model_combo(CLAUDE_CODE_MODELS, settings.claude_code_model)
        cf.addRow("Claude Code model", self.cc_model)
        self.or_model = _model_combo(OPENROUTER_MODELS, settings.openrouter_model)
        cf.addRow("OpenRouter model", self.or_model)
        self.local_url = QComboBox()
        self.local_url.setEditable(True)
        self.local_url.addItems(LOCAL_URLS)
        self.local_url.setCurrentText(settings.local_base_url)
        cf.addRow("Local server URL", self.local_url)
        lm = QWidget()
        lmh = QHBoxLayout(lm)
        lmh.setContentsMargins(0, 0, 0, 0)
        self.local_model = _model_combo([settings.local_model] if settings.local_model else [], settings.local_model)
        refresh = QPushButton("↻")
        refresh.setToolTip("List the models this server has")
        refresh.setFixedWidth(36)
        refresh.clicked.connect(self._list_local_models)
        lmh.addWidget(self.local_model, 1)
        lmh.addWidget(refresh)
        cf.addRow("Local model", lm)
        self.key_rows = {}
        for key in ("anthropic_api_key", "openrouter_api_key", "local_llm_api_key"):
            e = QLineEdit(get_secret(key))
            e.setEchoMode(QLineEdit.PasswordEchoOnEdit)
            cf.addRow(SECRET_NAMES[key], e)
            self.secrets[key] = e
        cc = QPushButton("Connect Claude Code (terminal)")
        cc.setToolTip("Also register KiBench's tools with Claude Code for use in a terminal")
        cc.clicked.connect(self._connect_claude_code)
        cf.addRow("", cc)
        self.chat_hint = QLabel()
        self.chat_hint.setWordWrap(True)
        self.chat_hint.setObjectName("muted")
        cf.addRow(self.chat_hint)
        self.llm.currentIndexChanged.connect(self._provider_rows)
        self._provider_rows()
        tabs.addTab(cw, "Chat")

        # library
        lw = QWidget()
        lf = QFormLayout(lw)
        self.target = QComboBox()
        self.target.addItem("This project's library (next to the .kicad_pro)", False)
        self.target.addItem("One global library for all projects", True)
        self.target.setCurrentIndex(1 if settings.use_global_library else 0)
        lf.addRow("Save imports to", self.target)
        self.libname = QComboBox()
        self.libname.setEditable(True)
        self.libname.addItems(sorted({settings.library_name or "KiBench", "KiBench"}))
        self.libname.setCurrentText(settings.library_name or "KiBench")
        lf.addRow("Library name", self.libname)
        self.global_dir = QLineEdit(settings.global_library_dir)
        self.global_dir.setPlaceholderText("e.g. ~/kicad-libs (folder for KiBench.kicad_sym / .pretty / .3dshapes)")
        self.global_row = _dir_row(self.global_dir, self)
        lf.addRow("Global library folder", self.global_row)
        self.target.currentIndexChanged.connect(lambda _i: lf.setRowVisible(self.global_row, bool(self.target.currentData())))
        lf.setRowVisible(self.global_row, settings.use_global_library)
        self.density = QComboBox()
        self.density.addItems(["A", "B", "C"])
        self.density.setToolTip("IPC-7351B: A = most solder fillet, B = nominal, C = least (dense boards)")
        self.density.setCurrentText(settings.ipc_density)
        lf.addRow("Default IPC density", self.density)
        self.theme = QComboBox()
        for k, label in (("", "Follow the system"), ("light", "Light"), ("dark", "Dark")):
            self.theme.addItem(label, k)
        self.theme.setCurrentIndex(max(0, self.theme.findData(settings.theme)))
        lf.addRow("Theme (after restart)", self.theme)
        self.downloads = QLineEdit(settings.downloads_dir)
        self.downloads.setPlaceholderText("default: system Downloads folder")
        lf.addRow("Downloads folder (watcher)", _dir_row(self.downloads, self))
        self.stock = QLineEdit(settings.stock_footprint_dir)
        self.stock.setPlaceholderText("auto-detected from KiCad")
        lf.addRow("KiCad stock footprints", _dir_row(self.stock, self))
        tabs.addTab(lw, "Library")

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.keyring_warning = QLabel("")
        v.addWidget(self.keyring_warning)

    def _provider_rows(self, *_):
        prov = self.llm.currentData()
        rows = {"anthropic": [self.model, self.secrets["anthropic_api_key"]],
                "claude-code": [self.cc_model],
                "openrouter": [self.or_model, self.secrets["openrouter_api_key"]],
                "local": [self.local_url, self.local_model.parentWidget(), self.secrets["local_llm_api_key"]]}
        shown = rows.get(prov, [])
        for w in {w for ws in rows.values() for w in ws}:
            self.chat_form.setRowVisible(w, w in shown)
        self.chat_hint.setText(PROVIDER_HINTS.get(prov, ""))

    def _list_local_models(self):
        import httpx
        url = self.local_url.currentText().rstrip("/") + "/models"
        try:
            key = self.secrets["local_llm_api_key"].text().strip()
            r = httpx.get(url, timeout=4, headers={"Authorization": f"Bearer {key}"} if key else {})
            ids = sorted(m.get("id", "") for m in r.json().get("data", []) if m.get("id"))
        except Exception as e:
            QMessageBox.warning(self, "Local models", f"Could not list models at {url}: {e}")
            return
        cur = self.local_model.currentText()
        self.local_model.clear()
        self.local_model.addItems(ids)
        self.local_model.setCurrentText(cur if cur in ids else (ids[0] if ids else cur))

    def _connect_claude_code(self):
        import contextlib
        import io

        from PySide6.QtWidgets import QMessageBox

        from ..mcp_server import register
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = register("user")
        box = QMessageBox.information if rc == 0 else QMessageBox.warning
        box(self, "Claude Code", buf.getvalue().strip() or ("Connected." if rc == 0 else "Failed."))

    def _save(self):
        s = self.s
        s.providers = {k: cb.isChecked() for k, cb in self.prov.items()}
        try:
            s.search_timeout_s = float(self.timeout.text())
        except ValueError:
            pass
        s.llm_provider = self.llm.currentData()
        s.llm_model = _model_value(self.model) or "claude-opus-5-5"
        s.claude_code_model = _model_value(self.cc_model)
        s.openrouter_model = _model_value(self.or_model)
        s.local_base_url = self.local_url.currentText().strip()
        s.local_model = _model_value(self.local_model)
        s.library_name = self.libname.currentText().strip() or "KiBench"
        s.use_global_library = bool(self.target.currentData())
        s.global_library_dir = str(Path(self.global_dir.text().strip()).expanduser()) if self.global_dir.text().strip() else ""
        s.theme = self.theme.currentData()
        s.ipc_density = self.density.currentText()
        s.downloads_dir = self.downloads.text().strip()
        s.stock_footprint_dir = self.stock.text().strip()
        s.save()
        ok = True
        for k, e in self.secrets.items():
            if e.text() != get_secret(k):
                ok = set_secret(k, e.text().strip()) and ok
        if not ok:
            self.keyring_warning.setText("No OS keyring available: keys are kept for this session only.")
        self.accept()
