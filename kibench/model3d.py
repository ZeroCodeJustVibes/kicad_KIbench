"""Load 3D models as triangle meshes for preview: STEP (via cascadio/OpenCascade), glTF binary and OBJ.

Meshes are returned in mm in KiCad's model frame (x right, y up, z up from the board), already
transformed by the footprint's model offset / rotation / scale, so they can be drawn over the pads.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
from array import array
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .paths import cache_dir

Vec = Tuple[float, float, float]
Tri = Tuple[Vec, Vec, Vec]


class Mesh:
    def __init__(self):
        self.tris: List[Tri] = []
        self.colors: List[Tuple[float, float, float]] = []     # one per triangle, 0..1

    def add(self, tri: Tri, color) -> None:
        self.tris.append(tri)
        self.colors.append(color)

    def bbox(self):
        if not self.tris:
            return None
        xs = [v[0] for t in self.tris for v in t]
        ys = [v[1] for t in self.tris for v in t]
        zs = [v[2] for t in self.tris for v in t]
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))

    def transformed(self, offset: Sequence[float] = (0, 0, 0), rotate: Sequence[float] = (0, 0, 0),
                    scale: Sequence[float] = (1, 1, 1)) -> "Mesh":
        """Apply a KiCad (model (offset) (scale) (rotate)) block: scale, rotate X then Y then Z (KiCad negates
        the angles), then offset in mm."""
        rx, ry, rz = (math.radians(-a) for a in rotate)
        cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
        ox, oy, oz = offset
        kx, ky, kz = scale

        def tf(v):
            x, y, z = v[0] * kx, v[1] * ky, v[2] * kz
            y, z = y * cx - z * sx, y * sx + z * cx
            x, z = x * cy + z * sy, -x * sy + z * cy
            x, y = x * cz - y * sz, x * sz + y * cz
            return (x + ox, y + oy, z + oz)
        out = Mesh()
        out.tris = [(tf(a), tf(b), tf(c)) for a, b, c in self.tris]
        out.colors = list(self.colors)
        return out


# ---------------------------------------------------------------------- loaders
def load(path: Path, data: Optional[bytes] = None) -> Mesh:
    """Mesh from a file (or its bytes): .step/.stp, .glb, .obj."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in (".step", ".stp"):
        return load_glb(step_to_glb(path, data))
    raw = data if data is not None else path.read_bytes()
    if ext == ".glb":
        return load_glb(raw)
    if ext == ".obj":
        return load_obj(raw.decode(errors="replace"))
    raise ValueError(f"no 3D preview for {ext or 'this'} files")


def step_to_glb(path: Path, data: Optional[bytes] = None) -> bytes:
    """Tessellate STEP with OpenCascade (cascadio); cached by content hash."""
    raw = data if data is not None else Path(path).read_bytes()
    key = hashlib.sha1(raw).hexdigest()[:16]
    out = cache_dir() / "meshes" / f"{key}.glb"
    if out.exists():
        return out.read_bytes()
    try:
        import cascadio
    except ImportError as e:
        raise RuntimeError("3D preview of STEP files needs the 'cascadio' package") from e
    out.parent.mkdir(parents=True, exist_ok=True)
    src = out.with_suffix(".step")
    src.write_bytes(raw)
    try:
        rc = cascadio.step_to_glb(str(src), str(out), 0.05, 0.5)
    finally:
        src.unlink(missing_ok=True)
    if rc != 0 or not out.exists():
        raise RuntimeError("could not read the STEP file")
    return out.read_bytes()


_COMP = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2), 5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def _mat_mul(a, b):
    return [sum(a[r + 4 * k] * b[k + 4 * c] for k in range(4)) for c in range(4) for r in range(4)]


def _node_matrix(n) -> List[float]:
    if "matrix" in n:
        return list(n["matrix"])
    tx, ty, tz = n.get("translation", (0, 0, 0))
    qx, qy, qz, qw = n.get("rotation", (0, 0, 0, 1))
    sx, sy, sz = n.get("scale", (1, 1, 1))
    r = [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy + qz * qw), 2 * (qx * qz - qy * qw), 0,
         2 * (qx * qy - qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz + qx * qw), 0,
         2 * (qx * qz + qy * qw), 2 * (qy * qz - qx * qw), 1 - 2 * (qx * qx + qy * qy), 0, 0, 0, 0, 1]
    for i in range(3):
        r[i] *= sx
        r[4 + i] *= sy
        r[8 + i] *= sz
    r[12], r[13], r[14] = tx, ty, tz
    return r                                  # column-major like glTF


def load_glb(raw: bytes) -> Mesh:
    if raw[:4] != b"glTF":
        raise ValueError("not a glTF binary")
    jlen = struct.unpack_from("<I", raw, 12)[0]
    doc = json.loads(raw[20:20 + jlen])
    off = 20 + jlen
    blen = struct.unpack_from("<I", raw, off)[0]
    binchunk = raw[off + 8: off + 8 + blen]

    def accessor(i):
        a = doc["accessors"][i]
        bv = doc["bufferViews"][a["bufferView"]]
        fmt, size = _COMP[a["componentType"]]
        n = _NCOMP[a["type"]]
        start = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
        stride = bv.get("byteStride", 0) or size * n
        arr = array(fmt)
        if stride == size * n:
            arr.frombytes(binchunk[start: start + a["count"] * stride])
            return arr, n
        for k in range(a["count"]):
            arr.frombytes(binchunk[start + k * stride: start + k * stride + size * n])
        return arr, n

    colors = []
    for m in doc.get("materials", []):
        c = m.get("pbrMetallicRoughness", {}).get("baseColorFactor", [0.6, 0.6, 0.6, 1])
        colors.append(tuple(c[:3]))
    mesh = Mesh()

    def walk(ni, parent):
        n = doc["nodes"][ni]
        mtx = _mat_mul(parent, _node_matrix(n))
        if "mesh" in n:
            for prim in doc["meshes"][n["mesh"]]["primitives"]:
                if prim.get("mode", 4) != 4:
                    continue
                pos, _ = accessor(prim["attributes"]["POSITION"])
                pts = []
                for k in range(0, len(pos), 3):
                    x, y, z = pos[k], pos[k + 1], pos[k + 2]
                    pts.append(((mtx[0] * x + mtx[4] * y + mtx[8] * z + mtx[12]) * 1000,
                                (mtx[1] * x + mtx[5] * y + mtx[9] * z + mtx[13]) * 1000,
                                (mtx[2] * x + mtx[6] * y + mtx[10] * z + mtx[14]) * 1000))
                idx = accessor(prim["indices"])[0] if "indices" in prim else range(len(pts))
                col = colors[prim["material"]] if "material" in prim and prim["material"] < len(colors) else (0.6, 0.6, 0.6)
                for k in range(0, len(idx) - 2, 3):
                    mesh.add((pts[idx[k]], pts[idx[k + 1]], pts[idx[k + 2]]), col)
        for c in n.get("children", []):
            walk(c, mtx)
    ident = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    scene = doc.get("scenes", [{"nodes": list(range(len(doc.get("nodes", []))))}])[doc.get("scene", 0)]
    for ni in scene.get("nodes", []):
        walk(ni, ident)
    return mesh


def load_obj(text: str) -> Mesh:
    """Wavefront OBJ with inline materials (EasyEDA's 3D endpoint), in mm."""
    verts: List[Vec] = []
    mats = {}
    cur_mat, cur = None, (0.6, 0.6, 0.6)
    mesh = Mesh()
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        tag = parts[0]
        if tag == "v":
            verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif tag == "newmtl":
            cur_mat = parts[1]
        elif tag == "Kd" and cur_mat is not None:
            mats[cur_mat] = (float(parts[1]), float(parts[2]), float(parts[3]))
        elif tag == "usemtl":
            cur = mats.get(parts[1], (0.6, 0.6, 0.6))
        elif tag == "f":
            ids = [int(p.split("/")[0]) for p in parts[1:]]
            ids = [i - 1 if i > 0 else len(verts) + i for i in ids]
            for k in range(1, len(ids) - 1):
                mesh.add((verts[ids[0]], verts[ids[k]], verts[ids[k + 1]]), cur)
    return mesh


def scene_for(prep) -> Tuple[Optional[Mesh], str]:
    """(mesh in the footprint frame, message) for a PreparedImport: its downloaded model, else the model the
    footprint references (KiCad stock models are STEP; a .wrl reference falls back to the .step next to it)."""
    from .paths import expand_kicad_vars
    try:
        if prep.model_bytes:
            m = load(Path(prep.model_filename or "model.step"), prep.model_bytes)
            return m.transformed(prep.model_offset, prep.model_rotate), ""
        fp = prep.footprint_model()
        if fp is not None and fp.models:
            mdl = fp.models[0]
            path = Path(expand_kicad_vars(mdl.path, None))
            if path.suffix.lower() == ".wrl" or not path.exists():
                for ext in (".step", ".stp", ".STEP"):
                    if path.with_suffix(ext).exists():
                        path = path.with_suffix(ext)
                        break
            if not path.exists():
                return None, f"3D model not found: {mdl.path}"
            return load(path).transformed(mdl.offset, mdl.rotate, mdl.scale), ""
    except Exception as e:                       # bad file, cascadio missing, ...
        return None, f"3D preview failed: {e}"
    return None, "This source has no 3D model"
