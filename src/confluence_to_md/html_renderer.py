from __future__ import annotations

import re
from collections.abc import Callable

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from .diagnostics import IssueCollector, error_marker
from .links import LinkResolver
from .models import PageDescriptor, RunStats

ImageRenderer = Callable[[Tag, PageDescriptor], str]

BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "div",
    "dl",
    "fieldset",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "ul",
}


def _class_names(node: Tag) -> set[str]:
    raw = node.get("class")
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {raw.lower()}
    return {str(value).lower() for value in raw}


class HtmlRenderer:
    def __init__(
        self,
        page: PageDescriptor,
        resolver: LinkResolver,
        issues: IssueCollector,
        stats: RunStats,
        image_renderer: ImageRenderer,
    ) -> None:
        self.page = page
        self.resolver = resolver
        self.issues = issues
        self.stats = stats
        self.image_renderer = image_renderer
        self.attachment_references: list[str] = []
        self._attachment_seen: set[str] = set()
        self._emitted_anchors: set[str] = set()
        self._suppressed_title_heading = False

    def render(self, body: Tag) -> str:
        chunks: list[str] = [self._anchor_from_tag(body)]
        if self.page.title_anchor:
            chunks.append(self._anchor(self.page.title_anchor))
        chunks.append(f"# {self._escape_text(self.page.title)}\n\n")
        chunks.append(self._render_children(body))
        return self._tidy("".join(chunks))

    def _render_node(self, node: object) -> str:
        if isinstance(node, NavigableString):
            value = str(node).replace("\xa0", " ")
            return self._escape_text(re.sub(r"\s+", " ", value))
        if not isinstance(node, Tag):
            return ""

        name = node.name.lower()
        classes = _class_names(node)

        if name in {"script", "style", "noscript", "template"}:
            return ""
        anchor = self._anchor_from_tag(node)
        if name == "img":
            return anchor + self.image_renderer(node, self.page)
        if name == "a":
            return anchor + self._render_link(node)
        if name == "svg":
            if str(node.get("aria-hidden", "")).strip().lower() == "true":
                return ""
            return anchor + self._unsupported(
                node,
                "UNSUPPORTED_INLINE_SVG",
                "Inline SVG content requires a verified structural handler",
            )
        if name == "math":
            return anchor + self._unsupported(
                node,
                "UNSUPPORTED_INLINE_MATH",
                "Inline MathML content requires a verified structural handler",
            )
        if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            return anchor + self._render_heading(node)
        if name == "p":
            return anchor + self._with_anchor(node, self._render_children(node).strip() + "\n\n")
        if name == "br":
            return anchor + "  \n"
        if name == "hr":
            return anchor + "\n---\n\n"
        if name in {"strong", "b"}:
            content = self._render_children(node).strip()
            return anchor + (f"**{content}**" if content else "")
        if name in {"em", "i"}:
            content = self._render_children(node).strip()
            return anchor + (f"*{content}*" if content else "")
        if name in {"del", "s", "strike"}:
            content = self._render_children(node).strip()
            return anchor + (f"~~{content}~~" if content else "")
        if name == "code" and node.parent and node.parent.name != "pre":
            return anchor + self._inline_code(node.get_text(" ", strip=True))
        if name == "pre" or "programlisting" in classes or "code-block" in classes:
            return anchor + self._render_code_block(node)
        if name in {"ul", "ol"}:
            if "rwui_tabs_menu" in classes:
                return anchor + self._render_tab_menu(node)
            if "toc-indentation" in classes:
                self.stats.increment_macro("toc")
            if "childpages-macro" in classes:
                self.stats.increment_macro("children")
            return anchor + self._render_list(node, ordered=name == "ol")
        if name == "li":
            return anchor + self._render_children(node)
        if name == "table":
            return anchor + self._render_table(node)
        if name == "blockquote":
            return anchor + self._blockquote(self._render_children(node))
        if name == "details":
            return anchor + self._render_details(node)
        if name == "summary":
            return anchor
        if name == "dl":
            return anchor + "\n" + self._render_children(node).strip() + "\n\n"
        if name == "dt":
            return anchor + f"**{self._render_children(node).strip()}**\n"
        if name == "dd":
            return anchor + self._indent(self._render_children(node).strip(), 2) + "\n"
        if name in {"sup", "sub", "kbd"}:
            return anchor + f"<{name}>{self._render_children(node).strip()}</{name}>"
        if name == "input":
            input_type = str(node.get("type", "")).lower()
            if input_type == "checkbox":
                return anchor + ("[x] " if node.has_attr("checked") else "[ ] ")
            return anchor + self._unsupported(node, "UNSUPPORTED_FORM_CONTROL", "An interactive form control cannot be represented faithfully")
        if name in {"iframe", "object", "embed", "canvas", "video", "audio", "form", "select", "textarea", "button"}:
            return anchor + self._unsupported(
                node,
                "UNSUPPORTED_EMBEDDED_CONTENT",
                f"Embedded or interactive <{name}> content cannot be represented faithfully",
            )

        if "scroll-expand-container" in classes:
            return anchor + self._render_expand(node)
        if self._is_tabs_container(classes, node):
            return anchor + self._render_tabs(node)
        if "childpages-macro" in classes:
            self.stats.increment_macro("children")
            return anchor + self._render_children(node)
        if "admonition" in classes or "confluence-information-macro" in classes:
            return anchor + self._render_admonition(node)
        if "panel" in classes and ("confbox" in classes or node.select_one(".panel-content")):
            return anchor + self._render_panel(node)
        if "status-macro" in classes:
            text = node.get_text(" ", strip=True)
            descendants = self._descendant_anchors(node)
            return anchor + descendants + (f"**Status: {self._escape_text(text)}**" if text else "")

        macro_name = node.get("data-macro-name") or node.get("data-macro-id")
        if macro_name:
            rendered = self._render_children(node)
            issue = self.issues.error(
                "UNSUPPORTED_MACRO",
                f"Unsupported Confluence macro was flattened without a verified semantic handler: {macro_name}",
                page=self.page.source_relative,
                reference=str(macro_name),
                element=name,
            )
            return anchor + self._with_anchor(node, rendered + error_marker(issue))

        rendered = self._render_children(node)
        if name in BLOCK_TAGS:
            rendered = "\n" + rendered.strip() + "\n\n" if rendered.strip() else ""
        return anchor + self._with_anchor(node, rendered)

    def _render_children(self, node: Tag, *, exclude: set[int] | None = None) -> str:
        omitted = exclude or set()
        return "".join(self._render_node(child) for child in node.children if id(child) not in omitted)

    def _render_link(self, node: Tag) -> str:
        anchor_id = node.get("id") or node.get("name")
        href = str(node.get("href", "")).strip()
        label = self._render_children(node).strip()
        prefix = self._anchor(str(anchor_id)) if anchor_id else ""
        if not href:
            return prefix + label

        resolution = self.resolver.resolve(href, self.page)
        if self._is_exported_email_fragment(node, href, label, resolution.problem_code):
            return prefix + label
        if resolution.problem_code:
            issue = self.issues.error(
                resolution.problem_code,
                resolution.problem_message or "Unresolved link",
                page=self.page.source_relative,
                reference=href,
                element="a",
            )
            marker = error_marker(issue)
            return prefix + (label or self._escape_text(href)) + marker
        marker = ""

        if resolution.kind == "empty":
            return prefix + label

        if (
            resolution.kind == "resource"
            and resolution.source_relative
            and resolution.source_relative not in self._attachment_seen
        ):
            self._attachment_seen.add(resolution.source_relative)
            self.attachment_references.append(resolution.source_relative)

        rendered_label = label or self._escape_text(href)
        return prefix + f"[{rendered_label}](<{self._destination(resolution.href)}>)" + marker

    @staticmethod
    def _is_exported_email_fragment(
        node: Tag,
        href: str,
        label: str,
        problem_code: str | None,
    ) -> bool:
        """Recognize a narrow Scroll exporter artifact that splits an email address.

        Some exports encode the domain half as a broken same-page source anchor:
        ``localpart<a href="#src-...">@example.test</a>``. Only suppress that
        link when both adjacent pieces form a syntactically plausible address.
        """

        if problem_code != "UNRESOLVED_ANCHOR" or not href.startswith("#"):
            return False
        if not re.fullmatch(r"@[^\s@<>()]+(?:\.[^\s@<>()]+)+", label):
            return False
        sibling = node.previous_sibling
        if isinstance(sibling, NavigableString):
            preceding = str(sibling)
        elif isinstance(sibling, Tag):
            preceding = sibling.get_text("", strip=False)
        else:
            return False
        local_atom = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
        return re.search(rf"{local_atom}(?:\.{local_atom})*$", preceding) is not None

    def _render_heading(self, node: Tag) -> str:
        level = int(node.name[1])
        text = self._render_children(node).strip()
        anchor = self._anchor_from_tag(node)
        plain = node.get_text(" ", strip=True)
        if level == 1 and not self._suppressed_title_heading and plain.casefold() == self.page.title.casefold():
            self._suppressed_title_heading = True
            return anchor
        return f"\n{anchor}{'#' * level} {text}\n\n" if text else anchor

    def _render_list(self, node: Tag, *, ordered: bool) -> str:
        items = [child for child in node.find_all("li", recursive=False)]
        lines: list[str] = []
        for index, item in enumerate(items, start=1):
            marker = f"{index}." if ordered else "-"
            direct_parts: list[str] = []
            nested_parts: list[str] = []
            for child in item.children:
                if isinstance(child, Tag) and child.name in {"ul", "ol"}:
                    nested_parts.append(self._render_list(child, ordered=child.name == "ol").strip())
                else:
                    direct_parts.append(self._render_node(child))
            item_anchor = self._anchor_from_tag(item)
            body = self._tidy_inline(item_anchor + "".join(direct_parts)) or "(empty item)"
            body_lines = body.splitlines()
            lines.append(f"{marker} {body_lines[0]}")
            lines.extend("  " + line for line in body_lines[1:])
            for nested in nested_parts:
                lines.extend("  " + line for line in nested.splitlines())
        anchor = self._anchor_from_tag(node)
        return f"\n{anchor}" + "\n".join(lines) + "\n\n" if lines else anchor

    def _render_table(self, table: Tag) -> str:
        rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
        cells_by_row = [
            [cell for cell in row.find_all(["th", "td"], recursive=False)] for row in rows
        ]
        caption_tag = table.find("caption", recursive=False)
        caption = self._tidy_inline(self._render_children(caption_tag)) if caption_tag else ""
        complex_spans = any(
            self._span(cell, "rowspan") > 1 or self._span(cell, "colspan") > 1
            for cells in cells_by_row
            for cell in cells
        )
        rectangular = bool(cells_by_row) and len({len(cells) for cells in cells_by_row}) == 1
        has_header = bool(cells_by_row) and all(cell.name == "th" for cell in cells_by_row[0])
        anchor = self._anchor_from_tag(table)

        if rectangular and has_header and not complex_spans:
            rendered_rows: list[list[str]] = []
            for row, cells in zip(rows, cells_by_row, strict=True):
                rendered = [self._table_cell_inline(cell) for cell in cells]
                row_anchor = self._anchor_from_tag(row).strip()
                if row_anchor and rendered:
                    rendered[0] = row_anchor + " " + rendered[0]
                rendered_rows.append(rendered)
            header = "| " + " | ".join(rendered_rows[0]) + " |"
            separator = "| " + " | ".join("---" for _ in rendered_rows[0]) + " |"
            body = ["| " + " | ".join(row) + " |" for row in rendered_rows[1:]]
            caption_anchor = self._anchor_from_tag(caption_tag) if caption_tag else ""
            title = f"**{self._escape_text(caption)}**\n\n" if caption else ""
            return "\n" + anchor + caption_anchor + title + "\n".join([header, separator, *body]) + "\n\n"

        self.stats.increment_macro("linearized-table")
        caption_anchor = self._anchor_from_tag(caption_tag) if caption_tag else ""
        lines = [caption_anchor + "**Table" + (f": {self._escape_text(caption)}" if caption else "") + "**"]
        occupied: dict[int, int] = {}
        for row_index, (row, cells) in enumerate(zip(rows, cells_by_row, strict=True), start=1):
            lines.append(self._anchor_from_tag(row) + f"- Row {row_index}")
            column_index = 1
            for cell in cells:
                while occupied.get(column_index, 0) >= row_index:
                    column_index += 1
                rowspan = self._span(cell, "rowspan")
                colspan = self._span(cell, "colspan")
                role = "header" if cell.name == "th" else "data"
                value = self._render_children(cell).strip() or "(empty)"
                meta = f"rowspan={rowspan}, colspan={colspan}, role={role}"
                value_lines = value.splitlines() or ["(empty)"]
                cell_anchor = self._anchor_from_tag(cell)
                lines.append(
                    cell_anchor
                    + f"  - Cell R{row_index}C{column_index} ({meta}): {value_lines[0]}"
                )
                lines.extend(f"    {line}" for line in value_lines[1:])
                if rowspan > 1:
                    for offset in range(colspan):
                        occupied[column_index + offset] = row_index + rowspan - 1
                column_index += colspan
        return "\n" + anchor + "\n".join(lines) + "\n\n"

    def _render_expand(self, node: Tag) -> str:
        self.stats.increment_macro("expand")
        control = node.select_one(".scroll-expand-control-text") or node.select_one(".scroll-expand-control")
        body = node.select_one(".scroll-expand-content")
        exported_title = control.get_text(" ", strip=True) if control else ""
        title = exported_title or "Expanded content"
        if body is None:
            return self._unsupported(node, "MALFORMED_EXPAND", "Expand macro has no exported body")
        excluded = {id(control), id(body)} if control else {id(body)}
        residual = self._render_children(node, exclude=excluded).strip()
        control_anchors = self._anchors_in_subtree(control) if control else ""
        body_anchor = self._anchor_from_tag(body)
        content = self._render_children(body).strip()
        extra = f"\n\n{residual}" if residual else ""
        marker = ""
        if not exported_title:
            marker = error_marker(
                self.issues.error(
                    "EXPAND_TITLE_MISSING",
                    "Expand macro has no exported title",
                    page=self.page.source_relative,
                    element=node.name,
                )
            )
        return (
            f"\n{control_anchors}#### Expanded: {self._escape_text(title)}\n\n"
            f"{body_anchor}{content}{extra}\n\n{marker}"
        )

    def _render_details(self, node: Tag) -> str:
        self.stats.increment_macro("expand")
        summary = node.find("summary", recursive=False)
        exported_title = summary.get_text(" ", strip=True) if summary else ""
        title = exported_title or "Expanded content"
        excluded = {id(summary)} if summary else set()
        content = self._render_children(node, exclude=excluded).strip()
        summary_anchors = self._anchors_in_subtree(summary) if summary else ""
        marker = ""
        if not exported_title:
            marker = error_marker(
                self.issues.error(
                    "EXPAND_TITLE_MISSING",
                    "Expanded details block has no exported title",
                    page=self.page.source_relative,
                    element=node.name,
                )
            )
        return f"\n{summary_anchors}#### Expanded: {self._escape_text(title)}\n\n{content}\n\n{marker}"

    def _render_tab_menu(self, node: Tag) -> str:
        if not node.get_text(" ", strip=True):
            return ""
        return self._unsupported(
            node,
            "TAB_MENU_WITHOUT_PANELS",
            "A tab menu was exported without identifiable tab panels",
        )

    def _render_tabs(self, node: Tag) -> str:
        panels = node.select(".tabs-pane, .tab-pane, .rwui_tab_content, [role='tabpanel']")
        if not panels:
            return self._render_children(node)
        self.stats.increment_macro("tabs")
        menu_labels = [
            item.get_text(" ", strip=True)
            for item in node.select(
                ".tabs-menu li, .nav-tabs li, .rwui_tabs_menu li, [role='tab']"
            )
            if item.get_text(" ", strip=True)
        ]
        menus = node.select(".tabs-menu, .nav-tabs, .rwui_tabs_menu, [role='tablist']")
        parts: list[str] = ["".join(self._anchors_in_subtree(menu) for menu in menus)]
        if menu_labels and len(menu_labels) != len(panels):
            issue = self.issues.error(
                "TAB_LABEL_PANEL_MISMATCH",
                (
                    "The exported tabs contain a different number of labels and panels: "
                    f"{len(menu_labels)} labels, {len(panels)} panels"
                ),
                page=self.page.source_relative,
                element=node.name,
            )
            parts.append(
                "\n**Tab labels exported:**\n\n"
                + "\n".join(f"- {self._escape_text(label)}" for label in menu_labels)
                + "\n\n"
                + error_marker(issue)
            )
        missing_titles = [
            index
            for index, panel in enumerate(panels, start=1)
            if not (
                panel.get("data-title")
                or panel.get("aria-label")
                or (menu_labels[index - 1] if index <= len(menu_labels) else None)
            )
        ]
        if missing_titles:
            issue = self.issues.error(
                "TAB_TITLE_MISSING",
                "The exported tab panels have no recoverable title: "
                + ", ".join(str(index) for index in missing_titles),
                page=self.page.source_relative,
                element=node.name,
            )
            parts.append(error_marker(issue))
        for index, panel in enumerate(panels, start=1):
            label = (
                panel.get("data-title")
                or panel.get("aria-label")
                or (menu_labels[index - 1] if index <= len(menu_labels) else None)
                or f"Tab {index}"
            )
            parts.append(
                f"\n{self._anchor_from_tag(panel)}#### Tab: {self._escape_text(str(label))}\n\n"
                f"{self._render_children(panel).strip()}\n"
            )
        return "".join(parts) + "\n"

    def _render_admonition(self, node: Tag) -> str:
        self.stats.increment_macro("admonition")
        classes = sorted(_class_names(node))
        kind = next((value.removeprefix("admonition-") for value in classes if value.startswith("admonition-")), "info")
        body = node.select_one(".admonition-body") or node
        icon = body.select_one(".confluence-information-macro-icon")
        excluded = {id(icon)} if icon else set()
        body_anchor = self._anchor_from_tag(body) if body is not node else ""
        icon_anchors = self._anchors_in_subtree(icon) if icon else ""
        content = self._render_children(body, exclude=excluded).strip()
        return body_anchor + icon_anchors + self._blockquote(f"**{kind.title()}**\n\n{content}")

    def _render_panel(self, node: Tag) -> str:
        self.stats.increment_macro("panel")
        title_node = node.select_one(".panel-title, .title")
        body = node.select_one(".panel-content") or node
        title = title_node.get_text(" ", strip=True) if title_node else "Panel"
        excluded = {id(title_node)} if title_node and body is node else set()
        title_anchors = self._anchors_in_subtree(title_node) if title_node else ""
        body_anchor = self._anchor_from_tag(body) if body is not node else ""
        content = self._render_children(body, exclude=excluded).strip()
        return f"\n{title_anchors}{body_anchor}#### {self._escape_text(title)}\n\n{content}\n\n"

    def _render_code_block(self, node: Tag) -> str:
        language = str(node.get("data-language", "")).strip()
        if not language:
            for class_name in _class_names(node):
                match = re.match(r"(?:language|brush)-([A-Za-z0-9_+-]+)", str(class_name))
                if match:
                    language = match.group(1)
                    break
        lines = node.select(".line, li")
        if lines:
            code = "\n".join(line.get_text("", strip=False).rstrip("\n") for line in lines)
        else:
            code = node.get_text("\n", strip=False)
        code = code.strip("\n")
        fence = "`" * max(3, self._longest_run(code, "`") + 1)
        return self._descendant_anchors(node) + f"\n{fence}{language}\n{code}\n{fence}\n\n"

    def _unsupported(self, node: Tag, code: str, message: str) -> str:
        issue = self.issues.error(
            code,
            message,
            page=self.page.source_relative,
            element=node.name,
        )
        fallback = self._render_children(node).strip()
        return (fallback + "\n\n" if fallback else "") + error_marker(issue)

    def _with_anchor(self, node: Tag, rendered: str) -> str:
        return self._anchor_from_tag(node) + rendered

    def _anchor_from_tag(self, node: Tag) -> str:
        anchor_id = node.get("id") or node.get("name")
        return self._anchor(str(anchor_id)) if anchor_id else ""

    def _anchors_in_subtree(self, node: Tag) -> str:
        return self._anchor_from_tag(node) + self._descendant_anchors(node)

    def _descendant_anchors(self, node: Tag) -> str:
        return "".join(self._anchor_from_tag(child) for child in node.find_all(True))

    def _anchor(self, anchor_id: str) -> str:
        if not anchor_id or anchor_id in self._emitted_anchors:
            return ""
        self._emitted_anchors.add(anchor_id)
        escaped = (
            anchor_id.replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        return f'<a id="{escaped}"></a>\n'

    def _table_cell_inline(self, cell: Tag) -> str:
        content = self._anchor_from_tag(cell) + self._render_children(cell).strip()
        content = re.sub(r"\s*\n+\s*", "<br>", content)
        return content.replace("|", "\\|") or " "

    @staticmethod
    def _span(cell: Tag, name: str) -> int:
        try:
            return max(1, int(str(cell.get(name, "1"))))
        except ValueError:
            return 1

    @staticmethod
    def _blockquote(value: str) -> str:
        content = value.strip()
        return "\n" + "\n".join("> " + line if line else ">" for line in content.splitlines()) + "\n\n"

    @staticmethod
    def _indent(value: str, spaces: int) -> str:
        prefix = " " * spaces
        return "\n".join(prefix + line if line else prefix for line in value.splitlines())

    @staticmethod
    def _inline_code(value: str) -> str:
        ticks = "`" * max(1, HtmlRenderer._longest_run(value, "`") + 1)
        padding = " " if value.startswith("`") or value.endswith("`") else ""
        return f"{ticks}{padding}{value}{padding}{ticks}"

    @staticmethod
    def _longest_run(value: str, character: str) -> int:
        matches = re.findall(re.escape(character) + "+", value)
        return max((len(match) for match in matches), default=0)

    @staticmethod
    def _escape_text(value: str) -> str:
        escaped_html = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return re.sub(r"([\\`*_[\]])", r"\\\1", escaped_html)

    @staticmethod
    def _destination(value: str) -> str:
        return value.replace("\\", "%5C").replace(">", "%3E").replace("\r", "").replace("\n", "")

    @staticmethod
    def _tidy_inline(value: str) -> str:
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r" *\n *", " ", value)
        return value.strip()

    @staticmethod
    def _tidy(value: str) -> str:
        lines = [line.rstrip() for line in value.splitlines()]
        result: list[str] = []
        blank = 0
        in_fence = False
        fence = ""
        for line in lines:
            stripped = line.lstrip()
            fence_match = re.match(r"^([`~]{3,})(.*)$", stripped)
            if fence_match:
                marker = fence_match.group(1)
                if not in_fence:
                    in_fence = True
                    fence = marker
                elif (
                    marker[0] == fence[0]
                    and len(marker) >= len(fence)
                    and not fence_match.group(2).strip()
                ):
                    in_fence = False
                    fence = ""
            if not line and not in_fence:
                blank += 1
                if blank > 1:
                    continue
            else:
                blank = 0
            result.append(line)
        return "\n".join(result).strip() + "\n"

    @staticmethod
    def _is_tabs_container(classes: set[str], node: Tag) -> bool:
        if node.name == "ul" and "rwui_tabs_menu" in classes:
            return False
        known = {"aui-tabs", "tabs-container", "tab-container", "rwui_tabs", "rwui-tabs"}
        return bool(classes & known) or node.get("role") == "tablist"


def discover_body(html: str) -> tuple[BeautifulSoup, Tag | None]:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("#main-content")
    return soup, body
