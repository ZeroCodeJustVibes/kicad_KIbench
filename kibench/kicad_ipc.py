"""Optional KiCad IPC API bridge (kicad-python). Everything degrades gracefully without it.

Used read-only: find the open project and the current selection in the schematic
or PCB editor. Placing symbols stays manual (they appear in the Add Symbol chooser).
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


class KiCadBridge:
    def __init__(self):
        self._kicad = None
        self.error = ""

    def connect(self) -> bool:
        if self._kicad is not None:
            return True
        try:
            from kipy import KiCad
            k = KiCad(client_name=f"kibench-{os.getpid()}", timeout_ms=2000)
            k.ping()
            self._kicad = k
            return True
        except Exception as e:  # kipy missing, API server disabled, KiCad closed
            self.error = str(e) or e.__class__.__name__
            return False

    @property
    def connected(self) -> bool:
        return self._kicad is not None

    def version(self) -> str:
        try:
            return str(self._kicad.get_version()) if self.connect() else ""
        except Exception:
            return ""

    def project_dir(self) -> Optional[Path]:
        if not self.connect():
            return None
        from kipy.proto.common.types import DocumentType
        for dt in (DocumentType.DOCTYPE_PROJECT, DocumentType.DOCTYPE_SCHEMATIC, DocumentType.DOCTYPE_PCB):
            try:
                docs = self._kicad.get_open_documents(dt)
            except Exception as e:
                log.debug("get_open_documents(%s) failed: %s", dt, e)
                continue
            for d in docs:
                p = d.project.path if d.HasField("project") else ""
                if p:
                    return Path(p)
                if getattr(d, "board_filename", ""):
                    return Path(d.board_filename).parent
        return None

    def selection_refs(self) -> List[str]:
        """References selected in the schematic editor (or PCB editor as fallback)."""
        if not self.connect():
            return []
        refs: List[str] = []
        from kipy.proto.common.types import DocumentType
        try:
            from kipy.schematic import Schematic
            docs = self._kicad.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)
            if docs:
                text = Schematic(self._kicad._client, docs[0]).get_selection_as_string()
                refs += _refs_from_text(text)
        except Exception as e:
            log.debug("schematic selection failed: %s", e)
        if not refs:
            try:
                text = self._kicad.get_board().get_selection_as_string()
                refs += _refs_from_text(text)
            except Exception as e:
                log.debug("board selection failed: %s", e)
        return sorted(set(r for r in refs if not r.startswith("#")))


    # ------------------------------------------------------------ open board
    def _board(self):
        if not self.connect():
            return None
        try:
            return self._kicad.get_board()
        except Exception as e:          # PCB editor not open
            log.debug("get_board failed: %s", e)
            return None

    def open_board_path(self) -> Optional[Path]:
        """File of the board open in KiCad's PCB editor (None if it is not open or the API is off)."""
        b = self._board()
        if b is None:
            return None
        try:
            name = b.name
            proj = b.document.project.path if b.document.HasField("project") else ""
        except Exception:
            return None
        p = Path(name)
        return p if p.is_absolute() or not proj else Path(proj) / p

    def save_board(self) -> bool:
        b = self._board()
        try:
            b.save()
            return True
        except Exception as e:
            self.error = str(e)
            return False

    def revert_board(self) -> bool:
        """Reload the open board from disk (File → Revert)."""
        b = self._board()
        try:
            b.revert()
            return True
        except Exception as e:
            self.error = str(e)
            return False


def _refs_from_text(text: str) -> List[str]:
    return re.findall(r'\(property\s+"Reference"\s+"([^"]+)"', text or "")


def project_from_env() -> Optional[Path]:
    """KiCad does not pass the project path to plugins. Fallbacks: $KIBENCH_PROJECT, then the
    open/most recent project recorded in KiCad's own settings (kicad.json)."""
    p = os.environ.get("KIBENCH_PROJECT")
    if p:
        return Path(p)
    try:
        import json
        from .paths import kicad_config_dir
        d = json.loads((kicad_config_dir() / "kicad.json").read_text())
        sysd = d.get("system") or {}
        for cand in list(sysd.get("open_projects") or []) + list(sysd.get("file_history") or [])[:1]:
            if cand and Path(cand).exists():
                return Path(cand).parent if Path(cand).is_file() else Path(cand)
    except (OSError, ValueError):
        pass
    return None
