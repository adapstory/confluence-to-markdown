# Synthetic Confluence export fixture

`full-export/` is a deterministic, sanitized approximation of the target
Confluence Data Center HTML export. It contains no copied corporate content.

The fixture covers:

- two content pages plus a WebHelp-like navigation page;
- an ordinary bilingual PNG suitable for Russian and English OCR checks;
- a text PDF and an image-only scanned PDF;
- minimal OOXML DOCX, PPTX, and XLSX attachments with extractable text;
- Markdown, CSV, JSON, YAML, EML with an embedded CSV, HTML, SVG, and TXT attachments;
- a Draw.io XML source and its PNG preview;
- TOC, Children, Expand, tabs, panels, a merged table, and internal anchors;
- WebHelp shell elements and inert CSS/JavaScript that converters must ignore.

Generate the checked-in fixture:

```bash
python3 scripts/build_synthetic_export.py
```

Verify it without writing:

```bash
python3 scripts/build_synthetic_export.py --check
```

The builder also accepts an explicit export root. All paths are namespaced with
`codex-conversion-fixture`; the page ID is `999999999999`. By default, any
collision aborts the entire write. `--replace-generated` permits replacement
only when the existing fixture manifest proves ownership of every collision.

`_codex_conversion_fixture_manifest.json` lists every generated path so the
fixture can later be removed precisely without touching unrelated export data.
