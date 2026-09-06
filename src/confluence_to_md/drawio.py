"""Read Draw.io sources without JavaScript or the Draw.io application."""

from __future__ import annotations

import base64
import html
import re
import urllib.parse
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar, Literal
from xml.etree import ElementTree as ET

MAX_DRAWIO_XML_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DrawioDiagnostic:
    code: str
    message: str
    severity: Literal["warning", "error"] = "error"
    page: str | None = None


@dataclass(frozen=True, slots=True)
class DrawioGeometry:
    x: str | None = None
    y: str | None = None
    width: str | None = None
    height: str | None = None


@dataclass(frozen=True, slots=True)
class DrawioNode:
    id: str
    label: str
    parent: str | None
    style: str | None
    link: str | None
    geometry: DrawioGeometry


@dataclass(frozen=True, slots=True)
class DrawioPoint:
    x: str | None
    y: str | None
    role: str


@dataclass(frozen=True, slots=True)
class DrawioEdge:
    id: str
    source: str | None
    target: str | None
    label: str
    parent: str | None
    style: str | None
    link: str | None
    points: tuple[DrawioPoint, ...]


@dataclass(frozen=True, slots=True)
class DrawioPage:
    id: str | None
    name: str
    nodes: tuple[DrawioNode, ...]
    edges: tuple[DrawioEdge, ...]


@dataclass(frozen=True, slots=True)
class DrawioDocument:
    pages: tuple[DrawioPage, ...]
    diagnostics: tuple[DrawioDiagnostic, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.pages) and not any(
            item.severity == "error" for item in self.diagnostics
        )


class _LabelTextParser(HTMLParser):
    _BLOCK_TAGS: ClassVar[set[str]] = {"br", "div", "p", "li", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._BLOCK_TAGS and self.parts:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _clean_label(value: str | None) -> str:
    if not value:
        return ""
    parser = _LabelTextParser()
    parser.feed(html.unescape(value))
    parser.close()
    lines = [re.sub(r"\s+", " ", line).strip() for line in parser.parts]
    return "\n".join(line for line in lines if line).strip()


def _read_source(source: Path | bytes | str) -> bytes:
    if isinstance(source, Path):
        return source.read_bytes()
    if isinstance(source, bytes):
        return source
    if source.lstrip().startswith("<"):
        return source.encode("utf-8")
    return Path(source).read_bytes()


def _decode_compressed_diagram(encoded: str) -> ET.Element:
    compact = "".join(encoded.split())
    compact += "=" * (-len(compact) % 4)
    try:
        payload = base64.b64decode(compact)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid base64 payload") from exc
    inflated: bytes | None = None
    for window_bits in (-15, zlib.MAX_WBITS):
        try:
            decompressor = zlib.decompressobj(window_bits)
            inflated = decompressor.decompress(payload, MAX_DRAWIO_XML_BYTES + 1)
            if (
                len(inflated) > MAX_DRAWIO_XML_BYTES
                or decompressor.unconsumed_tail
            ):
                raise ValueError(
                    "decompressed Draw.io XML exceeds supported limit "
                    f"of {MAX_DRAWIO_XML_BYTES} bytes"
                )
            tail = decompressor.flush()
            inflated += tail
            if len(inflated) > MAX_DRAWIO_XML_BYTES:
                raise ValueError(
                    "decompressed Draw.io XML exceeds supported limit "
                    f"of {MAX_DRAWIO_XML_BYTES} bytes"
                )
            if not decompressor.eof:
                raise zlib.error("incomplete deflate payload")
            break
        except ValueError:
            raise
        except zlib.error:
            continue
    if inflated is None:
        raise ValueError("invalid deflate payload")
    xml = urllib.parse.unquote(inflated.decode("utf-8"))
    return ET.fromstring(xml)


def _diagram_model(diagram: ET.Element) -> ET.Element:
    for child in diagram:
        if _local_name(child.tag) == "mxGraphModel":
            return child
    payload = (diagram.text or "").strip()
    if not payload:
        raise ValueError("diagram has no mxGraphModel payload")
    unescaped = html.unescape(payload)
    if unescaped.lstrip().startswith("<"):
        return ET.fromstring(unescaped)
    return _decode_compressed_diagram(payload)


def _parse_page(model: ET.Element, *, page_id: str | None, name: str) -> DrawioPage:
    parents = {child: parent for parent in model.iter() for child in parent}
    nodes: list[DrawioNode] = []
    edges: list[DrawioEdge] = []
    for cell in (
        element for element in model.iter() if _local_name(element.tag) == "mxCell"
    ):
        wrapper = parents.get(cell)
        metadata = (
            wrapper.attrib
            if wrapper is not None
            and _local_name(wrapper.tag).lower() in {"object", "userobject"}
            else {}
        )
        cell_id = cell.get("id") or metadata.get("id")
        if not cell_id:
            continue
        value = cell.get("value") or metadata.get("label") or metadata.get("value")
        link = cell.get("link") or metadata.get("link")
        if cell.get("vertex") == "1":
            geometry_element = next(
                (child for child in cell if _local_name(child.tag) == "mxGeometry"),
                None,
            )
            geometry = DrawioGeometry(
                x=geometry_element.get("x") if geometry_element is not None else None,
                y=geometry_element.get("y") if geometry_element is not None else None,
                width=(
                    geometry_element.get("width")
                    if geometry_element is not None
                    else None
                ),
                height=(
                    geometry_element.get("height")
                    if geometry_element is not None
                    else None
                ),
            )
            nodes.append(
                DrawioNode(
                    id=cell_id,
                    label=_clean_label(value),
                    parent=cell.get("parent"),
                    style=cell.get("style"),
                    link=link,
                    geometry=geometry,
                )
            )
        elif cell.get("edge") == "1":
            points: list[DrawioPoint] = []
            for point in (
                element
                for element in cell.iter()
                if _local_name(element.tag) == "mxPoint"
            ):
                points.append(
                    DrawioPoint(
                        x=point.get("x"),
                        y=point.get("y"),
                        role=point.get("as") or "waypoint",
                    )
                )
            edges.append(
                DrawioEdge(
                    id=cell_id,
                    source=cell.get("source"),
                    target=cell.get("target"),
                    label=_clean_label(value),
                    parent=cell.get("parent"),
                    style=cell.get("style"),
                    link=link,
                    points=tuple(points),
                )
            )
    return DrawioPage(
        id=page_id,
        name=name,
        nodes=tuple(nodes),
        edges=tuple(edges),
    )


def parse_drawio(source: Path | bytes | str) -> DrawioDocument:
    """Parse compressed and uncompressed Draw.io XML into a text-friendly graph."""

    diagnostics: list[DrawioDiagnostic] = []
    try:
        root = ET.fromstring(_read_source(source))
    except (OSError, ET.ParseError, UnicodeError) as exc:
        return DrawioDocument(
            pages=(),
            diagnostics=(
                DrawioDiagnostic(
                    "drawio_invalid_xml", f"Cannot parse Draw.io XML: {exc}"
                ),
            ),
        )

    candidates: list[tuple[str | None, str, ET.Element]] = []
    if _local_name(root.tag) == "mxGraphModel":
        candidates.append((None, "Page 1", root))
    elif _local_name(root.tag) == "mxfile":
        diagrams = [child for child in root if _local_name(child.tag) == "diagram"]
        for index, diagram in enumerate(diagrams, start=1):
            name = diagram.get("name") or f"Page {index}"
            try:
                candidates.append((diagram.get("id"), name, _diagram_model(diagram)))
            except (ValueError, ET.ParseError, UnicodeError) as exc:
                diagnostics.append(
                    DrawioDiagnostic(
                        "drawio_page_decode_failed",
                        f"Cannot decode diagram page {name!r}: {exc}",
                        page=name,
                    )
                )
    else:
        diagnostics.append(
            DrawioDiagnostic(
                "drawio_unexpected_root",
                f"Unexpected Draw.io root element: {_local_name(root.tag)}",
            )
        )

    pages = [
        _parse_page(model, page_id=page_id, name=name)
        for page_id, name, model in candidates
    ]
    for page in pages:
        if not page.nodes and not page.edges:
            diagnostics.append(
                DrawioDiagnostic(
                    "drawio_empty_page",
                    f"Diagram page {page.name!r} contains no nodes or edges",
                    page=page.name,
                )
            )
            continue
        node_ids = {node.id for node in page.nodes}
        for edge in page.edges:
            point_roles = {point.role for point in edge.points}
            source_available = bool(edge.source or "sourcePoint" in point_roles)
            target_available = bool(edge.target or "targetPoint" in point_roles)
            if not source_available or not target_available:
                diagnostics.append(
                    DrawioDiagnostic(
                        "drawio_edge_endpoint_missing",
                        f"Edge {edge.id!r} has no source or target",
                        page=page.name,
                    )
                )
                continue
            missing = [
                endpoint
                for endpoint in (edge.source, edge.target)
                if endpoint is not None and endpoint not in node_ids
            ]
            if missing:
                diagnostics.append(
                    DrawioDiagnostic(
                        "drawio_edge_endpoint_unresolved",
                        (
                            f"Edge {edge.id!r} refers to unknown node(s): "
                            + ", ".join(missing)
                        ),
                        page=page.name,
                    )
                )

    if not pages and not diagnostics:
        diagnostics.append(
            DrawioDiagnostic("drawio_no_pages", "Draw.io document contains no pages")
        )
    return DrawioDocument(pages=tuple(pages), diagnostics=tuple(diagnostics))


def render_drawio_text(document: DrawioDocument) -> str:
    """Render a Draw.io graph as deterministic Markdown node and edge lists."""

    sections: list[str] = []
    for page in document.pages:
        lines = [f"#### Draw.io page: {page.name}", "", "Nodes:"]
        if page.nodes:
            for node in page.nodes:
                label = node.label or "[unlabelled node]"
                details: list[str] = []
                geometry = node.geometry
                if any((geometry.x, geometry.y, geometry.width, geometry.height)):
                    details.append(
                        "geometry="
                        + ",".join(
                            value or ""
                            for value in (
                                geometry.x,
                                geometry.y,
                                geometry.width,
                                geometry.height,
                            )
                        )
                    )
                if node.link:
                    details.append(f"link={node.link}")
                if node.parent:
                    details.append(f"parent={node.parent}")
                if node.style:
                    details.append(f"style={node.style}")
                suffix = f" ({'; '.join(details)})" if details else ""
                lines.append(f"- `{node.id}`: {label}{suffix}")
        else:
            lines.append("- [no nodes]")
        lines.extend(("", "Connections:"))
        if page.edges:
            for edge in page.edges:
                source_point = next(
                    (point for point in edge.points if point.role == "sourcePoint"),
                    None,
                )
                target_point = next(
                    (point for point in edge.points if point.role == "targetPoint"),
                    None,
                )
                source = edge.source or (
                    f"point({source_point.x},{source_point.y})"
                    if source_point is not None
                    else "[missing source]"
                )
                target = edge.target or (
                    f"point({target_point.x},{target_point.y})"
                    if target_point is not None
                    else "[missing target]"
                )
                label = f": {edge.label}" if edge.label else ""
                link = f" (link={edge.link})" if edge.link else ""
                attributes: list[str] = []
                if edge.parent:
                    attributes.append(f"parent={edge.parent}")
                if edge.style:
                    attributes.append(f"style={edge.style}")
                waypoints = [point for point in edge.points if point.role == "waypoint"]
                if waypoints:
                    attributes.append(
                        "waypoints="
                        + ";".join(f"{point.x},{point.y}" for point in waypoints)
                    )
                attribute_suffix = f" ({'; '.join(attributes)})" if attributes else ""
                lines.append(
                    f"- `{edge.id}`: `{source}` -> `{target}`{label}{link}{attribute_suffix}"
                )
        else:
            lines.append("- [no connections]")
        sections.append("\n".join(lines))
    return "\n\n".join(sections).strip()
