"""Entry point: `python -m kibench [--project DIR]` or launched from the KiCad plugin button."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional


def _single_instance(project: Optional[Path], part: str = ""):
    """Return a QLocalServer if we are the first instance, else forward the project (and part) and return None."""
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    name = f"kibench-{os.environ.get('USER') or os.environ.get('USERNAME') or 'user'}"
    sock = QLocalSocket()
    sock.connectToServer(name)
    if sock.waitForConnected(300):
        sock.write(json.dumps({"project": str(project or ""), "part": part}).encode())
        sock.flush()
        sock.waitForBytesWritten(500)
        sock.disconnectFromServer()
        return None
    QLocalServer.removeServer(name)
    server = QLocalServer()
    server.listen(name)
    return server


def _apply_theme(app) -> None:
    from .ui.theme import apply
    apply(app)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="kibench", description="KiBench part assistant for KiCad 10")
    ap.add_argument("--project", type=Path, help="KiCad project folder or .kicad_pro file")
    ap.add_argument("--from-kicad", action="store_true", help="launched by the KiCad plugin (ask KiCad for the project)")
    ap.add_argument("--part", default="", help="open this MPN (used by the Claude Code connector)")
    ap.add_argument("--check", nargs="?", const="check", default=None,
                    choices=["check", "clean", "board", "board-tidy"],
                    help="open the Check & Clean window on start (optionally on a tab)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from .kicad_ipc import KiCadBridge, project_from_env
    from .ui.main_window import MainWindow
    from .ui.state import AppState

    project = args.project or project_from_env()
    bridge = KiCadBridge()
    if (args.from_kicad or project is None) and bridge.connect():
        project = bridge.project_dir() or project
    if project is None:
        found = sorted(Path.cwd().glob("*.kicad_pro"))
        project = found[0].parent if found else None
    if project is not None and project.is_file():
        project = project.parent

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("KiBench")
    app.setOrganizationName("KiBench")
    icon = Path(__file__).parent / "resources" / "kibench.png"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    server = _single_instance(project, args.part)
    if server is None:
        return 0           # an existing window was told to switch project and raise itself
    _apply_theme(app)
    state = AppState(project)
    state.kicad = bridge
    win = MainWindow(state)

    def on_conn():
        c = server.nextPendingConnection()
        if c is None:
            return
        c.waitForReadyRead(500)
        data = bytes(c.readAll()).decode().strip()
        try:
            msg = json.loads(data)
        except ValueError:                       # older launcher: plain project path
            msg = {"project": "" if data == "-" else data}
        if msg.get("project") and Path(msg["project"]) != state.project_dir:
            state.set_project(Path(msg["project"]))
        if msg.get("part"):
            open_mpn(msg["part"])
        win.showNormal()
        win.raise_()
        win.activateWindow()
    def open_mpn(mpn: str):
        def done(p):
            if p is not None:
                state.open_part(p)
            else:
                state.status.emit(f"{mpn} not found at any enabled distributor")
        state.bridge.run(state.engine.get(mpn), done, lambda e: state.status.emit(f"Lookup of {mpn} failed: {e}"))

    server.newConnection.connect(on_conn)
    win.show()
    if args.part:
        open_mpn(args.part)
    if args.check:
        win.open_check(args.check)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
