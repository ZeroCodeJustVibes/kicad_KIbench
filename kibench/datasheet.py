"""Datasheet download, disk cache, text extraction and page bookmarks."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import httpx

from .cache import get_cache
from .paths import cache_dir

BOOKMARK_PATTERNS = {
    "Pinout": [r"pin\s+configuration", r"pin\s+connections?", r"pin\s+assignment", r"pin\s+description", r"pinout", r"pin\s+functions",
               r"terminal\s+configuration", r"pin\s+definitions?"],
    "Package outline": [r"package\s+outline", r"mechanical\s+data", r"package\s+dimensions?",
                        r"package\s+information", r"physical\s+dimensions", r"outline\s+dimensions",
                        r"package\s+drawing", r"mechanical\s+drawing"],
    "Land pattern": [r"land\s+pattern", r"recommended\s+footprint", r"pcb\s+layout\s+pattern",
                     r"soldering\s+footprint", r"recommended\s+pad"],
    "Absolute maximum": [r"absolute\s+maximum\s+ratings"],
    "Electrical characteristics": [r"electrical\s+characteristics"],
    "Ordering": [r"ordering\s+information"],
}


@dataclass
class Bookmark:
    title: str
    page: int        # 0-based


def _ds_dir() -> Path:
    d = cache_dir() / "datasheets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fix_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    return url


async def fetch_datasheet(client: httpx.AsyncClient, mpn: str, url: str) -> Path:
    """Download a datasheet PDF (cached). Raises ValueError if the URL is not a PDF."""
    cache = get_cache()
    hit = cache.datasheet_path(mpn)
    if hit:
        return hit
    url = _fix_url(url)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", mpn)[:60] + "_" + hashlib.sha1(url.encode()).hexdigest()[:8] + ".pdf"
    path = _ds_dir() / name
    r = await client.get(url, timeout=40.0, headers={"Accept": "application/pdf,*/*"})
    r.raise_for_status()
    data = r.content
    if not data.startswith(b"%PDF"):
        # LCSC serves an HTML viewer page; the real PDF link is inside it.
        links = []
        for m in re.finditer(rb'(?:https?:)?//[^"\'\s<>\\]+?\.pdf(?:\?[^"\'\s<>\\]*)?', data):
            u = _fix_url(m.group(0).decode("utf-8", "replace")).replace("&amp;", "&")
            if u.split("?")[0] != url.split("?")[0] and "viewer.html" not in u and u not in links:
                links.append(u)
        links.sort(key=lambda u: 0 if "datasheet.lcsc.com" in u else 1)
        for real in links[:3]:
            r = await client.get(real, timeout=40.0)
            if r.status_code == 200 and r.content.startswith(b"%PDF"):
                data = r.content
                break
    if not data.startswith(b"%PDF"):
        raise ValueError("datasheet URL did not return a PDF")
    path.write_bytes(data)
    cache.put_datasheet(mpn, url, path)
    return path


def page_texts(path: Path) -> List[str]:
    import pymupdf as fitz
    with fitz.open(str(path)) as doc:
        return [p.get_text("text") for p in doc]


_text_cache: Dict[str, List[str]] = {}


def cached_texts(path: Path) -> List[str]:
    k = str(path)
    if k not in _text_cache:
        _text_cache[k] = page_texts(path)
    return _text_cache[k]


def detect_bookmarks(texts: List[str]) -> List[Bookmark]:
    rxs = {title: re.compile("|".join(pats), re.I) for title, pats in BOOKMARK_PATTERNS.items()}

    def heads(t: str) -> set:
        found = set()
        for line in t.splitlines():
            s = line.strip(" 0123456789.\t")
            if len(s) > 80:
                continue
            for title, rx in rxs.items():
                if rx.match(s):
                    found.add(title)
        return found
    per_page = [heads(t) for t in texts]
    out: List[Bookmark] = []
    for title in BOOKMARK_PATTERNS:
        cands = []
        for i, (t, h) in enumerate(zip(texts, per_page)):
            # skip a table of contents: dotted leaders or many section titles on one page
            if title not in h or t.count("....") > 8 or len(h) >= 4:
                continue
            if title in _DRAWINGS and _NOT_DRAWING.search(t):
                continue                       # tape-and-reel / packaging tables mention "package drawing"
            cands.append(i)
        if title in _DRAWINGS:                 # prefer a page that carries a dimension table
            cands.sort(key=lambda i: not _DIM_TABLE.search(texts[i]))
        if cands:
            out.append(Bookmark(title, cands[0]))
    return sorted(out, key=lambda b: b.page)


_DRAWINGS = ("Package outline", "Land pattern")
_NOT_DRAWING = re.compile(r"tape\s+and\s+reel|reel\s+dimensions|package\s+materials|packaging\s+information|"
                          r"tube\s+dimensions", re.I)
_DIM_TABLE = re.compile(r"\b(BSC|MIN|MAX|NOM)\b|\bmillimet", re.I)


def get_text(path: Path, pages: Optional[List[int]] = None, max_chars: int = 12000) -> str:
    """Extracted text with page markers (1-based in the output for citation)."""
    texts = cached_texts(path)
    idx = [p - 1 for p in pages] if pages else list(range(len(texts)))
    chunks = []
    total = 0
    for i in idx:
        if 0 <= i < len(texts):
            t = re.sub(r"[ \t]+", " ", texts[i]).strip()
            chunk = f"--- page {i + 1} ---\n{t}"
            if total + len(chunk) > max_chars:
                chunks.append(f"--- page {i + 1} (truncated) ---\n{t[:max(0, max_chars - total)]}")
                break
            chunks.append(chunk)
            total += len(chunk)
    return "\n".join(chunks)


def render_page_png(path: Path, page: int, zoom: float = 1.5) -> bytes:
    import pymupdf as fitz
    with fitz.open(str(path)) as doc:
        pix = doc[page].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        return pix.tobytes("png")


def find_package_text(texts: List[str]) -> str:
    """Try to find a package designation like 'SOIC-14' or 'QFN-32 5x5' in the datasheet."""
    rx = re.compile(r"\b((?:[LTVWU]?QFN|[LT]QFP|T?SSOP|SOIC|SOT|DFN|BGA|WLCSP|MSOP|TSOT)-?\d+[^\n,;)]{0,25})", re.I)
    for t in texts[:6]:
        m = rx.search(t)
        if m:
            return m.group(1).strip()
    return ""
