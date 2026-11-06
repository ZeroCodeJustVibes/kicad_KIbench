"""Query all enabled providers in parallel, merge by MPN, cache, filter and rank."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..cache import STOCK_TTL, get_cache
from ..models import Part, mpn_key, same_manufacturer
from ..providers import REGISTRY, Provider, make_client
from .query import Filters, ParsedQuery, matches_parsed, parse_query

log = logging.getLogger(__name__)


@dataclass
class SearchResult:
    query: ParsedQuery
    parts: List[Part]
    errors: Dict[str, str] = field(default_factory=dict)
    timings: Dict[str, float] = field(default_factory=dict)
    from_cache: bool = False


class SearchEngine:
    def __init__(self, settings):
        self.settings = settings
        self._client = None
        self._providers: Dict[str, Provider] = {}

    @property
    def client(self):
        if self._client is None:
            self._client = make_client()
        return self._client

    def provider(self, name: str) -> Optional[Provider]:
        if name not in self._providers and name in REGISTRY:
            self._providers[name] = REGISTRY[name](self.client, self.settings)
        return self._providers.get(name)

    def active_providers(self) -> List[Provider]:
        out = []
        for name, on in self.settings.providers.items():
            p = self.provider(name) if on else None
            if p and p.configured():
                out.append(p)
        return out

    async def _one(self, p: Provider, pq: ParsedQuery, f: Filters, res: SearchResult) -> List[Part]:
        cache = get_cache()
        ck = f"{pq.keyword}|{pq.lcsc}|{f.jlc}|{f.in_stock}"
        hit = cache.get(f"search:{p.name}", ck, ttl=STOCK_TTL)
        if hit is not None:
            res.timings[p.name] = 0.0
            return [Part.from_dict(d) for d in hit]
        t0 = time.monotonic()
        try:
            parts = await asyncio.wait_for(p.search(pq, f), timeout=self.settings.search_timeout_s)
        except asyncio.TimeoutError:
            res.errors[p.name] = "timeout"
            return []
        except Exception as e:  # provider failures never break the whole search
            log.warning("provider %s failed: %s", p.name, e)
            res.errors[p.name] = str(e) or e.__class__.__name__
            return []
        res.timings[p.name] = time.monotonic() - t0
        cache.put(f"search:{p.name}", ck, [x.to_dict() for x in parts])
        for x in parts:
            cache.put("part", x.key + "|" + p.name, x.to_dict())
        return parts

    async def search(self, text: str, filters: Optional[Filters] = None) -> SearchResult:
        pq = parse_query(text)
        f = filters or Filters()
        res = SearchResult(query=pq, parts=[])
        provs = self.active_providers()
        lists = await asyncio.gather(*(self._one(p, pq, f, res) for p in provs))
        res.from_cache = bool(provs) and all(v == 0.0 for v in res.timings.values())
        # Merge by MPN across providers, but keep same-MPN parts from different manufacturers apart
        # (LCSC lists many second-source "AO3400A"s).
        merged: Dict[str, List[Part]] = {}
        parts: List[Part] = []
        for lst in lists:
            for part in lst:
                k = part.key
                if not k:
                    continue
                target = next((m for m in merged.get(k, [])
                               if same_manufacturer(m.manufacturer, part.manufacturer)
                               and not set(m.providers) & set(part.providers)), None)
                if target:
                    target.merge(part)
                else:
                    merged.setdefault(k, []).append(part)
                    parts.append(part)
        parts = [p for p in parts if f.match(p)[0] and matches_parsed(pq, p)]
        res.parts = rank(parts, pq)
        return res

    async def get(self, mpn: str) -> Optional[Part]:
        """Fetch one part from every provider and merge (used by the agent and detail view)."""
        provs = self.active_providers()

        async def one(p):
            try:
                return await asyncio.wait_for(p.get(mpn), timeout=self.settings.search_timeout_s)
            except Exception as e:
                log.info("get %s from %s failed: %s", mpn, p.name, e)
                return None
        found = [x for x in await asyncio.gather(*(one(p) for p in provs)) if x]
        if not found:
            return None
        base = found[0]
        for x in found[1:]:
            if x.key == base.key:
                base.merge(x)
        return base


def rank(parts: List[Part], pq: ParsedQuery) -> List[Part]:
    qk = mpn_key(pq.raw)

    def score(p: Part):
        exact = 0 if (p.key == qk or (pq.lcsc and p.lcsc.upper() == pq.lcsc)) else 1
        prefix = 0 if qk and p.key.startswith(qk) else 1
        stocked = 0 if p.total_stock > 0 else 1
        jlc = {"Basic": 0, "Preferred": 0}.get(p.jlc_class, 1)
        return (exact, prefix, stocked, jlc, -len(p.providers), -p.total_stock)
    return sorted(parts, key=score)
