"""KiBench tools for Claude Code (or any MCP client) over stdio.

    python -m kibench.mcp_server               # serve (Claude Code starts this itself)
    python -m kibench.mcp_server --register    # add it to Claude Code: `claude mcp add kibench ...`

The tools are the chat agent's read-only tools, plus `open_in_kibench`, which shows a part in the
KiBench window so the user can check and import it there. Nothing here writes library files.
Protocol: MCP over stdio, newline-delimited JSON-RPC 2.0 (no SDK needed).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

if __name__ == "__main__" and __package__ in (None, ""):
    # run as a file (how Claude Code starts it): import the package copy and run that
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from kibench.mcp_server import main as _main
    sys.exit(_main())

from . import __version__  # noqa: E402

log = logging.getLogger("kibench.mcp")

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
CODE_ROOT = Path(__file__).resolve().parent.parent      # folder that contains the kibench package

INSTRUCTIONS = (
    "KiBench searches real distributor stock (LCSC/JLCPCB, Mouser, DigiKey, Nexar) and reads the user's open "
    "KiCad project. Call get_design_context first when the question is about the user's board, and check each pick "
    "against it: supply rails and logic levels, the interfaces it connects to, and parts already used (reuse "
    "existing MPNs and package families when they fit). Recommend only "
    "MPNs that search_parts or get_part returned, quote stock/price from tool output, and check limits and "
    "pinouts with get_datasheet_text (cite the page) before claiming them. To let the user import a part's "
    "symbol, footprint and 3D model, call open_in_kibench: the import happens in the KiBench window after "
    "the user checks it. For a schematic review call check_schematic (ERC, net voltages, each part's pins and "
    "known ratings) and compare datasheet absolute maximum ratings with the volts on each part's pins. "
    "For a board review call check_board (DRC, voltage clearances, track widths per net, decoupling). "
    "Never write KiCad library files yourself."
)

EXTRA_TOOLS = [
    {"name": "open_in_kibench",
     "description": "Open a part in the KiBench window (starts KiBench if needed) so the user can view the "
                    "datasheet and import its symbol, footprint and 3D model after checking them.",
     "parameters": {"type": "object", "properties": {"mpn": {"type": "string"}}, "required": ["mpn"],
                    "additionalProperties": False}},
    {"name": "set_project",
     "description": "Switch the KiCad project used by get_design_context (folder or .kicad_pro path).",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
                    "additionalProperties": False}},
]


def _find_project(explicit: Optional[str]) -> Optional[Path]:
    from .kicad_ipc import project_from_env
    if explicit:
        p = Path(explicit).expanduser()
        return p.parent if p.is_file() else p
    if os.environ.get("KIBENCH_PROJECT"):
        return project_from_env()
    # Claude Code usually runs inside the project folder
    for d in (Path.cwd(), *Path.cwd().parents[:2]):
        if any(d.glob("*.kicad_pro")):
            return d
    return project_from_env()


class Server:
    def __init__(self, project: Optional[Path], chat: bool = False):
        from .agent.tools import TOOL_SPECS, ToolBox
        from .config import Settings
        from .kicad_ipc import KiCadBridge
        from .search.engine import SearchEngine
        self.settings = Settings.load()
        self.engine = SearchEngine(self.settings)
        self.project = project
        self.kicad = KiCadBridge()
        self._fp_index = None
        self._fp_lock = threading.Lock()
        self.toolbox = ToolBox(self.engine, self._context, None, lambda: self.project)
        # recommend_parts only makes sense inside the KiBench chat panel, which renders its cards
        specs = [t for t in TOOL_SPECS if chat or t["name"] != "recommend_parts"] + EXTRA_TOOLS
        self.tools = [{"name": t["name"], "description": t["description"], "inputSchema": t["parameters"]}
                      for t in specs]
        self.out = sys.stdout
        self.write_lock = threading.Lock()

    # ------------------------------------------------------------------ tool backends
    def _context(self) -> dict:
        from .design.context import load_context
        sel = []
        try:
            if self.kicad.connect():
                sel = self.kicad.selection_refs()
                self.project = self.project or self.kicad.project_dir()
        except Exception:
            pass
        if not self.project:
            return {"error": "no KiCad project found; ask the user for the project folder and call set_project"}
        out = load_context(self.project, sel).summary()
        out["project"] = str(self.project)
        return out

    def _index(self):
        """KiCad stock footprint index, built on first use (~15 s once, then cached on disk)."""
        with self._fp_lock:
            if self._fp_index is None:
                from .matcher import FootprintIndex
                from .paths import stock_dir
                root = stock_dir("footprints", self.settings.stock_footprint_dir)
                if not root:
                    return None
                idx = FootprintIndex(stock=root)
                if not idx.ready():
                    idx.build()
                self._fp_index = idx
            return self._fp_index

    async def call(self, name: str, args: dict) -> str:
        if name == "set_project":
            p = Path(args["path"]).expanduser()
            p = p.parent if p.is_file() else p
            if not p.is_dir():
                return json.dumps({"error": f"{p} is not a folder"})
            self.project = p
            return json.dumps({"project": str(p)})
        if name == "open_in_kibench":
            return json.dumps(open_in_kibench(args["mpn"], self.project))
        if name == "recommend_parts":
            n = len(self.toolbox.cards)
            out = json.loads(await self.toolbox.run(name, args))
            out["cards"] = [{"part": c.part.to_dict(), "reason": c.reason, "citations": c.citations,
                             "unverified": c.unverified, "uncited": c.uncited} for c in self.toolbox.cards[n:]]
            return json.dumps(out, ensure_ascii=False)
        if name in ("match_footprint", "check_cad_availability") and self.toolbox.fp_index is None:
            self.toolbox.fp_index = await asyncio.to_thread(self._index)
        return await self.toolbox.run(name, args)

    # ------------------------------------------------------------------ JSON-RPC
    def send(self, msg: dict) -> None:
        line = json.dumps(msg, ensure_ascii=False)
        with self.write_lock:
            self.out.write(line + "\n")
            self.out.flush()

    async def handle(self, msg: dict) -> None:
        mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
        if mid is None:                  # notification (initialized, cancelled, ...)
            return
        try:
            if method == "initialize":
                want = params.get("protocolVersion")
                result = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "kibench", "title": "KiBench", "version": __version__},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tools}
            elif method == "tools/call":
                name = params.get("name", "")
                if name not in {t["name"] for t in self.tools}:
                    raise LookupError(f"unknown tool {name}")
                try:
                    text, err = await self.call(name, params.get("arguments") or {}), False
                except Exception as e:
                    log.exception("tool %s failed", name)
                    text, err = f"{name} failed: {e.__class__.__name__}: {e}", True
                result = {"content": [{"type": "text", "text": text}], "isError": err}
            else:
                self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}})
                return
            self.send({"jsonrpc": "2.0", "id": mid, "result": result})
        except LookupError as e:
            self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": str(e)}})
        except Exception as e:
            log.exception("request %s failed", method)
            self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}})

    async def serve(self, stdin) -> None:
        loop = asyncio.get_running_loop()
        tasks = set()
        while True:
            line = await loop.run_in_executor(None, stdin.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            for m in (msg if isinstance(msg, list) else [msg]):
                t = asyncio.create_task(self.handle(m))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def open_in_kibench(mpn: str, project: Optional[Path]) -> dict:
    """Start `kibench --part MPN`; an already open window receives it and raises itself."""
    cmd = [sys.executable, "-m", "kibench", "--part", mpn]
    if project:
        cmd += ["--project", str(project)]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(CODE_ROOT), os.environ.get("PYTHONPATH")])))
    kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000008}  # DETACHED_PROCESS
    try:
        subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **kw)
    except OSError as e:
        return {"error": f"could not start KiBench: {e}"}
    return {"opened": mpn, "note": "KiBench shows the part; the user imports it from the Sources tab."}


# ---------------------------------------------------------------------- registration
def _kicad_env_python() -> Optional[Path]:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "kicad"
        exe = "Scripts/python.exe"
    elif sys.platform == "darwin":
        base, exe = Path.home() / "Library/Caches/kicad", "bin/python"
    else:
        base, exe = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "kicad", "bin/python"
    p = base / "10.0" / "python-environments" / "io.github.kibench" / exe
    return p if p.exists() else None


def _has_deps(py: str) -> bool:
    r = subprocess.run([py, "-c", "import httpx, pymupdf, keyring"], capture_output=True)
    return r.returncode == 0


def register(scope: str = "user") -> int:
    """Add the server to Claude Code with an interpreter that has KiBench's dependencies."""
    candidates = [sys.executable] + [str(p) for p in [_kicad_env_python()] if p]
    py = next((c for c in candidates if _has_deps(c)), None)
    if py is None:
        print("No Python with KiBench's packages found. Open KiBench from KiCad once (it builds its "
              "environment), or run this with the project's .venv/bin/python.", file=sys.stderr)
        return 1
    server_cmd = [py, str(CODE_ROOT / "kibench" / "mcp_server.py")]
    if not (CODE_ROOT / "kibench" / "mcp_server.py").exists():
        server_cmd = [py, "-m", "kibench.mcp_server"]
    claude = shutil.which("claude")
    if not claude:
        print("Claude Code (`claude`) is not on PATH. Add this to your MCP settings instead:")
        print(json.dumps({"mcpServers": {"kibench": {"command": server_cmd[0], "args": server_cmd[1:]}}}, indent=2))
        return 1
    subprocess.run([claude, "mcp", "remove", "-s", scope, "kibench"], capture_output=True)
    r = subprocess.run([claude, "mcp", "add", "-s", scope, "kibench", "--", *server_cmd],
                       capture_output=True, text=True)
    print((r.stdout + r.stderr).strip())
    if r.returncode == 0:
        print("Done. Start `claude` in your KiCad project folder and ask, e.g. "
              "\"find a 3.3 V LDO for this board that JLC stocks\".")
    return r.returncode


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kibench-mcp", description=__doc__.splitlines()[0])
    ap.add_argument("--project", help="KiCad project folder (default: current folder, then KiCad's open project)")
    ap.add_argument("--register", action="store_true", help="add this server to Claude Code")
    ap.add_argument("--chat", action="store_true", help="started by the KiBench chat panel (adds recommend_parts)")
    ap.add_argument("--scope", default="user", choices=["user", "project", "local"])
    a = ap.parse_args(argv)
    if a.register:
        return register(a.scope)
    # stdout carries the protocol; route every stray print and log line to stderr
    out = sys.stdout
    sys.stdout = sys.stderr
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="kibench-mcp %(levelname)s: %(message)s")
    srv = Server(_find_project(a.project), chat=a.chat)
    srv.out = out
    asyncio.run(srv.serve(sys.stdin))
    return 0


if __name__ == "__main__":
    sys.exit(main())
