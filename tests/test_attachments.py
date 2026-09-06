from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from confluence_to_md.attachments import extract_attachment, extract_pdf_attachment
from confluence_to_md.ocr import OcrResult, OcrWord

WORK_ROOT = Path(__file__).parent / ".work"


def local_temporary_directory() -> tempfile.TemporaryDirectory[str]:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=WORK_ROOT)


class FakeOcr:
    def recognize(self, image_path: Path) -> OcrResult:
        return OcrResult(
            text="Распознанный text",
            words=(OcrWord("text", 99.0, 1, 1, 1, 1, 1),),
            mean_confidence=99.0,
            languages=("rus", "eng"),
            engine="fake",
            readable=True,
        )


def _blank_pdf_bytes() -> bytes:
    header = b"%PDF-1.4\n"
    objects = [
        b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n",
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n",
        b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>endobj\n",
        b"4 0 obj<< /Length 0 >>stream\n\nendstream\nendobj\n",
    ]
    body = bytearray(header)
    offsets = [0]
    for obj in objects:
        offsets.append(len(body))
        body.extend(obj)
    xref_start = len(body)
    xref = [b"xref\n0 5\n0000000000 65535 f \n"]
    xref.extend(f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets[1:])
    trailer = (
        b"trailer<< /Size 5 /Root 1 0 R >>\n"
        b"startxref\n"
        + str(xref_start).encode("ascii")
        + b"\n%%EOF\n"
    )
    body.extend(b"".join(xref))
    body.extend(trailer)
    return bytes(body)


class AttachmentTest(unittest.TestCase):
    def test_extracts_eml_embedded_png_and_pdf_with_shared_attachment_pipeline(self) -> None:
        png_bytes = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde"
            b"\x00\x00\x00\x0cIDATx\x9cc`\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xd8\x16\x1d\x1b"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        message = EmailMessage()
        message["Subject"] = "Embedded resources"
        message["From"] = "sender@example.test"
        message["To"] = "recipient@example.test"
        message.set_content("Body text")
        message.add_attachment(
            png_bytes,
            maintype="image",
            subtype="png",
            filename="image.png",
        )
        message.add_attachment(
            _blank_pdf_bytes(),
            maintype="application",
            subtype="pdf",
            filename="scan.pdf",
        )

        class RecordingOcr:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def recognize(self, image_path: Path) -> OcrResult:
                self.calls.append(image_path.name)
                return OcrResult(
                    text=f"OCR:{image_path.name}",
                    words=(OcrWord("OCR", 99.0, 1, 1, 1, 1, 1),),
                    mean_confidence=99.0,
                    languages=("rus", "eng"),
                    engine="fake",
                    readable=True,
                )

        with local_temporary_directory() as directory_name:
            directory = Path(directory_name)
            eml_path = directory / "mail.eml"
            eml_path.write_bytes(message.as_bytes())
            ocr = RecordingOcr()
            result = extract_attachment(
                eml_path,
                ocr_backend=ocr,
                work_directory=directory / "work",
            )

        self.assertTrue(result.ok)
        self.assertEqual([], [item.code for item in result.errors])
        self.assertIn("Body text", result.text)
        self.assertEqual(2, len(ocr.calls))
        self.assertIn("OCR:", result.text)

    def test_extracts_json_csv_and_eml(self) -> None:
        with local_temporary_directory() as directory_name:
            directory = Path(directory_name)
            json_path = directory / "data.json"
            json_path.write_text('{"ключ": 1}', encoding="utf-8")
            csv_path = directory / "data.csv"
            csv_path.write_text("name,value\nalpha,2\n", encoding="utf-8")
            eml_path = directory / "mail.eml"
            eml_path.write_bytes(
                b"Subject: Example\nFrom: a@example.test\nTo: b@example.test\n"
                b"Content-Type: text/plain; charset=utf-8\n\nBody text"
            )

            json_result = extract_attachment(json_path)
            csv_result = extract_attachment(csv_path)
            eml_result = extract_attachment(eml_path)

        self.assertTrue(json_result.ok)
        self.assertEqual({"ключ": 1}, json.loads(json_result.text))
        self.assertIn("Column 2: value", csv_result.text)
        self.assertIn("Body text", eml_result.text)

    def test_csv_bad_sniffer_delimiter_does_not_abort_attachment_extraction(self) -> None:
        with local_temporary_directory() as directory_name:
            csv_path = Path(directory_name) / "data.csv"
            csv_path.write_text("name,value\nalpha,2\n", encoding="utf-8")

            with patch(
                "confluence_to_md.attachments.csv.Sniffer.sniff",
                side_effect=ValueError("bad delimiter value"),
            ):
                result = extract_attachment(csv_path)

        self.assertTrue(result.ok)
        self.assertIn("Column 1: name", result.text)
        self.assertIn("Column 2: value", result.text)

    def test_extracts_eml_html_body_and_preserves_href_targets(self) -> None:
        message = EmailMessage()
        message["Subject"] = "HTML body"
        message.set_content(
            '<p>Open <a href="https://example.test/portal">portal</a>.</p>',
            subtype="html",
        )

        with local_temporary_directory() as directory_name:
            path = Path(directory_name) / "mail.eml"
            path.write_bytes(message.as_bytes())
            result = extract_attachment(path)

        self.assertTrue(result.ok)
        self.assertIn("portal <https://example.test/portal>", result.text)

    def test_eml_preserves_plain_and_html_alternatives_with_links(self) -> None:
        message = EmailMessage()
        message["Subject"] = "Alternative bodies"
        message.set_content("Open the portal")
        message.add_alternative(
            '<p>Open the <a href="https://example.test/portal">portal</a></p>',
            subtype="html",
        )
        with local_temporary_directory() as directory_name:
            path = Path(directory_name) / "alternatives.eml"
            path.write_bytes(message.as_bytes())
            result = extract_attachment(path)

        self.assertTrue(result.ok)
        self.assertIn("Text body:\nOpen the portal", result.text)
        self.assertIn(
            "HTML body:\nOpen the portal <https://example.test/portal>",
            result.text,
        )

    def test_eml_html_cid_image_is_ocrd_without_false_unextracted_error(self) -> None:
        message = EmailMessage()
        message["Subject"] = "Inline image"
        message.set_content(
            '<p>Screenshot</p><img src="cid:image001@example.test">',
            subtype="html",
        )
        message.add_related(
            b"synthetic image",
            maintype="image",
            subtype="png",
            cid="<image001@example.test>",
            filename="image001.png",
        )
        with local_temporary_directory() as directory_name:
            directory = Path(directory_name)
            path = directory / "inline.eml"
            path.write_bytes(message.as_bytes())
            result = extract_attachment(
                path,
                ocr_backend=FakeOcr(),
                work_directory=directory / "work",
            )

        self.assertTrue(result.ok)
        self.assertNotIn(
            "attachment_html_media_unextracted",
            {item.code for item in result.diagnostics},
        )
        self.assertIn("Attachment image001.png (image):", result.text)
        self.assertIn("Распознанный text", result.text)

    def test_extracts_docx_with_table_span(self) -> None:
        document = """<w:document xmlns:w="urn:w"><w:body>
        <w:p><w:r><w:t>Hello document</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>
        <w:p><w:r><w:t>Wide cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        </w:body></w:document>"""
        with local_temporary_directory() as directory_name:
            path = Path(directory_name) / "sample.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", document)
            result = extract_attachment(path)

        self.assertTrue(result.ok)
        self.assertIn("Hello document", result.text)
        self.assertIn("colspan=2", result.text)

    def test_extracts_pptx_slides_and_notes(self) -> None:
        slide = '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:cSld><a:p><a:r><a:t>Slide text</a:t></a:r></a:p></p:cSld></p:sld>'
        notes = '<p:notes xmlns:p="urn:p" xmlns:a="urn:a"><a:p><a:r><a:t>Speaker note</a:t></a:r></a:p></p:notes>'
        with local_temporary_directory() as directory_name:
            path = Path(directory_name) / "sample.pptx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("ppt/slides/slide1.xml", slide)
                archive.writestr("ppt/notesSlides/notesSlide1.xml", notes)
            result = extract_attachment(path)

        self.assertTrue(result.ok)
        self.assertIn("Slide text", result.text)
        self.assertIn("Speaker note", result.text)

    def test_extracts_xlsx_values_formulas_and_merges(self) -> None:
        workbook = """<workbook xmlns:r="urn:r"><sheets>
        <sheet name="Sheet A" state="hidden" r:id="rId1"/></sheets></workbook>"""
        relationships = """<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="worksheet"/></Relationships>"""
        strings = "<sst><si><t>Header</t></si></sst>"
        sheet = """<worksheet><sheetData><row r="1">
        <c r="A1" t="s"><v>0</v></c><c r="B1"><f>1+1</f><v>2</v></c>
        </row></sheetData><mergeCells><mergeCell ref="A2:B2"/></mergeCells></worksheet>"""
        with local_temporary_directory() as directory_name:
            path = Path(directory_name) / "sample.xlsx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("xl/workbook.xml", workbook)
                archive.writestr("xl/_rels/workbook.xml.rels", relationships)
                archive.writestr("xl/sharedStrings.xml", strings)
                archive.writestr("xl/worksheets/sheet1.xml", sheet)
            result = extract_attachment(path)

        self.assertTrue(result.ok)
        self.assertIn("state=hidden", result.text)
        self.assertIn("Merged cells: A2:B2", result.text)
        self.assertIn("Cell B1 (formula=1+1): 2", result.text)

    def test_extracts_image_with_injected_ocr(self) -> None:
        with local_temporary_directory() as directory_name:
            path = Path(directory_name) / "image.png"
            path.write_bytes(b"fixture")
            result = extract_attachment(path, ocr_backend=FakeOcr())

        self.assertTrue(result.ok)
        self.assertEqual("Распознанный text", result.text)
        self.assertEqual("99.00", result.metadata_dict()["ocr_mean_confidence"])

    def test_extracts_html_and_preserves_link_targets(self) -> None:
        with local_temporary_directory() as directory_name:
            path = Path(directory_name) / "attachment.html"
            path.write_text(
                '<h1>Title</h1><p>Read <a href="child.html#part">child</a>.</p>',
                encoding="utf-8",
            )
            result = extract_attachment(path)

        self.assertTrue(result.ok)
        self.assertIn("# Title", result.text)
        self.assertIn("child <child.html#part>", result.text)

    def test_extracts_svg_text_and_flags_embedded_raster(self) -> None:
        with local_temporary_directory() as directory_name:
            path = Path(directory_name) / "diagram.svg"
            path.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg"><title>Flow</title>'
                '<text><tspan>Start</tspan></text><image href="scan.png"/></svg>',
                encoding="utf-8",
            )
            result = extract_attachment(path)

        self.assertIn("Title: Flow", result.text)
        self.assertIn("Text: Start", result.text)
        self.assertIn("Vector shapes: 0", result.text)
        self.assertIn("SVG XML (complete vector structure):", result.text)
        self.assertIn("<ns0:image href=\"scan.png\"", result.text)
        self.assertFalse(result.ok)
        self.assertIn(
            "attachment_svg_embedded_image_unextracted",
            {item.code for item in result.errors},
        )

    def test_svg_without_text_preserves_complete_vector_xml(self) -> None:
        with local_temporary_directory() as directory_name:
            path = Path(directory_name) / "vector.svg"
            path.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg">'
                '<rect id="box" x="1" y="2" width="3" height="4"/>'
                "</svg>",
                encoding="utf-8",
            )
            result = extract_attachment(path)

        self.assertTrue(result.ok)
        self.assertIn("Vector shapes: 1", result.text)
        self.assertIn('id="box"', result.text)
        self.assertIn('width="3"', result.text)

    def test_office_embedded_media_is_a_hard_error(self) -> None:
        document = '<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>Text</w:t></w:r></w:p></w:body></w:document>'
        with local_temporary_directory() as directory_name:
            path = Path(directory_name) / "with-image.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", document)
                archive.writestr("word/media/image1.png", b"image")
            result = extract_attachment(path)

        self.assertFalse(result.ok)
        self.assertIn(
            "attachment_docx_embedded_media_unextracted",
            {item.code for item in result.errors},
        )

    def test_sparse_pdf_requires_explicit_work_directory(self) -> None:
        with local_temporary_directory() as directory_name:
            path = Path(directory_name) / "sparse.pdf"
            path.write_bytes(b"fixture")
            with patch(
                "confluence_to_md.attachments._pdf_native_pymupdf",
                return_value=["short"],
            ):
                result = extract_pdf_attachment(path)

        self.assertFalse(result.ok)
        self.assertIn(
            "attachment_pdf_work_directory_required",
            {item.code for item in result.errors},
        )

    def test_pdf_render_prefers_pymupdf(self) -> None:
        with local_temporary_directory() as directory_name:
            directory = Path(directory_name)
            path = directory / "sparse.pdf"
            path.write_bytes(b"fixture")
            rendered = directory / "page-1.png"
            rendered.write_bytes(b"png")
            with (
                patch(
                    "confluence_to_md.attachments._pdf_native_pymupdf",
                    return_value=[""],
                ),
                patch(
                    "confluence_to_md.attachments._render_pdf_page_pymupdf",
                    return_value=rendered,
                ) as pymupdf_render,
                patch(
                    "confluence_to_md.attachments._render_pdf_page_pdftoppm"
                ) as pdftoppm_render,
            ):
                result = extract_pdf_attachment(
                    path,
                    ocr_backend=FakeOcr(),
                    work_directory=directory / "work",
                )

        self.assertTrue(result.ok)
        pymupdf_render.assert_called_once()
        pdftoppm_render.assert_not_called()
        self.assertIn("Native text:\n[no extractable native text]", result.text)
        self.assertIn("OCR text:\nРаспознанный text", result.text)

    def test_pdf_rich_native_page_still_runs_render_and_ocr(self) -> None:
        with local_temporary_directory() as directory_name:
            directory = Path(directory_name)
            path = directory / "mixed.pdf"
            path.write_bytes(b"fixture")
            rendered = directory / "page-1.png"
            rendered.write_bytes(b"png")

            class RecordingOcr:
                def __init__(self) -> None:
                    self.calls: list[str] = []

                def recognize(self, image_path: Path) -> OcrResult:
                    self.calls.append(image_path.name)
                    return OcrResult(
                        text="IMAGE TEXT",
                        words=(OcrWord("IMAGE", 99.0, 1, 1, 1, 1, 1),),
                        mean_confidence=99.0,
                        languages=("rus", "eng"),
                        engine="fake",
                        readable=True,
                    )

            ocr = RecordingOcr()
            with (
                patch(
                    "confluence_to_md.attachments._pdf_native_pymupdf",
                    return_value=["A" * 200],
                ),
                patch(
                    "confluence_to_md.attachments._render_pdf_page_pymupdf",
                    return_value=rendered,
                ) as pymupdf_render,
                patch(
                    "confluence_to_md.attachments._render_pdf_page_pdftoppm"
                ) as pdftoppm_render,
            ):
                result = extract_pdf_attachment(
                    path,
                    ocr_backend=ocr,
                    work_directory=directory / "work",
                )

        self.assertTrue(result.ok)
        pymupdf_render.assert_called_once()
        pdftoppm_render.assert_not_called()
        self.assertEqual(["page-1.png"], ocr.calls)
        self.assertIn("Native text:\n" + ("A" * 200), result.text)
        self.assertIn("OCR text:\nIMAGE TEXT", result.text)

    def test_successful_pdf_fallback_does_not_emit_nonvisual_warning(self) -> None:
        with local_temporary_directory() as directory_name:
            directory = Path(directory_name)
            path = directory / "native.pdf"
            path.write_bytes(b"fixture")
            rendered = directory / "page-1.png"
            rendered.write_bytes(b"png")
            with (
                patch(
                    "confluence_to_md.attachments._pdf_native_pymupdf",
                    side_effect=RuntimeError("preferred backend failed"),
                ),
                patch(
                    "confluence_to_md.attachments._pdf_native_pdftotext",
                    return_value=["Enough deterministic native text for the fallback backend."],
                ),
                patch(
                    "confluence_to_md.attachments._render_pdf_page_pymupdf",
                    return_value=rendered,
                ),
            ):
                result = extract_pdf_attachment(
                    path,
                    ocr_backend=FakeOcr(),
                    work_directory=directory / "work",
                )

        self.assertTrue(result.ok)
        self.assertFalse(result.diagnostics)
        self.assertIn("OCR text:\nРаспознанный text", result.text)

    def test_unsupported_attachment_is_a_hard_error(self) -> None:
        with local_temporary_directory() as directory_name:
            path = Path(directory_name) / "archive.bin"
            path.write_bytes(b"unknown")
            result = extract_attachment(path)

        self.assertFalse(result.ok)
        self.assertEqual("attachment_type_unsupported", result.errors[0].code)


if __name__ == "__main__":
    unittest.main()
