"""Watch the Downloads folder for vendor CAD ZIPs of the active part."""
from __future__ import annotations

import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Dict, Optional

from ..models import mpn_key


def zip_mentions(path: Path, key: str) -> bool:
    if key and key in mpn_key(path.stem):
        return True
    try:
        with zipfile.ZipFile(path) as z:
            names = " ".join(z.namelist()[:200])
    except (zipfile.BadZipFile, OSError):
        return False
    return bool(key) and key in mpn_key(names)


class DownloadWatcher:
    """Polls a folder (cross-platform, no extra deps). Calls on_zip(path, matches_mpn) for new ZIPs."""

    def __init__(self, folder: Path, on_zip: Callable[[Path, bool], None], interval: float = 1.0):
        self.folder = Path(folder)
        self.on_zip = on_zip
        self.interval = interval
        self.active_mpn = ""
        self._seen: Dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.armed_since = 0.0

    def arm(self, mpn: str) -> None:
        """Start looking for ZIPs of this MPN (files that appear from now on)."""
        self.active_mpn = mpn
        self.armed_since = time.time() - 2
        if not self._thread or not self._thread.is_alive():
            self._snapshot()
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="kibench-watcher")
            self._thread.start()

    def disarm(self) -> None:
        self.active_mpn = ""

    def stop(self) -> None:
        self._stop.set()

    def _snapshot(self) -> None:
        try:
            for p in self.folder.glob("*.zip"):
                self._seen[str(p)] = p.stat().st_mtime
        except OSError:
            pass

    def _loop(self) -> None:
        pending: Dict[str, int] = {}
        while not self._stop.is_set():
            if self.active_mpn:
                try:
                    zips = list(self.folder.glob("*.zip"))
                except OSError:
                    zips = []
                for p in zips:
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    k = str(p)
                    if self._seen.get(k) == st.st_mtime or st.st_mtime < self.armed_since:
                        continue
                    # wait until the size is stable (browser finished writing)
                    if pending.get(k) != st.st_size:
                        pending[k] = st.st_size
                        continue
                    pending.pop(k, None)
                    self._seen[k] = st.st_mtime
                    # Matching ZIPs open the verification view directly; others are offered.
                    self.on_zip(p, zip_mentions(p, mpn_key(self.active_mpn)))
            self._stop.wait(self.interval)
