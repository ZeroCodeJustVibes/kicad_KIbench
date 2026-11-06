"""Locations of KiBench data and KiCad installation directories."""
from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

KICAD_VERSION_DIR = "10.0"


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback)


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = _xdg("XDG_DATA_HOME", ".local/share")
    d = base / "kibench"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    d = data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_file() -> Path:
    return data_dir() / "settings.json"


def kicad_config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "kicad"
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Preferences/kicad"
    else:
        base = _xdg("XDG_CONFIG_HOME", ".config") / "kicad"
    return base / KICAD_VERSION_DIR


def kicad_user_data_dir() -> Path:
    if sys.platform == "win32":
        return Path.home() / "Documents/KiCad" / KICAD_VERSION_DIR
    if sys.platform == "darwin":
        return Path.home() / "Documents/KiCad" / KICAD_VERSION_DIR
    return _xdg("XDG_DATA_HOME", ".local/share") / "kicad" / KICAD_VERSION_DIR


def _kicad_env_overrides() -> dict:
    """Path variables the user set in KiCad (Preferences > Configure Paths)."""
    try:
        d = json.loads((kicad_config_dir() / "kicad_common.json").read_text())
        return (d.get("environment") or {}).get("vars") or {}
    except Exception:
        return {}


def _share_candidates() -> List[Path]:
    c = [Path("/usr/share/kicad"), Path("/usr/local/share/kicad"),
         Path("/var/lib/flatpak/app/org.kicad.KiCad/current/active/files/share/kicad"),
         Path.home() / ".local/share/flatpak/app/org.kicad.KiCad/current/active/files/share/kicad",
         Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport")]
    c += [Path(p) for p in glob.glob("/tmp/.mount_kicad*/share/kicad")]
    c += [Path(p) for p in glob.glob("/tmp/.mount_kicad*/usr/share/kicad")]
    if sys.platform == "win32":
        for pf in (os.environ.get("ProgramFiles", r"C:\Program Files"),):
            c += [Path(p) / "share/kicad" for p in glob.glob(os.path.join(pf, "KiCad", "*"))]
    return c


def stock_dir(kind: str, override: str = "") -> Optional[Path]:
    """Find a stock KiCad library dir. kind: footprints | symbols | 3dmodels."""
    var = {"footprints": "KICAD10_FOOTPRINT_DIR", "symbols": "KICAD10_SYMBOL_DIR",
           "3dmodels": "KICAD10_3DMODEL_DIR"}[kind]
    for cand in (override, os.environ.get(var, ""), _kicad_env_overrides().get(var, "")):
        if cand and Path(cand).is_dir():
            return Path(cand)
    for share in _share_candidates():
        p = share / kind
        if p.is_dir():
            return p
    return None


def downloads_dir() -> Path:
    if sys.platform.startswith("linux"):
        try:
            cfg = (_xdg("XDG_CONFIG_HOME", ".config") / "user-dirs.dirs").read_text()
            for line in cfg.splitlines():
                if line.startswith("XDG_DOWNLOAD_DIR"):
                    p = line.split("=", 1)[1].strip().strip('"').replace("$HOME", str(Path.home()))
                    if Path(p).is_dir():
                        return Path(p)
        except OSError:
            pass
    return Path.home() / "Downloads"


def expand_kicad_vars(uri: str, project_dir: Optional[Path] = None) -> str:
    """Resolve ${KIPRJMOD} and KiCad path variables in a library URI."""
    out = uri
    if project_dir is not None:
        out = out.replace("${KIPRJMOD}", str(project_dir))
    env = dict(os.environ)
    env.update(_kicad_env_overrides())
    for kind in ("footprints", "symbols", "3dmodels"):
        d = stock_dir(kind)
        if d:
            var = {"footprints": "KICAD10_FOOTPRINT_DIR", "symbols": "KICAD10_SYMBOL_DIR",
                   "3dmodels": "KICAD10_3DMODEL_DIR"}[kind]
            for ver in ("10", "9", "8", "7", "6"):        # old footprints keep older variable names
                env.setdefault(var.replace("10", ver, 1), str(d))
            env.setdefault("KICAD10_TEMPLATE_DIR", str(d.parent / "template"))
    import re
    return re.sub(r"\$\{([A-Za-z0-9_]+)\}", lambda m: env.get(m.group(1), m.group(0)), out)
