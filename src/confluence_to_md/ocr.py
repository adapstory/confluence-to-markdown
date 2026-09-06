"""Deterministic local OCR backends.

The converter never downloads models at runtime and never sends images to a
remote service. RapidOCR is the default backend for portable corporate use;
the Tesseract command-line adapter remains available explicitly.
"""

from __future__ import annotations

import csv
import hashlib
import io
import shutil
import subprocess
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol


class OcrError(RuntimeError):
    """Base class for local OCR failures."""


class OcrUnavailableError(OcrError):
    """Raised when an OCR engine, model, or requested language is unavailable."""


class OcrExecutionError(OcrError):
    """Raised when an OCR engine cannot process an image."""


@dataclass(frozen=True, slots=True)
class OcrDiagnostic:
    code: str
    message: str
    severity: Literal["warning", "error"] = "error"


@dataclass(frozen=True, slots=True)
class OcrWord:
    text: str
    confidence: float
    page: int
    block: int
    paragraph: int
    line: int
    word: int


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str
    words: tuple[OcrWord, ...]
    mean_confidence: float | None
    languages: tuple[str, ...]
    engine: str
    readable: bool
    diagnostics: tuple[OcrDiagnostic, ...] = ()

    @property
    def ok(self) -> bool:
        return self.readable and not any(
            item.severity == "error" for item in self.diagnostics
        )


class OcrBackend(Protocol):
    """Narrow interface used by attachment and page-image handlers."""

    def recognize(self, image_path: Path) -> OcrResult:
        """Extract text from one local image."""
        ...


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


RAPIDOCR_MODEL_FILES = {
    "detection.onnx": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
    "classification.onnx": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
    "recognition-cyrillic.onnx": "90f761b4bfcce0c8c561c0cb5c887b0971d3ec01c32164bdf7374a35b0982711",
}


def default_rapidocr_model_dir() -> Path:
    """Return the project-local model directory used by the CLI."""

    return Path("models") / "rapidocr"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rapidocr_model_status(model_dir: Path) -> list[dict[str, object]]:
    """Inspect the exact offline models and their expected checksums."""

    root = Path(model_dir).expanduser().resolve(strict=False)
    statuses: list[dict[str, object]] = []
    for filename, expected_sha256 in RAPIDOCR_MODEL_FILES.items():
        path = root / filename
        available = path.is_file() and not path.is_symlink()
        actual_sha256 = _sha256(path) if available else None
        statuses.append(
            {
                "file": filename,
                "available": available,
                "expectedSha256": expected_sha256,
                "actualSha256": actual_sha256,
                "checksumValid": actual_sha256 == expected_sha256,
            }
        )
    return statuses


class RapidOcrBackend:
    """RapidOCR/ONNX adapter using only explicitly supplied local models."""

    def __init__(
        self,
        *,
        model_dir: Path | None = None,
        languages: Sequence[str] = ("rus", "eng"),
        minimum_confidence: float = 40.0,
        minimum_alphanumeric_characters: int = 2,
        engine_factory: Callable[..., Any] | None = None,
        verify_hashes: bool = True,
    ) -> None:
        if not languages:
            raise ValueError("At least one OCR language is required")
        unsupported = sorted(set(languages) - {"rus", "eng"})
        if unsupported:
            raise ValueError(
                "RapidOCR bundled model supports only rus and eng: "
                + ", ".join(unsupported)
            )
        self.model_dir = Path(model_dir) if model_dir is not None else default_rapidocr_model_dir()
        self.languages = tuple(languages)
        self.minimum_confidence = minimum_confidence
        self.minimum_alphanumeric_characters = minimum_alphanumeric_characters
        self._engine_factory = engine_factory
        self.verify_hashes = verify_hashes
        self._engine: Any | None = None

    def recognize(self, image_path: Path) -> OcrResult:
        path = Path(image_path)
        if not path.is_file():
            raise OcrExecutionError(f"OCR input is not a file: {path}")
        engine = self._load_engine()
        try:
            output = engine(path)
        except Exception as exc:
            raise OcrExecutionError(
                f"RapidOCR failed for {path.name}: {type(exc).__name__}: {exc}"
            ) from exc

        texts = tuple(str(value).strip() for value in (getattr(output, "txts", ()) or ()))
        raw_scores = tuple(float(value) for value in (getattr(output, "scores", ()) or ()))
        if len(texts) != len(raw_scores):
            raise OcrExecutionError("RapidOCR returned inconsistent text and score counts")
        scores = tuple(value * 100.0 if value <= 1.0 else value for value in raw_scores)
        words = tuple(
            OcrWord(text, score, 1, 1, 1, line_number, 1)
            for line_number, (text, score) in enumerate(zip(texts, scores), start=1)
            if text
        )
        text = "\n".join(word.text for word in words).strip()
        mean_confidence = (
            sum(word.confidence for word in words) / len(words) if words else None
        )
        alphanumeric_count = sum(character.isalnum() for character in text)
        readable = bool(
            text
            and mean_confidence is not None
            and mean_confidence >= self.minimum_confidence
            and alphanumeric_count >= self.minimum_alphanumeric_characters
        )
        diagnostics: list[OcrDiagnostic] = []
        if not text:
            diagnostics.append(OcrDiagnostic("ocr_no_text", "OCR did not detect any text"))
        elif alphanumeric_count < self.minimum_alphanumeric_characters:
            diagnostics.append(
                OcrDiagnostic(
                    "ocr_too_little_text",
                    "OCR detected too few alphanumeric characters to be reliable",
                )
            )
        if mean_confidence is not None and mean_confidence < self.minimum_confidence:
            diagnostics.append(
                OcrDiagnostic(
                    "ocr_low_confidence",
                    f"Mean OCR confidence {mean_confidence:.1f} is below the required {self.minimum_confidence:.1f}",
                )
            )
        return OcrResult(
            text=text,
            words=words,
            mean_confidence=mean_confidence,
            languages=self.languages,
            engine="rapidocr-onnxruntime",
            readable=readable,
            diagnostics=tuple(diagnostics),
        )

    def _load_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        paths = self._validated_model_paths()
        factory = self._engine_factory
        rapidocr_enums: dict[str, Any] | None = None
        if factory is None:
            try:
                from rapidocr import (
                    EngineType,
                    LangCls,
                    LangDet,
                    LangRec,
                    ModelType,
                    OCRVersion,
                    RapidOCR,
                )
            except (ImportError, OSError) as exc:
                raise OcrUnavailableError(
                    "RapidOCR is unavailable; install the project OCR extra"
                ) from exc
            factory = RapidOCR
            rapidocr_enums = {
                "engine": EngineType.ONNXRUNTIME,
                "det_language": LangDet.MULTI,
                "cls_language": LangCls.CH,
                "rec_language": LangRec.CYRILLIC,
                "mobile": ModelType.MOBILE,
                "small": ModelType.SMALL,
                "v4": OCRVersion.PPOCRV4,
                "v5": OCRVersion.PPOCRV5,
                "v6": OCRVersion.PPOCRV6,
            }
        params = {
            "Global.log_level": "error",
            "Global.use_det": True,
            "Global.use_cls": True,
            "Global.use_rec": True,
            "Det.engine_type": "onnxruntime",
            "Det.lang_type": "multi",
            "Det.model_type": "small",
            "Det.ocr_version": "PP-OCRv6",
            "Det.model_path": paths["detection.onnx"],
            "Cls.engine_type": "onnxruntime",
            "Cls.lang_type": "ch",
            "Cls.model_type": "mobile",
            "Cls.ocr_version": "PP-OCRv4",
            "Cls.model_path": paths["classification.onnx"],
            "Rec.engine_type": "onnxruntime",
            "Rec.lang_type": "cyrillic",
            "Rec.model_type": "mobile",
            "Rec.ocr_version": "PP-OCRv5",
            "Rec.model_path": paths["recognition-cyrillic.onnx"],
        }
        if rapidocr_enums is not None:
            params.update(
                {
                    "Det.engine_type": rapidocr_enums["engine"],
                    "Det.lang_type": rapidocr_enums["det_language"],
                    "Det.model_type": rapidocr_enums["small"],
                    "Det.ocr_version": rapidocr_enums["v6"],
                    "Cls.engine_type": rapidocr_enums["engine"],
                    "Cls.lang_type": rapidocr_enums["cls_language"],
                    "Cls.model_type": rapidocr_enums["mobile"],
                    "Cls.ocr_version": rapidocr_enums["v4"],
                    "Rec.engine_type": rapidocr_enums["engine"],
                    "Rec.lang_type": rapidocr_enums["rec_language"],
                    "Rec.model_type": rapidocr_enums["mobile"],
                    "Rec.ocr_version": rapidocr_enums["v5"],
                }
            )
        try:
            self._engine = factory(params=params)
        except Exception as exc:
            raise OcrUnavailableError(
                f"Cannot initialize RapidOCR from local models: {type(exc).__name__}: {exc}"
            ) from exc
        return self._engine

    def _validated_model_paths(self) -> dict[str, Path]:
        root = self.model_dir.expanduser().resolve(strict=False)
        resolved: dict[str, Path] = {}
        for status in rapidocr_model_status(root):
            filename = str(status["file"])
            path = root / filename
            if not status["available"]:
                raise OcrUnavailableError(f"RapidOCR model is missing: {path}")
            if self.verify_hashes and not status["checksumValid"]:
                raise OcrUnavailableError(f"RapidOCR model checksum mismatch: {path}")
            resolved[filename] = path
        return resolved


def create_ocr_backend(
    engine: str,
    *,
    languages: Sequence[str],
    rapidocr_model_dir: Path,
) -> OcrBackend:
    if engine == "rapidocr":
        return RapidOcrBackend(model_dir=rapidocr_model_dir, languages=languages)
    if engine == "tesseract":
        return TesseractOcr(languages=languages)
    raise ValueError(f"Unknown OCR engine: {engine}")


def parse_tesseract_tsv(
    tsv: str,
    *,
    languages: Sequence[str] = (),
    engine: str = "tesseract",
    minimum_confidence: float = 40.0,
    minimum_alphanumeric_characters: int = 2,
) -> OcrResult:
    """Parse Tesseract TSV and rebuild text using its line coordinates."""

    words: list[OcrWord] = []
    lines: dict[tuple[int, int, int, int], list[OcrWord]] = defaultdict(list)
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t")
    required = {
        "page_num",
        "block_num",
        "par_num",
        "line_num",
        "word_num",
        "conf",
        "text",
    }
    if reader.fieldnames is None or not required.issubset(reader.fieldnames):
        raise OcrExecutionError("Tesseract returned malformed TSV output")

    for row in reader:
        value = (row.get("text") or "").strip()
        if not value:
            continue
        try:
            confidence = float(row.get("conf") or "-1")
            if confidence < 0:
                continue
            item = OcrWord(
                text=value,
                confidence=confidence,
                page=int(row.get("page_num") or 0),
                block=int(row.get("block_num") or 0),
                paragraph=int(row.get("par_num") or 0),
                line=int(row.get("line_num") or 0),
                word=int(row.get("word_num") or 0),
            )
        except (TypeError, ValueError) as exc:
            raise OcrExecutionError("Tesseract returned malformed TSV values") from exc
        words.append(item)
        lines[(item.page, item.block, item.paragraph, item.line)].append(item)

    rendered_lines = [
        " ".join(word.text for word in sorted(lines[key], key=lambda word: word.word))
        for key in sorted(lines)
    ]
    text = "\n".join(line for line in rendered_lines if line).strip()
    mean_confidence = (
        sum(word.confidence for word in words) / len(words) if words else None
    )
    alphanumeric_count = sum(character.isalnum() for character in text)
    readable = bool(
        text
        and mean_confidence is not None
        and mean_confidence >= minimum_confidence
        and alphanumeric_count >= minimum_alphanumeric_characters
    )

    diagnostics: list[OcrDiagnostic] = []
    if not text:
        diagnostics.append(OcrDiagnostic("ocr_no_text", "OCR did not detect any text"))
    elif alphanumeric_count < minimum_alphanumeric_characters:
        diagnostics.append(
            OcrDiagnostic(
                "ocr_too_little_text",
                "OCR detected too few alphanumeric characters to be reliable",
            )
        )
    if mean_confidence is not None and mean_confidence < minimum_confidence:
        diagnostics.append(
            OcrDiagnostic(
                "ocr_low_confidence",
                (
                    f"Mean OCR confidence {mean_confidence:.1f} is below "
                    f"the required {minimum_confidence:.1f}"
                ),
            )
        )

    return OcrResult(
        text=text,
        words=tuple(words),
        mean_confidence=mean_confidence,
        languages=tuple(languages),
        engine=engine,
        readable=readable,
        diagnostics=tuple(diagnostics),
    )


class TesseractOcr:
    """Local Tesseract adapter using Russian and English by default."""

    def __init__(
        self,
        *,
        executable: str = "tesseract",
        languages: Sequence[str] = ("rus", "eng"),
        page_segmentation_mode: int = 3,
        minimum_confidence: float = 40.0,
        minimum_alphanumeric_characters: int = 2,
        timeout_seconds: float = 120.0,
        runner: RunCommand = subprocess.run,
    ) -> None:
        if not languages:
            raise ValueError("At least one OCR language is required")
        self.executable = executable
        self.languages = tuple(languages)
        self.page_segmentation_mode = page_segmentation_mode
        self.minimum_confidence = minimum_confidence
        self.minimum_alphanumeric_characters = minimum_alphanumeric_characters
        self.timeout_seconds = timeout_seconds
        self._runner = runner
        self._available_languages: tuple[str, ...] | None = None

    def available_languages(self) -> tuple[str, ...]:
        if self._available_languages is not None:
            return self._available_languages
        self._ensure_executable()
        completed = self._run([self.executable, "--list-langs"])
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown error").strip()
            raise OcrUnavailableError(f"Cannot list Tesseract languages: {detail}")
        languages = []
        for raw_line in completed.stdout.splitlines():
            line = raw_line.strip()
            if not line or line.lower().startswith("list of available languages"):
                continue
            languages.append(line)
        self._available_languages = tuple(languages)
        return self._available_languages

    def recognize(self, image_path: Path) -> OcrResult:
        path = Path(image_path)
        if not path.is_file():
            raise OcrExecutionError(f"OCR input is not a file: {path}")
        available = set(self.available_languages())
        missing = [language for language in self.languages if language not in available]
        if missing:
            raise OcrUnavailableError(
                "Tesseract language data is missing: " + ", ".join(missing)
            )
        command = [
            self.executable,
            str(path),
            "stdout",
            "-l",
            "+".join(self.languages),
            "--psm",
            str(self.page_segmentation_mode),
            "tsv",
        ]
        completed = self._run(command)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown error").strip()
            raise OcrExecutionError(f"Tesseract failed for {path.name}: {detail}")
        return parse_tesseract_tsv(
            completed.stdout,
            languages=self.languages,
            engine="tesseract-cli",
            minimum_confidence=self.minimum_confidence,
            minimum_alphanumeric_characters=self.minimum_alphanumeric_characters,
        )

    def _ensure_executable(self) -> None:
        if shutil.which(self.executable) is None:
            raise OcrUnavailableError(
                f"Tesseract executable was not found: {self.executable}"
            )

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OcrExecutionError(
                f"Tesseract exceeded {self.timeout_seconds:.0f} seconds"
            ) from exc
        except OSError as exc:
            raise OcrUnavailableError(f"Cannot start Tesseract: {exc}") from exc
