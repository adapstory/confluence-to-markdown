# ADR 0001 compliance evidence

This document maps the accepted decisions in
[`../decisions/0001-loss-aware-output.md`](../decisions/0001-loss-aware-output.md)
to implementation and executable evidence. It records implementation compliance;
the last same-exporter acceptance check remains an input qualification activity.

## Decision matrix

| Decision | Status | Implementation and evidence |
| --- | --- | --- |
| Q1 | Implemented | `HtmlRenderer` emits portable GFM and plain HTML only for standard inline `sup`, `sub`, and `kbd`; the zero-error fixture is read as Markdown. |
| Q2 | Implemented | The CLI accepts a local directory and has no network or Confluence API client. |
| Q3 | Implemented | `pyproject.toml` requires Python `>=3.13,<3.14`. |
| Q4 | Awaiting final input qualification | The deterministic sanitized fixture passes with code `0`; the supplied real Scroll export is also exercised end to end. A complete anonymized archive produced with the recommended profile is still required for the final same-exporter zero-error proof. |
| Q5 | Implemented | Discovery maps every export page to one source-relative `.md`; links and retained resources remain relative. |
| Q6 | Implemented | Discovery and rendering start at `#main-content`; WebHelp shell, scripts, styles, and templates are excluded. |
| Q7 | Implemented | The converter retains recoverable output, reports semantic gaps, and returns code `1` when any error exists. |
| Q8 | Implemented | Draw.io rendering emits preview, deterministic nodes, deterministic connections, and retained source. |
| Q9 | Implemented | Scroll tabs, tab panels, expand blocks, and HTML `details` are flattened with headings and full bodies; missing or mismatched titles are hard errors without dropping bodies. |
| Q10 | Implemented | The CLI prints counts and writes `conversion-errors.json` with runtime capabilities and an aggregated root-cause summary. |
| Q11 | Implemented | Page failures are isolated; the report and nonzero result are produced only after the full page pass. |
| Q12 | Implemented | Schema version 2 gives every error a unique `E…..` id, code, message, category, action, and available context; unexpected exceptions include a bounded traceback without locals. |
| Q13 | Implemented | Referenced content resources and the attachment tree are retained; WebHelp CSS and JavaScript are not copied as page content. |
| Q14 | Implemented | Nested exporter-relative paths are preserved and `.html`, `.htm`, or `.xhtml` becomes `.md`. |
| Q15 | Implemented | Input is an already unpacked root directory; ZIP input is not accepted. |
| Q16 | Implemented | Runtime Python dependency is only Beautiful Soup; Office and Draw.io use the standard library, PyMuPDF is optional, and local Tesseract and Poppler are supported. `uv.lock` is present. |
| Q17 | Implemented | A matching ownership marker or legacy report permits automatic replacement of a previous result; unrelated nonempty directories, symlinks, and paths overlapping the source are rejected before cleanup. |
| Q18 | Implemented | Known Confluence and Scroll constructs have handlers; unverified macros and embedded interactive content create hard errors with visible fallback text. |
| Q19 | Implemented | OCR and extraction are deterministic local processes; no LLM dependency or call exists. |
| Q20 | Implemented | Internal pages, anchors, and resources are rewritten and validated; unresolved targets become non-clickable text plus an error marker, while external schemes remain unchanged. |
| Q21 | Implemented | Semantic extraction diagnostics are errors; warnings are reserved for explicitly visual-only diagnostics. |
| Q22 | Implemented | Every page-linked error is reconciled to exactly one visible `CONVERSION ERROR` marker with the report id. |
| Q23 | Implemented | PDF, DOCX, PPTX, XLSX, TXT, Markdown, CSV, JSON, YAML, EML, HTML, images, SVG, and Draw.io have extraction handlers; originals remain retained. EML preserves HTML links, all body alternatives, and recursively extracts CID/embedded files. SVG includes its complete XML vector structure in Markdown. |
| Q24 | Implemented | Meaningful images use local `rus+eng` OCR; every PDF page receives native extraction plus OCR inspection. Unreadable images and image-only diagrams without structural sources are hard errors. |
| Q25 | Implemented | Merged or irregular tables become explicit rows and cells with `rowspan` and `colspan`; simple header tables remain GFM tables. |
| Q26 | Implemented | Draw.io output order is preview, nodes, connections, then source link on the owning page. |
| Q27 | Implemented | TOC and Children lists pass through list rendering and internal-link rewriting. |

## Latest local evidence

On 2026-09-04:

- static analysis: `uv run ruff check .` passed.
- type analysis: `uv run pyright src/confluence_to_md tests scripts/build_synthetic_export.py`
  completed with 0 errors and 0 warnings.
- unit and integration tests: 91 passed via
  `uv run python -m unittest discover -s tests -v`.
- dependency lock: `uv lock --check` passed.
- deterministic fixture check: `uv run python scripts/build_synthetic_export.py --check`
  passed.
- package build: wheel and sdist were produced in
  `build/package-review-final-2-20260904`.
- standalone qualification fixture: code `0`, 2 of 2 pages, 16 retained
  resources, 13 extracted attachments, 1 OCR image, 1 Draw.io model, 0 warnings,
  and 0 errors. Report:
  `build/qualification-review-final-2-20260904/conversion-errors.json`.
- supplied real export: code `1` after 341 of 341 pages, 90 retained resources,
  87 extracted attachments, 1 directly referenced OCR image, 2 Draw.io models,
  0 warnings, and 407 input-preservation errors. Report:
  `build/adr0001-review-final-2-20260904/conversion-errors.json`.
- output invariant audit over the real result found 0 missing page outputs,
  0 missing error markers, 0 duplicate error markers, and 0 invalid generated
  Markdown links or anchors.

The real-export errors are input completeness findings: 318 missing resources,
62 unresolved anchors, 22 missing Draw.io sources, and 5 unresolved internal
pages. They are expected to prevent a successful loss-aware result.

The review added regression coverage for non-clickable unresolved links,
`index.html` query/fragment aliases, `.xhtml` classification, report-name
collisions, failed marker reconciliation, meaningful redirect pages, symlink
directories, ambiguous Draw.io pairing, bounded Draw.io inflation, untitled or
mismatched tabs and expands, inline SVG/MathML, fenced code, mixed-content PDF,
EML HTML links and alternatives, CID images, complete SVG vector XML, and
Russian diagram labels.

On 2026-09-06, Q17 was revised to permit safe repeatable output replacement.
The full suite passed with 98 tests, Ruff and Pyright passed, and two consecutive
qualification conversions to the same output directory both completed with 2
of 2 pages and 0 errors. Regression coverage verifies matching ownership
markers, legacy matching reports, refusal of unrelated nonempty directories,
and refusal to clean an output path that contains the source tree.

The same review introduced report schema version 2. A real-export run completed
341 of 341 pages and emitted runtime capability data plus an `errorSummary` of
318 `source-export` missing resources, 62 `source-authoring` unresolved anchors,
22 `source-export` missing Draw.io sources, and 5 `source-authoring` unresolved
pages. Tests cover environment categorization, aggregation by code and affected
page, and bounded unexpected-exception tracebacks without local variables.
