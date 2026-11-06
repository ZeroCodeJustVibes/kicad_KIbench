"""Install KiBench as a KiCad 10 IPC plugin for the current user.

    python scripts/install_plugin.py            # copy into KiCad's plugins folder
    python scripts/install_plugin.py --dev      # symlink (edits take effect immediately)
    python scripts/install_plugin.py --uninstall

KiCad builds a Python environment for the plugin from requirements.txt the first time it
loads it. The IPC API server must be enabled once: Preferences > Plugins > Enable KiCad API.
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kibench.paths import kicad_user_data_dir  # noqa: E402

NAME = "io.github.kibench"


def target() -> Path:
    return kicad_user_data_dir() / "plugins" / NAME


def install(dev: bool) -> Path:
    dst = target()
    if dst.is_symlink() or dst.exists():
        uninstall(quiet=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dev:
        dst.mkdir()
        for item in ("plugin.json", "launch.py", "requirements.txt", "icons", "icon.png"):
            os.symlink(ROOT / "kicad_plugin" / item, dst / item)
        os.symlink(ROOT / "kibench", dst / "kibench")
    else:
        shutil.copytree(ROOT / "kicad_plugin", dst)
        shutil.copytree(ROOT / "kibench", dst / "kibench",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dst


def kicad_env_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "kicad"
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Caches/kicad"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "kicad"
    return base / "10.0" / "python-environments" / NAME


def prepare_env() -> None:
    """Build KiCad's plugin venv ourselves when the interpreter lacks ensurepip (Debian/Ubuntu
    without python3-venv): KiCad then creates an empty venv, cannot install requirements and
    silently hides the plugin."""
    import subprocess
    py = "/usr/bin/python3" if sys.platform.startswith("linux") else sys.executable
    try:
        import json
        from kibench.paths import kicad_config_dir
        py = json.loads((kicad_config_dir() / "kicad_common.json").read_text()).get("api", {}).get("interpreter_path") or py
    except (OSError, ValueError):
        pass
    if subprocess.run([py, "-c", "import ensurepip"], capture_output=True).returncode == 0:
        return                      # KiCad can build the environment itself
    env = kicad_env_dir()
    req = str(ROOT / "kicad_plugin" / "requirements.txt")
    print(f"{py} has no ensurepip; preparing KiCad's plugin environment at {env}")
    if not (env / "bin" / "python").exists():
        subprocess.run([py, "-m", "venv", "--without-pip", str(env)], check=True)
    envpy = str(env / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"))
    if shutil.which("uv"):
        subprocess.run(["uv", "pip", "install", "-q", "-p", envpy, "pip", "-r", req], check=True)
    else:
        site = subprocess.run([envpy, "-c", "import site; print(site.getsitepackages()[0])"],
                              capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run([py, "-m", "pip", "install", "-q", "--target", site, "pip", "-r", req], check=True)
    print("Plugin environment ready. (Permanent fix: sudo apt install python3-venv)")


def uninstall(quiet: bool = False) -> None:
    dst = target()
    if dst.is_symlink():
        dst.unlink()
    elif dst.exists():
        shutil.rmtree(dst)
    if not quiet:
        print(f"Removed {dst}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", action="store_true", help="symlink instead of copy")
    ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args()
    if a.uninstall:
        uninstall()
        return
    dst = install(a.dev)
    print(f"Installed KiBench plugin to {dst}")
    prepare_env()
    print("Next: in KiCad, Preferences > Plugins > enable the KiCad API, then restart KiCad.")
    print("The KiBench button appears at the right end of the schematic and PCB editor top toolbars.")


if __name__ == "__main__":
    main()
