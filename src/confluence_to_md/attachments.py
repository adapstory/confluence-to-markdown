"""Deterministic, low-dependency text extraction for exported attachments."""

from __future__ import annotations

import csv
import io
import json
import mimetypes
import posixpath
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar, Literal, cast
from xml.etree import ElementTree as ET

from .drawio import parse_drawio, render_drawio_text
from .ocr import OcrBackend, OcrError, RapidOcrBackend

Severity = Literal["warning", "error"]


@dataclass(frozen=True, slots=True)
class AttachmentDiagnostic:
    code: str
    message: str
    severity: Severity = "error"


@dataclass(frozen=True, slots=True)
class AttachmentExtraction:
    source: Path
    kind: str
    text: str
    diagnostics: tuple[AttachmentDiagnostic, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()
    extracted: bool = True

    @property
    def ok(self) -> bool:
        return self.extracted and not any(
            item.severity == "error" for item in self.diagnostics
        )

    @property
    def errors(self) -> tuple[AttachmentDiagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.severity == "error")

    def metadata_dict(self) -> dict[str, str]:
        return dict(self.metadata)


_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".log"}
_YAML_EXTENSIONS = {".yaml", ".yml"}
_IMAGE_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}

_HTML_EXTENSIONS = {".htm", ".html", ".xhtml"}
_MAX_EML_DEPTH = 4


def _diagnostic(
    code: str, message: str, severity: Severity = "error"
) -> AttachmentDiagnostic:
    return AttachmentDiagnostic(code=code, message=message, severity=severity)


def _decode_text(data: bytes) -> tuple[str, list[AttachmentDiagnostic]]:
    diagnostics: list[AttachmentDiagnostic] = []
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), diagnostics
    try:
        return data.decode("utf-8-sig"), diagnostics
    except UnicodeDecodeError:
        pass
    for encoding in ("cp1251", "latin-1"):
        try:
            text = data.decode(encoding)
            diagnostics.append(
                _diagnostic(
                    "attachment_encoding_fallback",
                    f"Text required an uncertain {encoding} decoding fallback",
                )
            )
            return text, diagnostics
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("unknown", data, 0, len(data), "unsupported encoding")


def _plain_text(path: Path, kind: str) -> AttachmentExtraction:
    try:
        text, diagnostics = _decode_text(path.read_bytes())
    except (OSError, UnicodeError) as exc:
        return AttachmentExtraction(
            source=path,
            kind=kind,
            text="",
            diagnostics=(_diagnostic("attachment_text_read_failed", str(exc)),),
            extracted=False,
        )
    return AttachmentExtraction(path, kind, text, tuple(diagnostics))


def _json(path: Path) -> AttachmentExtraction:
    raw = _plain_text(path, "json")
    if not raw.extracted:
        return raw
    try:
        value = json.loads(raw.text)
    except json.JSONDecodeError as exc:
        return AttachmentExtraction(
            path,
            "json",
            raw.text,
            raw.diagnostics
            + (_diagnostic("attachment_json_invalid", f"Invalid JSON: {exc}"),),
        )
    return AttachmentExtraction(
        path,
        "json",
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        raw.diagnostics,
    )


def _csv(path: Path) -> AttachmentExtraction:
    raw = _plain_text(path, "csv")
    if not raw.extracted:
        return raw
    try:
        dialect = (
            csv.Sniffer().sniff(raw.text[:8192]) if raw.text.strip() else csv.excel
        )
    except (csv.Error, ValueError):
        dialect = csv.excel
    try:
        rows = list(csv.reader(io.StringIO(raw.text), dialect=dialect))
    except (csv.Error, ValueError) as exc:
        return AttachmentExtraction(
            path,
            "csv",
            raw.text,
            raw.diagnostics
            + (_diagnostic("attachment_csv_invalid", f"Invalid CSV: {exc}"),),
        )
    lines: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        lines.append(f"Row {row_index}:")
        if not row:
            lines.append("  [empty row]")
        for column_index, value in enumerate(row, start=1):
            lines.append(f"  Column {column_index}: {value}")
    return AttachmentExtraction(path, "csv", "\n".join(lines), raw.diagnostics)


class _HtmlToText(HTMLParser):
    _BREAKS: ClassVar[set[str]] = {
        "br",
        "div",
        "p",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._BREAKS and self.parts:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        lines = [re.sub(r"\s+", " ", line).strip() for line in self.parts]
        return "\n".join(line for line in lines if line).strip()


class _AttachmentHtmlToText(HTMLParser):
    _BREAKS: ClassVar[set[str]] = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "header",
        "li",
        "main",
        "nav",
        "p",
        "section",
        "tr",
    }
    _IGNORED: ClassVar[set[str]] = {"script", "style", "template", "noscript"}
    _MEDIA: ClassVar[set[str]] = {
        "audio",
        "embed",
        "iframe",
        "img",
        "object",
        "source",
        "video",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.media_references: list[str] = []
        self._ignored_depth = 0
        self._links: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = dict(attrs)
        if tag in self._IGNORED:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in self._BREAKS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")
        elif re.fullmatch(r"h[1-6]", tag):
            self.parts.extend(("\n", "#" * int(tag[1]), " "))
        elif tag == "a":
            self._links.append(attributes.get("href"))
        if tag in self._MEDIA:
            reference = (
                attributes.get("src")
                or attributes.get("data")
                or attributes.get("href")
                or "[inline media]"
            )
            self.media_references.append(reference)
            alt = attributes.get("alt") or attributes.get("title")
            if alt:
                self.parts.append(f"[Media: {alt}]")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._IGNORED:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag == "a" and self._links:
            href = self._links.pop()
            if href:
                self.parts.append(f" <{href}>")
        if tag in self._BREAKS or re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        rendered = "".join(self.parts)
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in rendered.splitlines()]
        return "\n".join(line for line in lines if line).strip()


def _extract_html_text(
    value: str,
    *,
    handled_media_references: frozenset[str] = frozenset(),
) -> tuple[str, tuple[AttachmentDiagnostic, ...]]:
    parser = _AttachmentHtmlToText()
    try:
        parser.feed(value)
        parser.close()
    except (ValueError, UnicodeError) as exc:
        return (
            "",
            (_diagnostic("attachment_html_invalid", f"Cannot parse HTML: {exc}"),),
        )
    diagnostics: list[AttachmentDiagnostic] = []
    for reference in parser.media_references:
        if reference.strip().casefold() in handled_media_references:
            continue
        diagnostics.append(
            _diagnostic(
                "attachment_html_media_unextracted",
                f"HTML attachment contains media that needs separate extraction: {reference}",
            )
        )
    text = parser.text()
    if not text:
        diagnostics.append(
            _diagnostic(
                "attachment_html_empty", "HTML attachment contains no readable text"
            )
        )
    return text, tuple(diagnostics)


def _html(path: Path) -> AttachmentExtraction:
    raw = _plain_text(path, "html")
    if not raw.extracted:
        return raw
    text, diagnostics = _extract_html_text(raw.text)
    if any(item.code == "attachment_html_invalid" for item in diagnostics):
        return AttachmentExtraction(
            path,
            "html",
            "",
            raw.diagnostics + diagnostics,
            extracted=False,
        )
    return AttachmentExtraction(
        path,
        "html",
        text,
        raw.diagnostics + diagnostics,
        extracted=bool(text),
    )


def _svg(path: Path) -> AttachmentExtraction:
    try:
        root = ET.fromstring(path.read_bytes())
    except (OSError, ET.ParseError) as exc:
        return AttachmentExtraction(
            path,
            "svg",
            "",
            (_diagnostic("attachment_svg_invalid", f"Cannot parse SVG: {exc}"),),
            extracted=False,
        )
    lines: list[str] = []
    diagnostics: list[AttachmentDiagnostic] = []
    shape_count = 0
    media_references: list[str] = []
    for element in root.iter():
        local = element.tag.rsplit("}", 1)[-1]
        if local in {"title", "desc", "text"}:
            value = re.sub(r"\s+", " ", "".join(element.itertext())).strip()
            if value:
                lines.append(f"{local.title()}: {value}")
        if local in {
            "circle",
            "ellipse",
            "line",
            "path",
            "polygon",
            "polyline",
            "rect",
        }:
            shape_count += 1
        if local == "image":
            reference = next(
                (
                    value
                    for key, value in element.attrib.items()
                    if key.rsplit("}", 1)[-1] == "href"
                ),
                "[inline image]",
            )
            media_references.append(reference)
        if local == "a":
            href = next(
                (
                    value
                    for key, value in element.attrib.items()
                    if key.rsplit("}", 1)[-1] == "href"
                ),
                None,
            )
            if href:
                lines.append(f"Link: {href}")
    for reference in media_references:
        diagnostics.append(
            _diagnostic(
                "attachment_svg_embedded_image_unextracted",
                f"SVG contains an embedded image that needs OCR: {reference}",
            )
        )
    lines.extend(
        (
            f"Vector shapes: {shape_count}",
            "SVG XML (complete vector structure):\n\n```xml\n"
            + ET.tostring(root, encoding="unicode")
            + "\n```",
        )
    )
    return AttachmentExtraction(
        path,
        "svg",
        "\n".join(lines),
        tuple(diagnostics),
        (("vector_shapes", str(shape_count)),),
        extracted=True,
    )


def _message_body(
    part: Message,
    *,
    handled_media_references: frozenset[str] = frozenset(),
) -> tuple[str, tuple[AttachmentDiagnostic, ...]]:
    content = cast(EmailMessage, part).get_content()
    if not isinstance(content, str):
        return "", ()
    if part.get_content_type() == "text/html":
        return _extract_html_text(
            content,
            handled_media_references=handled_media_references,
        )
    return content.strip(), ()


def _safe_mime_filename(part: Message, index: int) -> tuple[str, str]:
    original = part.get_filename() or ""
    safe = Path(original.replace("\\", "/")).name
    safe = re.sub(r"[\x00-\x1f\x7f]", "_", safe).strip(" .")
    suffix = mimetypes.guess_extension(part.get_content_type()) or ""
    if not safe:
        safe = f"embedded-{index:04d}{suffix or '.bin'}"
    elif not Path(safe).suffix and suffix:
        safe += suffix
    return original or safe, f"part-{index:04d}-{safe}"


def _mime_part_bytes(part: Message) -> bytes | None:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    if part.get_content_type() == "message/rfc822":
        nested = part.get_payload()
        if isinstance(nested, list):
            return b"\n".join(
                item.as_bytes(policy=policy.default)
                for item in nested
                if isinstance(item, Message)
            )
    return None


def _is_embedded_mime_part(part: Message) -> bool:
    return (
        part.get_content_disposition() in {"attachment", "inline"}
        or bool(part.get_filename())
        or (not part.is_multipart() and part.get_content_maintype() != "text")
    )


def _iter_eml_parts(message: Message):
    payload = message.get_payload()
    if not message.is_multipart() or not isinstance(payload, list):
        yield message
        return
    for part in payload:
        if not isinstance(part, Message):
            continue
        if _is_embedded_mime_part(part):
            yield part
        elif part.is_multipart():
            yield from _iter_eml_parts(part)
        else:
            yield part


def _eml(
    path: Path,
    *,
    ocr_backend: OcrBackend | None,
    work_directory: Path | None,
    recursion_depth: int,
) -> AttachmentExtraction:
    try:
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    except (OSError, ValueError) as exc:
        return AttachmentExtraction(
            path,
            "eml",
            "",
            (_diagnostic("attachment_eml_read_failed", str(exc)),),
            extracted=False,
        )
    lines = [
        f"Subject: {message.get('subject', '')}",
        f"From: {message.get('from', '')}",
        f"To: {message.get('to', '')}",
        f"Date: {message.get('date', '')}",
        f"Message-ID: {message.get('message-id', '')}",
        "",
        "Body:",
    ]
    diagnostics: list[AttachmentDiagnostic] = []
    body_parts: list[str] = []
    embedded: list[str] = []
    embedded_text: list[str] = []
    embedded_parts: list[tuple[Message, str, str]] = []
    handled_body_media: set[str] = set()
    for candidate in _iter_eml_parts(message):
        if not _is_embedded_mime_part(candidate):
            continue
        content_id = str(candidate.get("content-id", "")).strip().strip("<>")
        if content_id:
            handled_body_media.add(f"cid:{content_id}".casefold())
        content_location = str(candidate.get("content-location", "")).strip()
        if content_location:
            handled_body_media.add(content_location.casefold())
        filename = candidate.get_filename()
        if filename:
            handled_body_media.add(filename.strip().casefold())
    handled_body_media_references = frozenset(handled_body_media)
    for part in _iter_eml_parts(message):
        is_embedded = _is_embedded_mime_part(part)
        if is_embedded:
            descriptor, safe_filename = _safe_mime_filename(part, len(embedded) + 1)
            embedded.append(f"- {descriptor} ({part.get_content_type()})")
            embedded_parts.append((part, descriptor, safe_filename))
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            body_text, body_diagnostics = _message_body(
                part,
                handled_media_references=handled_body_media_references,
            )
            body_parts.append("Text body:\n" + body_text)
            diagnostics.extend(body_diagnostics)
        elif content_type == "text/html":
            body_text, body_diagnostics = _message_body(
                part,
                handled_media_references=handled_body_media_references,
            )
            body_parts.append("HTML body:\n" + body_text)
            diagnostics.extend(
                AttachmentDiagnostic(
                    code=item.code,
                    message=f"HTML message body: {item.message}",
                    severity=item.severity,
                )
                for item in body_diagnostics
            )
    if embedded_parts:
        if recursion_depth >= _MAX_EML_DEPTH:
            diagnostics.append(
                _diagnostic(
                    "attachment_eml_recursion_limit",
                    f"Nested EML depth exceeds the supported limit of {_MAX_EML_DEPTH}",
                )
            )
        elif work_directory is None:
            diagnostics.append(
                _diagnostic(
                    "attachment_eml_work_directory_required",
                    "An explicit work directory is required to inspect embedded EML attachments",
                )
            )
        else:
            try:
                work_directory.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(
                    prefix="confluence-md-eml-", dir=work_directory
                ) as directory_name:
                    materialized_root = Path(directory_name)
                    for part, descriptor, safe_filename in embedded_parts:
                        payload = _mime_part_bytes(part)
                        if payload is None:
                            diagnostics.append(
                                _diagnostic(
                                    "attachment_eml_payload_unreadable",
                                    f"Embedded attachment payload cannot be decoded: {descriptor}",
                                )
                            )
                            continue
                        materialized = materialized_root / safe_filename
                        materialized.write_bytes(payload)
                        nested = extract_attachment(
                            materialized,
                            ocr_backend=ocr_backend,
                            work_directory=work_directory,
                            _recursion_depth=recursion_depth + 1,
                        )
                        if nested.text.strip():
                            embedded_text.append(
                                f"Attachment {descriptor} ({nested.kind}):\n{nested.text.strip()}"
                            )
                        elif not nested.diagnostics:
                            diagnostics.append(
                                _diagnostic(
                                    "attachment_eml_embedded_text_empty",
                                    f"Embedded attachment produced no text: {descriptor}",
                                )
                            )
                        diagnostics.extend(
                            AttachmentDiagnostic(
                                code=item.code,
                                message=f"Embedded attachment {descriptor}: {item.message}",
                                severity=item.severity,
                            )
                            for item in nested.diagnostics
                        )
            except OSError as exc:
                diagnostics.append(
                    _diagnostic(
                        "attachment_eml_work_directory_failed",
                        f"Cannot inspect embedded EML attachments in {work_directory}: {exc}",
                    )
                )

    lines.append("\n\n".join(part for part in body_parts if part).strip())
    if embedded:
        lines.extend(("", "Embedded attachments:", *embedded))
    if embedded_text:
        lines.extend(
            (
                "",
                "Embedded attachment text:",
                "Original payloads remain retained inside the original EML resource.",
                *embedded_text,
            )
        )
    return AttachmentExtraction(
        path, "eml", "\n".join(lines).strip(), tuple(diagnostics)
    )


def _xml(zf: zipfile.ZipFile, name: str) -> ET.Element:
    return ET.fromstring(zf.read(name))


def _relationships(zf: zipfile.ZipFile, part_name: str) -> dict[str, tuple[str, str]]:
    directory, basename = posixpath.split(part_name)
    rels_name = posixpath.join(directory, "_rels", basename + ".rels")
    if rels_name not in zf.namelist():
        return {}
    relationships: dict[str, tuple[str, str]] = {}
    for relation in _xml(zf, rels_name).iter():
        if relation.tag.rsplit("}", 1)[-1] != "Relationship":
            continue
        relationship_id = relation.get("Id")
        target = relation.get("Target")
        if relationship_id and target:
            relationships[relationship_id] = (target, relation.get("Type") or "")
    return relationships


def _embedded_ooxml_diagnostics(
    names: list[str],
    *,
    kind: str,
    categories: tuple[tuple[str, str], ...],
) -> list[AttachmentDiagnostic]:
    diagnostics: list[AttachmentDiagnostic] = []
    for prefix, label in categories:
        matches = sorted(
            name for name in names if name.startswith(prefix) and not name.endswith("/")
        )
        if not matches:
            continue
        preview = ", ".join(matches[:5])
        if len(matches) > 5:
            preview += f", and {len(matches) - 5} more"
        diagnostics.append(
            _diagnostic(
                f"attachment_{kind}_embedded_{label}_unextracted",
                f"{kind.upper()} contains embedded {label} not converted to text: {preview}",
            )
        )
    return diagnostics


def _word_paragraph(
    paragraph: ET.Element, relationships: dict[str, tuple[str, str]]
) -> str:
    parts: list[str] = []

    def visit(element: ET.Element) -> None:
        local = element.tag.rsplit("}", 1)[-1]
        if local in {"t", "instrText"} and element.text:
            parts.append(element.text)
        elif local == "tab":
            parts.append("\t")
        elif local in {"br", "cr"}:
            parts.append("\n")
        else:
            for child in element:
                visit(child)
        if local == "hyperlink":
            relationship_id = next(
                (value for key, value in element.attrib.items() if key.endswith("}id")),
                None,
            )
            if relationship_id and relationship_id in relationships:
                parts.append(f" <{relationships[relationship_id][0]}>")

    visit(paragraph)
    return "".join(parts).strip()


def _word_table(
    table: ET.Element, relationships: dict[str, tuple[str, str]]
) -> list[str]:
    lines: list[str] = []
    rows = [child for child in table if child.tag.rsplit("}", 1)[-1] == "tr"]
    for row_index, row in enumerate(rows, start=1):
        lines.append(f"Row {row_index}:")
        cells = [child for child in row if child.tag.rsplit("}", 1)[-1] == "tc"]
        for cell_index, cell in enumerate(cells, start=1):
            paragraphs = [
                _word_paragraph(item, relationships)
                for item in cell.iter()
                if item.tag.rsplit("}", 1)[-1] == "p"
            ]
            qualifiers: list[str] = []
            grid_span = next(
                (
                    item
                    for item in cell.iter()
                    if item.tag.rsplit("}", 1)[-1] == "gridSpan"
                ),
                None,
            )
            if grid_span is not None:
                value = next(iter(grid_span.attrib.values()), "")
                qualifiers.append(f"colspan={value}")
            vertical_merge = next(
                (
                    item
                    for item in cell.iter()
                    if item.tag.rsplit("}", 1)[-1] == "vMerge"
                ),
                None,
            )
            if vertical_merge is not None:
                value = next(iter(vertical_merge.attrib.values()), "continue")
                qualifiers.append(f"rowspan={value or 'continue'}")
            suffix = f" ({', '.join(qualifiers)})" if qualifiers else ""
            content = " / ".join(item for item in paragraphs if item) or "[empty]"
            lines.append(f"  Cell {cell_index}{suffix}: {content}")
    return lines


def _word_part(zf: zipfile.ZipFile, part_name: str, title: str) -> list[str]:
    root = _xml(zf, part_name)
    relationships = _relationships(zf, part_name)
    lines = [f"## {title}"]
    container = next(
        (item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "body"),
        root,
    )
    for child in container:
        local = child.tag.rsplit("}", 1)[-1]
        if local == "p":
            text = _word_paragraph(child, relationships)
            if text:
                lines.append(text)
        elif local == "tbl":
            lines.append("Table:")
            lines.extend(_word_table(child, relationships))
    return lines


def _docx(path: Path) -> AttachmentExtraction:
    diagnostics: list[AttachmentDiagnostic] = []
    try:
        with zipfile.ZipFile(path) as zf:
            if "word/document.xml" not in zf.namelist():
                raise ValueError("word/document.xml is missing")
            sections = [_word_part(zf, "word/document.xml", "Document")]
            optional_parts = sorted(
                name
                for name in zf.namelist()
                if re.fullmatch(
                    r"word/(?:header\d+|footer\d+|footnotes|endnotes|comments)\.xml",
                    name,
                )
            )
            for part_name in optional_parts:
                title = Path(part_name).stem.replace("_", " ").title()
                sections.append(_word_part(zf, part_name, title))
            diagnostics.extend(
                _embedded_ooxml_diagnostics(
                    zf.namelist(),
                    kind="docx",
                    categories=(
                        ("word/media/", "media"),
                        ("word/charts/", "charts"),
                        ("word/embeddings/", "objects"),
                        ("word/diagrams/", "diagrams"),
                    ),
                )
            )
            document_root = _xml(zf, "word/document.xml")
            unsupported_tags = {
                item.tag.rsplit("}", 1)[-1]
                for item in document_root.iter()
                if item.tag.rsplit("}", 1)[-1]
                in {"altChunk", "drawing", "object", "pict", "txbxContent"}
            }
            if unsupported_tags:
                diagnostics.append(
                    _diagnostic(
                        "attachment_docx_rich_object_unextracted",
                        "DOCX contains rich objects requiring a dedicated handler: "
                        + ", ".join(sorted(unsupported_tags)),
                    )
                )
    except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        return AttachmentExtraction(
            path,
            "docx",
            "",
            (_diagnostic("attachment_docx_invalid", f"Cannot extract DOCX: {exc}"),),
            extracted=False,
        )
    text = "\n\n".join("\n".join(section) for section in sections).strip()
    if not text.replace("## Document", "").strip():
        diagnostics.append(
            _diagnostic("attachment_docx_empty", "DOCX contains no text")
        )
    return AttachmentExtraction(path, "docx", text, tuple(diagnostics))


def _xml_text_paragraphs(root: ET.Element) -> list[str]:
    paragraphs: list[str] = []
    for paragraph in root.iter():
        if paragraph.tag.rsplit("}", 1)[-1] != "p":
            continue
        text = "".join(
            item.text or ""
            for item in paragraph.iter()
            if item.tag.rsplit("}", 1)[-1] == "t"
        ).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def _numbered_part_key(name: str) -> int:
    match = re.search(r"(\d+)", Path(name).stem)
    if match is None:
        raise ValueError(f"OOXML part name has no numeric index: {name}")
    return int(match.group(1))


def _pptx(path: Path) -> AttachmentExtraction:
    diagnostics: list[AttachmentDiagnostic] = []
    sections: list[str] = []
    content_found = False
    try:
        with zipfile.ZipFile(path) as zf:
            diagnostics.extend(
                _embedded_ooxml_diagnostics(
                    zf.namelist(),
                    kind="pptx",
                    categories=(
                        ("ppt/media/", "media"),
                        ("ppt/charts/", "charts"),
                        ("ppt/embeddings/", "objects"),
                        ("ppt/diagrams/", "diagrams"),
                    ),
                )
            )
            slide_names = sorted(
                (
                    name
                    for name in zf.namelist()
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
                ),
                key=_numbered_part_key,
            )
            if not slide_names:
                raise ValueError("presentation contains no slides")
            for slide_number, slide_name in enumerate(slide_names, start=1):
                root = _xml(zf, slide_name)
                paragraphs = _xml_text_paragraphs(root)
                content_found = content_found or bool(paragraphs)
                lines = [f"## Slide {slide_number}", *paragraphs]
                relationships = _relationships(zf, slide_name)
                links = sorted(
                    target
                    for target, relation_type in relationships.values()
                    if relation_type.endswith("/hyperlink")
                )
                if links:
                    lines.extend(("Links:", *(f"- {target}" for target in links)))
                sections.append("\n".join(lines))
            note_names = sorted(
                name
                for name in zf.namelist()
                if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", name)
            )
            for note_name in note_names:
                note_number = _numbered_part_key(note_name)
                notes = _xml_text_paragraphs(_xml(zf, note_name))
                if notes:
                    content_found = True
                    sections.append(
                        "\n".join((f"## Notes for slide {note_number}", *notes))
                    )
    except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        return AttachmentExtraction(
            path,
            "pptx",
            "",
            (_diagnostic("attachment_pptx_invalid", f"Cannot extract PPTX: {exc}"),),
            extracted=False,
        )
    if not content_found:
        diagnostics.append(
            _diagnostic("attachment_pptx_empty", "PPTX contains no text")
        )
    return AttachmentExtraction(path, "pptx", "\n\n".join(sections), tuple(diagnostics))


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    strings: list[str] = []
    for item in _xml(zf, "xl/sharedStrings.xml").iter():
        if item.tag.rsplit("}", 1)[-1] != "si":
            continue
        strings.append(
            "".join(
                child.text or ""
                for child in item.iter()
                if child.tag.rsplit("}", 1)[-1] == "t"
            )
        )
    return strings


def _xlsx_cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.get("t")
    inline = "".join(
        child.text or "" for child in cell.iter() if child.tag.rsplit("}", 1)[-1] == "t"
    )
    value_element = next(
        (child for child in cell if child.tag.rsplit("}", 1)[-1] == "v"),
        None,
    )
    raw = value_element.text if value_element is not None and value_element.text else ""
    if cell_type == "s" and raw:
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError):
            return f"[invalid shared string index: {raw}]"
    if cell_type == "inlineStr":
        return inline
    if cell_type == "b":
        return "TRUE" if raw == "1" else "FALSE"
    return raw or inline


def _xlsx(path: Path) -> AttachmentExtraction:
    diagnostics: list[AttachmentDiagnostic] = []
    sections: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            if "xl/workbook.xml" not in zf.namelist():
                raise ValueError("xl/workbook.xml is missing")
            shared_strings = _shared_strings(zf)
            diagnostics.extend(
                _embedded_ooxml_diagnostics(
                    zf.namelist(),
                    kind="xlsx",
                    categories=(
                        ("xl/media/", "media"),
                        ("xl/charts/", "charts"),
                        ("xl/embeddings/", "objects"),
                        ("xl/drawings/", "drawings"),
                    ),
                )
            )
            workbook = _xml(zf, "xl/workbook.xml")
            workbook_relationships = _relationships(zf, "xl/workbook.xml")
            sheets = [
                item
                for item in workbook.iter()
                if item.tag.rsplit("}", 1)[-1] == "sheet"
            ]
            for sheet_index, sheet in enumerate(sheets, start=1):
                relationship_id = next(
                    (
                        value
                        for key, value in sheet.attrib.items()
                        if key.endswith("}id")
                    ),
                    None,
                )
                if not relationship_id or relationship_id not in workbook_relationships:
                    diagnostics.append(
                        _diagnostic(
                            "attachment_xlsx_sheet_unresolved",
                            f"Cannot resolve worksheet {sheet.get('name') or sheet_index}",
                        )
                    )
                    continue
                target = workbook_relationships[relationship_id][0].lstrip("/")
                sheet_path = posixpath.normpath(posixpath.join("xl", target))
                if target.startswith("xl/"):
                    sheet_path = posixpath.normpath(target)
                root = _xml(zf, sheet_path)
                state = sheet.get("state") or "visible"
                lines = [
                    f"## Sheet: {sheet.get('name') or sheet_index} (state={state})"
                ]
                for merge in (
                    item
                    for item in root.iter()
                    if item.tag.rsplit("}", 1)[-1] == "mergeCell"
                ):
                    lines.append(f"Merged cells: {merge.get('ref', '[unknown]')}")
                for row in (
                    item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "row"
                ):
                    row_number = row.get("r") or "?"
                    lines.append(f"Row {row_number}:")
                    cells = [item for item in row if item.tag.rsplit("}", 1)[-1] == "c"]
                    if not cells:
                        lines.append("  [empty row]")
                    for cell in cells:
                        coordinate = cell.get("r") or "[unknown]"
                        value = _xlsx_cell_value(cell, shared_strings) or "[empty]"
                        formula = next(
                            (
                                child.text or ""
                                for child in cell
                                if child.tag.rsplit("}", 1)[-1] == "f"
                            ),
                            "",
                        )
                        suffix = f" (formula={formula})" if formula else ""
                        lines.append(f"  Cell {coordinate}{suffix}: {value}")
                relationships = _relationships(zf, sheet_path)
                hyperlinks = [
                    item
                    for item in root.iter()
                    if item.tag.rsplit("}", 1)[-1] == "hyperlink"
                ]
                for hyperlink in hyperlinks:
                    relationship_id = next(
                        (
                            value
                            for key, value in hyperlink.attrib.items()
                            if key.endswith("}id")
                        ),
                        None,
                    )
                    target_value = (
                        relationships.get(relationship_id, ("", ""))[0]
                        if relationship_id
                        else hyperlink.get("location") or ""
                    )
                    lines.append(
                        f"Hyperlink {hyperlink.get('ref', '[unknown]')}: {target_value}"
                    )
                sections.append("\n".join(lines))
    except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        return AttachmentExtraction(
            path,
            "xlsx",
            "",
            (_diagnostic("attachment_xlsx_invalid", f"Cannot extract XLSX: {exc}"),),
            extracted=False,
        )
    return AttachmentExtraction(path, "xlsx", "\n\n".join(sections), tuple(diagnostics))


def _image(path: Path, ocr_backend: OcrBackend | None) -> AttachmentExtraction:
    backend = ocr_backend or RapidOcrBackend()
    try:
        result = backend.recognize(path)
    except OcrError as exc:
        return AttachmentExtraction(
            path,
            "image",
            "",
            (_diagnostic("attachment_image_ocr_failed", str(exc)),),
            extracted=False,
        )
    diagnostics = tuple(
        _diagnostic(item.code, item.message, item.severity)
        for item in result.diagnostics
    )
    metadata = (
        ("ocr_engine", result.engine),
        ("ocr_languages", "+".join(result.languages)),
        (
            "ocr_mean_confidence",
            "" if result.mean_confidence is None else f"{result.mean_confidence:.2f}",
        ),
    )
    return AttachmentExtraction(
        path,
        "image",
        result.text,
        diagnostics,
        metadata,
        extracted=result.readable,
    )


def _pdf_native_pymupdf(path: Path) -> list[str] | None:
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError:
        return None
    document = pymupdf.open(path)
    try:
        return [str(page.get_text("text")) for page in document]
    finally:
        document.close()


def _pdf_native_pdftotext(path: Path) -> list[str] | None:
    executable = shutil.which("pdftotext")
    if executable is None:
        return None
    completed = subprocess.run(
        [executable, "-layout", str(path), "-"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        return None
    pages = completed.stdout.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def _render_pdf_page_pymupdf(
    path: Path, page_number: int, directory: Path
) -> Path | None:
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError:
        return None
    output = directory / f"page-{page_number}.png"
    try:
        document = pymupdf.open(path)
        try:
            page = document.load_page(page_number - 1)
            pixmap = page.get_pixmap(dpi=300, alpha=False)
            pixmap.save(str(output))
        finally:
            document.close()
    except Exception:  # noqa: BLE001  # PyMuPDF exposes backend-specific exceptions.
        return None
    return output if output.is_file() else None


def _render_pdf_page_pdftoppm(
    path: Path, page_number: int, directory: Path
) -> Path | None:
    executable = shutil.which("pdftoppm")
    if executable is None:
        return None
    prefix = directory / f"page-{page_number}"
    try:
        completed = subprocess.run(
            [
                executable,
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-r",
                "300",
                "-png",
                "-singlefile",
                str(path),
                str(prefix),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = prefix.with_suffix(".png")
    return output if completed.returncode == 0 and output.is_file() else None


def _render_pdf_page(path: Path, page_number: int, directory: Path) -> Path | None:
    rendered = _render_pdf_page_pymupdf(path, page_number, directory)
    if rendered is not None:
        return rendered
    return _render_pdf_page_pdftoppm(path, page_number, directory)


def extract_pdf_attachment(
    path: Path,
    *,
    ocr_backend: OcrBackend | None = None,
    work_directory: Path | None = None,
) -> AttachmentExtraction:
    """Extract native PDF text and OCR every page for loss-aware preservation."""

    diagnostics: list[AttachmentDiagnostic] = []
    backend = ocr_backend or RapidOcrBackend()
    try:
        pages = _pdf_native_pymupdf(path)
    except Exception:  # noqa: BLE001  # PyMuPDF exposes backend-specific exceptions.
        pages = None
    if pages is None:
        try:
            pages = _pdf_native_pdftotext(path)
        except (OSError, subprocess.SubprocessError):
            pages = None
    if pages is None:
        return AttachmentExtraction(
            path,
            "pdf",
            "",
            tuple(diagnostics)
            + (
                _diagnostic(
                    "attachment_pdf_backend_missing",
                    "Neither PyMuPDF nor pdftotext could extract the PDF",
                ),
            ),
            extracted=False,
        )
    if not pages:
        return AttachmentExtraction(
            path,
            "pdf",
            "",
            tuple(diagnostics)
            + (_diagnostic("attachment_pdf_empty", "PDF contains no pages"),),
            extracted=False,
        )

    rendered_pages: list[str] = []
    render_root: Path | None = None
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    if work_directory is None:
        diagnostics.append(
            _diagnostic(
                "attachment_pdf_work_directory_required",
                "Every PDF page requires an explicit work_directory for loss-aware OCR inspection",
            )
        )
    else:
        work_root = Path(work_directory)
        try:
            work_root.mkdir(parents=True, exist_ok=True)
            temporary_directory = tempfile.TemporaryDirectory(
                prefix="confluence-md-pdf-", dir=work_root
            )
            render_root = Path(temporary_directory.name)
        except OSError as exc:
            diagnostics.append(
                _diagnostic(
                    "attachment_pdf_work_directory_failed",
                    f"Cannot prepare PDF work directory {work_root}: {exc}",
                )
            )
            temporary_directory = None

    try:
        for page_number, native_text in enumerate(pages, start=1):
            native = native_text.strip()
            ocr_text = "[OCR unavailable]"
            if render_root is None:
                diagnostics.append(
                    _diagnostic(
                        "attachment_pdf_page_render_failed",
                        f"Cannot rasterize PDF page {page_number} for OCR",
                    )
                )
            else:
                image_path = _render_pdf_page(path, page_number, render_root)
                if image_path is None:
                    diagnostics.append(
                        _diagnostic(
                            "attachment_pdf_page_render_failed",
                            f"Cannot rasterize PDF page {page_number} for OCR",
                        )
                    )
                else:
                    try:
                        ocr_result = backend.recognize(image_path)
                    except OcrError as exc:
                        diagnostics.append(
                            _diagnostic(
                                "attachment_pdf_page_ocr_failed",
                                f"OCR failed for PDF page {page_number}: {exc}",
                            )
                        )
                        ocr_text = "[OCR failed]"
                    else:
                        ocr_text = ocr_result.text.strip() or "[no extractable OCR text]"
                        diagnostics.extend(
                            _diagnostic(
                                item.code,
                                f"Page {page_number}: {item.message}",
                                item.severity,
                            )
                            for item in ocr_result.diagnostics
                        )
                        if not ocr_result.readable:
                            diagnostics.append(
                                _diagnostic(
                                    "attachment_pdf_page_ocr_unreadable",
                                    f"OCR could not reliably read PDF page {page_number}",
                                )
                            )
            rendered_pages.append(
                "\n".join(
                    (
                        f"## PDF page {page_number}",
                        "Native text:",
                        native or "[no extractable native text]",
                        "",
                        "OCR text:",
                        ocr_text,
                    )
                )
            )
    finally:
        if temporary_directory is not None:
            temporary_directory.cleanup()
    return AttachmentExtraction(
        path,
        "pdf",
        "\n\n".join(rendered_pages),
        tuple(diagnostics),
        (("pages", str(len(pages))),),
    )


def _drawio(path: Path) -> AttachmentExtraction:
    document = parse_drawio(path)
    diagnostics = tuple(
        _diagnostic(item.code, item.message, item.severity)
        for item in document.diagnostics
    )
    return AttachmentExtraction(
        path,
        "drawio",
        render_drawio_text(document),
        diagnostics,
        (("pages", str(len(document.pages))),),
        extracted=bool(document.pages),
    )


def extract_attachment(
    path: Path,
    *,
    ocr_backend: OcrBackend | None = None,
    work_directory: Path | None = None,
    _recursion_depth: int = 0,
) -> AttachmentExtraction:
    """Extract one supported attachment into deterministic plain text.

    The caller remains responsible for copying the original resource and for
    inserting the returned text into the owning Markdown page.
    """

    source = Path(path)
    if not source.is_file():
        return AttachmentExtraction(
            source,
            "missing",
            "",
            (
                _diagnostic(
                    "attachment_missing", f"Attachment does not exist: {source}"
                ),
            ),
            extracted=False,
        )
    suffix = source.suffix.lower()
    if suffix in _TEXT_EXTENSIONS:
        return _plain_text(source, suffix.lstrip("."))
    if suffix in _YAML_EXTENSIONS:
        return _plain_text(source, "yaml")
    if suffix == ".json":
        return _json(source)
    if suffix == ".csv":
        return _csv(source)
    if suffix == ".eml":
        return _eml(
            source,
            ocr_backend=ocr_backend,
            work_directory=work_directory,
            recursion_depth=_recursion_depth,
        )
    if suffix == ".docx":
        return _docx(source)
    if suffix == ".pptx":
        return _pptx(source)
    if suffix == ".xlsx":
        return _xlsx(source)
    if suffix == ".pdf":
        return extract_pdf_attachment(
            source,
            ocr_backend=ocr_backend,
            work_directory=work_directory,
        )
    if suffix == ".drawio":
        return _drawio(source)
    if suffix in _HTML_EXTENSIONS:
        return _html(source)
    if suffix == ".svg":
        return _svg(source)
    if suffix in _IMAGE_EXTENSIONS:
        return _image(source, ocr_backend)
    return AttachmentExtraction(
        source,
        suffix.lstrip(".") or "unknown",
        "",
        (
            _diagnostic(
                "attachment_type_unsupported",
                f"Unsupported attachment type: {suffix or '[no extension]'}",
            ),
        ),
        extracted=False,
    )
