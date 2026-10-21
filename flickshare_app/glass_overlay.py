"""Full-screen Liquid Glass catch animation shown while the main window is hidden.

The look is ported from the MusicSM floating nav bar, which draws Apple-style Liquid Glass with
Kyant's ``backdrop`` library: ``vibrancy()`` -> ``blur(4dp)`` -> ``lens(16dp, 32dp, depthEffect)``
-> surface tint (``hazeTint`` + ``GlassFillStrong``) -> ``Highlight.Default`` -> ``Shadow.Default``.
Qt cannot run those AGSL shaders on a translucent desktop window, so the same maths runs on the
CPU with numpy instead:

* the screen under the overlay is captured (the overlay excludes itself from capture), lightly
  blurred and saturated 1.5x once per capture;
* the edge band of every glass shape refracts it with Kyant's rounded-rect SDF lens, computed
  only on the edge strips so a full-screen sheet stays cheap;
* an additive specular rim lit from 45 degrees traces the edge.

Motion is liquid: a small glass drop wells up at the screen edge the throw arrives from, then spreads
out from that point as a growing circle until the glass covers the whole screen. A glass card shows progress; a
catch squashes the card like jelly and sweeps the light around the rims, then the circle shrinks back into the
drop and it sinks behind the edge.
"""

from __future__ import annotations

import logging
import math
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

try:
    import numpy as np
except ImportError:  # pragma: no cover - the glass degrades to a tinted pane without numpy
    np = None

from PySide6.QtCore import QElapsedTimer, QFileInfo, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QImageReader,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QScreen,
)
from PySide6.QtWidgets import QFileIconProvider, QWidget

from flickshare_app.icons import render_logo
from flickshare_app.widgets import format_bytes

logger = logging.getLogger("flickshare.app")

# ---------------------------------------------------------------- MusicSM / Kyant glass tokens (dp)
GLASS_BLUR_DP = 4.0  # LIQUID_BLUR
SATURATION = 1.5  # vibrancy()
PILL_LENS = (16.0, 32.0)  # LIQUID_LENS_HEIGHT, LIQUID_LENS_AMOUNT
SHEET_LENS = (26.0, 52.0)  # same ratio, scaled up for a screen-sized pane
CARD_LENS = (16.0, 32.0)
HIGHLIGHT_ALPHA = 0.5  # HighlightStyle.Default colour
HIGHLIGHT_ANGLE = 45.0
HIGHLIGHT_WIDTH_DP = 1.0
SHADOW_RADIUS = 24.0  # Shadow.Default
SHADOW_OFFSET = 4.0
SHADOW_ALPHA = 0.10
HAZE_TINT = (255, 255, 255, 0x1A)  # DarkPalette.hazeTint
GLASS_FILL_STRONG = (0x29, 0x2A, 0x2F, 0x40)  # DarkPalette.glassFillStrong
CARD_PUCK = (0x0D, 0x0E, 0x13, 0x8C)  # MusicSM's darker-glass puck (surfaceLowest @ 0.55), for legible text
SHEET_TINTS = (HAZE_TINT, GLASS_FILL_STRONG)
CARD_TINTS = (HAZE_TINT, GLASS_FILL_STRONG, CARD_PUCK)

DROP_RADIUS = 32.0  # the liquid drop the glass spreads from (nav-bar height / 2)
SIDE_DROP_RISE = 130.0  # a side drop sits at least this far above the bottom of the work area
PILL_MARGIN = 14.0  # gap between the resting drop and the screen edge
SIDE_ENTRY_DEG = 50.0  # throws steeper than this enter from the left/right edge instead of the bottom
TILE_PX = 64  # large round glass is shaded in tiles so only the rim costs numpy work
CARD_SIZE = QSize(520, 148)
CARD_RADIUS = 44.0
THUMB = 100

CAUGHT_HOLD_MS = 2600
FAILED_HOLD_MS = 1800
SWEEP_MS = 1100
BACKDROP_REFRESH_MS = 1500
FRAME_MS = 16
IDLE_FRAME_MS = 50
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".heic", ".tif", ".tiff"}
UI_FONTS = ["Segoe UI Variable Display", "Segoe UI", "Inter", "Arial"]

WDA_EXCLUDEFROMCAPTURE = 0x11

Tint = Tuple[int, int, int, int]


class Phase(Enum):
    HIDDEN = "hidden"
    RISING = "rising"
    HOLDING = "holding"
    CAUGHT = "caught"
    DRAINING = "draining"


# ---------------------------------------------------------------- springs


class Spring:
    """Damped spring in the spirit of Compose's ``spring(dampingRatio, stiffness)``."""

    def __init__(self, value: float = 0.0, damping: float = 0.75, stiffness: float = 400.0) -> None:
        self.value = value
        self.target = value
        self.velocity = 0.0
        self.damping = damping
        self.stiffness = stiffness

    def animate_to(self, target: float, damping: Optional[float] = None, stiffness: Optional[float] = None) -> None:
        self.target = target
        if damping is not None:
            self.damping = damping
        if stiffness is not None:
            self.stiffness = stiffness

    def kick(self, velocity: float) -> None:
        self.velocity += velocity

    def snap(self, value: float) -> None:
        self.value = self.target = value
        self.velocity = 0.0

    @property
    def at_rest(self) -> bool:
        return self.velocity == 0.0 and self.value == self.target

    def step(self, dt_s: float) -> None:
        if self.at_rest:
            return
        omega = math.sqrt(self.stiffness)
        remaining = min(max(dt_s, 0.0), 0.1)
        while remaining > 1e-6:
            h = min(remaining, 0.004)
            accel = -self.stiffness * (self.value - self.target) - 2.0 * self.damping * omega * self.velocity
            self.velocity += accel * h
            self.value += self.velocity * h
            remaining -= h
        if abs(self.value - self.target) < 5e-4 and abs(self.velocity) < 5e-3:
            self.snap(self.target)


# ---------------------------------------------------------------- glass maths (physical pixels)


@dataclass(frozen=True)
class GlassShape:
    """A rounded-rect glass pane in physical pixels, plus Kyant lens/highlight parameters."""

    x: float
    y: float
    w: float
    h: float
    radius: float
    refraction_height: float
    refraction_amount: float
    highlight_width: float
    highlight_angle: float = HIGHLIGHT_ANGLE
    highlight_alpha: float = HIGHLIGHT_ALPHA
    tints: Tuple[Tint, ...] = ()
    depth_effect: bool = True


def shade_glass_block(src, shape: GlassShape, x0: int, y0: int, x1: int, y1: int):
    """Render the glass for pixels [x0,x1) x [y0,y1) as premultiplied BGRA (Kyant's shaders on numpy).

    ``src`` is the processed backdrop as an (H, W, 4) uint8 BGRA array in the same pixel space.
    """
    xs = np.arange(x0, x1, dtype=np.float32) + 0.5
    ys = np.arange(y0, y1, dtype=np.float32) + 0.5
    hx, hy = shape.w * 0.5, shape.h * 0.5
    radius = min(shape.radius, hx, hy)
    cx, cy = np.broadcast_arrays((xs - (shape.x + hx))[None, :], (ys - (shape.y + hy))[:, None])
    abs_x, abs_y = np.abs(cx), np.abs(cy)

    # sdRoundedRect
    ax, ay = abs_x - (hx - radius), abs_y - (hy - radius)
    sd = np.hypot(np.maximum(ax, 0), np.maximum(ay, 0)) - radius + np.minimum(np.maximum(ax, ay), 0)

    # gradSdRoundedRect with gradRadius = min(radius * 1.5, min(halfSize))
    grad_r = min(radius * 1.5, hx, hy)
    gax, gay = abs_x - (hx - grad_r), abs_y - (hy - grad_r)
    corner = (gax >= 0) | (gay >= 0)
    mx, my = np.maximum(gax, 0), np.maximum(gay, 0)
    norm = np.hypot(mx, my)
    norm = np.where(norm > 0, norm, 1.0)
    edge_x = (gax >= gay).astype(np.float32)
    sign_x, sign_y = np.sign(cx), np.sign(cy)
    gx = np.where(corner, mx / norm, edge_x) * sign_x
    gy = np.where(corner, my / norm, 1.0 - edge_x) * sign_y

    # Lens: circleMap(1 - depth / refractionHeight) * -refractionAmount along the (depth-biased) normal.
    depth = np.maximum(-sd, 0)
    t = 1.0 - np.minimum(depth / max(shape.refraction_height, 1e-3), 1.0)
    d = (1.0 - np.sqrt(np.maximum(1.0 - t * t, 0.0))) * -shape.refraction_amount
    rx, ry = gx, gy
    if shape.depth_effect:
        cl = np.hypot(cx, cy)
        cl = np.where(cl > 0, cl, 1.0)
        rx, ry = gx + cx / cl, gy + cy / cl
        rl = np.hypot(rx, ry)
        rl = np.where(rl > 0, rl, 1.0)
        rx, ry = rx / rl, ry / rl
    h_src, w_src = src.shape[:2]
    sx = np.clip(np.floor(xs[None, :] + d * rx), 0, w_src - 1).astype(np.intp)
    sy = np.clip(np.floor(ys[:, None] + d * ry), 0, h_src - 1).astype(np.intp)
    rgb = src[sy, sx, :3].astype(np.float32)

    # Surface tint (onDrawSurface), composited over the refracted content.
    for r, g, b, a in shape.tints:
        k = a / 255.0
        rgb *= 1.0 - k
        rgb += np.array([b, g, r], dtype=np.float32) * k

    # Highlight.Default: additive rim, intensity = |dot(grad, (cos a, sin a))|.
    angle = math.radians(shape.highlight_angle)
    intensity = np.abs(gx * math.cos(angle) + gy * math.sin(angle))
    stroke = np.clip(1.0 - depth / max(shape.highlight_width, 1e-3), 0.0, 1.0)
    rgb += (255.0 * shape.highlight_alpha * intensity * stroke)[..., None]
    np.minimum(rgb, 255.0, out=rgb)

    coverage = np.clip(0.5 - sd, 0.0, 1.0)
    out = np.empty(sd.shape + (4,), dtype=np.uint8)
    out[..., :3] = (rgb * coverage[..., None] + 0.5).astype(np.uint8)
    out[..., 3] = (coverage * 255.0 + 0.5).astype(np.uint8)
    return out


def _sd_rounded_rect(px: float, py: float, shape: GlassShape, radius: float) -> float:
    """Signed distance from (px, py) to ``shape`` (negative inside), same SDF as ``shade_glass_block``."""
    hx, hy = shape.w * 0.5, shape.h * 0.5
    ax = abs(px - (shape.x + hx)) - (hx - radius)
    ay = abs(py - (shape.y + hy)) - (hy - radius)
    return math.hypot(max(ax, 0.0), max(ay, 0.0)) - radius + min(max(ax, ay), 0.0)


def _image_array(image: QImage):
    """Zero-copy (H, W, 4) view of a 32-bit QImage; keep ``image`` alive while using it."""
    view = np.frombuffer(image.constBits(), dtype=np.uint8, count=image.sizeInBytes())
    return view.reshape(image.height(), image.bytesPerLine() // 4, 4)[:, : image.width()]


def _array_image(arr) -> QImage:
    arr = np.ascontiguousarray(arr)
    h, w = arr.shape[:2]
    return QImage(arr.data, w, h, w * 4, QImage.Format.Format_ARGB32_Premultiplied).copy()


def _box_blur_axis(a, r: int, axis: int):
    a = np.moveaxis(a, axis, 0)
    padded = np.concatenate([np.repeat(a[:1], r + 1, 0), a, np.repeat(a[-1:], r, 0)], 0)
    c = np.cumsum(padded, 0, dtype=np.float32)
    return np.moveaxis((c[2 * r + 1 :] - c[: -2 * r - 1]) / (2 * r + 1), 0, axis)


def process_backdrop(image: QImage, dpr: float) -> QImage:
    """vibrancy() + blur(4dp): saturate 1.5x and soften, done at half resolution for speed."""
    image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    w, h = image.width(), image.height()
    half = image.scaled(
        max(1, w // 2), max(1, h // 2), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    arr = _image_array(half)[..., :3].astype(np.float32)
    sigma = (0.57735 * GLASS_BLUR_DP * dpr + 0.5) / 2.0  # Skia radius->sigma, at half scale
    box = max(1, round((math.sqrt(6.0 * sigma * sigma + 1.0) - 1.0) / 2.0))
    for axis in (0, 1, 0, 1):
        arr = _box_blur_axis(arr, box, axis)
    inv = 1.0 - SATURATION
    lum = 0.213 * inv * arr[..., 2] + 0.715 * inv * arr[..., 1] + 0.072 * inv * arr[..., 0]
    out = np.empty(arr.shape[:2] + (4,), dtype=np.uint8)
    out[..., :3] = np.clip(arr * SATURATION + lum[..., None], 0, 255).astype(np.uint8)
    out[..., 3] = 255
    full = _array_image(out).scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    full.setDevicePixelRatio(dpr)
    return full


class GlassRenderer:
    """Paints liquid glass shapes over a processed screen backdrop, caching the edge strips."""

    def __init__(self) -> None:
        self.image: Optional[QImage] = None
        self._pixels = None
        self._draft_image: Optional[QImage] = None
        self._draft_pixels = None
        self.dpr = 1.0
        self._cache: Dict[str, Tuple[tuple, list]] = {}

    @property
    def available(self) -> bool:
        return self._pixels is not None

    @staticmethod
    def prepare(image: QImage, dpr: float) -> Tuple[QImage, QImage]:
        """Process a captured screen image; thread-safe (QImage + numpy only)."""
        full = process_backdrop(image, dpr)
        # Half-resolution copy for frames where the glass is moving; the backdrop is blurred anyway.
        draft = full.scaled(
            max(1, full.width() // 2), max(1, full.height() // 2),
            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation,
        )
        return full, draft

    def set_backdrop(self, pixmap: Optional[QPixmap]) -> None:
        if pixmap is None or pixmap.isNull() or np is None:
            self.set_prepared(None)
            return
        dpr = pixmap.devicePixelRatio() or 1.0
        self.set_prepared((*self.prepare(pixmap.toImage(), dpr), dpr))

    def set_prepared(self, prepared: Optional[Tuple[QImage, QImage, float]]) -> None:
        self._cache.clear()
        if prepared is None:
            self.image = self._pixels = self._draft_image = self._draft_pixels = None
            return
        self.image, self._draft_image, self.dpr = prepared
        self._pixels = _image_array(self.image)
        self._draft_pixels = _image_array(self._draft_image)

    def paint(self, p: QPainter, key: str, rect: QRectF, radius: float, lens: Tuple[float, float],
              angle: float, tints: Sequence[Tint], draft: bool = False) -> None:
        """Draw one glass shape; ``draft`` shades at half resolution (for frames mid-animation)."""
        radius = min(radius, rect.width() / 2.0, rect.height() / 2.0)
        if rect.width() < 1 or rect.height() < 1:
            return
        if not self.available:
            self._paint_fallback(p, rect, radius, angle, tints)
            return
        dpr = self.dpr * (0.5 if draft else 1.0)
        shape = GlassShape(
            round(rect.x() * dpr, 1), round(rect.y() * dpr, 1), round(rect.width() * dpr, 1),
            round(rect.height() * dpr, 1), round(radius * dpr, 1), round(lens[0] * dpr, 1),
            round(lens[1] * dpr, 1), max(1.0, HIGHLIGHT_WIDTH_DP * dpr), round(angle, 0), HIGHLIGHT_ALPHA,
            tuple(tints),
        )
        cached = self._cache.get(key)
        if cached is not None and cached[0] == (shape, draft):
            blocks = cached[1]
        else:
            blocks = self._build(shape, draft)
            self._cache[key] = ((shape, draft), blocks)
        for kind, data, target, source in blocks:
            if kind == "strip":
                p.drawImage(target, data)
            else:
                p.drawImage(target, self.image, source)
                for r, g, b, a in tints:
                    p.fillRect(target, QColor(r, g, b, a))

    def _build(self, shape: GlassShape, draft: bool) -> list:
        src = self._draft_pixels if draft else self._pixels
        h_src, w_src = src.shape[:2]
        scale = 0.5 if draft else 1.0
        dpr = self.dpr * scale
        ox, oy = math.floor(shape.x), math.floor(shape.y)
        ex, ey = math.ceil(shape.x + shape.w), math.ceil(shape.y + shape.h)
        radius = min(shape.radius, shape.w / 2.0, shape.h / 2.0)
        band_y = int(math.ceil(max(radius, shape.refraction_height, shape.highlight_width))) + 1
        band_x = int(math.ceil(max(shape.refraction_height, shape.highlight_width))) + 1
        if radius > TILE_PX and ex - ox > 2 * TILE_PX and ey - oy > 2 * TILE_PX:
            return self._build_tiled(src, shape, radius, band_x, dpr, scale)
        if ey - oy <= 2 * band_y or ex - ox <= 2 * band_x:
            strips, interior = [(ox, oy, ex, ey)], None
        else:
            strips = [
                (ox, oy, ex, oy + band_y),
                (ox, ey - band_y, ex, ey),
                (ox, oy + band_y, ox + band_x, ey - band_y),
                (ex - band_x, oy + band_y, ex, ey - band_y),
            ]
            interior = (ox + band_x, oy + band_y, ex - band_x, ey - band_y)

        def clip(box):
            x0, y0, x1, y1 = box
            x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, w_src), min(y1, h_src)
            return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None

        blocks = []
        for box in strips:
            box = clip(box)
            if box is None:
                continue
            x0, y0, x1, y1 = box
            img = _array_image(shade_glass_block(src, shape, x0, y0, x1, y1))
            img.setDevicePixelRatio(dpr)
            blocks.append(("strip", img, QRectF(x0 / dpr, y0 / dpr, (x1 - x0) / dpr, (y1 - y0) / dpr), None))
        if interior is not None:
            box = clip(interior)
            if box is not None:
                x0, y0, x1, y1 = box
                blocks.append(
                    ("interior", None, QRectF(x0 / dpr, y0 / dpr, (x1 - x0) / dpr, (y1 - y0) / dpr),
                     QRectF(x0 / scale, y0 / scale, (x1 - x0) / scale, (y1 - y0) / scale))
                )
        return blocks

    def _build_tiled(self, src, shape: GlassShape, radius: float, band: int, dpr: float, scale: float) -> list:
        """Blocks for very round glass (the spreading circle): shade only the tiles on its rim.

        Tiles deep inside are plain refraction-free backdrop, merged into one run per tile row; tiles
        wholly outside the shape are skipped.
        """
        h_src, w_src = src.shape[:2]
        x_lo, y_lo = max(math.floor(shape.x), 0), max(math.floor(shape.y), 0)
        x_hi, y_hi = min(math.ceil(shape.x + shape.w), w_src), min(math.ceil(shape.y + shape.h), h_src)
        blocks: list = []

        def interior(x0: int, x1: int, y0: int, y1: int) -> None:
            blocks.append(
                ("interior", None, QRectF(x0 / dpr, y0 / dpr, (x1 - x0) / dpr, (y1 - y0) / dpr),
                 QRectF(x0 / scale, y0 / scale, (x1 - x0) / scale, (y1 - y0) / scale))
            )

        for y0 in range(y_lo, y_hi, TILE_PX):
            y1 = min(y0 + TILE_PX, y_hi)
            run = None
            for x0 in range(x_lo, x_hi, TILE_PX):
                x1 = min(x0 + TILE_PX, x_hi)
                half_diag = math.hypot(x1 - x0, y1 - y0) / 2.0
                sd = _sd_rounded_rect((x0 + x1) / 2.0, (y0 + y1) / 2.0, shape, radius)
                if sd < -(band + half_diag):
                    run = x0 if run is None else run
                    continue
                if run is not None:
                    interior(run, x0, y0, y1)
                    run = None
                if sd < half_diag + 1.0:
                    img = _array_image(shade_glass_block(src, shape, x0, y0, x1, y1))
                    img.setDevicePixelRatio(dpr)
                    blocks.append(("strip", img, QRectF(x0 / dpr, y0 / dpr, (x1 - x0) / dpr, (y1 - y0) / dpr), None))
            if run is not None:
                interior(run, x_hi, y0, y1)
        return blocks

    @staticmethod
    def _paint_fallback(p: QPainter, rect: QRectF, radius: float, angle: float, tints: Sequence[Tint]) -> None:
        """Without a backdrop (or numpy) the glass is a tinted pane with the same rim light."""
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        p.fillPath(path, QColor(24, 26, 34, 120))
        for r, g, b, a in tints:
            p.fillPath(path, QColor(r, g, b, a))
        a = math.radians(angle)
        c = rect.center()
        reach = max(rect.width(), rect.height()) / 2.0
        light = QLinearGradient(c - QPointF(math.cos(a), math.sin(a)) * reach, c + QPointF(math.cos(a), math.sin(a)) * reach)
        light.setColorAt(0.0, QColor(255, 255, 255, 128))
        light.setColorAt(0.5, QColor(255, 255, 255, 0))
        light.setColorAt(1.0, QColor(255, 255, 255, 128))
        p.setPen(QPen(light, 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


# ---------------------------------------------------------------- helpers


@dataclass
class _Result:
    success: bool
    title: str
    detail: str
    meta: str
    thumb: Optional[QPixmap]


def load_thumbnail(path: Optional[Path], edge: int = THUMB * 2) -> Optional[QPixmap]:
    """Return a preview for images, or the shell icon for other files."""
    if path is None or not str(path):
        return None
    if path.suffix.lower() in IMAGE_SUFFIXES and path.exists():
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and size.width() > 0 and size.height() > 0:
            scale = edge / max(1, min(size.width(), size.height()))
            if scale < 1:
                reader.setScaledSize(QSize(max(1, round(size.width() * scale)), max(1, round(size.height() * scale))))
        image = reader.read()
        if not image.isNull():
            return QPixmap.fromImage(image)
    if path.exists():
        pix = QFileIconProvider().icon(QFileInfo(str(path))).pixmap(64, 64)
        if not pix.isNull():
            return pix
    return None


def _exclude_from_capture(widget: QWidget) -> bool:
    """Keep the overlay out of screen grabs so we can sample what is underneath it (Win10 2004+)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(int(widget.winId()), WDA_EXCLUDEFROMCAPTURE))
    except Exception:  # noqa: BLE001 - purely cosmetic capability probe
        return False


_worker: Optional[ThreadPoolExecutor] = None


def _backdrop_worker() -> ThreadPoolExecutor:
    global _worker
    if _worker is None:
        _worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="glass-backdrop")
    return _worker


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def _paint_shadow(p: QPainter, rect: QRectF, radius: float, opacity: float = 1.0) -> None:
    """Shadow.Default (24dp, y+4dp, black 10%) as stacked soft rings; the glass covers the inside."""
    steps = 6
    for i in range(steps):
        grow = SHADOW_RADIUS * (i + 1) / steps
        r = rect.adjusted(-grow * 0.5, -grow * 0.5 + SHADOW_OFFSET, grow * 0.5, grow * 0.5 + SHADOW_OFFSET)
        path = QPainterPath()
        path.addRoundedRect(r, radius + grow * 0.5, radius + grow * 0.5)
        p.fillPath(path, QColor(0, 0, 0, int(255 * SHADOW_ALPHA * opacity * 2.2 / steps * (1 - i / steps))))


# ---------------------------------------------------------------- overlay window


class LiquidGlassOverlay(QWidget):
    """Click-through glass animation over the whole screen, driven by transfer events."""

    def __init__(self, clock: Optional[Callable[[], int]] = None) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setWindowTitle("FlickShare glass")

        self._elapsed = QElapsedTimer()
        self._elapsed.start()
        self._clock = clock or self._elapsed.elapsed

        self.phase = Phase.HIDDEN
        self.lift = Spring()  # drop: 0 = hidden past the screen edge, 1 = resting just inside it
        self.spread = Spring()  # 0 = small drop, 1 = circle covering the whole screen
        self.card = Spring()  # progress card appearance
        self.squash = Spring(damping=0.3, stiffness=320.0)  # jelly wobble on catch
        self.lens_pulse = Spring(damping=0.35, stiffness=180.0)  # refraction surge on catch
        self._springs = (self.lift, self.spread, self.card, self.squash, self.lens_pulse)

        self._phase_started = 0
        self._last_tick = 0
        self._sweep_started: Optional[int] = None
        self._last_backdrop = 0
        self._card_angle = HIGHLIGHT_ANGLE
        self._transfers: Dict[str, Tuple[int, int]] = {}
        self._client = ""
        self._shown_progress = 0.0
        self._result: Optional[_Result] = None
        self._pending: Optional[_Result] = None
        self._frame_key: Optional[tuple] = None
        self._capture_excluded = False
        self._work: Optional[QRectF] = None  # work area (screen minus taskbar) in local coords
        self._direction: Optional[float] = None  # where the throw comes from, see transfer_started
        self._refresh: Optional[Future] = None
        self.renderer = GlassRenderer()
        self._logo = QPixmap.fromImage(render_logo(96))
        # Loading the font engine costs ~200-400 ms on first use; pay it now, not mid-animation.
        self._title_font = QFont()
        self._title_font.setFamilies(UI_FONTS)
        self._title_font.setPointSizeF(17)
        self._title_font.setWeight(QFont.Weight.DemiBold)
        self._body_font = QFont()
        self._body_font.setFamilies(UI_FONTS)
        self._body_font.setPointSizeF(11.5)
        self._title_metrics = QFontMetrics(self._title_font)
        self._body_metrics = QFontMetrics(self._body_font)
        warm = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
        warm_painter = QPainter(warm)
        for font in (self._title_font, self._body_font):
            warm_painter.setFont(font)
            warm_painter.drawText(QRectF(0, 0, 8, 8), "Catching a throw…  0% · 9.5 MB")
        warm_painter.end()

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------ public API

    @property
    def is_active(self) -> bool:
        return self.phase is not Phase.HIDDEN

    def is_tracking(self, transfer_id: str) -> bool:
        return transfer_id in self._transfers

    def transfer_started(
        self, transfer_id: str, client: str, total_bytes: int, direction: Optional[float] = None
    ) -> None:
        """Start (or join) the glass for a throw.

        ``direction`` is the phone's throw angle: 0 = straight up, -90..90 = thrown further left/right.
        The glass enters mirrored (a left throw enters from the right edge); steep angles use a side edge.
        """
        self._transfers[transfer_id] = (0, max(0, int(total_bytes)))
        self._client = client or "phone"
        self._pending = None
        new_direction = None if direction is None else max(-90.0, min(90.0, float(direction)))
        if self.phase is Phase.HIDDEN or self.lift.value < 0.05:  # don't jump a drop that is already out
            self._direction = new_direction
        elif self.phase in (Phase.CAUGHT, Phase.DRAINING) and new_direction != self._direction:
            # The previous throw already landed, so aim the glass at this one. Unless the glass still covers
            # the screen, restart it from the new edge instead of sliding the old drop across.
            self._direction = new_direction
            if self.spread.value < 0.9:
                self.lift.snap(0.0)
                self.spread.snap(0.0)
        if self.phase in (Phase.HIDDEN, Phase.DRAINING, Phase.CAUGHT):
            self._result = None
            self._shown_progress = 0.0
            self._begin_rise()

    def transfer_progress(self, transfer_id: str, received: int, total: int) -> None:
        if transfer_id in self._transfers:
            known_total = self._transfers[transfer_id][1]
            self._transfers[transfer_id] = (max(0, int(received)), max(int(total), known_total))

    def transfer_finished(
        self,
        transfer_id: str,
        success: bool,
        title: str,
        detail: str = "",
        meta: str = "",
        thumb_path: Optional[Path] = None,
    ) -> bool:
        """Land (or drop) a tracked throw. Returns False if the overlay was not showing it."""
        if transfer_id not in self._transfers:
            return False
        del self._transfers[transfer_id]
        result = _Result(success, title, detail, meta, load_thumbnail(thumb_path) if success else None)
        if self._transfers:
            return True  # other throws still in flight; keep the glass up
        if self.phase is Phase.RISING:
            self._pending = result
        else:
            self._land(result)
        return True

    def dismiss(self) -> None:
        """Drain the glass now (e.g. the main window was opened)."""
        self._transfers.clear()
        self._pending = None
        if self.phase in (Phase.RISING, Phase.HOLDING, Phase.CAUGHT):
            self._begin_drain()

    def set_backdrop(self, pixmap: Optional[QPixmap]) -> None:
        """Use ``pixmap`` (the screen under the overlay) as the content the glass refracts."""
        self._refresh = None
        self.renderer.set_backdrop(pixmap)
        self._last_backdrop = self._now()

    # ------------------------------------------------------------------ state machine

    def _now(self) -> int:
        return int(self._clock())

    def _begin_rise(self) -> None:
        if self.phase is Phase.HIDDEN:
            self._place_on_active_screen()
            for spring in self._springs:
                spring.snap(0.0)
            self._capture_backdrop()
            self.show()
            self.raise_()
            if not self._capture_excluded:
                self._capture_excluded = _exclude_from_capture(self)
        self._set_phase(Phase.RISING)
        self.lift.animate_to(1.0, damping=0.62, stiffness=260.0)
        if self.spread.target < 1.0 and self.lift.value > 0.6:
            self.spread.animate_to(1.0, damping=0.7, stiffness=95.0)
        self._last_tick = self._now()
        self._timer.start(FRAME_MS)

    def _land(self, result: _Result) -> None:
        self._result = result
        self._set_phase(Phase.CAUGHT)
        if result.success:
            self.squash.kick(14.0)
            self.lens_pulse.kick(9.0)
            self._sweep_started = self._now()
        else:
            self.squash.kick(-8.0)
        self._timer.start(FRAME_MS)

    def _begin_drain(self) -> None:
        if self._capture_excluded:
            self._refresh_backdrop()
        self._set_phase(Phase.DRAINING)
        self.card.animate_to(0.0, damping=1.0, stiffness=500.0)
        self.spread.animate_to(0.0, damping=1.0, stiffness=150.0)
        self._timer.start(FRAME_MS)

    def _finish(self) -> None:
        self._timer.stop()
        self._set_phase(Phase.HIDDEN)
        for spring in self._springs:
            spring.snap(0.0)
        self._result = self._pending = None
        self._sweep_started = None
        self._transfers.clear()
        self._frame_key = None
        self._refresh = None
        self.hide()
        self.renderer.set_backdrop(None)

    def _set_phase(self, phase: Phase) -> None:
        self.phase = phase
        self._phase_started = self._now()

    def _tick(self) -> None:
        now = self._now()
        dt = (now - self._last_tick) / 1000.0
        self._last_tick = now
        for spring in self._springs:
            spring.step(dt)
        since = now - self._phase_started

        if self.phase is Phase.RISING:
            if self.spread.target < 1.0 and self.lift.value > 0.6:
                self.spread.animate_to(1.0, damping=0.7, stiffness=95.0)
            if self.spread.value > 0.7:
                self.card.animate_to(1.0, damping=0.55, stiffness=380.0)
            if self.spread.value > 0.97 and abs(self.spread.velocity) < 0.6:
                if self._pending is not None:
                    pending, self._pending = self._pending, None
                    self._land(pending)
                else:
                    self._set_phase(Phase.HOLDING)
        elif self.phase is Phase.CAUGHT:
            hold = CAUGHT_HOLD_MS if (self._result and self._result.success) else FAILED_HOLD_MS
            if since >= hold:
                self._begin_drain()
        elif self.phase is Phase.DRAINING:
            if self.spread.value < 0.35 and self.lift.target > 0.0:
                self.lift.animate_to(0.0, damping=1.0, stiffness=240.0)
            if self.lift.target == 0.0 and self.lift.value < 0.01:
                self._finish()
                return

        self._collect_refresh()
        if self.phase is Phase.HOLDING and self._capture_excluded and now - self._last_backdrop >= BACKDROP_REFRESH_MS:
            self._refresh_backdrop()  # keep what is under the glass live

        received, total = self._aggregate()
        target = 1.0 if self.phase is Phase.CAUGHT else (received / total if total else 0.0)
        self._shown_progress += (target - self._shown_progress) * 0.22
        if abs(target - self._shown_progress) < 0.002:
            self._shown_progress = target
        if self._sweep_started is not None and now - self._sweep_started >= SWEEP_MS:
            self._sweep_started = None
        self._card_angle = self._cursor_light_angle()

        waiting = self.phase in (Phase.RISING, Phase.HOLDING) and self.card.value > 0.01  # logo bobs
        moving = (
            waiting
            or any(not s.at_rest for s in self._springs)
            or self._sweep_started is not None
            or self._shown_progress != target
        )
        interval = FRAME_MS if moving else IDLE_FRAME_MS
        if self._timer.interval() != interval:
            self._timer.setInterval(interval)
        key = (
            self.phase, round(self._shown_progress, 3), received, total, round(self._card_angle),
            id(self.renderer.image), tuple(round(s.value, 4) for s in self._springs), self._sweep_started,
            round(self._bob(), 1) if waiting else 0.0,
        )
        if key != self._frame_key:
            self._frame_key = key
            self.update()

    def _aggregate(self) -> Tuple[int, int]:
        received = sum(r for r, _ in self._transfers.values())
        total = sum(t for _, t in self._transfers.values())
        return received, total

    def _sweep(self) -> float:
        if self._sweep_started is None:
            return 0.0
        return 360.0 * _ease_out_cubic(min(1.0, (self._now() - self._sweep_started) / SWEEP_MS))

    def _bob(self) -> float:
        return math.sin(self._now() / 1000.0 * 3.2) * 4.0

    def _cursor_light_angle(self) -> float:
        """Light the card rim from the mouse, like Liquid Glass reacting to device tilt."""
        if not self.isVisible():
            return HIGHLIGHT_ANGLE
        local = self.mapFromGlobal(QCursor.pos())
        center = self._card_base().center()
        dx, dy = local.x() - center.x(), local.y() - center.y()
        if math.hypot(dx, dy) < 40:
            return self._card_angle
        target = math.degrees(math.atan2(dy, dx))
        delta = (target - self._card_angle + 180.0) % 360.0 - 180.0
        return round((self._card_angle + delta * 0.25) / 3.0) * 3.0

    # ------------------------------------------------------------------ screen handling

    def _target_screen(self) -> QScreen:
        return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()

    def _place_on_active_screen(self) -> None:
        screen = self._target_screen()
        if screen is None:
            return
        if self.windowHandle() is not None:
            self.windowHandle().setScreen(screen)
        # Cover the whole screen (taskbar included); the drop rises from the work-area edge so it
        # appears to come out from behind the taskbar.
        geometry, work = screen.geometry(), screen.availableGeometry()
        self.setGeometry(geometry)
        self._work = QRectF(work.translated(-geometry.topLeft()))

    def _grab_screen(self) -> Optional[QPixmap]:
        screen = self.screen() if self.isVisible() else self._target_screen()
        if screen is None or QGuiApplication.platformName() in ("offscreen", "minimal"):
            return None
        try:
            size = screen.geometry().size()
            pixmap = screen.grabWindow(0, 0, 0, size.width(), size.height())
        except Exception as exc:  # noqa: BLE001 - glass still renders without a backdrop
            logger.debug("Backdrop capture failed: %s", exc)
            return None
        return None if pixmap.isNull() else pixmap

    def _capture_backdrop(self) -> None:
        """Grab and process synchronously (before the glass is on screen)."""
        pixmap = self._grab_screen()
        if pixmap is not None or not self.renderer.available:
            self.set_backdrop(pixmap)

    def _refresh_backdrop(self) -> None:
        """Re-grab while the glass is up; the ~70 ms processing runs off the UI thread."""
        self._last_backdrop = self._now()
        if self._refresh is not None or np is None:
            return
        pixmap = self._grab_screen()
        if pixmap is not None:
            dpr = pixmap.devicePixelRatio() or 1.0
            image = pixmap.toImage()
            self._refresh = _backdrop_worker().submit(lambda: (*GlassRenderer.prepare(image, dpr), dpr))

    def _collect_refresh(self) -> None:
        if self._refresh is None or not self._refresh.done():
            return
        future, self._refresh = self._refresh, None
        try:
            self.renderer.set_prepared(future.result())
        except Exception as exc:  # noqa: BLE001 - keep the previous backdrop
            logger.debug("Backdrop processing failed: %s", exc)

    # ------------------------------------------------------------------ geometry

    def _entry(self) -> Tuple[str, float]:
        """Edge the drop enters from ("bottom"/"left"/"right") and how far along it (-1..1 or 0..1)."""
        if self._direction is None:
            return "bottom", 0.0
        # Mirrored: in real use a throw to the left reads as the glass arriving on the right edge, and vice versa.
        a = -self._direction
        if abs(a) <= SIDE_ENTRY_DEG:
            return "bottom", a / SIDE_ENTRY_DEG
        return ("left" if a < 0 else "right"), (abs(a) - SIDE_ENTRY_DEG) / (90.0 - SIDE_ENTRY_DEG)

    def _drop(self, work: QRectF) -> Tuple[QPointF, str]:
        """Centre of the liquid drop: hidden just past the work-area edge at lift 0, resting inside it at 1."""
        m, r = PILL_MARGIN, DROP_RADIUS
        lift = self.lift.value
        edge, along = self._entry()
        if edge == "bottom":
            reach = max(0.0, work.width() / 2.0 - r - m)
            x = work.center().x() + along * reach
            return QPointF(x, _lerp(work.bottom() + r + 10.0, work.bottom() - m - r, lift)), edge
        y = _lerp(work.bottom() - m - SIDE_DROP_RISE, work.top() + work.height() * 0.3, along)
        if edge == "left":
            return QPointF(_lerp(work.left() - r - 10.0, work.left() + m + r, lift), y), edge
        return QPointF(_lerp(work.right() + r + 10.0, work.right() - m - r, lift), y), edge

    def _sheet_geometry(self) -> Optional[Tuple[QRectF, float, Tuple[float, float], QRectF]]:
        """Glass rect, corner radius, lens, and the clip that hides the drop behind the taskbar.

        The glass is a liquid drop that wells up at the throw's edge, then spreads as a circle from that
        point until it covers the whole screen (and shrinks back into the drop when it drains).
        """
        full = QRectF(self.rect())
        work = self._work.intersected(full) if self._work is not None else full
        if work.isEmpty():
            work = full
        spread = self.spread.value
        t = max(0.0, min(1.0, spread))
        centre, edge = self._drop(work)
        far = max(
            math.hypot(corner.x() - centre.x(), corner.y() - centre.y())
            for corner in (full.topLeft(), full.topRight(), full.bottomLeft(), full.bottomRight())
        ) + 2.0
        r = max(0.0, _lerp(DROP_RADIUS, far, spread))
        # Liquid stretch: the drop lengthens along its travel and thins across it, settling as it spreads.
        stretch = min(0.3, abs(self.lift.velocity) * 0.06) * max(0.0, 1.0 - spread * 3.0)
        along_r, across_r = r * (1.0 + stretch), r * (1.0 - stretch * 0.5)
        rx, ry = (across_r, along_r) if edge == "bottom" else (along_r, across_r)
        rect = QRectF(centre.x() - rx, centre.y() - ry, 2.0 * rx, 2.0 * ry)
        clip = QRectF()
        clip.setCoords(
            _lerp(work.left(), full.left(), t), _lerp(work.top(), full.top(), t),
            _lerp(work.right(), full.right(), t), _lerp(work.bottom(), full.bottom(), t),
        )
        if not rect.intersects(clip):
            return None
        radius = min(rx, ry)
        height_dp = _lerp(PILL_LENS[0], SHEET_LENS[0], t)
        amount = _lerp(PILL_LENS[1], SHEET_LENS[1], t) * (1.0 + 0.12 * self.lens_pulse.value)
        return rect, radius, (height_dp, amount), clip

    def _card_base(self) -> QRectF:
        w, h = float(self.width()), float(self.height())
        cw = min(float(CARD_SIZE.width()), w - 2 * PILL_MARGIN - 40.0)
        ch = float(CARD_SIZE.height())
        return QRectF((w - cw) / 2.0, h * 0.42 - ch / 2.0, cw, ch)

    def _card_transform(self) -> Tuple[float, float, float]:
        appear = self.card.value
        scale = 0.55 + 0.45 * appear
        squash = self.squash.value * 0.06
        return scale * (1.0 + squash), scale * (1.0 - squash), max(0.0, min(1.0, appear * 1.6))

    # ------------------------------------------------------------------ painting

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        if self.phase is Phase.HIDDEN:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        sweep = self._sweep()
        sheet = self._sheet_geometry()
        if sheet is not None:
            rect, radius, lens, clip = sheet
            p.save()
            if clip != QRectF(self.rect()):
                p.setClipRect(clip)
            if self.spread.value < 0.6:
                _paint_shadow(p, rect, radius, 1.0 - self.spread.value / 0.6)
            moving = not (self.lift.at_rest and self.spread.at_rest and self.lens_pulse.at_rest)
            self.renderer.paint(p, "sheet", rect, radius, lens, HIGHLIGHT_ANGLE + sweep, SHEET_TINTS, draft=moving)
            p.restore()
        self._paint_card(p, sweep)
        p.end()

    def _paint_card(self, p: QPainter, sweep: float) -> None:
        sx, sy, alpha = self._card_transform()
        if alpha < 0.01:
            return
        base = self._card_base()
        c = base.center()
        rect = QRectF(c.x() - base.width() * sx / 2.0, c.y() - base.height() * sy / 2.0, base.width() * sx, base.height() * sy)
        radius = CARD_RADIUS * min(sx, sy)
        p.save()
        p.setOpacity(alpha)
        _paint_shadow(p, rect, radius)
        moving = not (self.card.at_rest and self.squash.at_rest)
        self.renderer.paint(p, "card", rect, radius, CARD_LENS, self._card_angle + sweep, CARD_TINTS, draft=moving)
        p.translate(c)
        p.scale(sx, sy)
        p.translate(-c)
        self._paint_card_content(p, base)
        p.restore()

    def _paint_card_content(self, p: QPainter, card: QRectF) -> None:
        thumb_rect = QRectF(card.left() + 22, card.center().y() - THUMB / 2.0, THUMB, THUMB)
        self._paint_thumb(p, thumb_rect)

        text_left = thumb_rect.right() + 18
        text_w = card.right() - 30 - text_left
        result = self._result if self.phase in (Phase.CAUGHT, Phase.DRAINING) else None
        received, total = self._aggregate()
        if result is not None:
            title, detail, meta = result.title, result.detail, result.meta
        else:
            pct = int(round(self._shown_progress * 100))
            title = f"Catching a throw…  {pct}%" if total else "Catching a throw…"
            detail = f"from {self._client}"
            meta = f"{format_bytes(received)} of {format_bytes(total)}" if total else format_bytes(received)

        title_font, body_font = self._title_font, self._body_font
        fm, bfm = self._title_metrics, self._body_metrics
        top = card.top() + 24
        lines = [
            (title_font, fm, title, Qt.TextElideMode.ElideRight, QColor(255, 255, 255, 250)),
            (body_font, bfm, detail, Qt.TextElideMode.ElideMiddle, QColor(236, 238, 248, 230)),
            (body_font, bfm, meta, Qt.TextElideMode.ElideRight, QColor(206, 210, 226, 205)),
        ]
        for i, (font, metrics, text, elide, color) in enumerate(lines):
            p.setFont(font)
            line = QRectF(text_left, top, text_w, metrics.height())
            shown = metrics.elidedText(text, elide, int(text_w))
            p.setPen(QColor(0, 0, 0, 90))
            p.drawText(line.translated(0, 1), Qt.AlignmentFlag.AlignLeft, shown)
            p.setPen(color)
            p.drawText(line, Qt.AlignmentFlag.AlignLeft, shown)
            top += metrics.height() + (6 if i == 0 else 2)

        bar = QRectF(text_left, card.bottom() - 28, text_w, 6)
        track = QPainterPath()
        track.addRoundedRect(bar, 3, 3)
        p.fillPath(track, QColor(255, 255, 255, 46))
        failed = result is not None and not result.success
        fill_w = bar.width() * (1.0 if failed else max(0.0, min(1.0, self._shown_progress)))
        if fill_w > 1:
            fill = QPainterPath()
            fill.addRoundedRect(QRectF(bar.left(), bar.top(), fill_w, bar.height()), 3, 3)
            grad = QLinearGradient(bar.topLeft(), bar.topRight())
            if failed:
                grad.setColorAt(0.0, QColor(255, 107, 107))
                grad.setColorAt(1.0, QColor(255, 160, 122))
            else:
                grad.setColorAt(0.0, QColor(124, 92, 255))
                grad.setColorAt(1.0, QColor(34, 211, 238))
            p.fillPath(fill, grad)

    def _paint_thumb(self, p: QPainter, rect: QRectF) -> None:
        clip = QPainterPath()
        clip.addRoundedRect(rect, 28, 28)
        p.save()
        p.setClipPath(clip)
        p.fillRect(rect, QColor(255, 255, 255, 26))
        result = self._result if self.phase in (Phase.CAUGHT, Phase.DRAINING) else None
        thumb = result.thumb if result is not None else None
        if thumb is not None and not thumb.isNull():
            if max(thumb.width(), thumb.height()) <= 72:  # shell icon: centre it
                icon_rect = QRectF(0, 0, 56, 56)
                icon_rect.moveCenter(rect.center())
                p.drawPixmap(icon_rect, thumb, QRectF(thumb.rect()))
            else:  # photo: cover-crop
                src = QRectF(thumb.rect())
                side = min(src.width(), src.height())
                p.drawPixmap(rect, thumb, QRectF(src.center().x() - side / 2, src.center().y() - side / 2, side, side))
        else:
            logo_rect = QRectF(0, 0, 62, 62)
            logo_rect.moveCenter(rect.center() + QPointF(0, 0.0 if result is not None else self._bob()))
            p.drawPixmap(logo_rect, self._logo, QRectF(self._logo.rect()))
        p.restore()
        p.setPen(QPen(QColor(255, 255, 255, 70), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(clip)
