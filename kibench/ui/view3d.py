"""Small software 3D viewer: the model drawn over the footprint's pads, so alignment is visible.

Drag to orbit, wheel to zoom, double-click to reset. Plain QPainter (painter's algorithm), no OpenGL.
"""
from __future__ import annotations

import math
from typing import List, Optional, Set

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from ..model3d import Mesh

MAX_TRIS = 30000          # full quality
DRAG_TRIS = 5000          # while orbiting


class Model3DView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(220, 180)
        self.mesh: Optional[Mesh] = None
        self.pads: List[tuple] = []               # (number, [(x, y, z) corners]) in the model frame
        self.highlight: Set[str] = set()
        self.message = "No 3D model"
        self.yaw, self.pitch, self.zoom = -35.0, 55.0, 1.0
        self._drag = None
        self._prep = []                           # (tri, normal, rgb)
        self._extent = 5.0
        self._center = (0.0, 0.0, 0.0)

    # ------------------------------------------------------------ data
    def set_scene(self, mesh: Optional[Mesh], footprint=None, message: str = "") -> None:
        self.mesh = mesh
        self.message = message or ("" if mesh else "No 3D model")
        self.pads = []
        if footprint is not None:
            for pad in footprint.pads:
                if pad.kind == "np_thru_hole":
                    continue
                x1, y1, x2, y2 = pad.bbox()
                # footprint Y points down, the 3D frame's Y points up
                self.pads.append((pad.number, [(x1, -y1, 0.01), (x2, -y1, 0.01), (x2, -y2, 0.01), (x1, -y2, 0.01)]))
        tris = mesh.tris if mesh else []
        step = max(1, len(tris) // MAX_TRIS)
        self._prep = []
        for i in range(0, len(tris), step):
            a, b, c = tris[i]
            ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
            vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
            nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            self._prep.append((tris[i], (nx / ln, ny / ln, nz / ln), mesh.colors[i]))
        pts = [v for t, _, _ in self._prep for v in t] + [v for _, q in self.pads for v in q]
        if pts:
            xs, ys, zs = [p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts]
            self._center = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (min(zs) + max(zs)) / 2)
            self._extent = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs), 0.5)
        self.update()

    def set_highlight(self, numbers) -> None:
        self.highlight = set(numbers or [])
        self.update()

    # ------------------------------------------------------------ interaction
    def mousePressEvent(self, e):
        self._drag = e.position()

    def mouseMoveEvent(self, e):
        if self._drag is None:
            return
        d = e.position() - self._drag
        self._drag = e.position()
        self.yaw += d.x() * 0.5
        self.pitch = max(0.0, min(90.0, self.pitch - d.y() * 0.5))
        self.update()

    def mouseReleaseEvent(self, e):
        self._drag = None
        self.update()

    def wheelEvent(self, e):
        self.zoom *= 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        self.update()

    def mouseDoubleClickEvent(self, e):
        self.yaw, self.pitch, self.zoom = -35.0, 55.0, 1.0
        self.update()

    # ------------------------------------------------------------ drawing
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, self._drag is None)
        p.fillRect(self.rect(), QColor(28, 32, 38))
        if not self._prep and not self.pads:
            p.setPen(QColor(150, 150, 150))
            p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, self.message)
            return
        cy, sy = math.cos(math.radians(self.yaw)), math.sin(math.radians(self.yaw))
        cp, sp = math.cos(math.radians(self.pitch)), math.sin(math.radians(self.pitch))
        cx0, cy0, cz0 = self._center
        s = min(self.width(), self.height()) / self._extent * 0.75 * self.zoom
        w2, h2 = self.width() / 2, self.height() / 2

        def view(v):
            x, y, z = v[0] - cx0, v[1] - cy0, v[2] - cz0
            x, y = x * cy - y * sy, x * sy + y * cy           # yaw about the board normal
            y, z = y * cp - z * sp, y * sp + z * cp           # tilt toward the viewer
            return (w2 + x * s, h2 - y * s, z)                # z: larger is closer

        light = (0.35, 0.45, 0.82)
        polys = []
        # board plane slightly below the pads
        if self.pads:
            xs = [v[0] for _, q in self.pads for v in q]
            ys = [v[1] for _, q in self.pads for v in q]
            m = 0.6
            board = [(min(xs) - m, min(ys) - m, 0), (max(xs) + m, min(ys) - m, 0),
                     (max(xs) + m, max(ys) + m, 0), (min(xs) - m, max(ys) + m, 0)]
            pb = [view(v) for v in board]
            polys.append((min(q[2] for q in pb) - 1e6, pb, QColor(20, 70, 45)))
            for num, quad in self.pads:
                pq = [view(v) for v in quad]
                col = QColor(255, 210, 60) if num in self.highlight else QColor(200, 150, 70)
                polys.append((sum(q[2] for q in pq) / 4 - 1e5, pq, col))
        step = max(1, len(self._prep) // DRAG_TRIS) if self._drag is not None else 1
        # normals rotate with the view; light is fixed relative to the viewer
        for i in range(0, len(self._prep), step):
            tri, n, rgb = self._prep[i]
            a, b, c = view(tri[0]), view(tri[1]), view(tri[2])
            nx, ny, nz = n
            nx, ny = nx * cy - ny * sy, nx * sy + ny * cy
            ny, nz = ny * cp - nz * sp, ny * sp + nz * cp
            lam = abs(nx * light[0] - ny * light[1] + nz * light[2])
            k = 0.35 + 0.65 * lam
            col = QColor(int(min(1, rgb[0] * k + 0.04) * 255), int(min(1, rgb[1] * k + 0.04) * 255),
                         int(min(1, rgb[2] * k + 0.04) * 255))
            polys.append(((a[2] + b[2] + c[2]) / 3, (a, b, c), col))
        polys.sort(key=lambda t: t[0])
        for _, pts, col in polys:
            p.setPen(QPen(col, 0.6))
            p.setBrush(col)
            p.drawPolygon(QPolygonF([QPointF(x, y) for x, y, _ in pts]))
        p.setPen(QColor(170, 170, 170))
        p.drawText(8, self.height() - 8, self.message or "drag to rotate · wheel to zoom · double-click to reset")
