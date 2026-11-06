"""KiCad IPC plugin entry point: opens (or raises) the KiBench window for the open project.

KiCad runs this inside the plugin's own virtual environment (packages from requirements.txt)
and sets KICAD_API_SOCKET / KICAD_API_TOKEN so KiBench can ask KiCad which project is open.
"""
import os
import sys
import traceback
from pathlib import Path

# The plugin folder itself (not resolved: with a --dev install launch.py is a symlink),
# plus the source tree the symlink points into.
HERE = Path(os.path.abspath(os.path.dirname(__file__)))
for p in (HERE, Path(__file__).resolve().parent.parent):
    if (p / "kibench").is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

if __name__ == "__main__":
    try:
        from kibench.app import main
        sys.exit(main(["--from-kicad"] + sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException:
        # KiCad shows nothing when a plugin crashes, so keep a log next to the settings.
        log = Path.home() / ".cache" / "kibench-launch.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(traceback.format_exc())
        raise
