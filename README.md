# Confluence HTML to Markdown

This Python 3.13 command converts an **unpacked** Scroll HTML Exporter directory into portable GitHub Flavored Markdown. It is designed for later text-only LLM reading: the converter never asks an LLM to interpret source content.

The conversion is loss-aware. It processes the complete export, writes every page it can recover, records every semantic gap in `conversion-errors.json`, inserts a matching visible marker into the affected Markdown page, then exits with code `1` when any error remains.

## Install

Clone the complete repository so the pinned OCR models are present:

```bash
git clone https://github.com/adapstory/confluence-to-markdown.git
cd confluence-to-markdown
```

The mandatory Python dependency set is intentionally small:

```bash
uv sync
```

Install the preferred PDF backend and the local RapidOCR backend:

```bash
uv sync --extra pdf --extra ocr
```

RapidOCR with ONNX Runtime is the default OCR engine. Its three pinned models
are stored in `models/rapidocr`; runtime network downloads are forbidden. The
bundled recognition model handles Russian and English text. Tesseract remains
an explicit compatibility backend selected with `--ocr-engine tesseract`.
PDF conversion first uses PyMuPDF when installed, then checks the local Poppler
commands as a fallback. Every PDF page is rasterized and OCR-inspected even
when it also contains native text, so screenshots embedded in text-rich pages
are not skipped.

## Run

The output directory may be new, empty, or a result previously created by this
converter from the same source path. A recognized previous result is cleared
automatically before the new run. An unrelated nonempty directory is refused.

```bash
uv run confluence-to-md \
  /path/to/unpacked-confluence-export \
  --output /path/to/documentation-md
```

Run the command from the project root so the default relative model directory
`models/rapidocr` resolves correctly. A different location must be supplied
explicitly with `--rapidocr-model-dir`.

Exit codes:

- `0`: every meaningful construct and internal reference was preserved.
- `1`: all pages were processed, but the report contains one or more preservation errors.
- `2`: invalid invocation, unsafe path, unreadable input root, or unrelated nonempty output directory.

## Recommended Confluence export profile

Use these Scroll HTML Exporter settings for a complete qualification export:

- Create: **one HTML file for each Confluence page**
- Template: **Scroll WebHelp Template**
- Export: **This page and its children**
- Output TOC macros: **on**
- Output Children macros: **on**
- Output section macros as table: **off**
- Output numbered headings macros: **on**
- Export images with original resolution: **on**
- Merge single first heading and page title: **on**
- Attachments: **Embed referenced attachments**
- File naming: **Page Title only**
- Whitespace: **replace with underscore**
- Special characters: **do not change**
- Extension: **`.html`**
- Search index: **off**
- Comala “Only published versions”: **off**, unless that restriction is intentional

## Output contract

- One `.md` file is written for every export page, preserving the source-relative name.
- Page shell, sidebar, global navigation, scripts, styles, breadcrumbs, and footer are excluded.
- Relative page links and explicit HTML anchors are rewritten and validated.
- Referenced resources are retained at source-relative paths.
- Supported attachment text is appended to the owning page while the original remains linked.
- Expand blocks and tabs become ordered Markdown sections.
- TOC and Children macros remain lists with rewritten links.
- Simple tables become GFM tables. Tables with merges become explicit row and cell records containing `rowspan` and `colspan`.
- Draw.io produces the rendered image, a deterministic node and edge model, and the retained `.drawio` source.
- Content images receive a local Russian and English OCR transcript. An unreadable meaningful image is an error.
- Unsupported macros, missing resources, unresolved internal links, and unavailable required backends are errors.

Warnings are reserved for visual differences that do not remove meaning.

`conversion-errors.json` uses schema version 2. It includes the converter,
Python, PyMuPDF, selected OCR engine, RapidOCR, ONNX Runtime, local model,
Tesseract, and Poppler runtime capabilities; an aggregated
`errorSummary`; and a deterministic `category` and `action` on every issue.
Unexpected page failures also contain a bounded traceback with file names,
line numbers, and function names. Tracebacks never include local variables or
source document contents.

## Supported attachment text

The built-in handlers cover plain text, Markdown, CSV, JSON, YAML, EML, HTML, DOCX, PPTX, XLSX, images, Draw.io, and PDF. Office handlers use ZIP and XML from the Python standard library. OCR is delegated to the selected deterministic local backend. The original attachment is always retained even when extraction fails.

## Tests

```bash
uv sync --extra pdf --extra ocr --extra dev
uv run ruff check .
uv run pyright src/confluence_to_md tests scripts/build_synthetic_export.py
uv run python -m unittest discover -s tests -v
uv run python scripts/build_synthetic_export.py --check
uv run confluence-to-md tests/fixtures/full-export \
  --output /path/to/empty-qualification-output
```

`scripts/build_synthetic_export.py` creates a deterministic sanitized fixture with
images, PDF, DOCX, PPTX, XLSX, TXT, Markdown, CSV, JSON, YAML, EML with an
embedded CSV, HTML, SVG, Draw.io, macros, tables, and links. The qualification
command above must return code `0`. The fixture proves supported semantics, while
a final acceptance run still needs a complete anonymized archive from the target
Scroll HTML Exporter to prove compatibility with that exact exporter output.

The same generator can materialize the namespaced fixture into a supplied export
root without changing existing pages.

```bash
uv run python scripts/build_synthetic_export.py \
  /path/to/unpacked-confluence-export

uv run python scripts/build_synthetic_export.py --check \
  /path/to/unpacked-confluence-export
```

The generated paths are recorded with hashes in `_codex_conversion_fixture_manifest.json`. A normal write refuses collisions; replacement is limited to files already owned by that manifest.
