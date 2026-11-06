"""Build a KiCad PCM package (ZIP) installable via Plugin and Content Manager > Install from File."""
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kibench import __version__  # noqa: E402

META = {
    "$schema": "https://go.kicad.org/pcm/schemas/v2",
    "name": "KiBench",
    "description": "Part search, datasheets, symbol/footprint import and builders, design-aware chat",
    "description_full": ("KiBench is one window next to KiCad that takes a part from idea to placed symbol: "
                         "search LCSC/JLCPCB, Mouser, DigiKey and Nexar in parallel, read the datasheet, import "
                         "symbol, footprint and 3D model (EasyEDA conversion or vendor ZIPs via a Downloads "
                         "watcher), match KiCad stock footprints by dimensions, and build missing footprints "
                         "(IPC-7351B) and symbols (pin tables). An optional chat agent recommends real parts "
                         "using the schematic's context. Only the deterministic pipeline writes library files."),
    "identifier": "io.github.kibench",
    "type": "plugin",
    "author": {"name": "Niklas Hahn", "contact": {}},
    "license": "GPL-3.0-or-later",
    "resources": {},
    "tags": ["library", "parts", "footprint", "symbol", "lcsc", "datasheet"],
    "versions": [{
        "version": __version__,
        "status": "testing",
        "kicad_version": "10.0",
        "runtime": "ipc",
        "platforms": ["linux", "windows", "macos"],
    }],
}


def main():
    out = ROOT / "dist"
    out.mkdir(exist_ok=True)
    zpath = out / f"kibench-pcm-{__version__}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("metadata.json", json.dumps(META, indent=2))
        z.write(ROOT / "kicad_plugin" / "icon.png", "resources/icon.png")
        for f in (ROOT / "kicad_plugin").rglob("*"):
            if f.is_file() and f.name != "icon.png":
                z.write(f, "plugins/" + f.relative_to(ROOT / "kicad_plugin").as_posix())
        for f in (ROOT / "kibench").rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, "plugins/kibench/" + f.relative_to(ROOT / "kibench").as_posix())
    sha = hashlib.sha256(zpath.read_bytes()).hexdigest()
    print(f"{zpath}  sha256={sha}  size={zpath.stat().st_size}")


if __name__ == "__main__":
    main()
