"""Search providers. Importing this package registers all built-in providers."""
from .base import REGISTRY, Provider, ProviderError, make_client  # noqa: F401
from . import lcsc, mouser, digikey, nexar  # noqa: F401  (registration side effects)
