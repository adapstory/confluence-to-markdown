from __future__ import annotations

import hashlib
import io
import json
import struct
import unittest
import xml.etree.ElementTree as ET
import zipfile
import zlib

from fixture_factory import (
    BUILDER,
    FIXTURE_ROOT,
    expected_artifacts,
    materialized_paths,
)


class SyntheticExportFixtureTest(unittest.TestCase):
    def test_materialized_fixture_matches_generator_byte_for_byte(self) -> None:
        expected = expected_artifacts()
        self.assertEqual(tuple(expected), materialized_paths())
        for relative_path, content in expected.items():
            with self.subTest(path=relative_path):
                self.assertEqual(content, (FIXTURE_ROOT / relative_path).read_bytes())

    def test_expected_export_paths_are_present(self) -> None:
        attachment_root = "attachments/999999999999/1"
        required = {
            "_codex_conversion_fixture.html",
            "_codex_conversion_fixture_child.html",
            "_codex_conversion_fixture_toc.html",
            "_codex_conversion_fixture_manifest.json",
            "images/inline/codex-conversion-fixture-bilingual.png",
            "images/inline/codex-conversion-fixture-flow.png",
            f"{attachment_root}/codex-conversion-fixture-text.pdf",
            f"{attachment_root}/codex-conversion-fixture-scan.pdf",
            f"{attachment_root}/codex-conversion-fixture-requirements.docx",
            f"{attachment_root}/codex-conversion-fixture-briefing.pptx",
            f"{attachment_root}/codex-conversion-fixture-metrics.xlsx",
            f"{attachment_root}/codex-conversion-fixture-notes.txt",
            f"{attachment_root}/codex-conversion-fixture-guide.md",
            f"{attachment_root}/codex-conversion-fixture-data.csv",
            f"{attachment_root}/codex-conversion-fixture-data.json",
            f"{attachment_root}/codex-conversion-fixture-config.yaml",
            f"{attachment_root}/codex-conversion-fixture-message.eml",
            f"{attachment_root}/codex-conversion-fixture-fragment.html",
            f"{attachment_root}/codex-conversion-fixture-vector.svg",
            f"{attachment_root}/codex-conversion-fixture-flow.drawio",
        }
        self.assertLessEqual(required, set(materialized_paths()))

    def test_bilingual_png_is_a_real_nonempty_rgb_raster(self) -> None:
        payload = (FIXTURE_ROOT / "images/inline/codex-conversion-fixture-bilingual.png").read_bytes()
        self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))
        chunks: dict[bytes, list[bytes]] = {}
        offset = 8
        while offset < len(payload):
            length = struct.unpack(">I", payload[offset : offset + 4])[0]
            kind = payload[offset + 4 : offset + 8]
            data = payload[offset + 8 : offset + 8 + length]
            chunks.setdefault(kind, []).append(data)
            offset += 12 + length
        width, height, bit_depth, color_type, _, _, _ = struct.unpack(">IIBBBBB", chunks[b"IHDR"][0])
        self.assertGreater(width, 500)
        self.assertGreater(height, 100)
        self.assertEqual((bit_depth, color_type), (8, 2))
        raw = zlib.decompress(b"".join(chunks[b"IDAT"]))
        stride = width * 3 + 1
        self.assertEqual(len(raw), stride * height)
        rows: list[bytes] = []
        previous = bytearray(width * 3)
        for row in range(height):
            start = row * stride
            filter_type = raw[start]
            self.assertIn(filter_type, range(5))
            current = bytearray(raw[start + 1 : start + stride])
            for index, encoded in enumerate(tuple(current)):
                left = current[index - 3] if index >= 3 else 0
                above = previous[index]
                upper_left = previous[index - 3] if index >= 3 else 0
                if filter_type == 1:
                    predictor = left
                elif filter_type == 2:
                    predictor = above
                elif filter_type == 3:
                    predictor = (left + above) // 2
                elif filter_type == 4:
                    estimate = left + above - upper_left
                    distances = (abs(estimate - left), abs(estimate - above), abs(estimate - upper_left))
                    predictor = (left, above, upper_left)[distances.index(min(distances))]
                else:
                    predictor = 0
                current[index] = (encoded + predictor) & 0xFF
            rows.append(bytes(current))
            previous = current
        raster = b"".join(rows)
        self.assertIn(bytes((255, 255, 255)), raster)
        self.assertIn(bytes((17, 24, 39)), raster)
        metadata = b"".join(chunks[b"iTXt"]).decode("utf-8")
        self.assertIn("ENGLISH TEXT", metadata)
        self.assertIn("РУССКИЙ ТЕКСТ", metadata)

    def test_text_and_scanned_pdfs_have_distinct_valid_shapes(self) -> None:
        root = FIXTURE_ROOT / "attachments/999999999999/1"
        text_pdf = (root / "codex-conversion-fixture-text.pdf").read_bytes()
        scan_pdf = (root / "codex-conversion-fixture-scan.pdf").read_bytes()
        for payload in (text_pdf, scan_pdf):
            self.assertTrue(payload.startswith(b"%PDF-1.4"))
            self.assertTrue(payload.endswith(b"%%EOF\n"))
            self.assertIn(b"xref\n", payload)
        self.assertIn(b"(Synthetic text PDF) Tj", text_pdf)
        self.assertIn(b"/Subtype /Image", scan_pdf)
        self.assertNotIn(b"SCANNED PDF", scan_pdf)

    def test_office_files_are_ooxml_zip_packages_with_extractable_text(self) -> None:
        root = FIXTURE_ROOT / "attachments/999999999999/1"
        cases = {
            "codex-conversion-fixture-requirements.docx": ("word/document.xml", "Synthetic DOCX requirement"),
            "codex-conversion-fixture-briefing.pptx": ("ppt/slides/slide1.xml", "Synthetic PPTX briefing"),
            "codex-conversion-fixture-metrics.xlsx": ("xl/worksheets/sheet1.xml", "Coverage"),
        }
        for filename, (content_path, expected_text) in cases.items():
            with self.subTest(filename=filename):
                payload = (root / filename).read_bytes()
                self.assertTrue(payload.startswith(b"PK\x03\x04"))
                with zipfile.ZipFile(io.BytesIO(payload)) as package:
                    self.assertIn("[Content_Types].xml", package.namelist())
                    self.assertIn("_rels/.rels", package.namelist())
                    self.assertIn(content_path, package.namelist())
                    self.assertIn(expected_text, package.read(content_path).decode("utf-8"))
                    self.assertIsNone(package.testzip())

    def test_drawio_source_contains_nodes_and_an_edge(self) -> None:
        source = FIXTURE_ROOT / "attachments/999999999999/1/codex-conversion-fixture-flow.drawio"
        document = ET.fromstring(source.read_bytes())
        self.assertEqual(document.tag, "mxfile")
        cells = document.findall(".//mxCell")
        vertices = {cell.attrib["id"] for cell in cells if cell.attrib.get("vertex") == "1"}
        edges = [cell for cell in cells if cell.attrib.get("edge") == "1"]
        self.assertEqual(vertices, {"node-a", "node-b"})
        self.assertEqual(len(edges), 1)
        self.assertEqual((edges[0].attrib["source"], edges[0].attrib["target"]), ("node-a", "node-b"))

    def test_html_contains_every_conversion_contract_construct(self) -> None:
        html = (FIXTURE_ROOT / "_codex_conversion_fixture.html").read_text(encoding="utf-8")
        fragments = (
            'id="main-content"',
            'class="toc-indentation"',
            'class="scroll-expand-container"',
            'class="rwui_tabs"',
            'class="rwui_tab_content"',
            'rowspan="2"',
            'colspan="2"',
            'class="drawio-diagram-image drawio-image-border"',
            'class="childpages-macro"',
            '_codex_conversion_fixture_child.html#src-999999999999-child-anchor',
            'href="https://example.com/reference"',
        )
        for fragment in fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, html)
        child = (FIXTURE_ROOT / "_codex_conversion_fixture_child.html").read_text(encoding="utf-8")
        self.assertIn('id="src-999999999999-child-anchor"', child)

    def test_manifest_hashes_every_payload_and_owns_only_generated_paths(self) -> None:
        manifest = json.loads((FIXTURE_ROOT / BUILDER.MANIFEST_PATH).read_text(encoding="utf-8"))
        self.assertEqual(manifest["format"], BUILDER.FIXTURE_FORMAT)
        self.assertEqual(manifest["namespace"], BUILDER.FIXTURE_NAMESPACE)
        self.assertEqual(set(manifest["generated_paths"]), set(materialized_paths()))
        self.assertNotIn(BUILDER.MANIFEST_PATH, manifest["files"])
        for relative_path, record in manifest["files"].items():
            with self.subTest(path=relative_path):
                content = (FIXTURE_ROOT / relative_path).read_bytes()
                self.assertEqual(record["bytes"], len(content))
                self.assertEqual(record["sha256"], hashlib.sha256(content).hexdigest())

    def test_default_write_refuses_collision_and_safe_replace_requires_manifest(self) -> None:
        with self.assertRaises(RuntimeError):
            BUILDER.write_fixture(FIXTURE_ROOT)
        owned = BUILDER._owned_paths_from_manifest(FIXTURE_ROOT)
        self.assertEqual(owned, set(materialized_paths()))


if __name__ == "__main__":
    unittest.main()
