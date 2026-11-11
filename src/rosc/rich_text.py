"""Minimal canonical rich text for Task Spec fields (P040).

The stored format is a tiny whitelist of HTML: ``<p>``, ``<ul>``/``<ol>``/``<li>``, ``<strong>``, ``<em>``, ``<br>``.
Anything else (attributes, scripts, styles, links, images, unknown tags) is dropped by ``canonicalize`` and only text
survives, so the stored value is safe to render and identical inputs give identical bytes (stable digests).
``to_text`` gives the readable form an Architect tool returns; ``from_text`` accepts plain text with ``- `` / ``1. ``
lists and ``**bold**`` / ``*italic*`` marks, which is what an Architect writes.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

MAX_FIELD_CHARS = 20_000
_SKIP = {"script", "style", "template", "iframe", "object", "head", "title"}
_BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "tr", "section", "article"}
_BOLD, _ITALIC = {"b", "strong"}, {"i", "em"}


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, list[list[tuple[str, bool, bool]]]]] = []  # (kind, items of runs)
        self.runs: list[tuple[str, bool, bool]] = []
        self.list_kind: str | None = None
        self.items: list[list[tuple[str, bool, bool]]] = []
        self.bold = self.italic = self.skip = 0
        self.in_item = False

    def _flush(self) -> None:
        if self.runs and any(text.strip() for text, _, _ in self.runs):
            self.blocks.append(("p", [self.runs]))
        self.runs = []

    def _end_list(self) -> None:
        if self.in_item:
            self._end_item()
        if self.list_kind and self.items:
            self.blocks.append((self.list_kind, self.items))
        self.list_kind, self.items = None, []

    def _end_item(self) -> None:
        if any(text.strip() for text, _, _ in self.runs):
            self.items.append(self.runs)
        self.runs, self.in_item = [], False

    def handle_starttag(self, tag, attrs) -> None:
        if tag in _SKIP:
            self.skip += 1
        elif self.skip:
            return
        elif tag in ("ul", "ol"):
            if self.list_kind:  # nested list: flatten into the outer list
                if self.in_item:
                    self._end_item()
                return
            self._flush()
            self.list_kind = tag
        elif tag == "li":
            if self.in_item:
                self._end_item()
            elif not self.list_kind:
                self._flush()
                self.list_kind = "ul"
            self.in_item = True
        elif tag in _BLOCK:
            if self.list_kind and self.in_item:
                self.runs.append((" ", self.bold > 0, self.italic > 0))
            elif not self.list_kind:
                self._flush()
        elif tag == "br":
            self.runs.append(("\n", False, False))
        elif tag in _BOLD:
            self.bold += 1
        elif tag in _ITALIC:
            self.italic += 1

    def handle_endtag(self, tag) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif self.skip:
            return
        elif tag in ("ul", "ol"):
            self._end_list()
        elif tag == "li":
            if self.in_item:
                self._end_item()
        elif tag in _BLOCK:
            if not self.list_kind:
                self._flush()
        elif tag in _BOLD:
            self.bold = max(0, self.bold - 1)
        elif tag in _ITALIC:
            self.italic = max(0, self.italic - 1)

    def handle_data(self, data) -> None:
        if self.skip or not data:
            return
        if not self.list_kind and not self.runs and not data.strip():
            return
        self.runs.append((re.sub(r"[ \t\r\f\v]+", " ", data.replace("\xa0", " ")), self.bold > 0, self.italic > 0))

    def close(self) -> None:
        super().close()
        if self.list_kind:
            self._end_list()
        self._flush()


def _runs_html(runs: list[tuple[str, bool, bool]]) -> str:
    merged: list[list] = []
    for text, bold, italic in runs:
        if merged and merged[-1][1] == bold and merged[-1][2] == italic:
            merged[-1][0] += text
        else:
            merged.append([text, bold, italic])
    out = []
    for text, bold, italic in merged:
        if text.strip() == "" and "\n" not in text:
            bold = italic = False
        pieces = [html.escape(part, quote=False) for part in text.split("\n")]
        inner = "<br>".join(pieces)
        if inner.replace("<br>", "") == "" and inner:
            out.append(inner)
            continue
        if italic:
            inner = f"<em>{inner}</em>"
        if bold:
            inner = f"<strong>{inner}</strong>"
        out.append(inner)
    value = re.sub(r" {2,}", " ", "".join(out))
    value = re.sub(r"(<br>)+$", "", re.sub(r"^(<br>)+", "", value.strip()))
    return value.strip()


def canonicalize(value: str) -> str:
    """Return the canonical whitelist HTML for ``value`` (arbitrary HTML or plain text)."""
    builder = _Builder()
    builder.feed(value or "")
    builder.close()
    out = []
    for kind, items in builder.blocks:
        if kind == "p":
            body = _runs_html(items[0])
            if body:
                out.append(f"<p>{body}</p>")
        else:
            rendered = [f"<li>{body}</li>" for body in (_runs_html(item) for item in items) if body]
            if rendered:
                out.append(f"<{kind}>{''.join(rendered)}</{kind}>")
    return "".join(out)


def to_text(canonical: str) -> str:
    """Readable plain/Markdown-ish text of canonical HTML (what an Architect tool returns)."""
    text = canonical or ""
    text = re.sub(r"<strong>(.*?)</strong>", r"**\1**", text)
    text = re.sub(r"<em>(.*?)</em>", r"*\1*", text)
    text = text.replace("<br>", "\n")
    lines: list[str] = []
    for match in re.finditer(r"<(p|ul|ol)>(.*?)</\1>", text, flags=re.S):
        kind, inner = match.groups()
        if kind == "p":
            lines.append(html.unescape(re.sub(r"<[^>]+>", "", inner)))
        else:
            for n, item in enumerate(re.findall(r"<li>(.*?)</li>", inner, flags=re.S), 1):
                lines.append((f"{n}. " if kind == "ol" else "- ") + html.unescape(re.sub(r"<[^>]+>", "", item)))
        lines.append("")
    return "\n".join(lines).strip()


def _inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    return re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", escaped)


def from_text(text: str) -> str:
    """Canonical HTML of plain text: blank-line paragraphs, ``- `` bullets, ``1. `` numbered items, ``**``/``*`` marks."""
    out: list[str] = []
    paragraph: list[str] = []
    items: list[str] = []
    kind = ""

    def flush_p() -> None:
        if paragraph:
            out.append("<p>" + "<br>".join(_inline(line) for line in paragraph) + "</p>")
            paragraph.clear()

    def flush_l() -> None:
        nonlocal kind
        if items:
            out.append(f"<{kind}>" + "".join(f"<li>{item}</li>" for item in items) + f"</{kind}>")
            items.clear()
        kind = ""

    for line in (text or "").replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        bullet = re.match(r"^[-*•]\s+(.*)$", stripped)
        number = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if not stripped:
            flush_p()
            flush_l()
        elif bullet and not stripped.startswith("**"):
            flush_p()
            if kind not in ("", "ul"):
                flush_l()
            kind = "ul"
            items.append(_inline(bullet.group(1)))
        elif number:
            flush_p()
            if kind not in ("", "ol"):
                flush_l()
            kind = "ol"
            items.append(_inline(number.group(1)))
        else:
            flush_l()
            paragraph.append(stripped)
    flush_p()
    flush_l()
    return canonicalize("".join(out))
