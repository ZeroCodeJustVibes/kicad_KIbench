"""Model providers behind one interface: Anthropic (Claude), OpenRouter, local OpenAI-compatible servers."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import httpx

from ..config import get_secret

log = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    input: Dict[str, Any]


@dataclass
class Turn:
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    stop: str = ""


class LLMError(Exception):
    pass


class Provider:
    """Holds the provider-native message history (append-only)."""

    def __init__(self, system: str, tools: List[dict]):
        self.system = system
        self.tools = tools
        self.messages: List[Any] = []

    def add_user(self, text: str) -> None:
        raise NotImplementedError

    async def step(self) -> Turn:
        raise NotImplementedError

    def add_tool_results(self, results: List[Tuple[ToolCall, str, bool]]) -> None:
        raise NotImplementedError


class AnthropicProvider(Provider):
    """Claude via the official anthropic SDK (Messages API with tool use)."""

    def __init__(self, system: str, tools: List[dict], model: str = "claude-opus-5-5", effort: str = "medium"):
        super().__init__(system, tools)
        import anthropic
        key = get_secret("anthropic_api_key")
        # Without a stored key the SDK falls back to ANTHROPIC_API_KEY / `ant auth login` profiles.
        self.client = anthropic.AsyncAnthropic(api_key=key) if key else anthropic.AsyncAnthropic()
        self.model = model
        self.effort = effort
        self._tools = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                       for t in tools]

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    async def step(self) -> Turn:
        import anthropic
        try:
            resp = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                tools=self._tools,
                messages=self.messages,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                betas=["server-side-fallback-2026-07-01"],
                extra_body={"fallbacks": "default"},
            )
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic API key missing or invalid (Settings > Chat).") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Anthropic rate limit reached; try again shortly.") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Cannot reach the Anthropic API: {e}") from e
        # Append the full content (thinking + tool_use blocks) unchanged: the history stays append-only.
        self.messages.append({"role": "assistant", "content": resp.content})
        turn = Turn(stop=resp.stop_reason or "")
        if resp.stop_reason == "refusal":
            turn.text = "The model declined this request."
            return turn
        for b in resp.content:
            if b.type == "text":
                turn.text += b.text
            elif b.type == "tool_use":
                turn.tool_calls.append(ToolCall(b.id, b.name, dict(b.input or {})))
        return turn

    def add_tool_results(self, results: List[Tuple[ToolCall, str, bool]]) -> None:
        # All results for one assistant turn go back in a single user message.
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.id, "content": out, **({"is_error": True} if err else {})}
            for c, out, err in results]})


class OpenAICompatProvider(Provider):
    """OpenRouter and local servers (Ollama, LM Studio) via /chat/completions with tools."""

    def __init__(self, system: str, tools: List[dict], base_url: str, model: str, api_key: str = "",
                 extra_headers: Dict[str, str] = None):
        super().__init__(system, tools)
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        self.headers.update(extra_headers or {})
        self.messages = [{"role": "system", "content": system}]
        self._tools = [{"type": "function", "function": t} for t in tools]

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    async def step(self) -> Turn:
        body = {"model": self.model, "messages": self.messages, "tools": self._tools, "tool_choice": "auto"}
        try:
            async with httpx.AsyncClient(timeout=180.0) as c:
                r = await c.post(f"{self.base_url}/chat/completions", json=body, headers=self.headers)
        except httpx.HTTPError as e:
            raise LLMError(f"Cannot reach {self.base_url}: {e}") from e
        if r.status_code != 200:
            raise LLMError(f"{self.base_url} returned HTTP {r.status_code}: {r.text[:300]}")
        d = r.json()
        choice = (d.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        self.messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")} or
                             {"role": "assistant", "content": ""})
        turn = Turn(text=msg.get("content") or "", stop=choice.get("finish_reason") or "")
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {"_invalid_json": fn.get("arguments")}
            turn.tool_calls.append(ToolCall(tc.get("id", ""), fn.get("name", ""), args))
        return turn

    def add_tool_results(self, results: List[Tuple[ToolCall, str, bool]]) -> None:
        for c, out, err in results:
            self.messages.append({"role": "tool", "tool_call_id": c.id, "content": ("ERROR: " if err else "") + out})


def make_provider(settings, system: str, tools: List[dict]) -> Provider:
    p = settings.llm_provider
    if p == "anthropic":
        return AnthropicProvider(system, tools, settings.llm_model or "claude-opus-5-5")
    if p == "openrouter":
        return OpenAICompatProvider(system, tools, "https://openrouter.ai/api/v1", settings.openrouter_model,
                                    get_secret("openrouter_api_key"),
                                    {"HTTP-Referer": "https://github.com/kibench", "X-Title": "KiBench"})
    return OpenAICompatProvider(system, tools, settings.local_base_url, settings.local_model,
                                get_secret("local_llm_api_key"))
