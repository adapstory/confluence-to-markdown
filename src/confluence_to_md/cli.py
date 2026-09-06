from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .converter import ConfigurationError, ConversionOptions, convert_export


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="confluence-to-md",
        description="Convert an unpacked Confluence Scroll HTML export to loss-aware portable Markdown.",
    )
    parser.add_argument("source_root", type=Path, help="Unpacked export root directory")
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="New, empty, or matching previous confluence-to-md output directory",
    )
    parser.add_argument(
        "--ocr-language",
        default="rus+eng",
        help="OCR language expression (default: rus+eng)",
    )
    parser.add_argument(
        "--ocr-engine",
        choices=("rapidocr", "tesseract"),
        default="rapidocr",
        help="Local OCR engine (default: rapidocr)",
    )
    parser.add_argument(
        "--rapidocr-model-dir",
        type=Path,
        default=Path("models/rapidocr"),
        help="Directory containing the three pinned RapidOCR ONNX models",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    options = ConversionOptions(
        source_root=args.source_root,
        output_root=args.output,
        ocr_language=args.ocr_language,
        ocr_engine=args.ocr_engine,
        rapidocr_model_dir=args.rapidocr_model_dir,
    )
    try:
        outcome = convert_export(options)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    stats = outcome.stats
    print(
        "Conversion complete: "
        f"pages={stats.converted_pages}/{stats.discovered_pages}, "
        f"resources={stats.copied_resources}, "
        f"attachments={stats.extracted_attachments}, "
        f"ocr_images={stats.ocr_images}, "
        f"drawio_models={stats.drawio_models}, "
        f"warnings={len(outcome.issues.warnings)}, "
        f"errors={len(outcome.issues.errors)}"
    )
    print(f"Report: {outcome.report_path}")
    return 1 if outcome.issues.errors else 0
