"""Nexar / Octopart GraphQL supply API (cross-distributor aggregation)."""
from __future__ import annotations

import time
from typing import List, Optional

from ..config import get_secret
from ..models import Offer, Part
from ..search.query import Filters, ParsedQuery
from .base import Provider, ProviderError, register

TOKEN_URL = "https://identity.nexar.com/connect/token"
API = "https://api.nexar.com/graphql"

QUERY = """
query Search($q: String!, $limit: Int!) {
  supSearch(q: $q, limit: $limit) {
    results { part {
      mpn manufacturer { name } shortDescription category { name }
      bestDatasheet { url } bestImage { url }
      specs { attribute { name } displayValue }
      sellers(authorizedOnly: true) { company { name } offers {
        sku inventoryLevel clickUrl prices { quantity price currency } } }
    } }
  }
}"""


def nexar_to_part(p: dict) -> Part:
    specs = {s["attribute"]["name"]: s["displayValue"] for s in p.get("specs") or []
             if s.get("attribute") and s.get("displayValue")}
    offers = []
    for s in p.get("sellers") or []:
        name = (s.get("company") or {}).get("name", "")
        for o in (s.get("offers") or [])[:1]:
            prices = [(int(x["quantity"]), float(x["price"])) for x in o.get("prices") or [] if x.get("currency") == "USD"]
            offers.append(Offer(name, o.get("sku", ""), o.get("inventoryLevel"), sorted(prices), "USD", o.get("clickUrl", "")))
    pkg = specs.get("Case/Package", "") or specs.get("Package", "")
    return Part(mpn=p.get("mpn", ""), manufacturer=(p.get("manufacturer") or {}).get("name", ""),
                description=p.get("shortDescription", "") or "", package=pkg,
                category=(p.get("category") or {}).get("name", ""), specs=specs,
                datasheet_url=(p.get("bestDatasheet") or {}).get("url", "") or "", offers=offers,
                image_url=(p.get("bestImage") or {}).get("url", "") or "", providers=["nexar"])


@register
class NexarProvider(Provider):
    name = "nexar"
    label = "Nexar / Octopart"
    needs_keys = ("nexar_client_id", "nexar_client_secret")
    _token = ""
    _expires = 0.0

    async def _headers(self) -> dict:
        if not self._token or time.time() > self._expires - 60:
            r = await self.client.post(TOKEN_URL, data={
                "grant_type": "client_credentials", "client_id": get_secret("nexar_client_id"),
                "client_secret": get_secret("nexar_client_secret"), "scope": "supply.domain"})
            if r.status_code != 200:
                raise ProviderError(f"Nexar auth HTTP {r.status_code}")
            d = r.json()
            NexarProvider._token = d["access_token"]
            NexarProvider._expires = time.time() + int(d.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}"}

    async def _search(self, q: str, limit: int) -> List[Part]:
        r = await self.client.post(API, json={"query": QUERY, "variables": {"q": q, "limit": limit}},
                                   headers=await self._headers())
        if r.status_code != 200:
            raise ProviderError(f"Nexar HTTP {r.status_code}")
        d = r.json()
        if d.get("errors"):
            raise ProviderError("Nexar: " + d["errors"][0].get("message", "error"))
        res = ((d.get("data") or {}).get("supSearch") or {}).get("results") or []
        return [nexar_to_part(x["part"]) for x in res if x.get("part")]

    async def search(self, query: ParsedQuery, filters: Filters) -> List[Part]:
        if query.kind == "lcsc":
            return []
        return await self._search(query.keyword, 20)

    async def get(self, mpn: str) -> Optional[Part]:
        from ..models import mpn_key
        for p in await self._search(mpn, 5):
            if mpn_key(p.mpn) == mpn_key(mpn):
                return p
        return None
