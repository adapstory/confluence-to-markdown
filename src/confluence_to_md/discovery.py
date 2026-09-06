from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from .diagnostics import IssueCollector
from .models import PageDescriptor

PAGE_ID_PATTERN = re.compile(r"^src-(\d+)(?:_|$)")
HTML_PAGE_SUFFIXES = {".html", ".htm", ".xhtml"}
NON_PAGE_ROOTS = {"assets", "attachments", "images"}
NON_CONTENT_ANCHOR_TAGS = {"noscript", "script", "style", "template"}


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    pages: list[PageDescriptor]
    failed_page_sources: frozenset[str]
    index_target: str | None
    ignored_shell_files: tuple[str, ...]


def discover_pages(source_root: Path, issues: IssueCollector) -> DiscoveryResult:
    pages: list[PageDescriptor] = []
    failed_page_sources: set[str] = set()
    ignored: list[str] = []
    for path in sorted(source_root.rglob("*"), key=lambda candidate: candidate.relative_to(source_root).as_posix().casefold()):
        if path.is_symlink() and path.is_dir():
            relative = path.relative_to(source_root).as_posix()
            issues.error(
                "SYMLINK_DIRECTORY",
                "Symbolic-link directories are not followed during export discovery",
                reference=relative,
            )
    html_files = sorted(
        (
            path
            for path in source_root.rglob("*")
            if path.is_file()
            and path.suffix.casefold() in HTML_PAGE_SUFFIXES
            and not _is_resource_html(path, source_root)
        ),
        key=lambda path: path.relative_to(source_root).as_posix().casefold(),
    )
    for path in html_files:
        relative = path.relative_to(source_root).as_posix()
        if path.is_symlink():
            issues.error(
                "SYMLINK_PAGE",
                "Symbolic-link HTML pages are not followed",
                page=relative,
            )
            pages.append(_failed_page(path, source_root))
            failed_page_sources.add(relative)
            continue
        try:
            html = _read_html(path)
        except UnicodeDecodeError as exc:
            issues.error(
                "UNREADABLE_HTML",
                f"HTML page is not valid UTF-8: {exc}",
                page=relative,
            )
            pages.append(_failed_page(path, source_root))
            failed_page_sources.add(relative)
            continue
        except OSError as exc:
            issues.error("UNREADABLE_HTML", f"Cannot read HTML page: {exc}", page=relative)
            pages.append(_failed_page(path, source_root))
            failed_page_sources.add(relative)
            continue

        soup = BeautifulSoup(html, "html.parser")
        body = soup.select_one("#main-content")
        if body is None:
            is_navigation_tree = bool(soup.select_one("a.ht-nav-page-link[data-destpageid]"))
            is_redirect = bool(
                soup.select_one("link[rel='canonical'][href]")
                or soup.select_one("meta[http-equiv='refresh'][content]")
            )
            if is_navigation_tree or (
                is_redirect
                and (
                    not _has_meaningful_visible_body(soup)
                    or _is_export_index_redirect(path, soup)
                )
            ):
                ignored.append(relative)
                continue
            issues.error(
                "MISSING_PAGE_CONTENT",
                "HTML file has no #main-content and cannot be classified as an export page",
                page=relative,
            )
            pages.append(_failed_page(path, source_root, soup=soup))
            failed_page_sources.add(relative)
            continue

        title_node = soup.select_one("header.ht-content-header h1") or body.find("h1")
        title = title_node.get_text(" ", strip=True) if isinstance(title_node, Tag) else ""
        if not title:
            title = path.stem
            issues.error(
                "MISSING_PAGE_TITLE",
                "Page title was absent; the filename stem was used",
                page=relative,
            )
        title_anchor = str(title_node.get("id")) if isinstance(title_node, Tag) and title_node.get("id") else None
        anchors = _collect_anchors(body)
        if title_anchor:
            anchors.add(title_anchor)
        page_id = _find_page_id(soup, anchors)
        output_relative = path.with_suffix(".md").relative_to(source_root).as_posix()
        pages.append(
            PageDescriptor(
                source_path=path,
                source_relative=relative,
                output_relative=output_relative,
                title=title,
                title_anchor=title_anchor,
                anchors=frozenset(anchors),
                page_id=page_id,
            )
        )

    return DiscoveryResult(
        pages,
        frozenset(failed_page_sources),
        _discover_index_target(source_root),
        tuple(ignored),
    )


def load_page_body(page: PageDescriptor) -> tuple[BeautifulSoup, Tag]:
    html = _read_html(page.source_path)
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("#main-content")
    if body is None:
        raise ValueError("page no longer contains #main-content")
    return soup, body


def _read_html(path: Path) -> str:
    """Decode exporter HTML consistently, accepting an optional UTF-8 BOM."""

    return path.read_text(encoding="utf-8-sig")


def _collect_anchors(body: Tag) -> set[str]:
    anchors: set[str] = set()
    for node in (body, *body.find_all(True)):
        if node.name in NON_CONTENT_ANCHOR_TAGS:
            continue
        if node.find_parent(tuple(NON_CONTENT_ANCHOR_TAGS)) is not None:
            continue
        if node.get("id"):
            anchors.add(str(node.get("id")))
        if node.name == "a" and node.get("name"):
            anchors.add(str(node.get("name")))
    return anchors


def _find_page_id(soup: BeautifulSoup, anchors: set[str]) -> str | None:
    iframe = soup.select_one("iframe#ht-nav[src*='pageId=']")
    if iframe and iframe.get("src"):
        match = re.search(r"(?:\?|&)pageId=(\d+)", str(iframe.get("src")))
        if match:
            return match.group(1)
    for anchor in anchors:
        match = PAGE_ID_PATTERN.match(anchor)
        if match:
            return match.group(1)
    return None


def _has_meaningful_visible_body(soup: BeautifulSoup) -> bool:
    body = soup.body
    if body is None:
        return False
    for node in body.descendants:
        if isinstance(node, NavigableString) and str(node).strip():
            parent = node.parent
            if isinstance(parent, Tag) and (
                parent.name in NON_CONTENT_ANCHOR_TAGS
                or parent.find_parent(tuple(NON_CONTENT_ANCHOR_TAGS)) is not None
            ):
                continue
            return True
        if (
            isinstance(node, Tag)
            and node.name not in NON_CONTENT_ANCHOR_TAGS
            and node.name in {"img", "svg", "math", "object", "embed", "video", "audio"}
        ):
            return True
    return False


def _is_export_index_redirect(path: Path, soup: BeautifulSoup) -> bool:
    """Recognize Scroll's small, visible fallback page without hiding real content."""

    if path.name.casefold() != "index.html" or soup.body is None:
        return False
    canonical = soup.select_one("link[rel='canonical'][href]")
    target = unquote(str(canonical.get("href"))).strip() if canonical else ""
    links = soup.body.find_all("a", href=True)
    if not target or not links:
        return False
    if any(
        node.name not in {"a", "body", "div", "p", "span"}
        for node in (soup.body, *soup.body.find_all(True))
    ):
        return False
    if len(soup.body.get_text(" ", strip=True)) > 500:
        return False
    return all(unquote(str(link.get("href"))).strip() == target for link in links)


def _discover_index_target(source_root: Path) -> str | None:
    index_path = source_root / "index.html"
    if not index_path.is_file():
        return None
    try:
        soup = BeautifulSoup(_read_html(index_path), "html.parser")
    except (OSError, UnicodeDecodeError):
        return None
    canonical = soup.select_one("link[rel='canonical'][href]")
    if canonical:
        return unquote(str(canonical.get("href")))
    refresh = soup.select_one("meta[http-equiv='refresh'][content]")
    if refresh:
        match = re.search(r"url\s*=\s*(.+)$", str(refresh.get("content")), re.IGNORECASE)
        if match:
            return unquote(match.group(1).strip(" '\""))
    link = soup.find("a", href=True)
    return unquote(str(link.get("href"))) if link else None


def _failed_page(
    path: Path,
    source_root: Path,
    *,
    soup: BeautifulSoup | None = None,
) -> PageDescriptor:
    title_node = soup.select_one("title") if soup is not None else None
    title = title_node.get_text(" ", strip=True) if isinstance(title_node, Tag) else ""
    return PageDescriptor(
        source_path=path,
        source_relative=path.relative_to(source_root).as_posix(),
        output_relative=path.with_suffix(".md").relative_to(source_root).as_posix(),
        title=title or path.stem,
        title_anchor=None,
        anchors=frozenset(),
        page_id=None,
    )


def _is_resource_html(path: Path, source_root: Path) -> bool:
    parts = path.relative_to(source_root).parts
    if len(parts) < 2:
        return False
    first_part = parts[0].casefold()
    return first_part in NON_PAGE_ROOTS or first_part.endswith("_assets")
