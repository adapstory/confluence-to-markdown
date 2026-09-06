from __future__ import annotations

import json
import shutil
import traceback
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bs4 import Tag

from .diagnostics import REPORT_FILENAME, IssueCollector, error_marker, write_report
from .discovery import discover_pages, load_page_body
from .html_renderer import HtmlRenderer
from .links import LinkResolver
from .models import Issue, PageDescriptor, PageResult, RunStats
from .resources import ResourceProcessor


class ConfigurationError(RuntimeError):
    pass


OUTPUT_MARKER_FILENAME = ".confluence-to-markdown-output.json"


@dataclass(frozen=True, slots=True)
class ConversionOptions:
    source_root: Path
    output_root: Path
    ocr_language: str = "rus+eng"
    ocr_engine: str = "rapidocr"
    rapidocr_model_dir: Path = Path("models/rapidocr")


@dataclass(frozen=True, slots=True)
class ConversionOutcome:
    report_path: Path
    issues: IssueCollector
    stats: RunStats
    pages: tuple[PageResult, ...]


def convert_export(options: ConversionOptions) -> ConversionOutcome:
    _validate_ocr_options(options)
    source_root, output_root = _prepare_paths(options)
    started_at = datetime.now(UTC)
    issues = IssueCollector()
    stats = RunStats()
    discovery = discover_pages(source_root, issues)
    if not discovery.pages:
        raise ConfigurationError("No export pages with #main-content were found at the input root")
    stats.discovered_pages = len(discovery.pages)
    _validate_page_outputs(discovery.pages)
    resolver = LinkResolver(
        source_root,
        discovery.pages,
        index_target=discovery.index_target,
    )
    resources = ResourceProcessor(
        source_root,
        output_root,
        resolver,
        issues,
        stats,
        {
            REPORT_FILENAME,
            OUTPUT_MARKER_FILENAME,
            *(page.output_relative for page in discovery.pages),
        },
        ocr_language=options.ocr_language,
        ocr_engine=options.ocr_engine,
        rapidocr_model_dir=options.rapidocr_model_dir,
    )
    resources.copy_attachment_tree()
    page_results: list[PageResult] = []

    for page in discovery.pages:
        result = PageResult(page.source_relative, page.output_relative, page.title)
        if page.source_relative in discovery.failed_page_sources:
            _write_discovery_failure_page(page, issues, stats, output_root)
        else:
            try:
                _convert_page(page, resolver, resources, issues, stats, result, output_root)
            except Exception as exc:  # noqa: BLE001 - each page must fail independently.
                issue = issues.error(
                    "PAGE_CONVERSION_FAILED",
                    f"Unexpected page conversion failure: {type(exc).__name__}: {exc}",
                    page=page.source_relative,
                    exception_type=type(exc).__name__,
                    traceback_lines=_safe_traceback(exc),
                )
                destination = output_root / page.output_relative
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(
                        f"# {page.title}\n" + error_marker(issue),
                        encoding="utf-8",
                    )
                except OSError as write_exc:
                    issues.error(
                        "PAGE_OUTPUT_WRITE_FAILED",
                        f"Cannot write failed-page marker: {write_exc}",
                        page=page.source_relative,
                    )
        page_results.append(result)

    resources.cleanup()
    _ensure_page_error_markers(output_root, discovery.pages, issues)
    for result in page_results:
        result.error_count = sum(
            issue.page == result.source for issue in issues.errors
        )
        result.warning_count = sum(
            issue.page == result.source for issue in issues.warnings
        )
    report_path = write_report(
        output_root,
        source_root,
        issues,
        stats,
        page_results,
        started_at=started_at,
        ocr_language=options.ocr_language,
        ocr_engine=options.ocr_engine,
        rapidocr_model_dir=options.rapidocr_model_dir,
    )
    return ConversionOutcome(report_path, issues, stats, tuple(page_results))


def _validate_ocr_options(options: ConversionOptions) -> None:
    if options.ocr_engine not in {"rapidocr", "tesseract"}:
        raise ConfigurationError(f"unsupported OCR engine: {options.ocr_engine}")
    languages = tuple(part for part in options.ocr_language.split("+") if part)
    if not languages:
        raise ConfigurationError("at least one OCR language is required")
    if options.ocr_engine == "rapidocr":
        unsupported = sorted(set(languages) - {"rus", "eng"})
        if unsupported:
            raise ConfigurationError(
                "bundled RapidOCR model supports only rus and eng: "
                + ", ".join(unsupported)
            )


def _safe_traceback(exc: Exception) -> tuple[str, ...]:
    frames = traceback.extract_tb(exc.__traceback__)
    return tuple(
        f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"
        for frame in frames[-20:]
    )


def _write_discovery_failure_page(
    page: PageDescriptor,
    issues: IssueCollector,
    stats: RunStats,
    output_root: Path,
) -> None:
    markers = "".join(
        error_marker(issue)
        for issue in issues.errors
        if issue.page == page.source_relative
    )
    destination = output_root / page.output_relative
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(f"# {page.title}\n{markers}", encoding="utf-8")
    except OSError as exc:
        issues.error(
            "PAGE_OUTPUT_WRITE_FAILED",
            f"Cannot write failed-page marker: {exc}",
            page=page.source_relative,
        )
        return
    stats.converted_pages += 1


def _ensure_page_error_markers(
    output_root: Path,
    pages: list[PageDescriptor],
    issues: IssueCollector,
) -> None:
    """Enforce the ADR invariant that every page error is visible in its Markdown."""

    errors_by_page: dict[str, list[Issue]] = {}
    for issue in issues.errors:
        if issue.page is not None:
            errors_by_page.setdefault(issue.page, []).append(issue)

    for page in pages:
        page_errors = errors_by_page.get(page.source_relative, [])
        if not page_errors:
            continue
        destination = output_root / page.output_relative
        try:
            markdown = destination.read_text(encoding="utf-8")
        except OSError:
            continue
        missing = [
            issue
            for issue in page_errors
            if f"CONVERSION ERROR `{issue.id}`" not in markdown
        ]
        if not missing:
            continue
        suffix = "\n## Conversion diagnostics\n" + "".join(
            error_marker(issue) for issue in missing
        )
        try:
            destination.write_text(markdown.rstrip() + "\n" + suffix, encoding="utf-8")
        except OSError as exc:
            issues.error(
                "PAGE_OUTPUT_WRITE_FAILED",
                f"Cannot append conversion diagnostics: {exc}",
                page=page.source_relative,
            )


def _prepare_paths(options: ConversionOptions) -> tuple[Path, Path]:
    source = options.source_root.expanduser()
    output = options.output_root.expanduser()
    try:
        if not source.exists() or not source.is_dir() or source.is_symlink():
            raise ConfigurationError("source_root must be an existing real directory")
        source = source.resolve()
        if output.exists() and output.is_symlink():
            raise ConfigurationError("output must not be a symbolic link")
        output = output.resolve(strict=False)
    except OSError as exc:
        raise ConfigurationError(f"cannot inspect input or output path: {exc}") from exc

    if (
        output == source
        or output.is_relative_to(source)
        or source.is_relative_to(output)
    ):
        raise ConfigurationError("output and source_root must be disjoint directories")
    try:
        if output.exists():
            if not output.is_dir():
                raise ConfigurationError("output exists and is not a directory")
            try:
                next(output.iterdir())
            except StopIteration:
                pass
            else:
                _validate_previous_output(source, output)
                _clear_previous_output(output)
        else:
            output.mkdir(parents=True)
        _write_output_marker(source, output)
    except OSError as exc:
        raise ConfigurationError(f"cannot prepare output directory: {exc}") from exc
    return source, output


def _validate_previous_output(source: Path, output: Path) -> None:
    marker_path = output / OUTPUT_MARKER_FILENAME
    report_path = output / REPORT_FILENAME
    metadata_path = marker_path if marker_path.is_file() else report_path
    if not metadata_path.is_file():
        raise ConfigurationError(
            "non-empty output is not recognized as a previous confluence-to-md result"
        )
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            f"cannot validate previous output metadata {metadata_path.name}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("previous output metadata must be a JSON object")

    if metadata_path == marker_path:
        if payload.get("tool") != "confluence-to-markdown":
            raise ConfigurationError("output ownership marker belongs to another tool")
    elif not (
        payload.get("schemaVersion") in {1, 2}
        and isinstance(payload.get("summary"), dict)
        and isinstance(payload.get("pages"), list)
        and isinstance(payload.get("errors"), list)
    ):
        raise ConfigurationError(
            "non-empty output report is not a recognized confluence-to-md report"
        )

    recorded_source = payload.get("sourceRoot")
    recorded_output = payload.get("outputRoot")
    if not isinstance(recorded_source, str) or not isinstance(recorded_output, str):
        raise ConfigurationError("previous output metadata has invalid root paths")
    if Path(recorded_source).expanduser().resolve(strict=False) != source:
        raise ConfigurationError("previous output was produced from a different source_root")
    if Path(recorded_output).expanduser().resolve(strict=False) != output:
        raise ConfigurationError("previous output metadata does not match output path")


def _clear_previous_output(output: Path) -> None:
    for child in output.iterdir():
        if child.is_symlink() or not child.is_dir():
            child.unlink()
        else:
            shutil.rmtree(child)


def _write_output_marker(source: Path, output: Path) -> None:
    payload = {
        "schemaVersion": 1,
        "tool": "confluence-to-markdown",
        "sourceRoot": str(source),
        "outputRoot": str(output),
    }
    (output / OUTPUT_MARKER_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _validate_page_outputs(pages: list[PageDescriptor]) -> None:
    seen: dict[str, str] = {}
    for page in pages:
        key = unicodedata.normalize("NFC", page.output_relative).casefold()
        previous = seen.get(key)
        if previous is not None:
            raise ConfigurationError(
                f"page output collision after case and Unicode normalization: {previous!r}, {page.output_relative!r}"
            )
        seen[key] = page.output_relative


def _convert_page(
    page: PageDescriptor,
    resolver: LinkResolver,
    resources: ResourceProcessor,
    issues: IssueCollector,
    stats: RunStats,
    result: PageResult,
    output_root: Path,
) -> None:
    _soup, body = load_page_body(page)
    drawio_assignments = _assign_drawio_sources(body, page, resolver)
    image_count = len([tag for tag in body.find_all("img") if isinstance(tag, Tag)])
    result.image_count = image_count

    renderer = HtmlRenderer(
        page,
        resolver,
        issues,
        stats,
        lambda tag, current: resources.render_image(
            tag,
            current,
            drawio_source=drawio_assignments.get(id(tag)),
        ),
    )
    markdown = renderer.render(body)
    attachment_markdown, attachment_count = resources.render_attachment_sections(
        page,
        renderer.attachment_references,
    )
    result.attachment_count = attachment_count
    destination = output_root / page.output_relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(markdown.rstrip() + "\n" + attachment_markdown, encoding="utf-8")
    stats.converted_pages += 1


def _assign_drawio_sources(
    body: Tag,
    page: PageDescriptor,
    resolver: LinkResolver,
) -> dict[int, str]:
    images = [
        image
        for image in body.select("img.drawio-diagram-image")
        if isinstance(image, Tag)
    ]
    if not images:
        return {}

    references = _drawio_links(body, page, resolver)
    assignments: dict[int, str] = {}
    for image in images:
        local_reference = _nearest_drawio_link(image, body, page, resolver)
        alias = unquote(str(image.get("data-linked-resource-default-alias", ""))).strip()
        alias_matches = [
            reference
            for reference in references
            if Path(unquote(reference)).name == alias
        ] if alias else []
        reference = (
            local_reference
            or (alias_matches[0] if len(alias_matches) == 1 else None)
            or _attachment_source_by_alias(alias, page, resolver)
        )
        if reference is not None:
            assignments[id(image)] = reference
    return assignments


def _drawio_links(
    container: Tag,
    page: PageDescriptor,
    resolver: LinkResolver,
) -> list[str]:
    references: list[str] = []
    seen: set[str] = set()
    for link in container.find_all("a", href=True):
        href = str(link.get("href", ""))
        if not unquote(urlsplit(href).path).casefold().endswith(".drawio"):
            continue
        resolution = resolver.resolve(href, page)
        if (
            resolution.kind == "resource"
            and resolution.source_relative
            and resolution.source_relative not in seen
        ):
            seen.add(resolution.source_relative)
            references.append(resolution.source_relative)
    return references


def _nearest_drawio_link(
    image: Tag,
    body: Tag,
    page: PageDescriptor,
    resolver: LinkResolver,
) -> str | None:
    for parent in image.parents:
        if not isinstance(parent, Tag):
            continue
        references = _drawio_links(parent, page, resolver)
        if len(references) == 1:
            return references[0]
        if references or parent is body:
            return None
    return None


def _attachment_source_by_alias(
    alias: str,
    page: PageDescriptor,
    resolver: LinkResolver,
) -> str | None:
    if not alias or Path(alias).name != alias:
        return None
    attachment_root = resolver.source_root / "attachments"
    if page.page_id and (attachment_root / page.page_id).is_dir():
        attachment_root = attachment_root / page.page_id
    if not attachment_root.is_dir() or attachment_root.is_symlink():
        return None
    matches = [
        candidate
        for candidate in attachment_root.rglob("*")
        if candidate.is_file()
        and not candidate.is_symlink()
        and unquote(candidate.name) == alias
    ]
    if len(matches) != 1:
        return None
    return matches[0].relative_to(resolver.source_root).as_posix()
