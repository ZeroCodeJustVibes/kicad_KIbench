"""Mouser Search API v1 (free API key from mouser.com/api-hub)."""
from __future__ import annotations

import re
from typing import List, Optional

from ..config import get_secret
from ..models import Offer, Part
from ..search.query import Filters, ParsedQuery
from .base import Provider, ProviderError, register

API = "https://api.mouser.com/api/v1/search"


def _price(s: str) -> Optional[float]:
    s = re.sub(r"[^\d,.]", "", s or "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.index(",") < s.index(".") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def mouser_to_part(p: dict) -> Part:
    breaks = []
    cur = "USD"
    for b in p.get("PriceBreaks") or []:
        v = _price(b.get("Price", ""))
        if v is not None:
            breaks.append((int(b.get("Quantity", 1)), v))
            cur = b.get("Currency") or cur
    stock = p.get("AvailabilityInStock")
    if stock is None:
        m = re.match(r"([\d.,]+)", p.get("Availability") or "")
        stock = int(re.sub(r"[.,]", "", m.group(1))) if m else 0
    specs = {a["AttributeName"]: a["AttributeValue"] for a in p.get("ProductAttributes") or []
             if a.get("AttributeName") and a.get("AttributeValue")}
    pkg = specs.get("Package / Case") or specs.get("Package/Case") or ""
    return Part(
        mpn=p.get("ManufacturerPartNumber", ""), manufacturer=p.get("Manufacturer", ""),
        description=p.get("Description", ""), package=pkg, category=p.get("Category", ""),
        specs=specs, datasheet_url=p.get("DataSheetUrl", "") or "",
        offers=[Offer("Mouser", p.get("MouserPartNumber", ""), int(stock or 0), sorted(breaks), cur,
                      p.get("ProductDetailUrl", ""))],
        image_url=p.get("ImagePath", "") or "", providers=["mouser"])


@register
class MouserProvider(Provider):
    name = "mouser"
    label = "Mouser"
    needs_keys = ("mouser_api_key",)

    async def _post(self, path: str, body: dict) -> List[dict]:
        r = await self.client.post(f"{API}/{path}", params={"apiKey": get_secret("mouser_api_key")}, json=body)
        if r.status_code != 200:
            raise ProviderError(f"Mouser HTTP {r.status_code}")
        d = r.json()
        if d.get("Errors"):
            raise ProviderError("Mouser: " + "; ".join(e.get("Message", "") for e in d["Errors"]))
        return (d.get("SearchResults") or {}).get("Parts") or []

    async def search(self, query: ParsedQuery, filters: Filters) -> List[Part]:
        if query.kind == "lcsc":
            return []
        body = {"SearchByKeywordRequest": {"keyword": query.keyword, "records": 25, "startingRecord": 0,
                                           "searchOptions": "InStock" if filters.in_stock else "",
                                           "searchWithYourSignUpLanguage": "false"}}
        return [mouser_to_part(p) for p in await self._post("keyword", body) if p.get("ManufacturerPartNumber")]

    async def get(self, mpn: str) -> Optional[Part]:
        body = {"SearchByPartRequest": {"mouserPartNumber": mpn, "partSearchOptions": "Exact"}}
        parts = await self._post("partnumber", body)
        return mouser_to_part(parts[0]) if parts else None
