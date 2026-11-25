"""PDF stickers of the machine passport: a QR code with the link, the machine, the site and the code.

One sticker is 90 x 50 mm; the sheet of an area is A4 with ten stickers (2 x 5). The QR code holds only
the link https://<host>/m/<code>: no token. The text is set in DejaVu Sans (a Latin and Cyrillic subset
shipped in ``fonts/``, see its license), so the PDF does not depend on fonts of the server.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from app.admin.passport import passport_url

FONTS_DIR = Path(__file__).parent / "fonts"
SANS, BOLD, MONO = "PassportSans", "PassportSans-Bold", "PassportMono-Bold"
STICKER_W, STICKER_H = 90 * mm, 50 * mm
QR_SIZE = 42 * mm
COLUMNS, ROWS = 2, 5


def _register_fonts() -> None:
    for name, file in ((SANS, "DejaVuSans.ttf"), (BOLD, "DejaVuSans-Bold.ttf"), (MONO, "DejaVuSansMono-Bold.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(FONTS_DIR / file)))


def _lines(text: str, font: str, size: float, width: float, limit: int) -> list[str]:
    """Words wrapped to ``width``, at most ``limit`` lines; the last one ends with … if cut."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if pdfmetrics.stringWidth(candidate, font, size) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    if len(lines) > limit:
        lines = lines[:limit]
        lines[-1] += " …"
    return [_fit(line, font, size, width) for line in lines]


def _fit(line: str, font: str, size: float, width: float) -> str:
    while line and pdfmetrics.stringWidth(line, font, size) > width:
        line = line[:-2] + "…"
    return line


def _qr(canvas: Canvas, url: str, x: float, y: float, size: float) -> None:
    widget = QrCodeWidget(url, barLevel="M")
    x0, y0, x1, y1 = widget.getBounds()
    drawing = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    drawing.add(widget)
    renderPDF.draw(drawing, canvas, x, y)
    canvas.linkURL(url, (x, y, x + size, y + size), relative=0)


def _sticker(canvas: Canvas, x: float, y: float, row: dict[str, Any], url: str) -> None:
    canvas.setStrokeGray(0.75)
    canvas.setLineWidth(0.3)
    canvas.rect(x, y, STICKER_W, STICKER_H)  # cutting line
    _qr(canvas, url, x + 3 * mm, y + (STICKER_H - QR_SIZE) / 2, QR_SIZE)
    left, width = x + 3 * mm + QR_SIZE + 2 * mm, STICKER_W - QR_SIZE - 8 * mm
    top = y + STICKER_H - 6 * mm
    canvas.setFillGray(0)
    for size in (12, 10, 9):
        name_lines = _lines(row["name"], BOLD, size, width, 3)
        if len(name_lines) <= 2:
            break
    canvas.setFont(BOLD, size)
    for line in name_lines:
        canvas.drawString(left, top, line)
        top -= size * 1.15
    place = " · ".join(part for part in (row["site_name"], row["area_name"]) if part)
    canvas.setFont(SANS, 7)
    for line in _lines(place, SANS, 7, width, 2):
        top -= 1.5
        canvas.drawString(left, top, line)
        top -= 7 * 1.1
    canvas.setFont(MONO, 15)
    canvas.drawString(left, y + 9 * mm, row["passport_code"])
    canvas.setFont(SANS, 5.5)
    canvas.setFillGray(0.35)
    canvas.drawString(left, y + 5 * mm, _fit(url, SANS, 5.5, width))


def _document(title: str, subject: str, pagesize: tuple[float, float]) -> tuple[io.BytesIO, Canvas]:
    _register_fonts()
    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=pagesize, pageCompression=1)
    canvas.setTitle(title)
    canvas.setSubject(subject)
    canvas.setCreator("Мониторинг станков")
    return buffer, canvas


def single(row: dict[str, Any], base_url: str) -> bytes:
    """One sticker on a page of its own size (for a label printer)."""
    url = passport_url(base_url, row["passport_code"])
    buffer, canvas = _document(f"QR-паспорт: {row['name']}", url, (STICKER_W, STICKER_H))
    _sticker(canvas, 0, 0, row, url)
    canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def sheet(rows: Sequence[dict[str, Any]], base_url: str, title: str) -> bytes:
    """Stickers of many machines on A4 pages, ten per page."""
    page_w, page_h = A4
    gap_x = (page_w - COLUMNS * STICKER_W) / (COLUMNS + 1)
    gap_y = (page_h - ROWS * STICKER_H) / (ROWS + 1)
    urls = [passport_url(base_url, row["passport_code"]) for row in rows]
    buffer, canvas = _document(title, " ".join(urls), A4)
    per_page = COLUMNS * ROWS
    for start in range(0, max(len(rows), 1), per_page):
        for i, row in enumerate(rows[start : start + per_page]):
            column, line = i % COLUMNS, i // COLUMNS
            x = gap_x + column * (STICKER_W + gap_x)
            y = page_h - (line + 1) * (STICKER_H + gap_y)
            _sticker(canvas, x, y, row, urls[start + i])
        canvas.showPage()
    canvas.save()
    return buffer.getvalue()
