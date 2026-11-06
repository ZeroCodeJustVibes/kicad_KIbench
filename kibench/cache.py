"""Local SQLite cache for search results, parts, CAD payloads and datasheets."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .paths import cache_dir

STOCK_TTL = 24 * 3600          # stock and price
CAD_TTL = 30 * 24 * 3600       # symbol/footprint payloads change rarely


class Cache:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or cache_dir() / "kibench.sqlite")
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS kv (
                ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                fetched REAL NOT NULL, PRIMARY KEY (ns, key));
            CREATE TABLE IF NOT EXISTS datasheets (
                mpn TEXT PRIMARY KEY, url TEXT, path TEXT, fetched REAL);
        """)
        self._db.commit()

    def get(self, ns: str, key: str, ttl: Optional[float] = STOCK_TTL) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value, fetched FROM kv WHERE ns=? AND key=?",
                                   (ns, key)).fetchone()
        if not row:
            return None
        if ttl is not None and time.time() - row[1] > ttl:
            return None
        return json.loads(row[0])

    def put(self, ns: str, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?,?)",
                             (ns, key, json.dumps(value), time.time()))
            self._db.commit()

    def datasheet_path(self, mpn: str) -> Optional[Path]:
        with self._lock:
            row = self._db.execute("SELECT path FROM datasheets WHERE mpn=?", (mpn,)).fetchone()
        if row and row[0] and Path(row[0]).exists():
            return Path(row[0])
        return None

    def put_datasheet(self, mpn: str, url: str, path: Path) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO datasheets VALUES (?,?,?,?)",
                             (mpn, url, str(path), time.time()))
            self._db.commit()


_cache: Optional[Cache] = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache()
    return _cache
