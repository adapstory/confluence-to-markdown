from __future__ import annotations

import base64
import binascii
import posixpath
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, unquote_plus, urlsplit

from .models import PageDescriptor

EXTERNAL_SCHEMES = {"http", "https", "mailto", "tel", "ftp"}
UNSAFE_SCHEMES = {"javascript", "vbscript", "file"}


@dataclass(frozen=True, slots=True)
class LinkResolution:
    href: str
    kind: str
    problem_code: str | None = None
    problem_message: str | None = None
    source_relative: str | None = None


class LinkResolver:
    def __init__(
        self,
        source_root: Path,
        pages: list[PageDescriptor],
        *,
        index_target: str | None = None,
    ) -> None:
        self.source_root = source_root
        self._pages: dict[str, PageDescriptor] = {
            self._normalize(page.source_relative): page for page in pages
        }
        self._casefold_pages = {key.casefold(): value for key, value in self._pages.items()}
        self._index_target = self._lookup_page(self._index_source_target(index_target)) if index_target else None

    def resolve(self, href: str, current: PageDescriptor) -> LinkResolution:
        raw = href.strip()
        if not raw:
            return LinkResolution("", "empty")
        if raw.startswith("//"):
            return LinkResolution(raw, "external")

        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        if scheme in EXTERNAL_SCHEMES:
            return LinkResolution(raw, "external")
        if scheme in UNSAFE_SCHEMES or scheme == "data":
            return LinkResolution(
                raw,
                "unsafe",
                "UNSAFE_LINK",
                f"Unsafe link scheme cannot be retained: {scheme or 'unknown'}",
            )
        if scheme or parsed.netloc:
            return LinkResolution(raw, "external")

        fragment = unquote(parsed.fragment)
        decoded_path = unquote(parsed.path)
        if not decoded_path:
            if not fragment:
                return LinkResolution(raw, "empty")
            canonical_fragment = self._canonical_anchor(current, fragment)
            encoded_fragment = self._quote_fragment(canonical_fragment or fragment)
            if canonical_fragment is None:
                return LinkResolution(
                    f"#{encoded_fragment}",
                    "anchor",
                    "UNRESOLVED_ANCHOR",
                    f"The target anchor does not exist in this page: {fragment}",
                )
            return LinkResolution(f"#{encoded_fragment}", "anchor")

        if decoded_path.lstrip("/").casefold() == "pages/createpage.action":
            return LinkResolution(
                raw,
                "missing",
                "UNRESOLVED_INTERNAL_PAGE",
                "A Confluence create-page link has no exported page target",
            )

        source_target = self._join_source(current.source_relative, decoded_path)
        if source_target == ".." or source_target.startswith("../"):
            return LinkResolution(
                raw,
                "unsafe",
                "PATH_TRAVERSAL_REFERENCE",
                f"A local reference escapes the export root: {decoded_path}",
            )
        page = self._lookup_page(source_target)
        if page is None and self._normalize(source_target) == "index.html":
            page = self._index_target

        if page is not None:
            rel = posixpath.relpath(
                page.output_relative,
                posixpath.dirname(current.output_relative) or ".",
            )
            target = self._quote_path(rel)
            if fragment:
                canonical_fragment = self._canonical_anchor(page, fragment)
                target += "#" + self._quote_fragment(canonical_fragment or fragment)
                if canonical_fragment is None:
                    return LinkResolution(
                        target,
                        "page",
                        "UNRESOLVED_ANCHOR",
                        f"The target anchor does not exist in {page.source_relative}: {fragment}",
                        page.source_relative,
                    )
            return LinkResolution(target, "page", source_relative=page.source_relative)

        candidate = self.source_root / PurePosixPath(source_target)
        if candidate.is_file():
            rel = posixpath.relpath(
                source_target,
                posixpath.dirname(current.output_relative) or ".",
            )
            target = self._quote_path(rel)
            if fragment:
                target += "#" + self._quote_fragment(fragment)
            return LinkResolution(target, "resource", source_relative=source_target)

        looks_like_page = PurePosixPath(source_target).suffix.lower() in {
            ".html",
            ".htm",
            ".xhtml",
        }
        code = "UNRESOLVED_INTERNAL_PAGE" if looks_like_page else "MISSING_RESOURCE"
        noun = "internal page" if looks_like_page else "local resource"
        return LinkResolution(
            self._quote_path(decoded_path),
            "missing",
            code,
            f"Referenced {noun} is absent from the export: {decoded_path}",
            source_target,
        )

    def output_resource_href(
        self,
        source_relative: str,
        current: PageDescriptor,
    ) -> str:
        """Map a validated export-root-relative resource to the owning Markdown page."""

        normalized = self._normalize(unquote(source_relative))
        relative = posixpath.relpath(
            normalized,
            posixpath.dirname(current.output_relative) or ".",
        )
        return self._quote_path(relative)

    @classmethod
    def _canonical_anchor(
        cls,
        page: PageDescriptor,
        fragment: str,
    ) -> str | None:
        if fragment in page.anchors:
            return fragment
        alias_key = cls._scroll_safe_anchor_key(fragment)
        if alias_key is None:
            return None
        matches = [
            anchor
            for anchor in page.anchors
            if cls._scroll_safe_anchor_key(anchor) == alias_key
        ]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None

        local_key = cls._scroll_safe_anchor_local_key(fragment)
        if local_key is None:
            return None
        local_matches = [
            anchor
            for anchor in page.anchors
            if cls._scroll_safe_anchor_local_key(anchor) == local_key
        ]
        return local_matches[0] if len(local_matches) == 1 else None

    @classmethod
    def _scroll_safe_anchor_key(cls, anchor: str) -> tuple[str, str] | None:
        decoded = cls._decode_scroll_safe_anchor(anchor)
        if decoded is None:
            return None
        prefix, payload = decoded
        normalized = "".join(
            character.casefold()
            for character in unicodedata.normalize("NFKC", payload)
            if character.isalnum()
        )
        return (prefix, normalized) if normalized else None

    @classmethod
    def _scroll_safe_anchor_local_key(cls, anchor: str) -> tuple[str, str] | None:
        decoded = cls._decode_scroll_safe_anchor(anchor)
        if decoded is None:
            return None
        prefix, payload = decoded
        _context, separator, segment = payload.rpartition("-")
        if not separator or len(segment) < 3:
            return None
        return prefix, segment

    @staticmethod
    def _decode_scroll_safe_anchor(anchor: str) -> tuple[str, str] | None:
        prefix, separator, encoded = anchor.rpartition("_safe-id-")
        if not separator or not prefix or not encoded:
            return None
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            payload = base64.b64decode(
                padded,
                altchars=b"-_",
                validate=True,
            ).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
        for _ in range(3):
            decoded = unquote_plus(payload)
            if decoded == payload:
                break
            payload = decoded
        return prefix, unicodedata.normalize("NFC", payload)

    def _lookup_page(self, source_relative: str | None) -> PageDescriptor | None:
        if not source_relative:
            return None
        normalized = self._normalize(unquote(source_relative))
        return self._pages.get(normalized) or self._casefold_pages.get(normalized.casefold())

    @staticmethod
    def _index_source_target(href: str | None) -> str | None:
        if not href:
            return None
        parsed = urlsplit(href)
        path = unquote(parsed.path).strip()
        return path or None

    @classmethod
    def _join_source(cls, current: str, target: str) -> str:
        if target.startswith("/"):
            return cls._normalize(target.lstrip("/"))
        parent = posixpath.dirname(current)
        return cls._normalize(posixpath.join(parent, target))

    @staticmethod
    def _normalize(value: str) -> str:
        return posixpath.normpath(value.replace("\\", "/")).lstrip("/")

    @staticmethod
    def _quote_path(value: str) -> str:
        return quote(unquote(value), safe="/._~-%")

    @staticmethod
    def _quote_fragment(value: str) -> str:
        return quote(value, safe="-._~:")
