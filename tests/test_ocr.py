from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from confluence_to_md.ocr import (
    OcrUnavailableError,
    RapidOcrBackend,
    TesseractOcr,
    parse_tesseract_tsv,
)

WORK_ROOT = Path(__file__).parent / ".work"


def local_temporary_directory() -> tempfile.TemporaryDirectory[str]:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=WORK_ROOT)


TSV = """level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext
5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t95.0\tHello
5\t1\t1\t1\t1\t2\t10\t0\t10\t10\t85.0\tмир
5\t1\t1\t1\t2\t1\t0\t20\t10\t10\t90.0\tSecond
"""


class ParseTesseractTsvTest(unittest.TestCase):
    def test_rebuilds_lines_and_confidence(self) -> None:
        result = parse_tesseract_tsv(TSV, languages=("rus", "eng"))

        self.assertEqual("Hello мир\nSecond", result.text)
        self.assertEqual(90.0, result.mean_confidence)
        self.assertTrue(result.readable)
        self.assertEqual(("rus", "eng"), result.languages)

    def test_low_confidence_is_an_error(self) -> None:
        low = TSV.replace("95.0", "5.0").replace("85.0", "5.0").replace("90.0", "5.0")
        result = parse_tesseract_tsv(low)

        self.assertFalse(result.ok)
        self.assertEqual("ocr_low_confidence", result.diagnostics[0].code)


class TesseractOcrTest(unittest.TestCase):
    def test_runs_tsv_with_requested_languages(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if "--list-langs" in command:
                return subprocess.CompletedProcess(
                    command, 0, "List of available languages (2):\neng\nrus\n", ""
                )
            return subprocess.CompletedProcess(command, 0, TSV, "")

        with local_temporary_directory() as directory:
            image = Path(directory) / "image.png"
            image.write_bytes(b"not decoded by fake runner")
            backend = TesseractOcr(executable=sys.executable, runner=runner)
            result = backend.recognize(image)

        self.assertTrue(result.ok)
        self.assertIn("rus+eng", calls[1])
        self.assertEqual("tsv", calls[1][-1])

    def test_missing_language_is_reported(self) -> None:
        def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, "eng\n", "")

        with local_temporary_directory() as directory:
            image = Path(directory) / "image.png"
            image.write_bytes(b"x")
            backend = TesseractOcr(executable=sys.executable, runner=runner)
            with self.assertRaises(OcrUnavailableError):
                backend.recognize(image)


class RapidOcrBackendTest(unittest.TestCase):
    def test_uses_explicit_local_models_and_returns_normalized_result(self) -> None:
        captured: dict[str, object] = {}

        class Output:
            txts = ("Hello", "мир")
            scores = (0.95, 0.85)

        class Engine:
            def __call__(self, _path: Path) -> Output:
                return Output()

        def factory(*, params: dict[str, object]) -> Engine:
            captured.update(params)
            return Engine()

        with local_temporary_directory() as directory:
            root = Path(directory)
            image = root / "image.png"
            image.write_bytes(b"fixture")
            for name in (
                "detection.onnx",
                "classification.onnx",
                "recognition-cyrillic.onnx",
            ):
                (root / name).write_bytes(b"model")
            backend = RapidOcrBackend(
                model_dir=root,
                engine_factory=factory,
                verify_hashes=False,
            )

            result = backend.recognize(image)

        self.assertTrue(result.ok)
        self.assertEqual("Hello\nмир", result.text)
        self.assertEqual(90.0, result.mean_confidence)
        self.assertEqual("rapidocr-onnxruntime", result.engine)
        self.assertEqual("error", captured["Global.log_level"])
        self.assertEqual("cyrillic", captured["Rec.lang_type"])
        self.assertTrue(str(captured["Rec.model_path"]).endswith("recognition-cyrillic.onnx"))

    def test_missing_local_model_is_reported_without_initializing_engine(self) -> None:
        with local_temporary_directory() as directory:
            root = Path(directory)
            image = root / "image.png"
            image.write_bytes(b"fixture")
            backend = RapidOcrBackend(
                model_dir=root,
                engine_factory=lambda **_: self.fail("factory must not be called"),
            )

            with self.assertRaisesRegex(OcrUnavailableError, "model is missing"):
                backend.recognize(image)


if __name__ == "__main__":
    unittest.main()
