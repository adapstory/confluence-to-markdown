#!/usr/bin/env python3
"""Build a deterministic, sanitized Confluence-like HTML export fixture.

The builder intentionally uses only the Python standard library.  It can write
into an otherwise populated export directory because every generated path is
namespaced.  Existing paths are never overwritten by default.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import struct
import sys
import zipfile
import zlib
from collections.abc import Iterable
from email.message import EmailMessage
from pathlib import Path, PurePosixPath
from textwrap import dedent

FIXTURE_FORMAT = "confluence-synthetic-export-fixture/v1"
FIXTURE_NAMESPACE = "codex-conversion-fixture"
FIXTURE_PAGE_ID = "999999999999"
FIXTURE_VERSION = "1"
MANIFEST_PATH = "_codex_conversion_fixture_manifest.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "full-export"


# Stable high-contrast OCR sample, embedded so fixture generation stays stdlib-only.
_BILINGUAL_PNG_BASE64 = (
    "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"
    "NWrVomm9OjVVvm7hZ56/iFJTr0TOGyHdRw05dDg04l5k1l4+3Xt80SKFuNEABJ64Q7UCA3xEN1iMuBe57rdt6qtD5KOnojEOpUpw"
    "s7LRkoUBohuuS/fi5as167d07DbAuWK9CVNmXrt+mxaGVjEyMlwaNCtrTyvq1Ko+qH8v2hAAiTtUr1jRwp5jh4qGzZm/9P37D2qq"
    "w7t34kdvFsifn5slZOtM91+Xz9PT01PtZeMTPm7YtN21bff6TX7Zsm1PSmoqTQ0tUbliuVHDB2T2VSYmJkuDZqm8pwAAiTv+v7Gj"
    "BtuXLKY45vPnL75+89RUgT/PDxdETh/My53KXm1aNlsQOF1NGcn9B4/GjvepVtv1wKFjNDW0hNfEMaXsS2TqJVMnjS1lX5ymAyAw"
    "xx1qYmJsPG+2b5eegxWH7dp7yK1318xuMCIpcU8UT9yzcGBqx24DpByCKEX5cs5nTxzgrTKgbw9TU1PPCdPU9Gj8zdvogcM8t2zf"
    "u2rZXNt8NjQ4snlsNDFZGhTQtqObxD2RqlQqP3JYf9oNgMATd6hVE5f67dq4ioZN9Jr540eaaov+/v27lGuamBhzm7RBz24djh7c"
    "6uhgr74iTp0+39i1860792htZLsa1apIX2baoH5tjh4DQOIOTZjt52VubqY4JvLhk1/XblJtuRLP6ElPT+ceaYmqVSqePXlguvd4"
    "a2srNRXxNjqmfZd+t+/ep7WRvRYtWx316o3E4FVrNt67/5BGA0DiDrUrXMhuoqf41sVzFy6PjnmnwnItzC0k5ffJnMEpaNX0KvfR"
    "Q+5eC/edOq6QXUF1FPHly9cuPQa9jHpNayO73H/waOGiVdLjU1N/jPbwUvnXkgBA4o7/MHLYgNKOpRTHJCYmeU8PVGGhhoYGJiYm"
    "goSjfLhB2sbKytJjzNDbV09u3biyXRtXKfcxU+LiE4aNnpiWRhqEbJCWljbGc2pml3Pcvnt/6Yp1tB4AEncIGti6eP4cX9GwA4eO"
    "nTl7UZX5n6X4Q/fYD3HcIO1kYGDQqkWTTeuWPo64uP7XoC6d2trkzaOqi/9x9ebqdZtpZGje0hXrb96OyMIL5wetePT4KQ0IQGBX"
    "GbmQ7yYkDerV6tShzb4DRxSHTZzqfz78kLGRkUoKzZ0714e4eMUx72M/0GG0nKWlRcf2rTu2b52RkXEn4sHpMxfOnL90+cqN79+V"
    "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"
    "YII="
)


FONT: dict[str, tuple[str, ...]] = {
    "?": ("01110", "10001", "00010", "00100", "00100", "00000", "00100"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ">": ("10000", "01000", "00100", "00010", "00100", "01000", "10000"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Е": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "И": ("10001", "10011", "10101", "10101", "11001", "10001", "10001"),
    "Й": ("01010", "00100", "10001", "10011", "10101", "11001", "10001"),
    "К": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "Р": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "С": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "Т": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "У": ("10001", "10001", "01010", "00100", "00100", "01000", "10000"),
}


def _text(value: str) -> bytes:
    return dedent(value).strip().encode("utf-8") + b"\n"


def _new_canvas(width: int, height: int, color: tuple[int, int, int]) -> bytearray:
    return bytearray(color * (width * height))


def _set_pixel(
    pixels: bytearray,
    width: int,
    height: int,
    x: int,
    y: int,
    color: tuple[int, int, int],
) -> None:
    if 0 <= x < width and 0 <= y < height:
        offset = (y * width + x) * 3
        pixels[offset : offset + 3] = bytes(color)


def _fill_rect(
    pixels: bytearray,
    width: int,
    height: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    color: tuple[int, int, int],
) -> None:
    for y in range(y0, y1):
        for x in range(x0, x1):
            _set_pixel(pixels, width, height, x, y, color)


def _draw_line(
    pixels: bytearray,
    width: int,
    height: int,
    start: tuple[int, int],
    end: tuple[int, int],
    color: tuple[int, int, int],
    thickness: int = 1,
) -> None:
    x0, y0 = start
    x1, y1 = end
    dx = abs(x1 - x0)
    sx = 1 if x0 < x1 else -1
    dy = -abs(y1 - y0)
    sy = 1 if y0 < y1 else -1
    error = dx + dy
    while True:
        radius = thickness // 2
        _fill_rect(pixels, width, height, x0 - radius, y0 - radius, x0 + radius + 1, y0 + radius + 1, color)
        if x0 == x1 and y0 == y1:
            break
        doubled = 2 * error
        if doubled >= dy:
            error += dy
            x0 += sx
        if doubled <= dx:
            error += dx
            y0 += sy


def _draw_text(
    pixels: bytearray,
    width: int,
    height: int,
    text: str,
    x: int,
    y: int,
    scale: int,
    color: tuple[int, int, int],
) -> None:
    cursor = x
    for character in text:
        if character == " ":
            cursor += 4 * scale
            continue
        glyph = FONT.get(character, FONT["?"])
        for row, bits in enumerate(glyph):
            for column, bit in enumerate(bits):
                if bit == "1":
                    _fill_rect(
                        pixels,
                        width,
                        height,
                        cursor + column * scale,
                        y + row * scale,
                        cursor + (column + 1) * scale,
                        y + (row + 1) * scale,
                        color,
                    )
        cursor += 6 * scale


def _render_text_bitmap(lines: Iterable[str], scale: int = 8) -> tuple[int, int, bytearray]:
    materialized = tuple(lines)
    margin = 4 * scale
    width = max(sum(4 if char == " " else 6 for char in line) for line in materialized) * scale + 2 * margin
    height = len(materialized) * 9 * scale + 2 * margin
    pixels = _new_canvas(width, height, (255, 255, 255))
    _fill_rect(pixels, width, height, 0, 0, width, scale, (37, 99, 235))
    _fill_rect(pixels, width, height, 0, height - scale, width, height, (37, 99, 235))
    for index, line in enumerate(materialized):
        _draw_text(pixels, width, height, line, margin, margin + index * 9 * scale, scale, (17, 24, 39))
    return width, height, pixels


def _diagram_bitmap() -> tuple[int, int, bytearray]:
    width, height = 640, 240
    pixels = _new_canvas(width, height, (255, 255, 255))
    blue = (37, 99, 235)
    ink = (17, 24, 39)
    for x0 in (70, 410):
        _fill_rect(pixels, width, height, x0, 65, x0 + 160, 175, (219, 234, 254))
        for offset in range(4):
            _draw_line(pixels, width, height, (x0, 65 + offset), (x0 + 160, 65 + offset), blue)
            _draw_line(pixels, width, height, (x0, 171 + offset), (x0 + 160, 171 + offset), blue)
            _draw_line(pixels, width, height, (x0 + offset, 65), (x0 + offset, 175), blue)
            _draw_line(pixels, width, height, (x0 + 156 + offset, 65), (x0 + 156 + offset, 175), blue)
    _draw_text(pixels, width, height, "A", 125, 85, 8, ink)
    _draw_text(pixels, width, height, "B", 465, 85, 8, ink)
    _draw_line(pixels, width, height, (230, 120), (410, 120), ink, 5)
    _draw_line(pixels, width, height, (380, 95), (410, 120), ink, 5)
    _draw_line(pixels, width, height, (380, 145), (410, 120), ink, 5)
    return width, height, pixels


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(kind)
    checksum = zlib.crc32(payload, checksum)
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum & 0xFFFFFFFF)


def _make_png(
    width: int,
    height: int,
    pixels: bytes | bytearray,
    description: str,
) -> bytes:
    rows = bytearray()
    stride = width * 3
    for row in range(height):
        rows.append(0)
        rows.extend(pixels[row * stride : (row + 1) * stride])
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    international_text = b"Description\x00\x00\x00\x00\x00" + description.encode("utf-8")
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", header),
            _png_chunk(b"iTXt", international_text),
            _png_chunk(b"IDAT", zlib.compress(bytes(rows), level=9)),
            _png_chunk(b"IEND", b""),
        )
    )


def _png_rgb_stream(payload: bytes) -> tuple[int, int, bytes]:
    """Return RGB dimensions and the original PNG-filtered Flate stream."""
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("embedded OCR raster is not a PNG")
    width = height = 0
    image_data: list[bytes] = []
    offset = 8
    while offset < len(payload):
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        kind = payload[offset + 4 : offset + 8]
        data = payload[offset + 8 : offset + 8 + length]
        if kind == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", data)
            if (bit_depth, color_type, interlace) != (8, 2, 0):
                raise ValueError("embedded OCR raster must be non-interlaced 8-bit RGB")
        elif kind == b"IDAT":
            image_data.append(data)
        elif kind == b"IEND":
            break
        offset += 12 + length
    if width <= 0 or height <= 0 or not image_data:
        raise ValueError("embedded OCR raster has incomplete PNG structure")
    return width, height, b"".join(image_data)


def _pdf(objects: Iterable[bytes]) -> bytes:
    materialized = tuple(objects)
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, body in enumerate(materialized, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(materialized) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(materialized) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def _text_pdf() -> bytes:
    stream = (
        b"BT\n/F1 20 Tf\n72 730 Td\n(Synthetic text PDF) Tj\n"
        b"0 -32 Td\n/F1 12 Tf\n(Requirement status: approved.) Tj\nET\n"
    )
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    )
    return _pdf(objects)


def _scanned_pdf() -> bytes:
    width, height, compressed = _png_rgb_stream(base64.b64decode(_BILINGUAL_PNG_BASE64))
    target_width = 500
    target_height = round(target_width * height / width)
    stream = f"q\n{target_width} 0 0 {target_height} 56 500 cm\n/Im0 Do\nQ\n".encode("ascii")
    image = (
        f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
        f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
        f"/DecodeParms << /Predictor 15 /Colors 3 /BitsPerComponent 8 /Columns {width} >> "
        f"/Length {len(compressed)} >>\n"
        "stream\n"
    ).encode("ascii") + compressed + b"\nendstream"
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>"
        ),
        image,
        f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"endstream",
    )
    return _pdf(objects)


def _zip_package(parts: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(parts):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, parts[name])
    return buffer.getvalue()


def _docx() -> bytes:
    return _zip_package(
        {
            "[Content_Types].xml": _text(
                """
                <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
                <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
                  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
                  <Default Extension="xml" ContentType="application/xml"/>
                  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
                </Types>
                """
            ),
            "_rels/.rels": _text(
                """
                <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
                </Relationships>
                """
            ),
            "word/document.xml": _text(
                """
                <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
                <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                  <w:body>
                    <w:p><w:r><w:t>Synthetic DOCX requirement</w:t></w:r></w:p>
                    <w:p><w:r><w:t>Русский текст документа</w:t></w:r></w:p>
                    <w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>
                  </w:body>
                </w:document>
                """
            ),
        }
    )


def _pptx() -> bytes:
    return _zip_package(
        {
            "[Content_Types].xml": _text(
                """
                <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
                <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
                  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
                  <Default Extension="xml" ContentType="application/xml"/>
                  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
                  <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
                </Types>
                """
            ),
            "_rels/.rels": _text(
                """
                <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
                </Relationships>
                """
            ),
            "ppt/presentation.xml": _text(
                """
                <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
                <p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
                  <p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>
                  <p:sldSz cx="9144000" cy="6858000" type="screen4x3"/>
                  <p:notesSz cx="6858000" cy="9144000"/>
                </p:presentation>
                """
            ),
            "ppt/_rels/presentation.xml.rels": _text(
                """
                <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
                </Relationships>
                """
            ),
            "ppt/slides/slide1.xml": _text(
                """
                <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
                <p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
                  <p:cSld><p:spTree>
                    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
                    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
                    <p:sp>
                      <p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
                      <p:spPr/>
                      <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Synthetic PPTX briefing</a:t></a:r></a:p><a:p><a:r><a:t>Русский текст слайда</a:t></a:r></a:p></p:txBody>
                    </p:sp>
                  </p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
                </p:sld>
                """
            ),
        }
    )


def _xlsx() -> bytes:
    return _zip_package(
        {
            "[Content_Types].xml": _text(
                """
                <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
                <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
                  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
                  <Default Extension="xml" ContentType="application/xml"/>
                  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
                  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
                </Types>
                """
            ),
            "_rels/.rels": _text(
                """
                <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
                </Relationships>
                """
            ),
            "xl/workbook.xml": _text(
                """
                <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
                <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
                  <sheets><sheet name="Metrics" sheetId="1" r:id="rId1"/></sheets>
                </workbook>
                """
            ),
            "xl/_rels/workbook.xml.rels": _text(
                """
                <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
                </Relationships>
                """
            ),
            "xl/worksheets/sheet1.xml": _text(
                """
                <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
                <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                  <sheetData>
                    <row r="1"><c r="A1" t="inlineStr"><is><t>Metric</t></is></c><c r="B1" t="inlineStr"><is><t>Value</t></is></c></row>
                    <row r="2"><c r="A2" t="inlineStr"><is><t>Coverage</t></is></c><c r="B2"><v>95</v></c></row>
                    <row r="3"><c r="A3" t="inlineStr"><is><t>Комментарий</t></is></c><c r="B3" t="inlineStr"><is><t>Проверено</t></is></c></row>
                  </sheetData>
                </worksheet>
                """
            ),
        }
    )


def _drawio() -> bytes:
    return _text(
        """
        <?xml version="1.0" encoding="UTF-8"?>
        <mxfile host="app.diagrams.net" modified="2026-01-01T00:00:00.000Z" agent="synthetic-fixture" version="24.7.17" compressed="false">
          <diagram id="fixture-flow" name="Page-1">
            <mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" page="1" pageScale="1" pageWidth="827" pageHeight="1169">
              <root>
                <mxCell id="0"/>
                <mxCell id="1" parent="0"/>
                <mxCell id="node-a" value="Start" style="rounded=1;whiteSpace=wrap;html=1;" vertex="1" parent="1"><mxGeometry x="80" y="120" width="160" height="80" as="geometry"/></mxCell>
                <mxCell id="node-b" value="Finish" style="rounded=1;whiteSpace=wrap;html=1;" vertex="1" parent="1"><mxGeometry x="400" y="120" width="160" height="80" as="geometry"/></mxCell>
                <mxCell id="edge-a-b" value="approved path" style="edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;" edge="1" parent="1" source="node-a" target="node-b"><mxGeometry relative="1" as="geometry"/></mxCell>
              </root>
            </mxGraphModel>
          </diagram>
        </mxfile>
        """
    )


def _eml() -> bytes:
    message = EmailMessage()
    message["Subject"] = "Synthetic fixture message"
    message["From"] = "sender@example.test"
    message["To"] = "recipient@example.test"
    message["Date"] = "Thu, 01 Jan 2026 00:00:00 +0000"
    message["Message-ID"] = "<confluence-fixture@example.test>"
    message.set_content(
        "Synthetic EML body in English.\n"
        "Синтетическое тело письма на русском."
    )
    message.add_attachment(
        b"name,value\nembedded,7\n",
        maintype="text",
        subtype="csv",
        filename="embedded-metrics.csv",
    )
    message.set_boundary("===============confluence-fixture==")
    return message.as_bytes()


def _root_page() -> bytes:
    attachment_root = f"attachments/{FIXTURE_PAGE_ID}/{FIXTURE_VERSION}"
    return _text(
        f"""
        <!doctype html>
        <html lang="ru">
        <head>
          <meta charset="utf-8"/>
          <title>Codex conversion fixture</title>
          <link rel="stylesheet" href="_codex_conversion_fixture_assets/theme.css"/>
          <script src="_codex_conversion_fixture_assets/theme.js"></script>
        </head>
        <body>
          <aside id="ht-sidebar"><a href="_codex_conversion_fixture_toc.html">Template navigation to ignore</a></aside>
          <iframe id="ht-nav" src="_codex_conversion_fixture_toc.html?pageId={FIXTURE_PAGE_ID}"></iframe>
          <article id="ht-content">
            <header class="ht-content-header"><h1 id="src-{FIXTURE_PAGE_ID}">Codex conversion fixture</h1></header>
            <div id="main-content" class="wiki-content sp-grid-section">
              <p>Sanitized English paragraph. Обезличенный русский абзац.</p>
              <ul class="toc-indentation">
                <li><a href="#src-{FIXTURE_PAGE_ID}-details">Details</a></li>
                <li><a href="#src-{FIXTURE_PAGE_ID}-attachments">Attachments</a></li>
              </ul>

              <h2 id="src-{FIXTURE_PAGE_ID}-details">Details</h2>
              <p>Follow the <a href="_codex_conversion_fixture_child.html#src-{FIXTURE_PAGE_ID}-child-anchor">child anchor</a> and keep the <a href="https://example.com/reference">external reference</a>.</p>

              <div class="scroll-expand-container">
                <div class="scroll-expand-control">Expanded acceptance notes</div>
                <div class="scroll-expand-content"><p>Hidden-by-default meaning must remain.</p><ul><li>First condition</li><li>Second condition</li></ul></div>
              </div>

              <div class="rwui_tabs">
                <ul class="rwui_tabs_menu">
                  <li><a href="#fixture-tab-overview">Overview tab</a></li>
                  <li><a href="#fixture-tab-risks">Risks tab</a></li>
                </ul>
                <div class="rwui_tab_content" id="fixture-tab-overview"><h3>Overview tab</h3><p>Overview content is meaningful.</p></div>
                <div class="rwui_tab_content" id="fixture-tab-risks"><h3>Risks tab</h3><p>Risk content is meaningful.</p></div>
              </div>

              <div class="confbox admonition admonition-info"><div class="admonition-body"><p>Information panel content.</p></div></div>
              <div class="confbox panel"><div class="panel-content"><p>Generic panel content.</p></div></div>

              <div class="tablewrap">
                <table class="confluenceTable">
                  <thead><tr><th class="confluenceTh" rowspan="2">Area</th><th class="confluenceTh" colspan="2">Checks</th></tr><tr><th class="confluenceTh">Primary</th><th class="confluenceTh">Secondary</th></tr></thead>
                  <tbody><tr><td class="confluenceTd" rowspan="2">Export</td><td class="confluenceTd">HTML</td><td class="confluenceTd">Links</td></tr><tr><td class="confluenceTd" colspan="2">Resources preserved</td></tr></tbody>
                </table>
              </div>

              <h2 id="src-{FIXTURE_PAGE_ID}-images">Images and diagrams</h2>
              <figure><img src="images/inline/codex-conversion-fixture-bilingual.png" alt="Visible English and Russian text"/><figcaption>Ordinary screenshot for OCR.</figcaption></figure>
              <div class="drawio-diagram">
                <img class="drawio-diagram-image drawio-image-border" src="images/inline/codex-conversion-fixture-flow.png" alt="Start to Finish flow" data-linked-resource-default-alias="codex-conversion-fixture-flow.drawio"/>
                <a class="confluence-embedded-file" href="{attachment_root}/codex-conversion-fixture-flow.drawio">Draw.io source</a>
              </div>

              <h2 id="src-{FIXTURE_PAGE_ID}-attachments">Attachments</h2>
              <ul>
                <li><a class="confluence-embedded-file" href="{attachment_root}/codex-conversion-fixture-text.pdf">Text PDF</a></li>
                <li><a class="confluence-embedded-file" href="{attachment_root}/codex-conversion-fixture-scan.pdf">Scanned PDF</a></li>
                <li><a class="confluence-embedded-file" href="{attachment_root}/codex-conversion-fixture-requirements.docx">DOCX requirements</a></li>
                <li><a class="confluence-embedded-file" href="{attachment_root}/codex-conversion-fixture-briefing.pptx">PPTX briefing</a></li>
                <li><a class="confluence-embedded-file" href="{attachment_root}/codex-conversion-fixture-metrics.xlsx">XLSX metrics</a></li>
                <li><a class="confluence-embedded-file" href="{attachment_root}/codex-conversion-fixture-notes.txt">Plain text notes</a></li>
                <li><a class="confluence-embedded-file" href="{attachment_root}/codex-conversion-fixture-guide.md">Markdown guide</a></li>
                <li><a class="confluence-embedded-file" href="{attachment_root}/codex-conversion-fixture-data.csv">CSV data</a></li>
                <li><a class="confluence-embedded-file" href="{attachment_root}/codex-conversion-fixture-data.json">JSON data</a></li>
                <li><a class="confluence-embedded-file" href="{attachment_root}/codex-conversion-fixture-config.yaml">YAML configuration</a></li>
                <li><a class="confluence-embedded-file" href="{attachment_root}/codex-conversion-fixture-message.eml">EML message</a></li>
                <li><a class="confluence-embedded-file" href="{attachment_root}/codex-conversion-fixture-fragment.html">HTML fragment</a></li>
                <li><a class="confluence-embedded-file" href="{attachment_root}/codex-conversion-fixture-vector.svg">SVG diagram</a></li>
              </ul>

              <div class="childpages-macro">
                <h2>Child pages</h2>
                <ul><li><a href="_codex_conversion_fixture_child.html">Codex conversion fixture child</a></li></ul>
              </div>
            </div>
          </article>
          <nav id="ht-post-nav"><a href="_codex_conversion_fixture_child.html">Template next link to ignore</a></nav>
          <footer id="ht-footer">Template footer to ignore</footer>
        </body>
        </html>
        """
    )


def _child_page() -> bytes:
    return _text(
        f"""
        <!doctype html>
        <html lang="en">
        <head><meta charset="utf-8"/><title>Codex conversion fixture child</title></head>
        <body>
          <aside id="ht-sidebar">Template navigation to ignore</aside>
          <article id="ht-content">
            <header class="ht-content-header"><h1 id="src-{FIXTURE_PAGE_ID}-child">Codex conversion fixture child</h1></header>
            <div id="main-content" class="wiki-content sp-grid-section">
              <h2 id="src-{FIXTURE_PAGE_ID}-child-anchor">Child anchor</h2>
              <p>This target proves cross-page anchor rewriting.</p>
              <p><a href="_codex_conversion_fixture.html#src-{FIXTURE_PAGE_ID}-details">Return to details</a>.</p>
            </div>
          </article>
          <footer id="ht-footer">Template footer to ignore</footer>
        </body>
        </html>
        """
    )


def _navigation_page() -> bytes:
    return _text(
        """
        <!doctype html>
        <html lang="en"><head><meta charset="utf-8"/><title>Fixture navigation</title></head>
        <body><ul class="nav ht-pages-nav-sub"><li><a class="ht-nav-page-link" data-destpageid="999999999999" href="_codex_conversion_fixture.html">Codex conversion fixture</a><ul><li><a class="ht-nav-page-link" data-destpageid="999999999999-child" href="_codex_conversion_fixture_child.html">Child</a></li></ul></li></ul></body></html>
        """
    )


def _payload_artifacts() -> dict[str, bytes]:
    attachment_root = f"attachments/{FIXTURE_PAGE_ID}/{FIXTURE_VERSION}"
    diagram_width, diagram_height, diagram_pixels = _diagram_bitmap()
    return {
        "_codex_conversion_fixture.html": _root_page(),
        "_codex_conversion_fixture_child.html": _child_page(),
        "_codex_conversion_fixture_toc.html": _navigation_page(),
        "_codex_conversion_fixture_assets/theme.css": _text(
            """
            /* Synthetic WebHelp chrome. Content conversion must ignore this asset. */
            #ht-sidebar, #ht-footer, #ht-post-nav { color: #667085; }
            .rwui_tab_content { display: block; }
            """
        ),
        "_codex_conversion_fixture_assets/theme.js": _text(
            """
            // Synthetic inert theme code. A converter must never execute exported JavaScript.
            window.syntheticConfluenceFixture = true;
            """
        ),
        f"{attachment_root}/codex-conversion-fixture-text.pdf": _text_pdf(),
        f"{attachment_root}/codex-conversion-fixture-scan.pdf": _scanned_pdf(),
        f"{attachment_root}/codex-conversion-fixture-requirements.docx": _docx(),
        f"{attachment_root}/codex-conversion-fixture-briefing.pptx": _pptx(),
        f"{attachment_root}/codex-conversion-fixture-metrics.xlsx": _xlsx(),
        f"{attachment_root}/codex-conversion-fixture-notes.txt": _text(
            """
            Synthetic attachment notes
            English: preserve this line.
            Русский: сохранить эту строку.
            """
        ),
        f"{attachment_root}/codex-conversion-fixture-guide.md": _text(
            """
            # Synthetic Markdown guide

            Preserve this Markdown attachment verbatim.
            """
        ),
        f"{attachment_root}/codex-conversion-fixture-data.csv": _text(
            """
            name,value
            alpha,2
            beta,3
            """
        ),
        f"{attachment_root}/codex-conversion-fixture-data.json": (
            json.dumps(
                {"english": "preserved", "русский": "сохранён"},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
        f"{attachment_root}/codex-conversion-fixture-config.yaml": _text(
            """
            enabled: true
            mode: loss-aware
            """
        ),
        f"{attachment_root}/codex-conversion-fixture-message.eml": _eml(),
        f"{attachment_root}/codex-conversion-fixture-fragment.html": _text(
            """
            <!doctype html>
            <html><body><h1>Synthetic HTML attachment</h1>
            <p>HTML attachment text is preserved.</p></body></html>
            """
        ),
        f"{attachment_root}/codex-conversion-fixture-vector.svg": _text(
            """
            <svg xmlns="http://www.w3.org/2000/svg" width="320" height="120">
              <title>Synthetic SVG diagram</title>
              <desc>Vector text and shape metadata are preserved.</desc>
              <rect x="10" y="10" width="300" height="100"/>
              <text x="24" y="64">SVG CONTENT</text>
              <a href="https://example.com/vector"><text x="24" y="90">External vector link</text></a>
            </svg>
            """
        ),
        f"{attachment_root}/codex-conversion-fixture-flow.drawio": _drawio(),
        "images/inline/codex-conversion-fixture-bilingual.png": base64.b64decode(_BILINGUAL_PNG_BASE64),
        "images/inline/codex-conversion-fixture-flow.png": _make_png(
            diagram_width,
            diagram_height,
            diagram_pixels,
            "Synthetic Draw.io preview: node A points to node B",
        ),
    }


def build_artifacts() -> dict[str, bytes]:
    """Return every generated relative path and its deterministic bytes."""
    payload = _payload_artifacts()
    generated_paths = sorted((*payload, MANIFEST_PATH))
    manifest = {
        "format": FIXTURE_FORMAT,
        "namespace": FIXTURE_NAMESPACE,
        "generated_paths": generated_paths,
        "files": {
            path: {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            for path, content in sorted(payload.items())
        },
    }
    artifacts = dict(payload)
    artifacts[MANIFEST_PATH] = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return dict(sorted(artifacts.items()))


def _safe_destination(root: Path, relative_path: str) -> Path:
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"unsafe generated path: {relative_path}")
    destination = root.joinpath(*pure.parts)
    cursor = root
    for part in pure.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"generated path traverses a symlink: {relative_path}")
    try:
        destination.resolve(strict=False).relative_to(root)
    except ValueError as error:
        raise ValueError(f"generated path escapes output root: {relative_path}") from error
    return destination


def _owned_paths_from_manifest(root: Path) -> set[str]:
    manifest_file = root / MANIFEST_PATH
    try:
        document = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot validate existing fixture manifest: {manifest_file}: {error}") from error
    if document.get("format") != FIXTURE_FORMAT or document.get("namespace") != FIXTURE_NAMESPACE:
        raise RuntimeError(f"existing manifest is not owned by {FIXTURE_NAMESPACE}: {manifest_file}")
    paths = document.get("generated_paths")
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise RuntimeError(f"existing manifest has invalid generated_paths: {manifest_file}")
    return set(paths)


def write_fixture(root: Path, *, replace_generated: bool = False) -> tuple[str, ...]:
    """Write the fixture, refusing collisions unless ownership is proven."""
    root = root.resolve()
    artifacts = build_artifacts()
    collisions = tuple(path for path in artifacts if _safe_destination(root, path).exists())
    if collisions and not replace_generated:
        rendered = "\n  - ".join(collisions)
        raise RuntimeError(f"refusing to overwrite existing fixture paths:\n  - {rendered}")
    if collisions:
        owned = _owned_paths_from_manifest(root)
        unowned = sorted(set(collisions) - owned)
        if unowned:
            rendered = "\n  - ".join(unowned)
            raise RuntimeError(f"safe replacement rejected unowned collisions:\n  - {rendered}")
    for relative_path, content in artifacts.items():
        destination = _safe_destination(root, relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    return tuple(artifacts)


def check_fixture(root: Path) -> tuple[str, ...]:
    """Compare materialized files with generated bytes without writing."""
    root = root.resolve()
    errors: list[str] = []
    for relative_path, expected in build_artifacts().items():
        destination = _safe_destination(root, relative_path)
        if not destination.is_file():
            errors.append(f"missing: {relative_path}")
            continue
        actual = destination.read_bytes()
        if actual != expected:
            errors.append(f"different: {relative_path}")
    return tuple(errors)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"export root to populate (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--replace-generated",
        action="store_true",
        help="replace only paths proven owned by an existing fixture manifest",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify generated paths without writing",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_args(sys.argv[1:] if argv is None else argv)
    if arguments.check:
        errors = check_fixture(arguments.output)
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
        print(f"fixture is current: {arguments.output.resolve()}")
        return 0
    try:
        generated = write_fixture(arguments.output, replace_generated=arguments.replace_generated)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"fixture generation failed: {error}", file=sys.stderr)
        return 2
    print(f"generated {len(generated)} files under {arguments.output.resolve()}")
    for relative_path in generated:
        print(relative_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
