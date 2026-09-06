from __future__ import annotations

import json
import platform
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from importlib import metadata
from pathlib import Path
from typing import Any

from .models import Issue, IssueCategory, PageResult, RunStats, Severity
from .ocr import rapidocr_model_status

REPORT_FILENAME = "conversion-errors.json"


@dataclass(frozen=True, slots=True)
class IssueKey:
    severity: Severity
    code: str
    page: str | None
    reference: str | None
    element: str | None
    message: str


@dataclass(frozen=True, slots=True)
class IssueGuidance:
    category: IssueCategory
    action: str


def _issue_guidance(code: str, message: str) -> IssueGuidance:
    normalized = code.upper()
    message_lower = message.casefold()
    if any(
        marker in message_lower
        for marker in (
            "executable was not found",
            "language data is missing",
            "cannot list tesseract languages",
            "rapidocr is unavailable",
            "rapidocr model is missing",
            "rapidocr model checksum mismatch",
            "cannot initialize rapidocr",
        )
    ):
        return IssueGuidance(
            IssueCategory.ENVIRONMENT,
            "Install the selected local OCR backend with its pinned models and rerun conversion",
        )
    if "PDF_BACKEND_MISSING" in normalized:
        return IssueGuidance(
            IssueCategory.ENVIRONMENT,
            "Install the locked PyMuPDF optional dependency and rerun conversion",
        )
    if normalized in {"PAGE_OUTPUT_WRITE_FAILED", "UNREADABLE_HTML"}:
        return IssueGuidance(
            IssueCategory.ENVIRONMENT,
            "Correct filesystem access for the referenced path, then rerun conversion",
        )
    if normalized == "PAGE_CONVERSION_FAILED":
        return IssueGuidance(
            IssueCategory.CONVERTER_DEFECT,
            "Update the converter using the recorded exception traceback",
        )
    if normalized == "MISSING_RESOURCE":
        return IssueGuidance(
            IssueCategory.SOURCE_EXPORT,
            "Re-export Confluence with referenced attachments embedded",
        )
    if normalized == "DRAWIO_SOURCE_MISSING":
        return IssueGuidance(
            IssueCategory.SOURCE_EXPORT,
            "Re-export the page with its original Draw.io source attachment",
        )
    if normalized == "IMAGE_ONLY_DIAGRAM":
        return IssueGuidance(
            IssueCategory.SOURCE_EXPORT,
            "Include the original diagram source model in the export",
        )
    if normalized == "UNRESOLVED_ANCHOR":
        return IssueGuidance(
            IssueCategory.SOURCE_AUTHORING,
            "Repair the source Confluence anchor link and export the page again",
        )
    if normalized == "UNRESOLVED_INTERNAL_PAGE":
        return IssueGuidance(
            IssueCategory.SOURCE_AUTHORING,
            "Include the target page in the export",
        )
    if "INVALID" in normalized:
        return IssueGuidance(
            IssueCategory.SOURCE_AUTHORING,
            "Repair the invalid source item, then export the page again",
        )
    if any(
        marker in normalized
        for marker in ("UNEXTRACTED", "UNSUPPORTED", "UNKNOWN_MACRO", "EMBEDDED_INTERACTIVE")
    ):
        return IssueGuidance(
            IssueCategory.UNSUPPORTED_CONTENT,
            "Add a dedicated deterministic extraction handler for this construct",
        )
    return IssueGuidance(
        IssueCategory.SOURCE_EXPORT,
        "Inspect the referenced source construct and repeat the export after correction",
    )


class IssueCollector:
    def __init__(self) -> None:
        self._issues: list[Issue] = []
        self._warnings_by_key: dict[IssueKey, Issue] = {}
        self._error_number = 0
        self._warning_number = 0

    @property
    def issues(self) -> tuple[Issue, ...]:
        return tuple(self._issues)

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self._issues if issue.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self._issues if issue.severity is Severity.WARNING)

    def error(
        self,
        code: str,
        message: str,
        *,
        page: str | None = None,
        reference: str | None = None,
        element: str | None = None,
        exception_type: str | None = None,
        traceback_lines: tuple[str, ...] = (),
    ) -> Issue:
        return self._add(
            Severity.ERROR,
            code,
            message,
            page,
            reference,
            element,
            exception_type,
            traceback_lines,
        )

    def warning(
        self,
        code: str,
        message: str,
        *,
        page: str | None = None,
        reference: str | None = None,
        element: str | None = None,
    ) -> Issue:
        return self._add(
            Severity.WARNING,
            code,
            message,
            page,
            reference,
            element,
            None,
            (),
        )

    def _add(
        self,
        severity: Severity,
        code: str,
        message: str,
        page: str | None,
        reference: str | None,
        element: str | None,
        exception_type: str | None,
        traceback_lines: tuple[str, ...],
    ) -> Issue:
        key = IssueKey(severity, code, page, reference, element, message)
        if severity is Severity.WARNING:
            existing = self._warnings_by_key.get(key)
            if existing is not None:
                return existing
        if severity is Severity.ERROR:
            self._error_number += 1
            issue_id = f"E{self._error_number:05d}"
        else:
            self._warning_number += 1
            issue_id = f"W{self._warning_number:05d}"
        guidance = _issue_guidance(code, message)
        issue = Issue(
            issue_id,
            severity,
            code,
            message,
            guidance.category,
            guidance.action,
            page,
            reference,
            element,
            exception_type,
            traceback_lines,
        )
        self._issues.append(issue)
        if severity is Severity.WARNING:
            self._warnings_by_key[key] = issue
        return issue


def error_marker(issue: Issue) -> str:
    message = issue.message.replace("\n", " ").strip()
    return (
        f"\n\n> **CONVERSION ERROR `{issue.id}`** — {message} "
        f"See `conversion-errors.json`, record `{issue.id}`.\n\n"
    )


def _distribution_version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


@cache
def _runtime_snapshot(
    ocr_language: str,
    ocr_engine: str,
    rapidocr_model_dir: str,
) -> dict[str, Any]:
    requested_languages = [part for part in ocr_language.split("+") if part]
    tesseract_path = shutil.which("tesseract")
    available_languages: list[str] = []
    if tesseract_path is not None:
        try:
            completed = subprocess.run(
                [tesseract_path, "--list-langs"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if completed.returncode == 0:
                available_languages = sorted(
                    line.strip()
                    for line in completed.stdout.splitlines()
                    if line.strip()
                    and not line.casefold().startswith("list of available languages")
                )
        except (OSError, subprocess.SubprocessError):
            available_languages = []
    model_root = Path(rapidocr_model_dir).expanduser().resolve(strict=False)
    rapidocr_models = rapidocr_model_status(model_root)
    rapidocr_version = _distribution_version("rapidocr")
    onnxruntime_version = _distribution_version("onnxruntime")
    return {
        "converterVersion": _distribution_version("confluence-to-markdown")
        or "unknown",
        "pythonVersion": platform.python_version(),
        "pymupdfVersion": _distribution_version("PyMuPDF"),
        "ocrEngine": ocr_engine,
        "rapidocr": {
            "available": rapidocr_version is not None and onnxruntime_version is not None,
            "version": rapidocr_version,
            "onnxruntimeVersion": onnxruntime_version,
            "modelDirectory": str(model_root),
            "models": rapidocr_models,
        },
        "tesseract": {
            "available": tesseract_path is not None,
            "requestedLanguages": requested_languages,
            "availableLanguages": available_languages,
        },
        "pdftotextAvailable": shutil.which("pdftotext") is not None,
        "pdftoppmAvailable": shutil.which("pdftoppm") is not None,
    }


def _error_summary(errors: tuple[Issue, ...]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, IssueCategory, str], list[Issue]] = defaultdict(list)
    for issue in errors:
        grouped[(issue.code, issue.category, issue.action)].append(issue)
    summary = []
    for (code, category, action), issues in grouped.items():
        summary.append(
            {
                "code": code,
                "category": category.value,
                "count": len(issues),
                "affectedPages": len(
                    {issue.page for issue in issues if issue.page is not None}
                ),
                "action": action,
            }
        )
    return sorted(
        summary,
        key=lambda item: (-int(item["count"]), str(item["code"]), str(item["category"])),
    )


def write_report(
    output_root: Path,
    source_root: Path,
    collector: IssueCollector,
    stats: RunStats,
    pages: list[PageResult],
    *,
    started_at: datetime,
    ocr_language: str,
    ocr_engine: str,
    rapidocr_model_dir: Path,
) -> Path:
    finished_at = datetime.now(UTC)
    payload: dict[str, Any] = {
        "schemaVersion": 2,
        "tool": "confluence-to-markdown",
        "status": "failed" if collector.errors else "success",
        "sourceRoot": str(source_root),
        "outputRoot": str(output_root),
        "startedAt": started_at.isoformat(),
        "finishedAt": finished_at.isoformat(),
        "summary": {
            **stats.as_dict(),
            "errors": len(collector.errors),
            "warnings": len(collector.warnings),
        },
        "runtime": _runtime_snapshot(
            ocr_language,
            ocr_engine,
            str(rapidocr_model_dir),
        ),
        "errorSummary": _error_summary(collector.errors),
        "errors": [issue.as_dict() for issue in collector.errors],
        "warnings": [issue.as_dict() for issue in collector.warnings],
        "pages": [page.as_dict() for page in pages],
    }
    report_path = output_root / REPORT_FILENAME
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report_path
