"""Read-only tools for the chat agent, plus the registry that keeps cards grounded in tool results."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..models import Part, mpn_key
from ..search.query import Filters
from ..search.units import find_values

FILTER_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "description": "e.g. 'MOSFET', 'LDO', 'Resistor'"},
        "package": {"type": "string", "description": "e.g. 'SOT-23', '0603'"},
        "in_stock": {"type": "boolean"},
        "jlc": {"type": "string", "enum": ["", "Basic", "Extended", "Basic+Preferred"]},
        "has_cad": {"type": "boolean"},
        "parametric": {"type": "string", "description": "Comma separated conditions, e.g. 'Vds>=30V, Rds(on)<=20mΩ'"},
    },
    "additionalProperties": False,
}

TOOL_SPECS = [
    {"name": "search_parts",
     "description": "Search all enabled distributors (LCSC/JLCPCB, Mouser, DigiKey, Nexar). Returns up to 10 real, "
                    "orderable parts with stock, price, package and key specs. Use plain keywords plus filters.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "keywords or MPN, e.g. 'n-channel mosfet logic level' or 'AO3400A'"},
         "filters": FILTER_SCHEMA}, "required": ["query"], "additionalProperties": False}},
    {"name": "get_part",
     "description": "Full details (all specs and offers) for one MPN.",
     "parameters": {"type": "object", "properties": {"mpn": {"type": "string"}}, "required": ["mpn"],
                    "additionalProperties": False}},
    {"name": "get_design_context",
     "description": "The open KiCad project: power rails with voltages, existing parts (reference, value, footprint, MPN), "
                    "interfaces inferred from net labels, the user's design constraints, and the current selection.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "check_schematic",
     "description": "Run a schematic check on the open project: KiCad ERC, net label problems, and voltage ratings. "
                    "Returns issues, every named net with its voltage (from the net name, the design constraints or "
                    "the user's overrides; null = unknown), and each part with its MPN, known ratings and pins "
                    "(pin number, function, type, net, volts). Use it to review a design: compare part limits "
                    "(datasheet absolute maximum ratings) with the voltages on their pins.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "check_board",
     "description": "Run a board (PCB) check on the open project: KiCad DRC with schematic parity, voltage "
                    "clearances (IPC-2221B) between nets with known voltages, silkscreen references, and "
                    "decoupling capacitor distances. Returns issues, every net with its voltage, pad count, track "
                    "width range (mm) and routed length, and each part's position and side. Use it to review a "
                    "layout: e.g. power track widths against the expected current, spacing on high-voltage nets.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_datasheet_text",
     "description": "Extracted datasheet text with '--- page N ---' markers. Without pages, returns the detected "
                    "bookmarks (pinout, absolute maximum ratings, package outline...) and the first pages. "
                    "Use it to check limits and pinouts before claiming them.",
     "parameters": {"type": "object", "properties": {
         "mpn": {"type": "string"},
         "pages": {"type": "array", "items": {"type": "integer"}, "description": "1-based page numbers"}},
         "required": ["mpn"], "additionalProperties": False}},
    {"name": "match_footprint",
     "description": "Rank KiCad stock footprints for a package description (e.g. 'SOT-23-5', 'QFN-32 5x5 0.5').",
     "parameters": {"type": "object", "properties": {"package": {"type": "string"}}, "required": ["package"],
                    "additionalProperties": False}},
    {"name": "check_cad_availability",
     "description": "Which sources have a symbol/footprint/3D model for an MPN (user libraries, LCSC/EasyEDA, "
                    "SnapMagic, Ultra Librarian, SamacSys, KiCad stock footprint).",
     "parameters": {"type": "object", "properties": {"mpn": {"type": "string"}}, "required": ["mpn"],
                    "additionalProperties": False}},
    {"name": "recommend_parts",
     "description": "Show recommended parts to the user as clickable cards. Only MPNs returned by search_parts or "
                    "get_part in this conversation are accepted. Stock, price and specs on the card are filled in "
                    "from tool data; you supply only why it fits. Cite datasheet pages for any limit you mention.",
     "parameters": {"type": "object", "properties": {
         "items": {"type": "array", "items": {"type": "object", "properties": {
             "mpn": {"type": "string"},
             "reason": {"type": "string", "description": "one or two short sentences, e.g. 'Vgs(th) 1.2 V, OK for 3V3 gate drive'"},
             "citations": {"type": "array", "items": {"type": "object", "properties": {
                 "page": {"type": "integer"}, "claim": {"type": "string"}},
                 "required": ["page", "claim"], "additionalProperties": False}}},
             "required": ["mpn", "reason"], "additionalProperties": False}}},
         "required": ["items"], "additionalProperties": False}},
]

LIMIT_WORDS = re.compile(r"\b(max(imum)?|absolute|rated|rating|limit|up to|withstand|breakdown|abs\.? ?max)\b", re.I)


@dataclass
class Card:
    part: Part
    reason: str
    citations: List[dict] = field(default_factory=list)
    unverified: List[str] = field(default_factory=list)   # numbers in the reason not found in tool data
    uncited: bool = False                                 # limit claim without a datasheet citation


class ToolBox:
    """Executes tools against KiBench services and records everything the model has seen."""

    def __init__(self, engine, context_fn: Callable[[], dict], fp_index=None, project_dir_fn=None,
                 sources_fn=None):
        self.engine = engine
        self.context_fn = context_fn
        self.fp_index = fp_index
        self.project_dir_fn = project_dir_fn or (lambda: None)
        self.sources_fn = sources_fn
        self.check_fn = None            # set by the UI to share its last check run
        self.board_check_fn = None
        self.registry: Dict[str, Part] = {}
        self.datasheet_pages: Dict[str, List[str]] = {}
        self.context_text = ""
        self.cards: List[Card] = []

    def _register(self, parts: List[Part]) -> None:
        for p in parts:
            if p.key in self.registry:
                self.registry[p.key].merge(p)
            else:
                self.registry[p.key] = p

    async def run(self, name: str, args: dict) -> str:
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            raise ValueError(f"unknown tool {name}")
        out = await fn(**args)
        return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)

    async def t_search_parts(self, query: str, filters: Optional[dict] = None):
        f = Filters(**{k: v for k, v in (filters or {}).items() if k in Filters.__dataclass_fields__})
        res = await self.engine.search(query, f)
        parts = res.parts[:10]
        self._register(parts)
        return {"count": len(res.parts), "parts": [p.summary() for p in parts],
                **({"provider_errors": res.errors} if res.errors else {})}

    async def t_get_part(self, mpn: str):
        p = self.registry.get(mpn_key(mpn)) or await self.engine.get(mpn)
        if not p:
            return {"error": f"{mpn} not found at any enabled distributor"}
        self._register([p])
        d = p.summary()
        d["specs"] = p.specs
        d["offers"] = [{"source": o.source, "sku": o.sku, "stock": o.stock, "price_breaks": o.price_breaks[:5],
                        "currency": o.currency} for o in p.offers]
        d["datasheet_url"] = p.datasheet_url
        return d

    async def t_get_design_context(self):
        ctx = self.context_fn()
        self.context_text = json.dumps(ctx)
        return ctx

    async def t_check_schematic(self):
        proj = self.project_dir_fn()
        if not proj:
            return {"error": "no KiCad project is open"}
        if self.check_fn is not None:
            rep = await asyncio.to_thread(self.check_fn)
        else:
            from ..design.check import run_check
            rep = await asyncio.to_thread(run_check, Path(proj))
        out = rep.for_agent()
        self.context_text += json.dumps(out)[:20000]       # numbers in it count as grounded
        return out

    async def t_check_board(self):
        proj = self.project_dir_fn()
        if not proj:
            return {"error": "no KiCad project is open"}
        if self.board_check_fn is not None:
            rep = await asyncio.to_thread(self.board_check_fn)
        else:
            from ..design.board_check import run_board_check
            rep = await asyncio.to_thread(run_board_check, Path(proj))
        out = rep.for_agent()
        self.context_text += json.dumps(out)[:20000]
        return out

    async def t_get_datasheet_text(self, mpn: str, pages: Optional[List[int]] = None):
        from .. import datasheet as ds
        p = self.registry.get(mpn_key(mpn)) or await self.engine.get(mpn)
        if not p or not p.datasheet_url:
            return {"error": f"no datasheet URL known for {mpn}"}
        self._register([p])
        path = await ds.fetch_datasheet(self.engine.client, p.mpn, p.datasheet_url)
        texts = await asyncio.to_thread(ds.cached_texts, path)
        self.datasheet_pages[p.key] = texts
        if not pages:
            bms = ds.detect_bookmarks(texts)
            first = [1, 2] + [b.page + 1 for b in bms if b.title == "Absolute maximum"][:1]
            return {"pages_total": len(texts),
                    "bookmarks": [{"title": b.title, "page": b.page + 1} for b in bms],
                    "text": ds.get_text(path, sorted(set(first)), max_chars=9000)}
        return {"pages_total": len(texts), "text": ds.get_text(path, pages[:6], max_chars=14000)}

    async def t_match_footprint(self, package: str):
        if self.fp_index is None:
            return {"error": "footprint index not available"}
        from ..matcher import match
        ms = await asyncio.to_thread(match, self.fp_index, package, 5)
        return [{"footprint": m.lib_id, "score": round(m.score), "diff": m.explain()} for m in ms]

    async def t_check_cad_availability(self, mpn: str):
        p = self.registry.get(mpn_key(mpn)) or await self.engine.get(mpn) or Part(mpn=mpn)
        from ..library.sourcing import find_sources
        hits = await find_sources(p, self.engine, self.project_dir_fn(), self.fp_index)
        return [{"source": h.source, "symbol": h.symbol, "footprint": h.footprint, "3d": h.model3d,
                 "detail": h.detail} for h in hits if h.action not in ("build",)]

    async def t_recommend_parts(self, items: List[dict]):
        accepted, rejected = [], []
        for it in items:
            p = self.registry.get(mpn_key(it.get("mpn", "")))
            if not p:
                rejected.append(it.get("mpn", ""))
                continue
            card = Card(p, it.get("reason", "").strip(), it.get("citations") or [])
            card.unverified = self.unverified_numbers(card)
            card.uncited = bool(LIMIT_WORDS.search(card.reason)) and not card.citations
            accepted.append(card)
        self.cards += accepted
        out = {"shown": [c.part.mpn for c in accepted]}
        if rejected:
            out["rejected"] = rejected
            out["note"] = "Rejected MPNs were not returned by search_parts/get_part; search for them first."
        return out

    def unverified_numbers(self, card: Card) -> List[str]:
        """Numbers with units in the reason that appear in none of the tool data the model saw."""
        p = card.part
        hay = " ".join([p.description, p.package, " ".join(f"{k} {v}" for k, v in p.specs.items()),
                        " ".join(self.datasheet_pages.get(p.key, [])), self.context_text])
        known = find_values(hay)
        for m in re.finditer(r"(\d+)V(\d+)", hay):          # rails like 3V3
            known.append((float(f"{m.group(1)}.{m.group(2)}"), "V"))
        bad = []
        for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*([pnuµμmkKMG]?)(Ω|ohm|F|H|V|A|W|Hz|°C|℃)(?![a-zA-Z])", card.reason):
            vals = find_values(m.group(0))
            if not vals:
                continue
            v, u = vals[0]
            if not any(u == ku and abs(v - kv) <= max(abs(kv) * 0.02, 1e-12) for kv, ku in known):
                bad.append(m.group(0))
        for m in re.finditer(r"(\d+)V(\d+)", card.reason):
            v = float(f"{m.group(1)}.{m.group(2)}")
            if not any(u == "V" and abs(v - kv) < 1e-6 for kv, u in known):
                bad.append(m.group(0))
        return bad
