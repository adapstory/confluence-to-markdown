from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from confluence_to_md.diagnostics import IssueCollector, write_report
from confluence_to_md.models import RunStats


class IssueCollectorTest(unittest.TestCase):
    def test_identical_errors_receive_distinct_ids(self) -> None:
        collector = IssueCollector()

        first = collector.error(
            "MISSING_RESOURCE",
            "Referenced local resource is absent from the export: images/test.png",
            page="Page.html",
            reference="images/test.png",
            element="img",
        )
        second = collector.error(
            "MISSING_RESOURCE",
            "Referenced local resource is absent from the export: images/test.png",
            page="Page.html",
            reference="images/test.png",
            element="img",
        )

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(2, len(collector.errors))

    def test_environment_failure_receives_category_and_action(self) -> None:
        collector = IssueCollector()

        issue = collector.error(
            "attachment_image_ocr_failed",
            "Tesseract executable was not found: tesseract",
            page="Page.html",
        )

        self.assertEqual("environment", issue.category.value)
        self.assertIn("selected local OCR backend", issue.action)
        self.assertEqual("environment", issue.as_dict()["category"])

    def test_report_contains_runtime_snapshot_and_aggregated_error_summary(self) -> None:
        collector = IssueCollector()
        collector.error(
            "attachment_image_ocr_failed",
            "Tesseract executable was not found: tesseract",
            page="One.html",
        )
        collector.error(
            "attachment_image_ocr_failed",
            "Tesseract executable was not found: tesseract",
            page="One.html",
        )
        runtime = {
            "converterVersion": "0.1.0",
            "pythonVersion": "3.13.5",
            "pymupdfVersion": "1.28.2",
            "tesseract": {
                "available": False,
                "requestedLanguages": ["rus", "eng"],
                "availableLanguages": [],
            },
            "pdftotextAvailable": False,
            "pdftoppmAvailable": False,
        }

        with tempfile.TemporaryDirectory() as directory_name:
            root = Path(directory_name)
            with patch(
                "confluence_to_md.diagnostics._runtime_snapshot",
                return_value=runtime,
            ):
                report_path = write_report(
                    root,
                    root / "source",
                    collector,
                    RunStats(),
                    [],
                    started_at=datetime.now(UTC),
                    ocr_language="rus+eng",
                    ocr_engine="rapidocr",
                    rapidocr_model_dir=Path("models/rapidocr"),
                )
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(2, report["schemaVersion"])
        self.assertEqual(runtime, report["runtime"])
        self.assertEqual(
            {
                "code": "attachment_image_ocr_failed",
                "category": "environment",
                "count": 2,
                "affectedPages": 1,
                "action": "Install the selected local OCR backend with its pinned models and rerun conversion",
            },
            report["errorSummary"][0],
        )


if __name__ == "__main__":
    unittest.main()
