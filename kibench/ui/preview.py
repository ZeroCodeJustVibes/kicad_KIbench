"""Footprint and symbol preview widgets (QPainter, no KiCad needed)."""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from ..kicad import footprint as F
from ..kicad import symbol as S

LAYER_COLORS = {
    "F.Cu": QColor(200, 52, 52, 200), "B.Cu": QColor(77, 127, 196, 170), "F.Paste": QColor(180, 160, 154, 120),
    "F.SilkS": QColor(242, 237, 161), "B.SilkS": QColor(232, 178, 167), "F.Fab": QColor(175, 175, 175),
    "B.Fab": QColor(88, 93, 132), "F.CrtYd": QColor(255, 38, 226), "B.CrtYd": QColor(38, 233, 255),
    "Edge.Cuts": QColor(208, 210, 205), "Cmts.User": QColor(89, 148, 220), "Dwgs.User": QColor(194, 194, 194),
    "F.Mask": QColor(216, 100, 255, 60),
}
LAYER_ORDER = ["B.Cu", "F.Paste", "F.Fab", "F.CrtYd", "F.SilkS", "Edge.Cuts", "Cmts.User", "Dwgs.User", "F.Mask"]


def _arc_path(start, mid, end, steps: int = 24):
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [start, end]
    ux = ((x1 ** 2 + y1 ** 2) * (y2 - y3) + (x2 ** 2 + y2 ** 2) * (y3 - y1) + (x3 ** 2 + y3 ** 2) * (y1 - y2)) / d
    uy = ((x1 ** 2 + y1 ** 2) * (x3 - x2) + (x2 ** 2 + y2 ** 2) * (x1 - x3) + (x3 ** 2 + y3 ** 2) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1, a2, a3 = (math.atan2(p[1] - uy, p[0] - ux) for p in (start, mid, end))

    def norm(a):
        return a % (2 * math.pi)
    ccw = norm(a2 - a1) < norm(a3 - a1)
    span = norm(a3 - a1) if ccw else -norm(a1 - a3)
    return [(ux + r * math.cos(a1 + span * k / steps), uy + r * math.sin(a1 + span * k / steps)) for k in range(steps + 1)]


class _ZoomView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(220, 180)
        self.zoom = 1.0
        self.pan = QPointF(0, 0)
        self._drag = None
        self.dark = True
        self.highlight = set()        # pin / pad numbers to emphasise

    def set_highlight(self, numbers) -> None:
        self.highlight = set(numbers or [])
        self.update()

    def wheelEvent(self, e):
        self.zoom *= 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        self.update()

    def mousePressEvent(self, e):
        self._drag = e.position()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.pan += e.position() - self._drag
            self._drag = e.position()
            self.update()

    def mouseReleaseEvent(self, e):
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        self.zoom, self.pan = 1.0, QPointF(0, 0)
        self.update()

    def _bg(self):
        return QColor(0, 16, 35) if self.dark else QColor(245, 244, 239)

    def _fit(self, p: QPainter, bbox, flip_y: bool = False):
        x1, y1, x2, y2 = bbox
        w, h = max(x2 - x1, 0.5), max(y2 - y1, 0.5)
        s = min((self.width() - 30) / w, (self.height() - 30) / h) * self.zoom
        p.translate(self.width() / 2 + self.pan.x(), self.height() / 2 + self.pan.y())
        p.scale(s, -s if flip_y else s)
        p.translate(-(x1 + x2) / 2, -(y1 + y2) / 2)
        return s


class FootprintView(_ZoomView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.fp: Optional[F.Footprint] = None
        self.message = "No footprint"

    def set_footprint(self, fp: Optional[F.Footprint], message: str = "") -> None:
        self.fp = fp
        self.message = message or ("" if fp else "No footprint")
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), self._bg())
        if not self.fp:
            p.setPen(QColor(150, 150, 150))
            p.drawText(self.rect(), Qt.AlignCenter, self.message)
            return
        fp = self.fp
        pts = []
        for pad in fp.pads:
            b = pad.bbox()
            pts += [(b[0], b[1]), (b[2], b[3])]
        for g in fp.graphics:
            pts += F.graphic_points(g)
        if not pts:
            return
        bbox = (min(x for x, _ in pts), min(y for _, y in pts), max(x for x, _ in pts), max(y for _, y in pts))
        p.save()
        scale = self._fit(p, bbox)
        by_layer = {}
        for g in fp.graphics:
            by_layer.setdefault(getattr(g, "layer", ""), []).append(g)
        for layer in [l for l in LAYER_ORDER if l in by_layer and l not in ("F.SilkS",)]:
            self._draw_graphics(p, by_layer[layer], LAYER_COLORS.get(layer, QColor(200, 200, 200)))
        if "F.Cu" in by_layer:
            self._draw_graphics(p, by_layer["F.Cu"], LAYER_COLORS["F.Cu"])
        for pad in fp.pads:
            self._draw_pad(p, pad)
        if "F.SilkS" in by_layer:
            self._draw_graphics(p, by_layer["F.SilkS"], LAYER_COLORS["F.SilkS"])
        p.restore()
        # pad numbers in screen space (readable at any zoom)
        if scale > 8:
            p.save()
            p.setPen(QColor(255, 255, 255))
            f = QFont()
            cx = self.width() / 2 + self.pan.x()
            cy = self.height() / 2 + self.pan.y()
            mx, my = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
            for pad in fp.pads:
                if not pad.number:
                    continue
                p.setPen(QColor(0, 0, 0) if pad.number in self.highlight else QColor(255, 255, 255))
                size_px = min(pad.w, pad.h) * scale
                if size_px < 9:
                    continue
                f.setPixelSize(int(max(7, min(14, size_px * 0.55))))
                p.setFont(f)
                sx, sy = cx + (pad.x - mx) * scale, cy + (pad.y - my) * scale
                p.drawText(QRectF(sx - 40, sy - 10, 80, 20), Qt.AlignCenter, pad.number)
            p.restore()
        p.setPen(QColor(140, 140, 140))
        x1, y1, x2, y2 = fp.pad_bbox()
        p.drawText(8, self.height() - 8, f"{fp.name}   pads {len(fp.numbered_pads())}   span {x2 - x1:.2f}×{y2 - y1:.2f} mm")

    def _draw_graphics(self, p: QPainter, items, color: QColor):
        for g in items:
            pen = QPen(color, max(getattr(g, "width", 0.1), 0.02))
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            fill = getattr(g, "fill", False)
            p.setBrush(QBrush(color) if fill else Qt.NoBrush)
            if isinstance(g, F.Line):
                p.drawLine(QPointF(g.x1, g.y1), QPointF(g.x2, g.y2))
            elif isinstance(g, F.Rect):
                p.drawRect(QRectF(QPointF(g.x1, g.y1), QPointF(g.x2, g.y2)).normalized())
            elif isinstance(g, F.Circle):
                p.drawEllipse(QPointF(g.cx, g.cy), g.r, g.r)
            elif isinstance(g, F.Arc):
                pts = _arc_path(g.start, g.mid, g.end)
                p.drawPolyline(QPolygonF([QPointF(x, y) for x, y in pts]))
            elif isinstance(g, F.Poly) and g.pts:
                poly = QPolygonF([QPointF(x, y) for x, y in g.pts])
                if fill:
                    p.drawPolygon(poly)
                else:
                    p.drawPolyline(poly)

    def _draw_pad(self, p: QPainter, pad: F.Pad):
        if pad.kind == "np_thru_hole":
            color = QColor(90, 90, 90)
        elif "B.Cu" in pad.layers and "F.Cu" not in pad.layers:
            color = LAYER_COLORS["B.Cu"]
        elif pad.layers == ["F.Paste"]:
            color = LAYER_COLORS["F.Paste"]
        elif pad.kind == "thru_hole":
            color = QColor(194, 166, 70, 220)
        else:
            color = LAYER_COLORS["F.Cu"]
        lit = pad.number in self.highlight
        if lit:
            color = QColor(255, 214, 64)
        p.save()
        p.translate(pad.x, pad.y)
        p.rotate(-pad.rot)
        p.setPen(QPen(QColor(255, 255, 255), 0.08) if lit else Qt.NoPen)
        p.setBrush(QBrush(color))
        r = QRectF(-pad.w / 2, -pad.h / 2, pad.w, pad.h)
        if pad.shape == "custom" and pad.polygon:
            p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pad.polygon]))
        elif pad.shape == "circle":
            p.drawEllipse(r)
        elif pad.shape == "oval":
            rad = min(pad.w, pad.h) / 2
            p.drawRoundedRect(r, rad, rad)
        elif pad.shape == "roundrect":
            rad = min(pad.w, pad.h) * pad.rratio
            p.drawRoundedRect(r, rad, rad)
        else:
            p.drawRect(r)
        if pad.drill:
            p.setBrush(QBrush(self._bg()))
            if isinstance(pad.drill, tuple):
                dw, dh = pad.drill
                rad = min(dw, dh) / 2
                p.drawRoundedRect(QRectF(-dw / 2, -dh / 2, dw, dh), rad, rad)
            else:
                p.drawEllipse(QPointF(0, 0), pad.drill / 2, pad.drill / 2)
        p.restore()


class SymbolView(_ZoomView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.sym: Optional[S.Symbol] = None
        self.unit = 0
        self.dark = False

    def set_symbol(self, sym: Optional[S.Symbol], unit: int = 0) -> None:
        self.sym = sym
        self.unit = unit
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(245, 244, 239))
        if not self.sym or not self.sym.units:
            p.setPen(QColor(150, 150, 150))
            p.drawText(self.rect(), Qt.AlignCenter, "No symbol")
            return
        u = self.sym.units[min(self.unit, len(self.sym.units) - 1)]
        tmp = S.Symbol(self.sym.name, units=[u])
        x1, y1, x2, y2 = tmp.bbox()
        p.save()
        self._fit(p, (x1 - 6, y1 - 3, x2 + 6, y2 + 3), flip_y=True)
        body = QColor(132, 0, 0)
        fillc = QColor(255, 255, 194)
        for g in u.graphics + self.sym.common:
            pen = QPen(body, max(getattr(g, "width", 0.254), 0.15))
            p.setPen(pen)
            fill = getattr(g, "fill", "none")
            p.setBrush(QBrush(fillc) if fill == "background" else QBrush(body) if fill == "outline" else Qt.NoBrush)
            if isinstance(g, S.SRect):
                p.drawRect(QRectF(QPointF(g.x1, g.y1), QPointF(g.x2, g.y2)).normalized())
            elif isinstance(g, S.SPoly):
                poly = QPolygonF([QPointF(x, y) for x, y in g.pts])
                p.drawPolygon(poly) if fill != "none" else p.drawPolyline(poly)
            elif isinstance(g, S.SCircle):
                p.drawEllipse(QPointF(g.cx, g.cy), g.r, g.r)
            elif isinstance(g, S.SArc):
                p.drawPolyline(QPolygonF([QPointF(x, y) for x, y in _arc_path(g.start, g.mid, g.end)]))
        p.setPen(QPen(body, 0.15))
        dirs = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}
        font = QFont()
        font.setPixelSize(1)
        for pin in u.pins:
            dx, dy = dirs.get(pin.rot % 360, (1, 0))
            ex, ey = pin.x + dx * pin.length, pin.y + dy * pin.length
            lit = pin.number in self.highlight
            if lit:
                p.setPen(QPen(QColor(230, 120, 0), 0.45))
                p.drawLine(QPointF(pin.x, pin.y), QPointF(ex, ey))
                p.setBrush(QColor(230, 120, 0))
                p.drawEllipse(QPointF(pin.x, pin.y), 0.5, 0.5)
                p.setBrush(Qt.NoBrush)
            p.setPen(QPen(body, 0.15))
            p.drawLine(QPointF(pin.x, pin.y), QPointF(ex, ey))
            p.save()
            p.scale(1, -1)
            p.setFont(font)
            p.setPen(QColor(0, 100, 100))
            if not self.sym.hide_pin_names and pin.name:
                if dx:
                    tx = ex + dx * 0.6
                    rect = QRectF(tx if dx > 0 else tx - 20, -ey - 0.6, 20, 1.2)
                    p.drawText(rect, (Qt.AlignLeft if dx > 0 else Qt.AlignRight) | Qt.AlignVCenter, pin.name)
                else:
                    p.save()
                    p.translate(ex, -(ey + dy * 0.6))
                    p.rotate(-90)
                    rect = QRectF(0 if dy > 0 else -20, -0.6, 20, 1.2)
                    p.drawText(rect, (Qt.AlignLeft if dy > 0 else Qt.AlignRight) | Qt.AlignVCenter, pin.name)
                    p.restore()
            if not self.sym.hide_pin_numbers:
                p.setPen(QColor(160, 0, 0))
                mx, my = (pin.x + ex) / 2, (pin.y + ey) / 2
                if dx:
                    p.drawText(QRectF(mx - 3, -my - 1.3, 6, 1.2), Qt.AlignCenter, pin.number)
                else:
                    p.save()
                    p.translate(mx - 0.7, -my)
                    p.rotate(-90)
                    p.drawText(QRectF(-3, -0.6, 6, 1.2), Qt.AlignCenter, pin.number)
                    p.restore()
            p.restore()
        p.restore()
        p.setPen(QColor(90, 90, 90))
        label = f"{self.sym.name}   unit {self.unit + 1}/{len(self.sym.units)}" + (f" ({u.name})" if u.name else "")
        p.drawText(8, self.height() - 8, label + f"   {len(self.sym.pins)} pins")


def preview_symbol(prep: Optional[object]):
    """The symbol to draw for a prepared import; vendor symbols (raw text) get an auto layout of their pins."""
    from ..builder import symbols as _SB
    if prep is None:
        return None
    if prep.symbol is not None:
        return prep.symbol
    pins = prep.pins()
    if not pins:
        return None
    return _SB.build_symbol(prep.symbol_name or "Symbol", [_SB.PinDef(n, nm, t) for n, nm, t in pins], "U")
