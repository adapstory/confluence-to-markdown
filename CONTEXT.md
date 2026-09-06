# Confluence Export Conversion

This context describes the offline transformation of a Scroll HTML Exporter archive into portable Markdown.

## Language

**HTML export archive**:
A ZIP archive produced by Scroll HTML Exporter from a selected Confluence page set.
_Avoid_: Confluence backup, space dump

**Export root**:
The top-level directory obtained by unpacking one HTML export archive and supplied as the canonical conversion input.
_Avoid_: ZIP input, individual HTML file

**Export page**:
The HTML representation of one Confluence page contained in an HTML export archive.
_Avoid_: source page, document

**Portable Markdown**:
GitHub Flavored Markdown that does not require extensions specific to a documentation platform or note-taking application.
_Avoid_: Obsidian Markdown, MkDocs Markdown

**Markdown page**:
The portable Markdown representation of one export page, linked to other Markdown pages and resources through relative paths.
_Avoid_: combined document, HTML page

**Output collection**:
The set of Markdown pages and retained resources produced from one HTML export archive while preserving their source-relative names.
_Avoid_: combined document, exported website

**Page content**:
The meaningful documentation contained in an export page, excluding the Scroll WebHelp navigation, search controls, breadcrumbs, and footer.
_Avoid_: page shell, exported website

**Resource**:
An image, attachment, or diagram source associated with page content and retained from the HTML export archive.
_Avoid_: WebHelp static asset

**Embedded attachment text**:
Text deterministically extracted from a supported attachment and placed in the Markdown page that references the retained original resource.
_Avoid_: attachment link, generated summary

**Supported attachment**:
A referenced PDF, DOCX, PPTX, XLSX, TXT, Markdown, CSV, JSON, YAML, EML, HTML, image, or Draw.io resource for which the converter retains the original and produces an inline text representation.
_Avoid_: opaque attachment, linked-only resource

**Attachment backend**:
A deterministic local parser or executable that extracts text from one supported attachment type without sending content to a remote service.
_Avoid_: LLM extractor, remote conversion

**Local conversion**:
Offline processing of a supplied export root without Confluence credentials or API access.
_Avoid_: synchronization, migration

**Text-only consumption**:
Downstream use in which an LLM receives only Markdown text and does not open linked resources; the LLM does not participate in conversion.
_Avoid_: multimodal conversion, LLM-generated description

**Loss-aware conversion**:
A conversion that preserves source resources, reports every unsupported meaningful construct, and is successful only when no such construct remains.
_Avoid_: best-effort conversion, silent fallback

**Unsupported construct**:
A meaningful source element for which the converter cannot produce a faithful portable representation or retain the required source resource.
_Avoid_: warning, cosmetic difference

**Internal reference**:
A link from one export page to another export page, a section anchor, or a retained resource within the same output collection.
_Avoid_: external URL

**Warning**:
A reported cosmetic difference that changes presentation without losing meaning, resources, or a resolvable internal reference.
_Avoid_: unsupported construct, conversion error

**Error marker**:
A visible placeholder in a Markdown page that identifies the matching unsupported construct in the error report.
_Avoid_: silent omission, unchecked HTML fallback

**Diagram text model**:
A deterministic listing of diagram pages, labeled nodes, and labeled connections placed after the rendered image in the owning Markdown page and retained with the original diagram source.
_Avoid_: generated interpretation, image caption

**OCR transcript**:
Russian and English text extracted locally from a content image and placed beside the retained image in its owning Markdown page.
_Avoid_: LLM description, image caption

**Linearized table**:
A table represented as explicit row and cell records, including merged-cell relationships, so its structure remains available in text-only consumption.
_Avoid_: layout table, unchecked HTML table

**Flattened interactive block**:
A tabs or expandable construct represented as linear Markdown sections that retain every heading and content item without preserving interaction state.
_Avoid_: active tab, collapsed content

**Content navigation list**:
A table-of-contents or child-page macro represented as a Markdown list whose internal references target the output collection.
_Avoid_: WebHelp navigation, page shell

**Conversion summary**:
The console output listing conversion counts, preserved resources, recognized macros, warnings, and errors.
_Avoid_: error report

**Error report**:
The `conversion-errors.json` durable output containing every error and unsupported meaningful construct discovered after processing the complete export root.
_Avoid_: console summary, application log

**Structural fixture**:
An export root that exercises real exporter HTML structures but does not contain every referenced resource and therefore cannot establish a successful loss-aware conversion.
_Avoid_: qualification fixture, complete archive

**Qualification fixture**:
An export root containing every required representative construct and resource, used to demonstrate a zero-error conversion for the supported capability matrix.
_Avoid_: structural fixture, production promise
