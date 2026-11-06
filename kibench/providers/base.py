"""Provider plugin interface. Adding a distributor = one file with a Provider subclass."""
from __future__ import annotations

import abc
from typing import Dict, List, Optional, Type

import httpx

from .. import __version__
from ..models import Part
from ..search.query import Filters, ParsedQuery

USER_AGENT = f"KiBench/{__version__} (KiCad part assistant)"

REGISTRY: Dict[str, Type["Provider"]] = {}


def register(cls: Type["Provider"]) -> Type["Provider"]:
    REGISTRY[cls.name] = cls
    return cls


class ProviderError(Exception):
    pass


class Provider(abc.ABC):
    name: str = ""
    label: str = ""
    needs_keys: tuple = ()          # secret names from config.SECRET_NAMES

    def __init__(self, client: httpx.AsyncClient, settings):
        self.client = client
        self.settings = settings

    def configured(self) -> bool:
        from ..config import get_secret
        return all(get_secret(k) for k in self.needs_keys)

    @abc.abstractmethod
    async def search(self, query: ParsedQuery, filters: Filters) -> List[Part]:
        ...

    @abc.abstractmethod
    async def get(self, mpn: str) -> Optional[Part]:
        ...


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(10.0, connect=5.0),
                             follow_redirects=True)
