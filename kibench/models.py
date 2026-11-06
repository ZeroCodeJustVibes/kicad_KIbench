"""Shared data types: parts, offers and CAD availability."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple


def mpn_key(mpn: str) -> str:
    """Normalise an MPN for merging: upper case, no spaces/dashes/slashes."""
    return re.sub(r"[\s\-_/.,#]", "", (mpn or "").upper())


MFR_ALIASES = {
    "ti": "texasinstruments", "texas": "texasinstruments", "st": "stmicroelectronics", "stm": "stmicroelectronics",
    "aos": "alphaomega", "alpha": "alphaomega", "adi": "analogdevices", "analog": "analogdevices",
    "on": "onsemi", "onsemiconductor": "onsemi", "nxp": "nxp", "microchip": "microchip", "atmel": "microchip",
    "maxim": "analogdevices", "linear": "analogdevices", "infineon": "infineon", "ir": "infineon",
    "vishay": "vishay", "rohm": "rohm", "toshiba": "toshiba", "diodes": "diodes", "nexperia": "nexperia",
    "renesas": "renesas", "murata": "murata", "samsung": "samsung", "yageo": "yageo", "uniroyal": "uniroyal",
}


def mfr_key(name: str) -> str:
    """Rough manufacturer identity: 'Texas Instruments' == 'TI' == 'TI(德州仪器)'."""
    n = re.sub(r"\(.*?\)|（.*?）", "", (name or "")).lower()
    n = re.sub(r"&", " ", n)
    words = re.findall(r"[a-z0-9]+", n)
    if not words:
        return ""
    first = words[0]
    if first in MFR_ALIASES:
        return MFR_ALIASES[first]
    joined = "".join(w for w in words if w not in ("inc", "corp", "co", "ltd", "llc", "semiconductor",
                                                    "semiconductors", "semi", "electronics", "technology", "tech"))
    for k, v in MFR_ALIASES.items():
        if len(k) > 3 and joined.startswith(k):
            return v
    return joined[:8]


def same_manufacturer(a: str, b: str) -> bool:
    ka, kb = mfr_key(a), mfr_key(b)
    return not ka or not kb or ka == kb or ka.startswith(kb[:5]) or kb.startswith(ka[:5])


@dataclass
class Offer:
    source: str                      # "LCSC", "Mouser", "DigiKey", seller name from Nexar
    sku: str = ""                    # distributor part number (C12594, 595-TL074CDR, ...)
    stock: Optional[int] = None
    price_breaks: List[Tuple[int, float]] = field(default_factory=list)
    currency: str = "USD"
    url: str = ""
    jlc_class: str = ""              # "Basic" / "Extended" / "Preferred" for JLC

    def unit_price(self, qty: int = 1) -> Optional[float]:
        best = None
        for q, p in sorted(self.price_breaks):
            if q <= qty:
                best = p
        if best is None and self.price_breaks:
            best = sorted(self.price_breaks)[0][1]
        return best


@dataclass
class CadInfo:
    source: str                      # "LCSC/EasyEDA", "SnapMagic", "Library", ...
    symbol: bool = False
    footprint: bool = False
    model3d: bool = False
    url: str = ""
    ref: str = ""                    # source specific id (LCSC code, lib:name, ...)


@dataclass
class Part:
    mpn: str
    manufacturer: str = ""
    description: str = ""
    package: str = ""
    category: str = ""
    specs: Dict[str, str] = field(default_factory=dict)
    datasheet_url: str = ""
    offers: List[Offer] = field(default_factory=list)
    cad: List[CadInfo] = field(default_factory=list)
    image_url: str = ""
    providers: List[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return mpn_key(self.mpn)

    def sku(self, source: str) -> str:
        for o in self.offers:
            if o.source.lower().startswith(source.lower()) and o.sku:
                return o.sku
        return ""

    @property
    def lcsc(self) -> str:
        return self.sku("LCSC")

    def best_offer(self, qty: int = 1) -> Optional[Offer]:
        """Offer with stock >= qty and the lowest unit price (else most stock)."""
        stocked = [o for o in self.offers if (o.stock or 0) >= qty and o.unit_price(qty) is not None]
        if stocked:
            return min(stocked, key=lambda o: o.unit_price(qty))
        if self.offers:
            return max(self.offers, key=lambda o: o.stock or 0)
        return None

    @property
    def total_stock(self) -> int:
        return sum(o.stock or 0 for o in self.offers)

    @property
    def jlc_class(self) -> str:
        for o in self.offers:
            if o.jlc_class:
                return o.jlc_class
        return ""

    def has_cad(self, what: str = "any") -> bool:
        for c in self.cad:
            if what == "any" and (c.symbol or c.footprint):
                return True
            if getattr(c, what, False):
                return True
        return False

    def merge(self, other: "Part") -> None:
        """Merge another provider's result for the same MPN into this one."""
        for attr in ("manufacturer", "description", "package", "category", "datasheet_url", "image_url"):
            if not getattr(self, attr) and getattr(other, attr):
                setattr(self, attr, getattr(other, attr))
        for k, v in other.specs.items():
            self.specs.setdefault(k, v)
        known = {(o.source, o.sku) for o in self.offers}
        self.offers += [o for o in other.offers if (o.source, o.sku) not in known]
        known_cad = {c.source for c in self.cad}
        self.cad += [c for c in other.cad if c.source not in known_cad]
        for p in other.providers:
            if p not in self.providers:
                self.providers.append(p)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Part":
        d = dict(d)
        d["offers"] = [Offer(**{**o, "price_breaks": [tuple(x) for x in o.get("price_breaks", [])]})
                       for o in d.get("offers", [])]
        d["cad"] = [CadInfo(**c) for c in d.get("cad", [])]
        return Part(**d)

    def summary(self) -> dict:
        """Compact dict for the chat agent (all values come from provider data)."""
        o = self.best_offer()
        return {
            "mpn": self.mpn,
            "manufacturer": self.manufacturer,
            "description": self.description[:200],
            "package": self.package,
            "category": self.category,
            "specs": dict(list(self.specs.items())[:25]),
            "stock": self.total_stock,
            "best_price": (o.unit_price(1) if o else None),
            "currency": (o.currency if o else ""),
            "best_source": (o.source if o else ""),
            "lcsc": self.lcsc,
            "jlc_class": self.jlc_class,
            "has_datasheet": bool(self.datasheet_url),
            "cad": [c.source for c in self.cad if c.symbol or c.footprint],
        }
