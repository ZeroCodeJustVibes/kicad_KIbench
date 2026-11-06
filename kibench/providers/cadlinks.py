"""CAD-only sources: SnapMagic, Ultra Librarian, SamacSys (Component Search Engine).

None of these has an open search API, so by default we build a pre-filled
search URL and let the Downloads watcher import the ZIP the user downloads.
A SnapMagic API key is optional; when present we query availability directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import quote

import httpx

from ..config import get_secret


@dataclass
class CadSite:
    name: str
    url: str
    note: str = ""


def cad_sites(mpn: str, manufacturer: str = "") -> List[CadSite]:
    m = quote(mpn)
    return [
        CadSite("SnapMagic", f"https://www.snapeda.com/search/?q={m}", "Free account; KiCad format"),
        CadSite("Ultra Librarian", f"https://app.ultralibrarian.com/search?queryText={m}", "Choose 'KiCAD' export"),
        CadSite("SamacSys", f"https://componentsearchengine.com/search?term={m}", "Component Search Engine; KiCad ZIP"),
    ]


async def snapmagic_availability(client: httpx.AsyncClient, mpn: str) -> Optional[dict]:
    """Return {'symbol': bool, 'footprint': bool, 'model3d': bool, 'url': str} or None.

    Only used when the user has a SnapMagic API key; otherwise returns None.
    """
    key = get_secret("snapmagic_api_key")
    if not key:
        return None
    try:
        r = await client.get("https://www.snapeda.com/api/v1/parts/search",
                             params={"q": mpn, "token": key}, timeout=8.0)
        if r.status_code != 200:
            return None
        results = (r.json() or {}).get("results") or []
    except Exception:
        return None
    from ..models import mpn_key
    for res in results:
        if mpn_key(res.get("part_number", "")) == mpn_key(mpn):
            return {"symbol": bool(res.get("has_symbol")), "footprint": bool(res.get("has_footprint")),
                    "model3d": bool(res.get("has_3d") or res.get("models")),
                    "url": res.get("url") or f"https://www.snapeda.com/search/?q={quote(mpn)}"}
    return None
