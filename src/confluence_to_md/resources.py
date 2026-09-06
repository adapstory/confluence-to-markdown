from __future__ import annotations

import base64
import hashlib
import mimetypes
import posixpath
import re
import shutil
import unicodedata
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote

from bs4 import Tag

from .attachments import AttachmentDiagnostic, AttachmentExtraction, extract_attachment
from .diagnostics import IssueCollector, error_marker
from .drawio import parse_drawio, render_drawio_text
from .links import LinkResolver
from .models import PageDescriptor, RunStats
from .ocr import create_ocr_backend

RASTER_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
IMAGE_SUFFIXES = RASTER_IMAGE_SUFFIXES | {".svg"}
SEMANTIC_EMOTICONS = {
    "emoticon-plus": "➕",
    "emoticon-question": "❓",
    "emoticon-minus": "➖",
    "emoticon-smile": "🙂",
}


def _class_names(tag: Tag) -> set[str]:
    raw = tag.get("class")
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {raw.lower()}
    return {str(value).lower() for value in raw}


class ResourceProcessor:
    def __init__(
        self,
        source_root: Path,
        output_root: Path,
        resolver: LinkResolver,
        issues: IssueCollector,
        stats: RunStats,
        reserved_output_paths: set[str],
        *,
        ocr_language: str,
        ocr_engine: str,
        rapidocr_model_dir: Path,
    ) -> None:
        self.source_root = source_root.resolve()
        self.output_root = output_root.resolve()
        self.resolver = resolver
        self.issues = issues
        self.stats = stats
        languages = tuple(part for part in ocr_language.split("+") if part)
        self.ocr = create_ocr_backend(
            ocr_engine,
            languages=languages or ("rus", "eng"),
            rapidocr_model_dir=rapidocr_model_dir,
        )
        self._reserved_outputs = {
            unicodedata.normalize("NFC", value).casefold() for value in reserved_output_paths
        }
        self.work_directory = self.output_root / ".work"
        self._copied: set[str] = set()
        self._extraction_cache: dict[str, AttachmentExtraction] = {}
        self._drawio_used: set[tuple[str, str]] = set()
        self._rendered_images: set[tuple[str, str]] = set()

    def copy_attachment_tree(self) -> None:
        root = self.source_root / "attachments"
        if not root.exists():
            return
        if root.is_symlink() or not root.is_dir():
            self.issues.error(
                "UNSAFE_ATTACHMENTS_ROOT",
                "The attachments entry must be a real directory inside the export root",
                reference="attachments",
            )
            return
        for source in sorted(root.rglob("*"), key=lambda path: path.as_posix().casefold()):
            if source.is_symlink():
                relative = source.relative_to(self.source_root).as_posix()
                self.issues.error(
                    "SYMLINK_RESOURCE",
                    "Symbolic-link resources are not followed",
                    reference=relative,
                )
                continue
            if source.is_file():
                self.ensure_copy(source.relative_to(self.source_root).as_posix())

    def render_image(
        self,
        tag: Tag,
        page: PageDescriptor,
        *,
        drawio_source: str | None = None,
    ) -> str:
        semantic_emoticon = self._semantic_emoticon(tag)
        if semantic_emoticon is not None:
            return semantic_emoticon
        if self._is_decorative(tag):
            return ""
        source_ref = str(tag.get("src", "")).strip()
        alt = str(tag.get("alt", "")).strip()
        is_drawio = "drawio-diagram-image" in _class_names(tag)
        if not source_ref:
            issue = self.issues.error(
                "IMAGE_SOURCE_MISSING",
                "A content image has no source reference",
                page=page.source_relative,
                element="img",
            )
            base = error_marker(issue)
            return base + (self._render_drawio(page, drawio_source) if is_drawio else "")

        local_source: Path | None = None
        output_href = source_ref
        resource_relative: str | None = None
        if source_ref.startswith("data:"):
            local_source, output_href, resource_relative, marker = self._materialize_data_image(source_ref, page)
            base_marker = marker
        else:
            resolution = self.resolver.resolve(source_ref, page)
            output_href = resolution.href
            resource_relative = resolution.source_relative
            base_marker = ""
            if resolution.problem_code:
                issue = self.issues.error(
                    resolution.problem_code,
                    resolution.problem_message or "Image resource could not be resolved",
                    page=page.source_relative,
                    reference=source_ref,
                    element="img",
                )
                base_marker = error_marker(issue)
            if resolution.kind == "resource" and resource_relative:
                local_source = self.ensure_copy(resource_relative, page=page.source_relative)
                self._rendered_images.add((page.source_relative, resource_relative))
            elif resolution.kind == "external":
                issue = self.issues.error(
                    "REMOTE_IMAGE_NOT_RETAINED",
                    "A remote image cannot be retained for offline text-only consumption",
                    page=page.source_relative,
                    reference=source_ref,
                    element="img",
                )
                base_marker += error_marker(issue)

        display_alt = alt if alt and alt != source_ref else (Path(unquote(source_ref)).name or "image")
        image_markdown = f"\n![{self._escape_label(display_alt)}](<{self._destination(output_href)}>)\n\n"
        parts = [image_markdown, base_marker]

        if is_drawio:
            self.stats.increment_macro("drawio")
            parts.append(self._render_drawio(page, drawio_source))
            return "".join(parts)

        if local_source is None:
            return "".join(parts)

        extraction = self._extract(resource_relative or local_source.as_posix(), local_source)
        markers = self._record_diagnostics(extraction.diagnostics, page, source_ref, element="img")
        if extraction.extracted and extraction.text.strip():
            if extraction.kind == "svg":
                label = "SVG text and vector structure"
            else:
                self.stats.ocr_images += 1
                confidence = extraction.metadata_dict().get("ocr_mean_confidence", "")
                detail = f"; mean confidence {confidence}" if confidence else ""
                label = f"Image text (local OCR, {self._escape_label('+'.join(getattr(self.ocr, 'languages', ())))}{detail})"
            parts.append(f"**{label}:**\n\n" + self._quote_text(extraction.text) + "\n\n")
        elif not markers:
            issue = self.issues.error(
                "UNREADABLE_CONTENT_IMAGE",
                "A meaningful image produced no reliable local text",
                page=page.source_relative,
                reference=source_ref,
                element="img",
            )
            markers.append(error_marker(issue))
        parts.extend(markers)

        if self._is_image_only_diagram(tag, source_ref):
            issue = self.issues.error(
                "IMAGE_ONLY_DIAGRAM",
                "A diagram image has no parseable source model; OCR cannot preserve its relationships",
                page=page.source_relative,
                reference=source_ref,
                element="img",
            )
            parts.append(error_marker(issue))
        return "".join(parts)

    def render_attachment_sections(
        self,
        page: PageDescriptor,
        references: list[str],
    ) -> tuple[str, int]:
        sections: list[str] = []
        count = 0
        for reference in references:
            if (page.source_relative, reference) in self._drawio_used:
                continue
            if (page.source_relative, reference) in self._rendered_images:
                continue
            source = self.ensure_copy(reference, page=page.source_relative)
            if source is None:
                continue
            display_name = PurePosixPath(reference).name
            output_href = self.resolver.output_resource_href(reference, page)
            section = [
                f"\n## Attachment: {self._escape_label(display_name)}\n\n",
                f"Original: [{self._escape_label(display_name)}](<{self._destination(output_href)}>)\n\n",
            ]
            extraction = self._extract(reference, source)
            markers = self._record_diagnostics(
                extraction.diagnostics,
                page,
                reference,
                element="attachment",
            )
            if extraction.extracted and extraction.text.strip():
                if extraction.kind == "drawio":
                    section.append(extraction.text.strip() + "\n\n")
                    self.stats.drawio_models += 1
                elif extraction.kind in {"image", "svg"}:
                    label = (
                        "SVG text and vector structure"
                        if extraction.kind == "svg"
                        else "Image text (local OCR)"
                    )
                    section.append(
                        f"![{self._escape_label(display_name)}](<{self._destination(output_href)}>)\n\n"
                        f"**{label}:**\n\n"
                        + self._quote_text(extraction.text)
                        + "\n\n"
                    )
                    if extraction.kind == "image":
                        self.stats.ocr_images += 1
                else:
                    section.append(self._fenced(extraction.text, extraction.kind) + "\n\n")
                self.stats.extracted_attachments += 1
            elif not markers:
                issue = self.issues.error(
                    "ATTACHMENT_TEXT_EMPTY",
                    "A supported attachment produced no text",
                    page=page.source_relative,
                    reference=reference,
                    element="attachment",
                )
                markers.append(error_marker(issue))
            section.extend(markers)
            sections.append("".join(section))
            count += 1
        return "".join(sections), count

    def ensure_copy(self, source_relative: str, *, page: str | None = None) -> Path | None:
        normalized = posixpath.normpath(unquote(source_relative).replace("\\", "/"))
        if normalized == ".." or normalized.startswith(("../", "/")):
            self.issues.error(
                "UNSAFE_RESOURCE_PATH",
                "A resource path escapes the export root",
                page=page,
                reference=source_relative,
            )
            return None
        source = self.source_root / PurePosixPath(normalized)
        try:
            resolved_source = source.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not resolved_source.is_relative_to(self.source_root) or source.is_symlink() or not source.is_file():
            self.issues.error(
                "UNSAFE_RESOURCE_PATH",
                "A resource is not a regular file contained by the export root",
                page=page,
                reference=source_relative,
            )
            return None
        destination = self.output_root / PurePosixPath(normalized)
        collision_key = unicodedata.normalize("NFC", normalized).casefold()
        if collision_key in self._reserved_outputs:
            self.issues.error(
                "OUTPUT_PATH_COLLISION",
                "A retained resource would overwrite a Markdown page",
                page=page,
                reference=source_relative,
            )
            return None
        if normalized not in self._copied:
            if destination.exists():
                self.issues.error(
                    "OUTPUT_PATH_COLLISION",
                    "A retained resource would overwrite an existing output file",
                    page=page,
                    reference=source_relative,
                )
                return None
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            except OSError as exc:
                self.issues.error(
                    "RESOURCE_COPY_FAILED",
                    f"Cannot retain resource: {exc}",
                    page=page,
                    reference=source_relative,
                )
                return None
            self._copied.add(normalized)
            self.stats.copied_resources += 1
        return source

    def cleanup(self) -> None:
        try:
            self.work_directory.rmdir()
        except OSError:
            pass

    def _render_drawio(self, page: PageDescriptor, source_reference: str | None) -> str:
        if not source_reference:
            issue = self.issues.error(
                "DRAWIO_SOURCE_MISSING",
                "A rendered Draw.io diagram has no referenced .drawio source",
                page=page.source_relative,
                element="img",
            )
            return error_marker(issue)
        source = self.ensure_copy(source_reference, page=page.source_relative)
        output_href = self.resolver.output_resource_href(source_reference, page)
        self._drawio_used.add((page.source_relative, source_reference))
        if source is None:
            issue = self.issues.error(
                "DRAWIO_SOURCE_MISSING",
                "The referenced Draw.io source is absent from the export",
                page=page.source_relative,
                reference=source_reference,
                element="img",
            )
            return error_marker(issue)
        document = parse_drawio(source)
        markers: list[str] = []
        for diagnostic in document.diagnostics:
            if diagnostic.severity == "warning":
                self.issues.warning(
                    diagnostic.code,
                    diagnostic.message,
                    page=page.source_relative,
                    reference=source_reference,
                    element="drawio",
                )
            else:
                issue = self.issues.error(
                    diagnostic.code,
                    diagnostic.message,
                    page=page.source_relative,
                    reference=source_reference,
                    element="drawio",
                )
                markers.append(error_marker(issue))
        model = render_drawio_text(document)
        if model:
            self.stats.drawio_models += 1
        elif not markers:
            issue = self.issues.error(
                "DRAWIO_MODEL_EMPTY",
                "Draw.io source produced no nodes or connections",
                page=page.source_relative,
                reference=source_reference,
                element="drawio",
            )
            markers.append(error_marker(issue))
        return (
            (model + "\n\n" if model else "")
            + f"**Draw.io source:** [{self._escape_label(PurePosixPath(source_reference).name)}](<{self._destination(output_href)}>)\n\n"
            + "".join(markers)
        )

    def _extract(self, cache_key: str, source: Path) -> AttachmentExtraction:
        cached = self._extraction_cache.get(cache_key)
        if cached is not None:
            return cached
        extraction = extract_attachment(
            source,
            ocr_backend=self.ocr,
            work_directory=self.work_directory,
        )
        self._extraction_cache[cache_key] = extraction
        return extraction

    def _record_diagnostics(
        self,
        diagnostics: tuple[AttachmentDiagnostic, ...],
        page: PageDescriptor,
        reference: str,
        *,
        element: str,
    ) -> list[str]:
        markers: list[str] = []
        for diagnostic in diagnostics:
            if diagnostic.severity == "warning":
                self.issues.warning(
                    diagnostic.code,
                    diagnostic.message,
                    page=page.source_relative,
                    reference=reference,
                    element=element,
                )
            else:
                issue = self.issues.error(
                    diagnostic.code,
                    diagnostic.message,
                    page=page.source_relative,
                    reference=reference,
                    element=element,
                )
                markers.append(error_marker(issue))
        return markers

    def _materialize_data_image(
        self,
        source_ref: str,
        page: PageDescriptor,
    ) -> tuple[Path | None, str, str | None, str]:
        match = re.fullmatch(r"data:([^;,]+)(;base64)?,(.*)", source_ref, flags=re.DOTALL)
        if not match or not match.group(2):
            issue = self.issues.error(
                "UNSUPPORTED_DATA_IMAGE",
                "Only base64-encoded data images can be retained",
                page=page.source_relative,
                element="img",
            )
            return None, source_ref, None, error_marker(issue)
        mime_type = match.group(1).lower()
        try:
            payload = base64.b64decode(match.group(3), validate=True)
        except ValueError:
            issue = self.issues.error(
                "INVALID_DATA_IMAGE",
                "Embedded image data is not valid base64",
                page=page.source_relative,
                element="img",
            )
            return None, source_ref, None, error_marker(issue)
        suffix = mimetypes.guess_extension(mime_type) or ".bin"
        digest = hashlib.sha256(payload).hexdigest()
        relative = f"resources/embedded/{digest}{suffix}"
        destination = self.output_root / PurePosixPath(relative)
        if relative not in self._copied:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            self._copied.add(relative)
            self.stats.copied_resources += 1
        href = posixpath.relpath(relative, posixpath.dirname(page.output_relative) or ".")
        return destination, quote(href, safe="/._~-"), relative, ""

    @staticmethod
    def _semantic_emoticon(tag: Tag) -> str | None:
        class_names = _class_names(tag)
        for class_name, replacement in SEMANTIC_EMOTICONS.items():
            if class_name in class_names:
                return replacement
        return None

    @staticmethod
    def _is_decorative(tag: Tag) -> bool:
        class_names = _class_names(tag)
        if class_names & {"confluence-information-macro-icon", "ht-icon", "aui-icon"}:
            return True
        source_ref = str(tag.get("src", "")).strip().lower()
        alt = str(tag.get("alt", "")).strip()
        parent = tag.parent
        if isinstance(parent, Tag):
            parent_classes = _class_names(parent)
            if parent_classes & {"admonition-icon", "confluence-information-macro-icon"}:
                return True
            if (
                "jira-issue-key" in parent_classes
                and "icon" in class_names
                and (
                    "viewavatar" in source_ref
                    or "issuetype" in source_ref
                    or alt == source_ref
                )
            ):
                return True
        return False

    @staticmethod
    def _is_image_only_diagram(tag: Tag, source_ref: str) -> bool:
        ancestor_classes: set[str] = set()
        parent = tag.parent
        while isinstance(parent, Tag):
            ancestor_classes.update(_class_names(parent))
            if parent.get("id") == "main-content":
                break
            parent = parent.parent
        searchable = " ".join(
            (
                *sorted(_class_names(tag) | ancestor_classes),
                unquote(source_ref).casefold(),
                str(tag.get("alt", "")).casefold(),
                str(tag.get("title", "")).casefold(),
            )
        )
        return any(
            signal in searchable
            for signal in (
                "diagram",
                "gliffy",
                "graphviz",
                "mermaid",
                "plantuml",
                "roadmap",
                "диаграм",
                "схем",
                "граф",
                "дорожн",
            )
        )

    @staticmethod
    def _escape_label(value: str) -> str:
        return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")

    @staticmethod
    def _destination(value: str) -> str:
        return value.replace("\\", "%5C").replace(">", "%3E").replace("\r", "").replace("\n", "")

    @staticmethod
    def _quote_text(value: str) -> str:
        return "\n".join("> " + line if line else ">" for line in value.strip().splitlines())

    @staticmethod
    def _fenced(value: str, language: str) -> str:
        longest = max((len(match) for match in re.findall(r"`+", value)), default=0)
        fence = "`" * max(3, longest + 1)
        safe_language = re.sub(r"[^A-Za-z0-9_+-]", "", language)
        return f"{fence}{safe_language}\n{value.rstrip()}\n{fence}"
