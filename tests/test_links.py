from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from confluence_to_md.links import LinkResolver
from confluence_to_md.models import PageDescriptor


class LinkResolverTest(unittest.TestCase):
    def setUp(self) -> None:
        work_root = Path(__file__).parent / ".work"
        work_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=work_root)
        self.root = Path(self.temporary.name)
        (self.root / "attachments").mkdir()
        (self.root / "attachments" / "sample.txt").write_text("text", encoding="utf-8")
        self.first = self._page("First.html", "First.md", {"first", "section"})
        self.second = self._page("Second page.html", "Second page.md", {"target"})
        self.resolver = LinkResolver(self.root, [self.first, self.second], index_target="First.html")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_rewrites_page_and_validates_anchor(self) -> None:
        result = self.resolver.resolve("Second%20page.html#target", self.first)
        self.assertEqual("Second%20page.md#target", result.href)
        self.assertIsNone(result.problem_code)

    def test_rewrites_index_alias(self) -> None:
        result = self.resolver.resolve("index.html#section", self.second)
        self.assertEqual("First.md#section", result.href)

    def test_rewrites_index_alias_when_index_target_contains_query_and_fragment(self) -> None:
        resolver = LinkResolver(
            self.root,
            [self.first, self.second],
            index_target="First.html?focusedCommentId=1#section",
        )

        result = resolver.resolve("index.html#section", self.second)

        self.assertEqual("First.md#section", result.href)
        self.assertEqual("page", result.kind)
        self.assertIsNone(result.problem_code)

    def test_marks_missing_anchor(self) -> None:
        result = self.resolver.resolve("#absent", self.first)
        self.assertEqual("UNRESOLVED_ANCHOR", result.problem_code)

    def test_resolves_unique_scroll_safe_id_encoding_alias(self) -> None:
        actual = self._safe_id("src-2", "id-Page-Actor")
        encoded_alias = self._safe_id("src-2", "id-%50age-actor")
        second = self._page("Second page.html", "Second page.md", {actual})
        resolver = LinkResolver(self.root, [self.first, second])

        result = resolver.resolve(
            f"Second%20page.html#{encoded_alias}",
            self.first,
        )

        self.assertIsNone(result.problem_code)
        self.assertEqual(f"Second%20page.md#{actual}", result.href)

    def test_rejects_ambiguous_scroll_safe_id_encoding_alias(self) -> None:
        first_actual = self._safe_id("src-2", "id-Page-Actor")
        second_actual = self._safe_id("src-2", "id-page-actor")
        encoded_alias = self._safe_id("src-2", "id-%50age-ACTOR")
        second = self._page(
            "Second page.html",
            "Second page.md",
            {first_actual, second_actual},
        )
        resolver = LinkResolver(self.root, [self.first, second])

        result = resolver.resolve(
            f"Second%20page.html#{encoded_alias}",
            self.first,
        )

        self.assertEqual("UNRESOLVED_ANCHOR", result.problem_code)

    def test_resolves_unique_scroll_safe_id_after_page_title_change(self) -> None:
        actual = self._safe_id("src-2", "id-New page-b2c")
        stale_alias = self._safe_id("src-2", "id-Old page-b2c")
        second = self._page("Second page.html", "Second page.md", {actual})
        resolver = LinkResolver(self.root, [self.first, second])

        result = resolver.resolve(
            f"Second%20page.html#{stale_alias}",
            self.first,
        )

        self.assertIsNone(result.problem_code)
        self.assertEqual(f"Second%20page.md#{actual}", result.href)

    def test_rejects_ambiguous_scroll_safe_id_after_page_title_change(self) -> None:
        first_actual = self._safe_id("src-2", "id-New page A-b2c")
        second_actual = self._safe_id("src-2", "id-New page B-b2c")
        stale_alias = self._safe_id("src-2", "id-Old page-b2c")
        second = self._page(
            "Second page.html",
            "Second page.md",
            {first_actual, second_actual},
        )
        resolver = LinkResolver(self.root, [self.first, second])

        result = resolver.resolve(
            f"Second%20page.html#{stale_alias}",
            self.first,
        )

        self.assertEqual("UNRESOLVED_ANCHOR", result.problem_code)

    def test_preserves_external_url(self) -> None:
        href = "https://example.test/path?q=1#fragment"
        result = self.resolver.resolve(href, self.first)
        self.assertEqual(href, result.href)
        self.assertEqual("external", result.kind)

    def test_resolves_existing_resource(self) -> None:
        result = self.resolver.resolve("attachments/sample.txt", self.first)
        self.assertEqual("resource", result.kind)
        self.assertEqual("attachments/sample.txt", result.source_relative)

    def test_rejects_reference_outside_root(self) -> None:
        result = self.resolver.resolve("../../secret.txt", self.first)
        self.assertEqual("PATH_TRAVERSAL_REFERENCE", result.problem_code)

    def test_classifies_confluence_create_page_link_as_unresolved_page(self) -> None:
        result = self.resolver.resolve(
            "/pages/createpage.action?spaceKey=DOC&title=Missing",
            self.first,
        )

        self.assertEqual("UNRESOLVED_INTERNAL_PAGE", result.problem_code)

    def test_classifies_missing_xhtml_as_unresolved_page(self) -> None:
        result = self.resolver.resolve("Missing.xhtml", self.first)

        self.assertEqual("UNRESOLVED_INTERNAL_PAGE", result.problem_code)

    def _page(self, source: str, output: str, anchors: set[str]) -> PageDescriptor:
        return PageDescriptor(
            source_path=self.root / source,
            source_relative=source,
            output_relative=output,
            title=Path(source).stem,
            title_anchor=None,
            anchors=frozenset(anchors),
            page_id=None,
        )

    @staticmethod
    def _safe_id(prefix: str, payload: str) -> str:
        encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        return f"{prefix}_safe-id-{encoded}"


if __name__ == "__main__":
    unittest.main()
