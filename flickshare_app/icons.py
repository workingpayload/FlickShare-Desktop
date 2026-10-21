"""Programmatically drawn FlickShare icons (no binary assets needed)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

ACCENT_A = "#7C5CFF"
ACCENT_B = "#22D3EE"
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def render_logo(size: int, active: bool = True) -> QImage:
    """Rounded gradient tile with a paper plane being flicked up and to the right."""
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    s = float(size)
    tile = QRectF(s * 0.04, s * 0.04, s * 0.92, s * 0.92)
    grad = QLinearGradient(tile.topLeft(), tile.bottomRight())
    if active:
        grad.setColorAt(0.0, QColor(ACCENT_A))
        grad.setColorAt(1.0, QColor(ACCENT_B))
    else:
        grad.setColorAt(0.0, QColor("#4B5060"))
        grad.setColorAt(1.0, QColor("#6B7280"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(grad))
    p.drawRoundedRect(tile, s * 0.22, s * 0.22)

    # Motion streaks
    if size >= 32:
        pen = QPen(QColor(255, 255, 255, 110), max(1.0, s * 0.045))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(s * 0.20, s * 0.66), QPointF(s * 0.34, s * 0.58))
        p.drawLine(QPointF(s * 0.24, s * 0.78), QPointF(s * 0.40, s * 0.69))

    # Paper plane
    plane = QPainterPath()
    plane.moveTo(s * 0.80, s * 0.20)
    plane.lineTo(s * 0.26, s * 0.44)
    plane.lineTo(s * 0.46, s * 0.52)
    plane.lineTo(s * 0.54, s * 0.74)
    plane.closeSubpath()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#FFFFFF"))
    p.drawPath(plane)

    fold = QPainterPath()
    fold.moveTo(s * 0.80, s * 0.20)
    fold.lineTo(s * 0.46, s * 0.52)
    fold.lineTo(s * 0.50, s * 0.62)
    fold.closeSubpath()
    p.setBrush(QColor(255, 255, 255, 170) if active else QColor(220, 220, 220, 170))
    p.drawPath(fold)

    p.end()
    return image


def app_icon(active: bool = True) -> QIcon:
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(QPixmap.fromImage(render_logo(size, active)))
    return icon


def export_ico(path: str) -> None:
    """Write a multi-resolution .ico (used by the PyInstaller build)."""
    import io

    from PIL import Image
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    render_logo(256).save(buffer, "PNG")
    buffer.close()
    with Image.open(io.BytesIO(bytes(data))) as img:
        img.save(path, format="ICO", sizes=[(n, n) for n in ICON_SIZES])
