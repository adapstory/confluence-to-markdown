from __future__ import annotations

import base64
import unittest
import urllib.parse
import zlib

from confluence_to_md.drawio import (
    MAX_DRAWIO_XML_BYTES,
    parse_drawio,
    render_drawio_text,
)

MODEL = """<mxGraphModel><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="n1" value="Start" vertex="1" parent="1"><mxGeometry x="1" y="2" width="3" height="4"/></mxCell>
<object id="n2" label="Finish&lt;br&gt;done" link="https://example.test"><mxCell vertex="1" parent="1"/></object>
<mxCell id="e1" value="next" edge="1" source="n1" target="n2" parent="1"/>
</root></mxGraphModel>"""


class DrawioTest(unittest.TestCase):
    def test_parses_uncompressed_model_and_renders_graph(self) -> None:
        document = parse_drawio(
            f'<mxfile><diagram id="p1" name="Flow">{MODEL}</diagram></mxfile>'
        )

        self.assertTrue(document.ok)
        self.assertEqual(2, len(document.pages[0].nodes))
        self.assertEqual("Finish\ndone", document.pages[0].nodes[1].label)
        self.assertEqual("n1", document.pages[0].edges[0].source)
        rendered = render_drawio_text(document)
        self.assertIn("`n1`: Start", rendered)
        self.assertIn("`n1` -> `n2`: next", rendered)

    def test_parses_compressed_diagram(self) -> None:
        compressor = zlib.compressobj(wbits=-15)
        encoded_xml = urllib.parse.quote(MODEL).encode("utf-8")
        payload = compressor.compress(encoded_xml) + compressor.flush()
        encoded = base64.b64encode(payload).decode("ascii")

        document = parse_drawio(
            f'<mxfile><diagram name="Compressed">{encoded}</diagram></mxfile>'
        )

        self.assertTrue(document.ok)
        self.assertEqual("Start", document.pages[0].nodes[0].label)

    def test_reports_oversized_compressed_diagram_without_crashing(self) -> None:
        huge_label = "A" * (MAX_DRAWIO_XML_BYTES + 1024)
        model = (
            '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
            f'<mxCell id="n1" value="{huge_label}" vertex="1" parent="1"/>'
            "</root></mxGraphModel>"
        )
        compressor = zlib.compressobj(wbits=-15)
        encoded_xml = urllib.parse.quote(model).encode("utf-8")
        payload = compressor.compress(encoded_xml) + compressor.flush()
        encoded = base64.b64encode(payload).decode("ascii")

        document = parse_drawio(
            f'<mxfile><diagram name="Oversized">{encoded}</diagram></mxfile>'
        )

        self.assertFalse(document.ok)
        self.assertEqual((), document.pages)
        self.assertIn(
            "drawio_page_decode_failed",
            {diagnostic.code for diagnostic in document.diagnostics},
        )
        self.assertIn("supported limit", document.diagnostics[0].message)

    def test_reports_unresolved_edge_endpoint(self) -> None:
        model = MODEL.replace('target="n2"', 'target="missing"')
        document = parse_drawio(model)

        self.assertFalse(document.ok)
        self.assertIn(
            "drawio_edge_endpoint_unresolved",
            {diagnostic.code for diagnostic in document.diagnostics},
        )

    def test_preserves_free_edge_endpoint_points(self) -> None:
        model = """<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>
        <mxCell id="free" edge="1" parent="1"><mxGeometry relative="1">
        <Array as="points"><mxPoint x="5" y="6"/></Array>
        <mxPoint x="1" y="2" as="sourcePoint"/><mxPoint x="3" y="4" as="targetPoint"/>
        </mxGeometry></mxCell></root></mxGraphModel>"""

        document = parse_drawio(model)

        self.assertTrue(document.ok)
        rendered = render_drawio_text(document)
        self.assertIn("point(1,2)", rendered)
        self.assertIn("waypoints=5,6", rendered)


if __name__ == "__main__":
    unittest.main()
