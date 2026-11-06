"""Symbol/footprint sourcing cascade for a selected part. Shows every hit; the first usable one is the default."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ..models import Part, mpn_key
from ..providers.cadlinks import cad_sites, snapmagic_availability
from .existing import find_in_libraries

log = logging.getLogger(__name__)


@dataclass
class SourceHit:
    step: int
    source: str
    action: str                 # use_existing | import_lcsc | open_browser | stock_footprint | build
    symbol: bool = False
    footprint: bool = False
    model3d: bool = False
    detail: str = ""
    url: str = ""
    ref: str = ""               # lib:symbol, LCSC code, stock footprint lib id
    extra: dict = field(default_factory=dict)


async def find_sources(part: Part, engine, project_dir: Optional[Path], fp_index=None) -> List[SourceHit]:
    hits: List[SourceHit] = []
    # 1. already in your libraries
    try:
        for h in await asyncio.wait_for(asyncio.to_thread(find_in_libraries, part.mpn, part.lcsc, project_dir), 20):
            hits.append(SourceHit(1, f"Library {h.lib}", "use_existing", True, bool(h.footprint), False,
                                  f"{h.lib}:{h.symbol} (matched {h.matched})", ref=f"{h.lib}:{h.symbol}",
                                  extra={"footprint": h.footprint, "file": h.path}))
    except Exception as e:
        log.info("library lookup failed: %s", e)
        hits.append(SourceHit(1, "Your libraries", "none", detail=f"lookup skipped: {e.__class__.__name__}"))
    # 2. LCSC / EasyEDA
    lcsc = part.lcsc
    if not lcsc:
        try:
            prov = engine.provider("lcsc")
            p2 = await asyncio.wait_for(prov.get(part.mpn), 10) if prov else None
            if p2 and p2.lcsc:
                lcsc = p2.lcsc
                part.merge(p2)
        except Exception as e:
            log.info("LCSC lookup for %s failed: %s", part.mpn, e)
    if lcsc:
        try:
            cad = await asyncio.wait_for(engine.provider("lcsc").fetch_cad(lcsc), 15)
            has3d = any(s.startswith("SVGNODE") for s in cad.get("packageDetail", {}).get("dataStr", {}).get("shape", []))
            hits.append(SourceHit(2, "LCSC/EasyEDA", "import_lcsc", True, True, has3d,
                                  f"{lcsc}: {cad.get('packageDetail', {}).get('title', '')}",
                                  url=f"https://www.lcsc.com/product-detail/{lcsc}.html", ref=lcsc,
                                  extra={"package": cad.get("packageDetail", {}).get("title", "")}))
        except Exception as e:
            hits.append(SourceHit(2, "LCSC/EasyEDA", "none", detail=f"{lcsc}: {e}"))
    # 3-4. SnapMagic (API optional), Ultra Librarian, SamacSys
    try:
        sm = await asyncio.wait_for(snapmagic_availability(engine.client, part.mpn), 8)
    except Exception:
        sm = None
    for site in cad_sites(part.mpn, part.manufacturer):
        if site.name == "SnapMagic" and sm:
            hits.append(SourceHit(3, "SnapMagic", "open_browser", sm["symbol"], sm["footprint"], sm["model3d"],
                                  "available (API)" if (sm["symbol"] or sm["footprint"]) else "not available",
                                  url=sm["url"]))
        else:
            step = 3 if site.name == "SnapMagic" else 4
            hits.append(SourceHit(step, site.name, "open_browser", detail=f"{site.note}; the Downloads watcher imports the ZIP",
                                  url=site.url))
    # 5. KiCad stock footprint
    pkg = part.package or ""
    lc = next((h for h in hits if h.action == "import_lcsc"), None)
    if lc and lc.extra.get("package"):
        pkg = lc.extra["package"]
    if fp_index is not None and pkg:
        try:
            from ..matcher import match
            ms = await asyncio.wait_for(asyncio.to_thread(match, fp_index, pkg, 1), 10)
            if ms:
                m = ms[0]
                hits.append(SourceHit(5, "KiCad stock footprint", "stock_footprint", False, True, False,
                                      f"{m.lib_id} [{m.explain()}]", ref=m.lib_id, extra={"query": pkg}))
        except Exception as e:
            log.info("matcher failed: %s", e)
    # 6. build helper
    hits.append(SourceHit(6, "Build helper", "build", True, True, False,
                          "parametric footprint + pin-table symbol", extra={"package": pkg}))
    hits.sort(key=lambda h: h.step)
    return hits
