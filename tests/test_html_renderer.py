from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from confluence_to_md.diagnostics import IssueCollector
from confluence_to_md.html_renderer import HtmlRenderer
from confluence_to_md.links import LinkResolver
from confluence_to_md.models import PageDescriptor, RunStats


class HtmlRendererTest(unittest.TestCase):
    def setUp(self) -> None:
        work_root = Path(__file__).parent / ".work"
        work_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=work_root)
        root = Path(self.temporary.name)
        self.page = PageDescriptor(
            source_path=root / "Page.html",
            source_relative="Page.html",
            output_relative="Page.md",
            title="Page title",
            title_anchor="src-42",
            anchors=frozenset({"src-42", "details"}),
            page_id="42",
        )
        self.issues = IssueCollector()
        self.stats = RunStats()
        self.resolver = LinkResolver(root, [self.page])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def render(self, fragment: str) -> str:
        soup = BeautifulSoup(f'<div id="main-content">{fragment}</div>', "html.parser")
        body = soup.select_one("#main-content")
        assert body is not None
        renderer = HtmlRenderer(
            self.page,
            self.resolver,
            self.issues,
            self.stats,
            lambda _tag, _page: "![image](images/test.png)\n\n",
        )
        return renderer.render(body)

    def test_renders_expand_and_keeps_body(self) -> None:
        result = self.render(
            '<div class="scroll-expand-container">'
            '<div class="scroll-expand-control"><span class="scroll-expand-control-text">Details</span></div>'
            '<div class="scroll-expand-content"><p>Hidden meaning</p></div>'
            "</div>"
        )
        self.assertIn("#### Expanded: Details", result)
        self.assertIn("Hidden meaning", result)
        self.assertEqual(1, self.stats.macro_counts["expand"])

    def test_linearizes_merged_table_with_span_metadata(self) -> None:
        result = self.render(
            "<table><tr><th rowspan='2'>Name</th><th>Value</th></tr>"
            "<tr><td colspan='2'>Combined</td></tr></table>"
        )
        self.assertIn("Cell R1C1 (rowspan=2, colspan=1, role=header)", result)
        self.assertIn("Cell R2C2 (rowspan=1, colspan=2, role=data)", result)

    def test_unresolved_anchor_is_visible_and_reported(self) -> None:
        result = self.render('<p><a href="#missing">Broken</a></p>')
        self.assertIn("CONVERSION ERROR `E00001`", result)
        self.assertEqual("UNRESOLVED_ANCHOR", self.issues.errors[0].code)
        self.assertNotIn("[Broken](", result)
        self.assertIn("Broken", result)

    def test_unknown_macro_keeps_fallback_text_and_is_a_hard_error(self) -> None:
        result = self.render(
            '<div data-macro-name="future-widget"><h2>Widget title</h2>'
            '<p>Meaningful fallback body</p></div>'
        )

        self.assertIn("Widget title", result)
        self.assertIn("Meaningful fallback body", result)
        self.assertIn("CONVERSION ERROR `E00001`", result)
        self.assertEqual("UNSUPPORTED_MACRO", self.issues.errors[0].code)
        self.assertEqual("future-widget", self.issues.errors[0].reference)

    def test_embedded_interactive_content_is_a_hard_error(self) -> None:
        result = self.render(
            '<iframe src="https://example.test/widget" title="Meaningful widget"></iframe>'
        )

        self.assertIn("CONVERSION ERROR `E00001`", result)
        self.assertEqual("UNSUPPORTED_EMBEDDED_CONTENT", self.issues.errors[0].code)

    def test_email_like_localpart_anchor_is_rendered_as_plain_email(self) -> None:
        result = self.render('<p>localpart<a href="#missing">@example.test</a></p>')
        self.assertIn("localpart@example.test", result)
        self.assertNotIn("CONVERSION ERROR", result)
        self.assertFalse(self.issues.errors)

    def test_empty_exporter_tab_menu_is_not_an_error(self) -> None:
        result = self.render('<ul class="rwui_tabs_menu"></ul><p>Linear content</p>')
        self.assertIn("Linear content", result)
        self.assertFalse(self.issues.errors)

    def test_real_tab_panels_are_flattened(self) -> None:
        result = self.render(
            '<div class="tabs-container">'
            '<ul class="tabs-menu"><li>First</li><li>Second</li></ul>'
            '<div class="tab-pane"><p>Alpha</p></div>'
            '<div class="tab-pane"><p>Beta</p></div>'
            "</div>"
        )
        self.assertIn("#### Tab: First", result)
        self.assertIn("#### Tab: Second", result)
        self.assertIn("Alpha", result)
        self.assertIn("Beta", result)

    def test_tab_label_panel_mismatch_is_reported_without_losing_text(self) -> None:
        result = self.render(
            '<div class="tabs-container">'
            '<ul class="tabs-menu"><li>First</li><li>Second</li></ul>'
            '<div class="tab-pane"><p>Alpha</p></div>'
            "</div>"
        )
        self.assertIn("CONVERSION ERROR `E00001`", result)
        self.assertEqual("TAB_LABEL_PANEL_MISMATCH", self.issues.errors[0].code)
        self.assertIn("First", result)
        self.assertIn("Second", result)
        self.assertIn("Alpha", result)

    def test_tab_without_any_recoverable_title_is_a_hard_error(self) -> None:
        result = self.render(
            '<div class="tabs-container">'
            '<div class="tab-pane"><p>Untitled body</p></div>'
            "</div>"
        )

        self.assertIn("#### Tab: Tab 1", result)
        self.assertIn("Untitled body", result)
        self.assertIn("CONVERSION ERROR `E00001`", result)
        self.assertEqual("TAB_TITLE_MISSING", self.issues.errors[0].code)

    def test_expand_without_title_is_reported_while_preserving_body(self) -> None:
        result = self.render(
            '<div class="scroll-expand-container">'
            '<div class="scroll-expand-content"><p>Hidden meaning</p></div>'
            "</div>"
        )
        self.assertIn("Hidden meaning", result)
        self.assertIn("CONVERSION ERROR `E00001`", result)
        self.assertEqual("EXPAND_TITLE_MISSING", self.issues.errors[0].code)

    def test_details_without_summary_is_reported_while_preserving_body(self) -> None:
        result = self.render('<details><p>Body only</p></details>')
        self.assertIn("Body only", result)
        self.assertIn("CONVERSION ERROR `E00001`", result)
        self.assertEqual("EXPAND_TITLE_MISSING", self.issues.errors[0].code)

    def test_inline_svg_and_math_are_hard_errors_but_keep_fallback(self) -> None:
        result = self.render(
            '<p><svg aria-label="Diagram"><text>Start</text></svg>'
            '<svg aria-hidden="true"><text>Ignore</text></svg>'
            '<math><mi>x</mi><mo>=</mo><mn>1</mn></math></p>'
        )
        self.assertIn("Start", result)
        self.assertIn("x=1", result.replace(" ", ""))
        self.assertNotIn("Ignore", result)
        self.assertEqual(
            ["UNSUPPORTED_INLINE_SVG", "UNSUPPORTED_INLINE_MATH"],
            [issue.code for issue in self.issues.errors],
        )

    def test_tidy_preserves_long_fenced_code_with_language(self) -> None:
        result = self.render(
            '<pre data-language="python"><code>value = """```"""</code></pre>'
        )
        self.assertIn("````python", result)
        self.assertIn('value = """```"""', result)
        self.assertIn("\n````\n", result)

    def test_tidy_does_not_close_fence_on_code_starting_with_backticks(self) -> None:
        value = "`````python\n`````still code\n\nnext line\n`````\n"

        result = HtmlRenderer._tidy(value)

        self.assertIn("`````still code\n\nnext line", result)

    def test_preserves_anchors_on_linearized_structures(self) -> None:
        result = self.render(
            '<div id="tabs" class="rwui_tabs">'
            '<ul id="tab-menu" class="rwui_tabs_menu"><li id="tab-control">'
            '<a href="#details">Details tab</a></li></ul>'
            '<div id="details" class="rwui_tab_content"><p>Tab body</p></div>'
            '</div>'
            '<details id="expand"><summary id="summary">More</summary><p>Body</p></details>'
            '<ul id="list"><li id="item">List item</li></ul>'
            '<table id="table"><tr id="row"><th id="cell">Header</th></tr>'
            '<tr><td>Value</td></tr></table>'
            '<pre id="code"><code id="code-body">value = 1</code></pre>'
            '<img id="image" src="image.png" alt="Image">'
        )

        for anchor in (
            "tabs",
            "tab-menu",
            "tab-control",
            "details",
            "expand",
            "summary",
            "list",
            "item",
            "table",
            "row",
            "cell",
            "code",
            "code-body",
            "image",
        ):
            with self.subTest(anchor=anchor):
                self.assertEqual(1, result.count(f'<a id="{anchor}"></a>'))
        self.assertIn("#### Tab: Details tab", result)


if __name__ == "__main__":
    unittest.main()
