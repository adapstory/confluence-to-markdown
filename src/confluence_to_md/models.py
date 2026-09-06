from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class IssueCategory(StrEnum):
    ENVIRONMENT = "environment"
    SOURCE_EXPORT = "source-export"
    SOURCE_AUTHORING = "source-authoring"
    UNSUPPORTED_CONTENT = "unsupported-content"
    CONVERTER_DEFECT = "converter-defect"


@dataclass(frozen=True, slots=True)
class Issue:
    id: str
    severity: Severity
    code: str
    message: str
    category: IssueCategory
    action: str
    page: str | None = None
    reference: str | None = None
    element: str | None = None
    exception_type: str | None = None
    traceback: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "category": self.category.value,
            "action": self.action,
        }
        if self.page is not None:
            result["page"] = self.page
        if self.reference is not None:
            result["reference"] = self.reference
        if self.element is not None:
            result["element"] = self.element
        if self.exception_type is not None:
            result["exceptionType"] = self.exception_type
        if self.traceback:
            result["traceback"] = list(self.traceback)
        return result


@dataclass(frozen=True, slots=True)
class PageDescriptor:
    source_path: Path
    source_relative: str
    output_relative: str
    title: str
    title_anchor: str | None
    anchors: frozenset[str]
    page_id: str | None


@dataclass(slots=True)
class PageResult:
    source: str
    output: str
    title: str
    attachment_count: int = 0
    image_count: int = 0
    error_count: int = 0
    warning_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "output": self.output,
            "title": self.title,
            "attachments": self.attachment_count,
            "images": self.image_count,
            "errors": self.error_count,
            "warnings": self.warning_count,
        }


@dataclass(slots=True)
class RunStats:
    discovered_pages: int = 0
    converted_pages: int = 0
    copied_resources: int = 0
    extracted_attachments: int = 0
    ocr_images: int = 0
    drawio_models: int = 0
    macro_counts: dict[str, int] = field(default_factory=dict)

    def increment_macro(self, name: str) -> None:
        self.macro_counts[name] = self.macro_counts.get(name, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "discoveredPages": self.discovered_pages,
            "convertedPages": self.converted_pages,
            "copiedResources": self.copied_resources,
            "extractedAttachments": self.extracted_attachments,
            "ocrImages": self.ocr_images,
            "drawioModels": self.drawio_models,
            "macros": dict(sorted(self.macro_counts.items())),
        }
