from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from confluence_to_md.cli import main as cli_main
from confluence_to_md.converter import (
    OUTPUT_MARKER_FILENAME,
    ConfigurationError,
    ConversionOptions,
    _assign_drawio_sources,
    _ensure_page_error_markers,
    convert_export,
)
from confluence_to_md.diagnostics import IssueCollector
from confluence_to_md.links import LinkResolver
from confluence_to_md.models import PageDescriptor
from confluence_to_md.ocr import OcrResult, OcrWord


class ConverterIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Path(__file__).parent / "fixtures" / "full-export"
        self.work_root = Path(__file__).parent / ".work"
        self.work_root.mkdir(exist_ok=True)

    @unittest.skipUnless(
        shutil.which("tesseract") and shutil.which("pdftotext") and shutil.which("pdftoppm"),
        "local Tesseract and Poppler are required for the full fidelity smoke test",
    )
    def test_full_fixture_converts_without_loss_errors(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            output = Path(directory) / "output"
            outcome = convert_export(ConversionOptions(self.fixture, output))
            self.assertFalse(outcome.issues.errors)
            markdown = (output / "_codex_conversion_fixture.md").read_text(encoding="utf-8")
            self.assertIn("#### Expanded: Expanded acceptance notes", markdown)
            self.assertIn("#### Tab: Overview tab", markdown)
            self.assertIn("#### Tab: Risks tab", markdown)
            self.assertIn("rowspan=2", markdown)
            self.assertIn("ENGLISH TEXT", markdown)
            self.assertIn("РУССКИЙ ТЕКСТ", markdown)
            self.assertIn("Nodes:", markdown)
            self.assertIn("Connections:", markdown)
            self.assertLess(
                markdown.index("![Start to Finish flow]"), markdown.index("Nodes:")
            )
            self.assertLess(markdown.index("Nodes:"), markdown.index("Connections:"))
            self.assertLess(
                markdown.index("Connections:"), markdown.index("**Draw.io source:**")
            )
            self.assertIn("## Attachment: codex-conversion-fixture-requirements.docx", markdown)
            self.assertIn(
                "[Codex conversion fixture child](<_codex_conversion_fixture_child.md>)",
                markdown,
            )
            self.assertIn(
                "[external reference](<https://example.com/reference>)",
                markdown,
            )
            self.assertNotIn("Template navigation to ignore", markdown)
            self.assertNotIn("Template footer to ignore", markdown)
            for attachment_text in (
                "Synthetic Markdown guide",
                "Column 2: value",
                '"english": "preserved"',
                "mode: loss-aware",
                "Synthetic EML body in English.",
                "Attachment embedded-metrics.csv (csv):",
                "Synthetic HTML attachment",
                "Title: Synthetic SVG diagram",
                "SVG CONTENT",
            ):
                with self.subTest(attachment_text=attachment_text):
                    self.assertIn(attachment_text, markdown)
            for macro_name in (
                "admonition",
                "children",
                "drawio",
                "expand",
                "linearized-table",
                "panel",
                "tabs",
                "toc",
            ):
                with self.subTest(macro_name=macro_name):
                    self.assertGreaterEqual(outcome.stats.macro_counts.get(macro_name, 0), 1)
            self.assertEqual("success", __import__("json").loads(outcome.report_path.read_text())["status"])

    def test_nonempty_unowned_output_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            output = Path(directory) / "output"
            output.mkdir()
            (output / "owned.txt").write_text("do not overwrite", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "not recognized"):
                convert_export(ConversionOptions(self.fixture, output))

    def test_previous_owned_output_is_automatically_replaced(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            page = source / "Page.html"
            page.write_text(
                '<html><head><title>First</title></head><body><div id="main-content"><h1>First</h1><p>first</p></div></body></html>',
                encoding="utf-8",
            )
            output = root / "output"
            convert_export(ConversionOptions(source, output))
            (output / "stale.txt").write_text("stale", encoding="utf-8")
            page.write_text(
                '<html><head><title>Second</title></head><body><div id="main-content"><h1>Second</h1><p>second</p></div></body></html>',
                encoding="utf-8",
            )

            outcome = convert_export(ConversionOptions(source, output))

            self.assertFalse(outcome.issues.errors)
            self.assertFalse((output / "stale.txt").exists())
            self.assertTrue((output / OUTPUT_MARKER_FILENAME).is_file())
            self.assertIn("second", (output / "Page.md").read_text(encoding="utf-8"))

    def test_legacy_matching_report_allows_automatic_replacement(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Page.html").write_text(
                '<html><head><title>Page</title></head><body><div id="main-content"><p>body</p></div></body></html>',
                encoding="utf-8",
            )
            output = root / "output"
            output.mkdir()
            (output / "stale.txt").write_text("stale", encoding="utf-8")
            (output / "conversion-errors.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "sourceRoot": str(source.resolve()),
                        "outputRoot": str(output.resolve()),
                        "summary": {},
                        "errors": [],
                        "pages": [],
                    }
                ),
                encoding="utf-8",
            )

            convert_export(ConversionOptions(source, output))

            self.assertFalse((output / "stale.txt").exists())
            self.assertTrue((output / "Page.md").is_file())

    def test_output_ancestor_of_source_is_refused_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            output = Path(directory) / "output"
            source = output / "source"
            source.mkdir(parents=True)
            (source / "Page.html").write_text(
                '<html><head><title>Page</title></head><body><div id="main-content"><p>body</p></div></body></html>',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigurationError, "disjoint"):
                convert_export(ConversionOptions(source, output))

            self.assertTrue((source / "Page.html").is_file())

    def test_exporter_emoticons_do_not_require_missing_webhelp_svg(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Page.html").write_text(
                """<!doctype html><html><head><title>Emoticons</title></head><body>
                <div id="main-content"><h1>Emoticons</h1><p>
                <img class="emoticon emoticon-plus" src="images/s/add.svg">
                <img class="emoticon emoticon-question" src="images/s/help.svg">
                <img class="emoticon emoticon-minus" src="images/s/forbidden.svg">
                <img class="emoticon emoticon-smile" src="images/s/smile.svg">
                </p></div></body></html>""",
                encoding="utf-8",
            )

            output = root / "output"
            outcome = convert_export(ConversionOptions(source, output))
            markdown = (output / "Page.md").read_text(encoding="utf-8")

        self.assertFalse(outcome.issues.errors)
        self.assertIn("➕", markdown)
        self.assertIn("❓", markdown)
        self.assertIn("➖", markdown)
        self.assertIn("🙂", markdown)

    def test_generic_icon_class_is_treated_as_content(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Page.html").write_text(
                '<div id="main-content"><h1>Icon</h1>'
                '<img class="icon" src="images/missing-status.png" alt="Status"></div>',
                encoding="utf-8",
            )

            output = root / "output"
            outcome = convert_export(ConversionOptions(source, output))
            markdown = (output / "Page.md").read_text(encoding="utf-8")

        self.assertEqual("MISSING_RESOURCE", outcome.issues.errors[0].code)
        self.assertIn("![Status]", markdown)
        self.assertIn("CONVERSION ERROR", markdown)

    def test_image_only_diagram_is_a_hard_error_even_when_ocr_succeeds(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            image = source / "images" / "download" / "export" / "plantuml-1.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"fixture")
            (source / "Page.html").write_text(
                '<div id="main-content"><h1>Diagram</h1>'
                '<img src="images/download/export/plantuml-1.png" alt="Flow"></div>',
                encoding="utf-8",
            )
            ocr_result = OcrResult(
                text="Start Finish",
                words=(OcrWord("Start", 99.0, 1, 1, 1, 1, 1),),
                mean_confidence=99.0,
                languages=("rus", "eng"),
                engine="fake",
                readable=True,
            )

            output = root / "output"
            with patch(
                "confluence_to_md.ocr.RapidOcrBackend.recognize",
                return_value=ocr_result,
            ):
                outcome = convert_export(ConversionOptions(source, output))
            markdown = (output / "Page.md").read_text(encoding="utf-8")

        self.assertEqual("IMAGE_ONLY_DIAGRAM", outcome.issues.errors[0].code)
        self.assertIn("Start Finish", markdown)
        self.assertIn("CONVERSION ERROR", markdown)

    def test_russian_diagram_alt_is_a_hard_error_with_generic_filename(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            image = source / "images" / "content.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"fixture")
            (source / "Page.html").write_text(
                '<div id="main-content"><h1>Diagram</h1>'
                '<img src="images/content.png" alt="Архитектурная схема"></div>',
                encoding="utf-8",
            )
            ocr_result = OcrResult(
                text="Сервис База данных",
                words=(OcrWord("Сервис", 99.0, 1, 1, 1, 1, 1),),
                mean_confidence=99.0,
                languages=("rus", "eng"),
                engine="fake",
                readable=True,
            )

            output = root / "output"
            with patch(
                "confluence_to_md.ocr.RapidOcrBackend.recognize",
                return_value=ocr_result,
            ):
                outcome = convert_export(ConversionOptions(source, output))

        self.assertIn("IMAGE_ONLY_DIAGRAM", {issue.code for issue in outcome.issues.errors})

    def test_jira_issue_type_icon_is_treated_as_decorative(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Page.html").write_text(
                '<div id="main-content"><h1>Issue</h1>'
                '<p><a class="jira-issue-key" href="https://jira.example.test/browse/AIGAS-1">'
                '<img class="icon" '
                'src="images/jira.example.test/secure/viewavatar-size-xsmall-avatarid-17015-avatartype-issuetype.svg" '
                'alt="images/jira.example.test/secure/viewavatar-size-xsmall-avatarid-17015-avatartype-issuetype.svg">'
                'AIGAS-1</a></p></div>',
                encoding="utf-8",
            )

            output = root / "output"
            outcome = convert_export(ConversionOptions(source, output))
            markdown = (output / "Page.md").read_text(encoding="utf-8")

        self.assertFalse(outcome.issues.errors)
        self.assertIn("[AIGAS-1](<https://jira.example.test/browse/AIGAS-1>)", markdown)
        self.assertNotIn("viewavatar", markdown)
        self.assertNotIn("CONVERSION ERROR", markdown)

    def test_discovers_nested_pages_and_preserves_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            nested = source / "section"
            nested.mkdir(parents=True)
            attachment_root = source / "attachments" / "100" / "1"
            attachment_root.mkdir(parents=True)
            asset_root = source / "assets"
            asset_root.mkdir()
            (source / "Root.html").write_text(
                """<!doctype html><html><body><div id="main-content">
                <h1 id="src-100-root">Root</h1>
                <p><a href="section/Child.html#src-200-child">Child</a></p>
                <p><a href="attachments/100/1/Payload.html">HTML attachment</a></p>
                </div></body></html>""",
                encoding="utf-8",
            )
            (nested / "Child.html").write_text(
                """<!doctype html><html><body><div id="main-content">
                <h1 id="src-200-child">Child</h1>
                <p>Nested page body</p>
                <p><a href="../attachments/100/1/Payload.html">Nested HTML attachment</a></p>
                </div></body></html>""",
                encoding="utf-8",
            )
            (attachment_root / "Payload.html").write_text(
                '<div id="main-content"><h1>Attachment is not a page</h1></div>',
                encoding="utf-8",
            )
            (asset_root / "help.html").write_text(
                '<div id="main-content"><h1>WebHelp asset is not a page</h1></div>',
                encoding="utf-8",
            )

            output = root / "output"
            outcome = convert_export(ConversionOptions(source, output))

            self.assertFalse(outcome.issues.errors)
            self.assertEqual(2, outcome.stats.discovered_pages)
            self.assertTrue((output / "Root.md").is_file())
            self.assertTrue((output / "section" / "Child.md").is_file())
            self.assertFalse((output / "assets" / "help.md").exists())
            self.assertTrue((output / "attachments" / "100" / "1" / "Payload.html").is_file())
            root_markdown = (output / "Root.md").read_text(encoding="utf-8")
            child_markdown = (output / "section" / "Child.md").read_text(encoding="utf-8")
            self.assertIn("[Child](<section/Child.md#src-200-child>)", root_markdown)
            self.assertIn("Attachment is not a page", root_markdown)
            self.assertIn(
                "Original: [Payload.html](<../attachments/100/1/Payload.html>)",
                child_markdown,
            )
            self.assertIn("Attachment is not a page", child_markdown)

    def test_utf8_bom_page_uses_the_same_decoder_for_discovery_and_conversion(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Bom.html").write_bytes(
                b"\xef\xbb\xbf"
                + b'<div id="main-content"><h1>BOM page</h1><p>Readable body.</p></div>'
            )

            output = root / "output"
            outcome = convert_export(ConversionOptions(source, output))
            markdown = (output / "Bom.md").read_text(encoding="utf-8")

        self.assertFalse(outcome.issues.errors)
        self.assertIn("Readable body.", markdown)

    def test_nested_drawio_links_use_export_root_relative_resources(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            nested = source / "section"
            attachments = source / "attachments" / "100" / "1"
            images = source / "images"
            nested.mkdir(parents=True)
            attachments.mkdir(parents=True)
            images.mkdir()
            (images / "flow.png").write_bytes(b"synthetic preview")
            (attachments / "flow.drawio").write_text(
                """<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>
                <mxCell id="node" value="Start" vertex="1" parent="1"/>
                </root></mxGraphModel>""",
                encoding="utf-8",
            )
            (nested / "Diagram.html").write_text(
                """<div id="main-content"><h1 id="src-100">Diagram</h1>
                <div class="drawio-diagram"><img class="drawio-diagram-image"
                src="../images/flow.png" alt="Flow">
                <a href="../attachments/100/1/flow.drawio">Source</a></div></div>""",
                encoding="utf-8",
            )

            output = root / "output"
            outcome = convert_export(ConversionOptions(source, output))
            markdown = (output / "section" / "Diagram.md").read_text(encoding="utf-8")

        self.assertFalse(outcome.issues.errors)
        self.assertIn("![Flow](<../images/flow.png>)", markdown)
        self.assertIn(
            "**Draw.io source:** [flow.drawio](<../attachments/100/1/flow.drawio>)",
            markdown,
        )

    def test_page_linked_errors_have_one_report_record_and_one_visible_marker(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Broken.html").write_text(
                """<!doctype html><html><body><div id="main-content">
                <p><a href="#missing-anchor">Broken anchor</a></p>
                <p><img src="images/missing.png" alt="Missing image"></p>
                <p><a href="Broken.md">Output collision</a></p>
                </div></body></html>""",
                encoding="utf-8",
            )
            (source / "Broken.md").write_text("Source attachment", encoding="utf-8")

            output = root / "output"
            outcome = convert_export(ConversionOptions(source, output))
            report = json.loads(outcome.report_path.read_text(encoding="utf-8"))
            markdown = (output / "Broken.md").read_text(encoding="utf-8")

        page_error_ids = [item["id"] for item in report["errors"] if item.get("page") == "Broken.html"]
        self.assertEqual(
            {
                "MISSING_PAGE_TITLE",
                "UNRESOLVED_ANCHOR",
                "MISSING_RESOURCE",
                "OUTPUT_PATH_COLLISION",
            },
            {item["code"] for item in report["errors"]},
        )
        self.assertEqual(4, len(page_error_ids))
        self.assertEqual(4, len(set(page_error_ids)))
        for issue_id in page_error_ids:
            self.assertEqual(1, markdown.count(f"CONVERSION ERROR `{issue_id}`"))
            self.assertEqual(1, sum(1 for item in report["errors"] if item["id"] == issue_id))

    def test_cli_returns_zero_and_prints_summary_for_clean_fixture(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Clean.html").write_text(
                '<div id="main-content"><h1>Clean</h1><p>Complete text.</p></div>',
                encoding="utf-8",
            )
            output = root / "output"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = cli_main([str(source), "--output", str(output)])

        self.assertEqual(0, exit_code)
        self.assertEqual("", stderr.getvalue())
        self.assertIn("Conversion complete: pages=1/1", stdout.getvalue())
        self.assertIn("errors=0", stdout.getvalue())
        self.assertIn("Report:", stdout.getvalue())

    def test_cli_returns_one_after_full_pass_when_semantic_errors_exist(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Broken.html").write_text(
                """<!doctype html><html><body><div id="main-content">
                <h1>Broken</h1>
                <p><a href="#missing-anchor">Broken anchor</a></p>
                </div></body></html>""",
                encoding="utf-8",
            )
            output = root / "output"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = cli_main([str(source), "--output", str(output)])

            self.assertEqual(1, exit_code)
            self.assertEqual("", stderr.getvalue())
            self.assertIn("pages=1/1", stdout.getvalue())
            self.assertIn("errors=1", stdout.getvalue())
            self.assertTrue((output / "Broken.md").is_file())
            self.assertTrue((output / "conversion-errors.json").is_file())

    def test_cli_returns_two_for_configuration_error(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            output = root / "output"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = cli_main([str(source), "--output", str(output)])

        self.assertEqual(2, exit_code)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("Configuration error:", stderr.getvalue())

    def test_malformed_page_gets_marker_while_other_pages_continue(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Broken.html").write_text(
                "<html><head><title>Broken shell</title></head><body>No content root</body></html>",
                encoding="utf-8",
            )
            (source / "Good.html").write_text(
                '<div id="main-content"><h1>Good</h1><p>Still converted.</p></div>',
                encoding="utf-8",
            )

            output = root / "output"
            outcome = convert_export(ConversionOptions(source, output))
            broken_markdown = (output / "Broken.md").read_text(encoding="utf-8")
            good_markdown = (output / "Good.md").read_text(encoding="utf-8")

        self.assertEqual(2, outcome.stats.discovered_pages)
        self.assertEqual(2, outcome.stats.converted_pages)
        self.assertEqual("MISSING_PAGE_CONTENT", outcome.issues.errors[0].code)
        self.assertIn("CONVERSION ERROR `E00001`", broken_markdown)
        self.assertIn("Still converted.", good_markdown)

    def test_unexpected_page_failure_reports_safe_traceback(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Page.html").write_text(
                '<div id="main-content"><h1>Page</h1><p>body</p></div>',
                encoding="utf-8",
            )
            output = root / "output"

            with patch(
                "confluence_to_md.converter._convert_page",
                side_effect=ValueError("bad delimiter value"),
            ):
                outcome = convert_export(ConversionOptions(source, output))
            report = json.loads(outcome.report_path.read_text(encoding="utf-8"))

        failure = next(
            item for item in report["errors"] if item["code"] == "PAGE_CONVERSION_FAILED"
        )
        self.assertEqual("converter-defect", failure["category"])
        self.assertEqual("ValueError", failure["exceptionType"])
        self.assertTrue(failure["traceback"])
        self.assertTrue(all("/" not in frame for frame in failure["traceback"]))

    def test_malformed_root_index_is_not_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "index.html").write_text(
                "<html><head><title>Broken index</title></head><body>Lost body</body></html>",
                encoding="utf-8",
            )

            output = root / "output"
            outcome = convert_export(ConversionOptions(source, output))
            markdown = (output / "index.md").read_text(encoding="utf-8")

        self.assertEqual(1, outcome.stats.discovered_pages)
        self.assertEqual("MISSING_PAGE_CONTENT", outcome.issues.errors[0].code)
        self.assertIn("CONVERSION ERROR `E00001`", markdown)

    def test_report_filename_resource_collision_does_not_overwrite_report(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Page.html").write_text(
                '<div id="main-content"><h1>Page</h1>'
                '<a href="conversion-errors.json">Conflicting attachment</a></div>',
                encoding="utf-8",
            )
            (source / "conversion-errors.json").write_text(
                '{"source": true}',
                encoding="utf-8",
            )

            output = root / "output"
            outcome = convert_export(ConversionOptions(source, output))
            report = json.loads(outcome.report_path.read_text(encoding="utf-8"))
            markdown = (output / "Page.md").read_text(encoding="utf-8")

        self.assertEqual("failed", report["status"])
        self.assertIn("OUTPUT_PATH_COLLISION", {item["code"] for item in report["errors"]})
        self.assertIn("CONVERSION ERROR", markdown)
        self.assertNotEqual({"source": True}, report)

    def test_marker_reconciliation_write_failure_is_reported_without_raising(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            output = Path(directory)
            destination = output / "Page.md"
            destination.write_text("# Page\n", encoding="utf-8")
            page = PageDescriptor(
                source_path=output / "Page.html",
                source_relative="Page.html",
                output_relative="Page.md",
                title="Page",
                title_anchor=None,
                anchors=frozenset(),
                page_id=None,
            )
            issues = IssueCollector()
            issues.error("EXISTING_ERROR", "Needs a visible marker", page="Page.html")

            with patch.object(Path, "write_text", side_effect=OSError("disk full")):
                _ensure_page_error_markers(output, [page], issues)

        self.assertEqual(
            ["EXISTING_ERROR", "PAGE_OUTPUT_WRITE_FAILED"],
            [issue.code for issue in issues.errors],
        )

    def test_drawio_images_use_their_nearest_source_link(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            attachments = root / "attachments" / "100" / "1"
            attachments.mkdir(parents=True)
            for name in ("unrelated.drawio", "first.drawio", "second.drawio"):
                (attachments / name).write_text(
                    "<mxGraphModel><root/></mxGraphModel>",
                    encoding="utf-8",
                )
            page = PageDescriptor(
                source_path=root / "Page.html",
                source_relative="Page.html",
                output_relative="Page.md",
                title="Page",
                title_anchor=None,
                anchors=frozenset(),
                page_id="100",
            )
            soup = BeautifulSoup(
                '<div id="main-content">'
                '<a href="attachments/100/1/unrelated.drawio">Template</a>'
                '<div><img id="first" class="drawio-diagram-image" src="first.png">'
                '<a href="attachments/100/1/first.drawio">First source</a></div>'
                '<div><img id="second" class="drawio-diagram-image" src="second.png">'
                '<a href="attachments/100/1/second.drawio">Second source</a></div>'
                "</div>",
                "html.parser",
            )
            body = soup.select_one("#main-content")
            assert body is not None

            assignments = _assign_drawio_sources(
                body,
                page,
                LinkResolver(root, [page]),
            )
            first = body.select_one("#first")
            second = body.select_one("#second")
            assert first is not None and second is not None

        self.assertEqual("attachments/100/1/first.drawio", assignments[id(first)])
        self.assertEqual("attachments/100/1/second.drawio", assignments[id(second)])

    def test_drawio_alias_finds_unique_embedded_source(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            attachments = root / "attachments" / "100" / "1"
            attachments.mkdir(parents=True)
            (attachments / "flow.drawio").write_text(
                "<mxGraphModel><root/></mxGraphModel>",
                encoding="utf-8",
            )
            page = PageDescriptor(
                source_path=root / "Page.html",
                source_relative="Page.html",
                output_relative="Page.md",
                title="Page",
                title_anchor=None,
                anchors=frozenset(),
                page_id="100",
            )
            soup = BeautifulSoup(
                '<div id="main-content"><img id="diagram" '
                'class="drawio-diagram-image" src="flow.png" '
                'data-linked-resource-default-alias="flow.drawio"></div>',
                "html.parser",
            )
            body = soup.select_one("#main-content")
            image = soup.select_one("#diagram")
            assert body is not None and image is not None

            assignments = _assign_drawio_sources(
                body,
                page,
                LinkResolver(root, [page]),
            )

        self.assertEqual("attachments/100/1/flow.drawio", assignments[id(image)])

    def test_ambiguous_drawio_sources_are_not_assigned_by_document_order(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            attachments = root / "attachments" / "100" / "1"
            attachments.mkdir(parents=True)
            for name in ("first.drawio", "second.drawio"):
                (attachments / name).write_text(
                    "<mxGraphModel><root/></mxGraphModel>",
                    encoding="utf-8",
                )
            page = PageDescriptor(
                source_path=root / "Page.html",
                source_relative="Page.html",
                output_relative="Page.md",
                title="Page",
                title_anchor=None,
                anchors=frozenset(),
                page_id="100",
            )
            soup = BeautifulSoup(
                '<div id="main-content">'
                '<img id="first" class="drawio-diagram-image" src="one.png">'
                '<img id="second" class="drawio-diagram-image" src="two.png">'
                '<a href="attachments/100/1/first.drawio">First source</a>'
                '<a href="attachments/100/1/second.drawio">Second source</a>'
                "</div>",
                "html.parser",
            )
            body = soup.select_one("#main-content")
            assert body is not None

            assignments = _assign_drawio_sources(
                body,
                page,
                LinkResolver(root, [page]),
            )

        self.assertEqual({}, assignments)

    def test_redirect_shell_with_meaningful_body_is_not_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Page.html").write_text(
                '<div id="main-content"><h1>Page</h1><p>Kept page</p></div>',
                encoding="utf-8",
            )
            (source / "redirect.html").write_text(
                '<html><head><link rel="canonical" href="Page.html"></head>'
                '<body><p>Meaningful fallback body</p></body></html>',
                encoding="utf-8",
            )

            output = root / "output"
            outcome = convert_export(ConversionOptions(source, output))
            markdown = (output / "redirect.md").read_text(encoding="utf-8")

        self.assertEqual(2, outcome.stats.discovered_pages)
        self.assertIn(
            "MISSING_PAGE_CONTENT",
            [issue.code for issue in outcome.issues.errors],
        )
        self.assertIn("CONVERSION ERROR", markdown)

    def test_symlink_directory_is_reported_and_not_followed(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            real = source / "real"
            real.mkdir()
            (real / "Page.html").write_text(
                '<div id="main-content"><h1>Real</h1></div>',
                encoding="utf-8",
            )
            __import__("os").symlink(real, source / "linked")

            output = root / "output"
            outcome = convert_export(ConversionOptions(source, output))

        self.assertEqual(1, outcome.stats.discovered_pages)
        symlink_errors = [
            issue for issue in outcome.issues.errors if issue.code == "SYMLINK_DIRECTORY"
        ]
        self.assertEqual(1, len(symlink_errors))
        self.assertEqual("linked", symlink_errors[0].reference)
        self.assertIsNone(symlink_errors[0].page)

    def test_redirect_shell_with_only_script_body_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Page.html").write_text(
                '<div id="main-content"><h1>Page</h1><p>Kept page</p></div>',
                encoding="utf-8",
            )
            (source / "index.html").write_text(
                '<html><head><link rel="canonical" href="Page.html"></head>'
                '<body><script>window.location = "Page.html";</script></body></html>',
                encoding="utf-8",
            )

            output = root / "output"
            outcome = convert_export(ConversionOptions(source, output))

        self.assertEqual(1, outcome.stats.discovered_pages)
        self.assertFalse(outcome.issues.errors)
        self.assertFalse((output / "index.md").exists())

    def test_scroll_index_redirect_with_visible_fallback_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_root) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Page.html").write_text(
                '<div id="main-content"><h1>Page</h1><p>Kept page</p></div>',
                encoding="utf-8",
            )
            (source / "index.html").write_text(
                '<html><head><link rel="canonical" href="Page.html"></head><body>'
                '<p>If you are not redirected, click <a href="Page.html">here</a>.</p>'
                "</body></html>",
                encoding="utf-8",
            )

            output = root / "output"
            outcome = convert_export(ConversionOptions(source, output))

        self.assertEqual(1, outcome.stats.discovered_pages)
        self.assertFalse(outcome.issues.errors)
        self.assertFalse((output / "index.md").exists())


if __name__ == "__main__":
    unittest.main()
