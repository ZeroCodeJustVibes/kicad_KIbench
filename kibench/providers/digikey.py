"""DigiKey Product Information API v4 (free OAuth app, client-credentials flow)."""
from __future__ import annotations

import time
from typing import List, Optional

from ..config import get_secret
from ..models import Offer, Part
from ..search.query import Filters, ParsedQuery
from .base import Provider, ProviderError, register

TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
API = "https://api.digikey.com/products/v4/search"


def digikey_to_part(p: dict) -> Part:
    desc = p.get("Description") or {}
    specs = {}
    for prm in p.get("Parameters") or []:
        n, v = prm.get("ParameterText"), prm.get("ValueText")
        if n and v and v != "-":
            specs[n] = v
    offers = []
    for var in p.get("ProductVariations") or []:
        breaks = [(int(b["BreakQuantity"]), float(b["UnitPrice"])) for b in var.get("StandardPricing") or []]
        offers.append(Offer("DigiKey", var.get("DigiKeyProductNumber", ""), int(p.get("QuantityAvailable") or 0),
                            sorted(breaks), "USD", p.get("ProductUrl", "")))
        break  # first variation (cut tape) is enough for display
    if not offers:
        offers.append(Offer("DigiKey", "", int(p.get("QuantityAvailable") or 0),
                            [(1, float(p["UnitPrice"]))] if p.get("UnitPrice") else [], "USD", p.get("ProductUrl", "")))
    return Part(
        mpn=p.get("ManufacturerProductNumber", ""), manufacturer=(p.get("Manufacturer") or {}).get("Name", ""),
        description=desc.get("DetailedDescription") or desc.get("ProductDescription", ""),
        package=specs.get("Package / Case", "") or specs.get("Supplier Device Package", ""),
        category=(p.get("Category") or {}).get("Name", ""), specs=specs,
        datasheet_url=p.get("DatasheetUrl", "") or "", offers=offers,
        image_url=p.get("PhotoUrl", "") or "", providers=["digikey"])


@register
class DigiKeyProvider(Provider):
    name = "digikey"
    label = "DigiKey"
    needs_keys = ("digikey_client_id", "digikey_client_secret")
    _token = ""
    _expires = 0.0

    async def _auth(self) -> dict:
        if not self._token or time.time() > self._expires - 60:
            r = await self.client.post(TOKEN_URL, data={
                "client_id": get_secret("digikey_client_id"), "client_secret": get_secret("digikey_client_secret"),
                "grant_type": "client_credentials"})
            if r.status_code != 200:
                raise ProviderError(f"DigiKey auth HTTP {r.status_code}")
            d = r.json()
            DigiKeyProvider._token = d["access_token"]
            DigiKeyProvider._expires = time.time() + int(d.get("expires_in", 600))
        return {"Authorization": f"Bearer {self._token}", "X-DIGIKEY-Client-Id": get_secret("digikey_client_id"),
                "X-DIGIKEY-Locale-Site": self.settings.digikey_locale_site,
                "X-DIGIKEY-Locale-Currency": self.settings.digikey_currency}

    async def search(self, query: ParsedQuery, filters: Filters) -> List[Part]:
        if query.kind == "lcsc":
            return []
        body = {"Keywords": query.keyword, "Limit": 25, "Offset": 0}
        if filters.in_stock:
            body["FilterOptionsRequest"] = {"SearchOptions": ["InStock"]}
        r = await self.client.post(f"{API}/keyword", json=body, headers=await self._auth())
        if r.status_code != 200:
            raise ProviderError(f"DigiKey HTTP {r.status_code}")
        return [digikey_to_part(p) for p in r.json().get("Products") or []]

    async def get(self, mpn: str) -> Optional[Part]:
        r = await self.client.get(f"{API}/{mpn}/productdetails", headers=await self._auth())
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ProviderError(f"DigiKey HTTP {r.status_code}")
        p = r.json().get("Product")
        return digikey_to_part(p) if p else None
