"""Render the KiBench icon (chip + magnifier) to PNGs for the app and the KiCad toolbar."""
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen

ROOT = Path(__file__).resolve().parents[1]


def draw(size: int, dark: bool) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 24.0
    body = QColor(230, 232, 236) if dark else QColor(38, 50, 66)
    pin = QColor(200, 205, 212) if dark else QColor(90, 102, 118)
    accent = QColor(64, 170, 255)
    # chip body
    p.setPen(Qt.NoPen)
    p.setBrush(pin)
    for i in range(3):
        y = (6 + i * 4) * s
        p.drawRect(QRectF(1.5 * s, y, 3 * s, 1.6 * s))
        p.drawRect(QRectF(15.5 * s, y, 3 * s, 1.6 * s))
    p.setBrush(body)
    p.drawRoundedRect(QRectF(4 * s, 3.5 * s, 12 * s, 15 * s), 1.6 * s, 1.6 * s)
    p.setBrush(accent)
    p.drawEllipse(QPointF(7 * s, 6.5 * s), 1.1 * s, 1.1 * s)
    # magnifier
    pen = QPen(accent, 2.2 * s)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(QColor(255, 255, 255, 200) if not dark else QColor(20, 24, 30, 200))
    p.drawEllipse(QPointF(15 * s, 14.5 * s), 4.2 * s, 4.2 * s)
    p.drawLine(QPointF(18.1 * s, 17.6 * s), QPointF(22 * s, 21.5 * s))
    p.end()
    return img


def main():
    app = QGuiApplication(sys.argv[:1])  # noqa: F841
    out = ROOT / "kicad_plugin" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    for size in (24, 48):
        draw(size, False).save(str(out / f"kibench-light-{size}.png"))
        draw(size, True).save(str(out / f"kibench-dark-{size}.png"))
    res = ROOT / "kibench" / "resources"
    res.mkdir(parents=True, exist_ok=True)
    draw(256, False).save(str(res / "kibench.png"))
    draw(64, False).save(str(ROOT / "kicad_plugin" / "icon.png"))   # PCM listing icon
    print("icons written")


if __name__ == "__main__":
    main()
