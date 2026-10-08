"""Erzeugt das App-Icon (SVG -> PNGs + Windows-ICO).

Aufruf:  QT_QPA_PLATFORM=offscreen python tools/make_icon.py
Schreibt: assets/icon.svg, assets/icon.ico, assets/icon-512.png,
          esxi_backupper/assets/icon.png, docs/icon.png, docs/favicon.png
"""

from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parent.parent
C = 256          # Mittelpunkt
R = 184          # Radius des Kreispfeils
STROKE = 26


def _pt(deg: float, r: float = R) -> tuple[float, float]:
    a = math.radians(deg)
    return C + r * math.cos(a), C + r * math.sin(a)


def _arc(start: float, end: float) -> str:
    x1, y1 = _pt(start)
    x2, y2 = _pt(end)
    large = 1 if (end - start) % 360 > 180 else 0
    return f"M{x1:.1f} {y1:.1f} A{R} {R} 0 {large} 1 {x2:.1f} {y2:.1f}"


def _arrowhead(deg: float) -> str:
    """Dreieck am Ende eines im Uhrzeigersinn laufenden Bogens."""
    px, py = _pt(deg)
    a = math.radians(deg)
    tx, ty = -math.sin(a), math.cos(a)        # Tangente (Uhrzeigersinn)
    nx, ny = math.cos(a), math.sin(a)         # Normale (nach außen)
    tip = (px + tx * 66, py + ty * 66)
    b1 = (px + nx * 40 - tx * 10, py + ny * 40 - ty * 10)
    b2 = (px - nx * 40 - tx * 10, py - ny * 40 - ty * 10)
    return "M{:.1f} {:.1f} L{:.1f} {:.1f} L{:.1f} {:.1f}Z".format(*tip, *b1, *b2)


def build_svg() -> str:
    arcs = [(204, 322), (24, 142)]
    arc_paths = "".join(
        f'<path d="{_arc(s, e)}" fill="none" stroke="url(#ring)" '
        f'stroke-width="{STROKE}" stroke-linecap="round"/>'
        f'<path d="{_arrowhead(e)}" fill="#34d399"/>'
        for s, e in arcs
    )
    bars = ""
    for i, y in enumerate((146, 214, 282)):
        led = "#34d399" if i != 1 else "#38bdf8"
        bars += (
            f'<rect x="152" y="{y}" width="208" height="56" rx="14" fill="url(#bar)"/>'
            f'<circle cx="182" cy="{y + 28}" r="9" fill="{led}"/>'
            f'<rect x="208" y="{y + 21}" width="80" height="14" rx="7" fill="#9db4e0" opacity=".55"/>'
            f'<rect x="300" y="{y + 21}" width="36" height="14" rx="7" fill="#9db4e0" opacity=".35"/>'
        )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
<defs>
<linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="#1f3170"/><stop offset="1" stop-color="#0b1230"/></linearGradient>
<linearGradient id="bar" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="#eaf2ff"/><stop offset="1" stop-color="#b7c9ee"/></linearGradient>
<linearGradient id="ring" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#34d399"/><stop offset="1" stop-color="#38bdf8"/></linearGradient>
<radialGradient id="glow" cx=".5" cy=".42" r=".6">
<stop offset="0" stop-color="#3b82f6" stop-opacity=".45"/><stop offset="1" stop-color="#3b82f6" stop-opacity="0"/></radialGradient>
</defs>
<rect width="512" height="512" rx="112" fill="url(#bg)"/>
<rect width="512" height="512" rx="112" fill="url(#glow)"/>
{arc_paths}
{bars}
</svg>
'''


def render(svg: bytes, size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    QSvgRenderer(QByteArray(svg)).render(p)
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def write_ico(path: Path, svg: bytes, sizes=(16, 24, 32, 48, 64, 128, 256)) -> None:
    images = [(s, png_bytes(render(svg, s))) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for s, data in images:
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32,
                               len(data), offset + len(blobs))
        blobs += data
    path.write_bytes(header + entries + blobs)


def main() -> None:
    QGuiApplication(sys.argv)
    svg_text = build_svg()
    svg = svg_text.encode()
    for d in ("assets", "esxi_backupper/assets", "docs"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    (ROOT / "assets/icon.svg").write_text(svg_text, encoding="utf-8")
    write_ico(ROOT / "assets/icon.ico", svg)
    render(svg, 512).save(str(ROOT / "assets/icon-512.png"))
    render(svg, 256).save(str(ROOT / "esxi_backupper/assets/icon.png"))
    render(svg, 512).save(str(ROOT / "docs/icon.png"))
    render(svg, 64).save(str(ROOT / "docs/favicon.png"))
    print("Icons geschrieben.")


if __name__ == "__main__":
    main()
