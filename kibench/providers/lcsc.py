"""LCSC / JLCPCB parts search (keyless) and EasyEDA CAD data."""
from __future__ import annotations

from typing import List, Optional

from ..cache import CAD_TTL, get_cache
from ..models import CadInfo, Offer, Part, mpn_key
from ..search.query import Filters, ParsedQuery
from .base import Provider, ProviderError, register

JLC_SEARCH = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList/v2"
EASYEDA_COMPONENT = "https://easyeda.com/api/products/{code}/components?version=6.4.19.5"
EASYEDA_STEP = "https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/{uuid}"
EASYEDA_ATTRIBUTION = "EasyEDA/JLCEDA official library (https://easyeda.com, https://lceda.cn)"


def _jlc_class(item: dict) -> str:
    if item.get("preferredComponentFlag"):
        return "Preferred"
    t = item.get("componentLibraryType")
    return {"base": "Basic", "expand": "Extended"}.get(t, t or "")


def jlc_item_to_part(item: dict) -> Part:
    prices = [(int(p["startNumber"]), float(p["productPrice"]))
              for p in item.get("componentPrices") or [] if p.get("productPrice") is not None]
    specs = {}
    for a in item.get("attributes") or []:
        n, v = a.get("attribute_name_en"), a.get("attribute_value_name")
        if n and v and v != "-":
            specs[n] = v
    code = item.get("componentCode", "")
    return Part(
        mpn=item.get("componentModelEn") or code,
        manufacturer=item.get("componentBrandEn") or "",
        description=item.get("describe") or item.get("componentName") or "",
        package=item.get("componentSpecificationEn") or "",
        category=item.get("componentTypeEn") or item.get("firstSortName") or "",
        specs=specs,
        datasheet_url=item.get("dataManualUrl") or "",
        offers=[Offer(source="LCSC", sku=code, stock=item.get("stockCount"), price_breaks=sorted(prices),
                      currency="USD", url=item.get("lcscGoodsUrl") or f"https://www.lcsc.com/product-detail/{code}.html",
                      jlc_class=_jlc_class(item))],
        cad=[CadInfo(source="LCSC/EasyEDA", symbol=True, footprint=True, model3d=False,
                     url=f"https://easyeda.com/component/{code}", ref=code)] if code else [],
        image_url=item.get("minImageAccessIdUrl") or "",
        providers=["lcsc"],
    )


@register
class LcscProvider(Provider):
    name = "lcsc"
    label = "LCSC / JLCPCB"

    async def _query(self, keyword: str, page_size: int = 30, jlc: str = "") -> List[dict]:
        body = {"keyword": keyword, "currentPage": 1, "pageSize": page_size}
        if jlc == "Basic":
            body["componentLibraryType"] = "base"
        r = await self.client.post(JLC_SEARCH, json=body)
        if r.status_code != 200:
            raise ProviderError(f"JLCPCB search HTTP {r.status_code}")
        d = r.json()
        data = d.get("data") or {}
        return ((data.get("componentPageInfo") or {}).get("list")) or []

    async def search(self, query: ParsedQuery, filters: Filters) -> List[Part]:
        kw = query.lcsc or query.keyword or query.raw
        items = await self._query(kw, jlc=filters.jlc)
        return [jlc_item_to_part(i) for i in items]

    async def get(self, mpn: str) -> Optional[Part]:
        items = await self._query(mpn, page_size=10)
        key = mpn_key(mpn)
        for i in items:
            if mpn_key(i.get("componentModelEn", "")) == key or i.get("componentCode", "").upper() == mpn.upper():
                return jlc_item_to_part(i)
        return None

    async def fetch_cad(self, code: str) -> dict:
        """Raw EasyEDA component JSON (symbol + footprint + 3D refs) for an LCSC code."""
        cache = get_cache()
        hit = cache.get("easyeda", code, ttl=CAD_TTL)
        if hit:
            return hit
        r = await self.client.get(EASYEDA_COMPONENT.format(code=code))
        if r.status_code != 200:
            raise ProviderError(f"EasyEDA HTTP {r.status_code}")
        d = r.json()
        res = d.get("result")
        if not d.get("success", True) or not res:
            raise ProviderError(f"EasyEDA has no CAD data for {code}")
        cache.put("easyeda", code, res)
        return res

    async def fetch_step(self, uuid: str) -> bytes:
        r = await self.client.get(EASYEDA_STEP.format(uuid=uuid), timeout=30.0)
        if r.status_code != 200 or len(r.content) < 100:
            raise ProviderError(f"EasyEDA 3D model HTTP {r.status_code}")
        return r.content
