"""User settings (JSON file) and API keys (OS keyring)."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Dict, List

from .paths import config_file

log = logging.getLogger(__name__)
KEYRING_SERVICE = "kibench"

# Names of secrets kept in the keyring.
SECRET_NAMES = {
    "mouser_api_key": "Mouser API key",
    "digikey_client_id": "DigiKey client ID",
    "digikey_client_secret": "DigiKey client secret",
    "nexar_client_id": "Nexar client ID",
    "nexar_client_secret": "Nexar client secret",
    "snapmagic_api_key": "SnapMagic API key (optional)",
    "anthropic_api_key": "Anthropic API key",
    "openrouter_api_key": "OpenRouter API key",
    "local_llm_api_key": "Local LLM API key (usually empty)",
}


@dataclass
class Settings:
    providers: Dict[str, bool] = field(default_factory=lambda: {
        "lcsc": True, "mouser": True, "digikey": True, "nexar": False})
    library_name: str = "KiBench"
    use_global_library: bool = False
    global_library_dir: str = ""
    downloads_dir: str = ""
    stock_footprint_dir: str = ""
    ipc_density: str = "B"
    llm_provider: str = "anthropic"          # anthropic | openrouter | local | claude-code
    claude_code_model: str = ""              # empty: Claude Code's own default model
    llm_model: str = "claude-opus-5-5"
    openrouter_model: str = "anthropic/claude-opus-5.5"
    local_base_url: str = "http://localhost:11434/v1"
    local_model: str = "qwen3:14b"
    digikey_locale_site: str = "US"
    digikey_currency: str = "USD"
    mouser_country: str = ""
    search_timeout_s: float = 8.0
    chat_visible: bool = True
    theme: str = ""                          # "" follows the OS, or light | dark
    recent_queries: List[str] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(config_file().read_text())
        except (OSError, ValueError):
            return cls()
        s = cls()
        for k, v in data.items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s

    def save(self) -> None:
        config_file().write_text(json.dumps(asdict(self), indent=2))


_mem_secrets: Dict[str, str] = {}


def get_secret(name: str) -> str:
    try:
        import keyring
        v = keyring.get_password(KEYRING_SERVICE, name)
        if v:
            return v
    except Exception as e:  # no keyring backend available
        log.debug("keyring read failed: %s", e)
    return _mem_secrets.get(name, "")


def set_secret(name: str, value: str) -> bool:
    """Store a secret. Returns False if only kept in memory (no keyring backend)."""
    _mem_secrets[name] = value
    try:
        import keyring
        if value:
            keyring.set_password(KEYRING_SERVICE, name, value)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, name)
            except Exception:
                pass
        return True
    except Exception as e:
        log.warning("keyring write failed, key kept for this session only: %s", e)
        return False
