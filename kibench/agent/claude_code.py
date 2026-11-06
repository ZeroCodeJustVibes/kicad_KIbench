"""Chat backend that runs the user's own Claude Code CLI (`claude -p`), so a Claude Pro/Max plan works
without an API key. Claude Code signs in by itself; KiBench never sees its credentials.

Each message runs `claude -p` with KiBench's MCP server as the only tool source; `--resume` continues
the conversation. Cards arrive as `recommend_parts` tool results, which carry the validated part data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Dict, Optional

from ..models import Part
from .agent import SYSTEM_PROMPT
from .tools import Card

log = logging.getLogger(__name__)

SERVER = "kibench"


def find_claude() -> Optional[str]:
    """`claude` on PATH, or the usual install locations (KiCad may start plugins with a minimal PATH)."""
    exe = shutil.which("claude")
    if exe:
        return exe
    home = Path.home()
    for p in (home / ".local/bin/claude", home / ".claude/local/claude", Path("/usr/local/bin/claude"),
              home / "AppData/Roaming/npm/claude.cmd", Path("/opt/homebrew/bin/claude")):
        if p.exists():
            return str(p)
    return None


class ClaudeCodeAgent:
    def __init__(self, settings, toolbox, project_dir_fn: Callable[[], Optional[Path]]):
        self.settings = settings
        self.tools = toolbox
        self.project_dir_fn = project_dir_fn
        self.session_id: Optional[str] = None
        self.proc: Optional[asyncio.subprocess.Process] = None

    def reset(self) -> None:
        self.session_id = None
        self.tools.cards = []
        if self.proc and self.proc.returncode is None:
            self.proc.kill()

    def _cmd(self, exe: str, project: Optional[Path]) -> list:
        server = [str(Path(__file__).resolve().parent.parent / "mcp_server.py"), "--chat"]
        if project:
            server += ["--project", str(project)]
        mcp = {"mcpServers": {SERVER: {"command": sys.executable, "args": server}}}
        cmd = [exe, "-p", "--output-format", "stream-json", "--verbose",
               "--mcp-config", json.dumps(mcp), "--strict-mcp-config",
               "--tools", "",                              # no shell, file or web tools
               "--allowedTools", f"mcp__{SERVER}",
               "--append-system-prompt", SYSTEM_PROMPT]
        if self.settings.claude_code_model:
            cmd += ["--model", self.settings.claude_code_model]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        return cmd

    async def ask(self, text: str, on_event: Callable[[str, object], None]) -> None:
        """Same events as ChatAgent.ask: text, tool, cards, error, done."""
        exe = find_claude()
        if not exe:
            on_event("error", "Claude Code not found. Install it (https://claude.com/claude-code), run `claude` "
                              "once to sign in, then try again.")
            on_event("done", None)
            return
        project = self.project_dir_fn()
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self._cmd(exe, project), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=str(project) if project and project.is_dir() else None,
                env=dict(os.environ), limit=16 * 1024 * 1024)
        except OSError as e:
            on_event("error", f"Could not start Claude Code: {e}")
            on_event("done", None)
            return
        self.proc.stdin.write(text.encode())
        await self.proc.stdin.drain()
        self.proc.stdin.close()
        names: Dict[str, str] = {}
        said_text = False
        result: dict = {}
        try:
            async for raw in self.proc.stdout:
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                typ = ev.get("type")
                if ev.get("session_id"):
                    self.session_id = ev["session_id"]
                if typ == "assistant":
                    for b in ev.get("message", {}).get("content", []):
                        if b.get("type") == "text" and b.get("text", "").strip():
                            on_event("text", b["text"].strip())
                            said_text = True
                        elif b.get("type") == "tool_use":
                            name = b.get("name", "").removeprefix(f"mcp__{SERVER}__")
                            names[b.get("id", "")] = name
                            if name != "recommend_parts":
                                on_event("tool", (name, b.get("input") or {}))
                elif typ == "user":
                    content = ev.get("message", {}).get("content", [])
                    for b in content if isinstance(content, list) else []:
                        if b.get("type") == "tool_result" and names.get(b.get("tool_use_id")) == "recommend_parts":
                            cards = self._cards(b.get("content"))
                            if cards:
                                self.tools.cards += cards
                                on_event("cards", cards)
                elif typ == "result":
                    result = ev
            err = (await self.proc.stderr.read()).decode(errors="replace").strip()
            await self.proc.wait()
            if result.get("is_error") or (self.proc.returncode not in (0, None) and not result):
                msg = str(result.get("result") or err or f"Claude Code exited with {self.proc.returncode}")
                if "login" in msg.lower() or "auth" in msg.lower():
                    msg += "  (Run `claude` in a terminal once to sign in.)"
                on_event("error", msg[:800])
            elif not said_text and result.get("result"):
                on_event("text", str(result["result"]).strip())
        except asyncio.CancelledError:
            self.proc.kill()
            raise
        except Exception as e:
            log.exception("claude code chat failed")
            on_event("error", f"{e.__class__.__name__}: {e}")
        on_event("done", None)

    @staticmethod
    def _cards(content) -> list:
        texts = [content] if isinstance(content, str) else [c.get("text", "") for c in content or []
                                                             if isinstance(c, dict)]
        out = []
        for t in texts:
            try:
                data = json.loads(t)
            except ValueError:
                continue
            for c in data.get("cards", []):
                try:
                    out.append(Card(Part.from_dict(c["part"]), c.get("reason", ""), c.get("citations") or [],
                                    c.get("unverified") or [], bool(c.get("uncited"))))
                except (KeyError, TypeError, ValueError):
                    log.info("bad card from Claude Code: %s", c)
        return out
