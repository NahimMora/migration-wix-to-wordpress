"""Content formatting for Wix richContent and plain contentText."""

from __future__ import annotations

import csv
import html
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .encoding_utils import decode_text_file, fix_mojibake
from .normalizer import clean_text


CONTENT_FORMAT_REPORT_FIELDS = [
    "wix_id",
    "title",
    "content_source_used",
    "original_length",
    "paragraphs_generated",
    "avg_paragraph_length",
    "warnings",
]

TEMPORAL_CONNECTORS = (
    "Cerca de",
    "Luego",
    "Finalmente",
    "Además",
    "Segun",
    "Según",
    "Efectivos",
    "Personal",
    "Cabe destacar",
)

SENTENCE_RE = re.compile(r"[^.!?]+(?:[.!?]+(?:[\"'”’)]*)|$)", re.UNICODE)


@dataclass(frozen=True)
class ContentFormatResult:
    html: str
    source_used: str
    paragraphs: list[str]
    warnings: list[str]
    original_length: int

    @property
    def paragraph_count(self) -> int:
        return len(self.paragraphs)

    @property
    def avg_paragraph_length(self) -> int:
        if not self.paragraphs:
            return 0
        return round(sum(len(paragraph) for paragraph in self.paragraphs) / len(self.paragraphs))

    def report_row(self, wix_id: str, title: str) -> dict[str, Any]:
        return {
            "wix_id": wix_id,
            "title": title,
            "content_source_used": self.source_used,
            "original_length": self.original_length,
            "paragraphs_generated": self.paragraph_count,
            "avg_paragraph_length": self.avg_paragraph_length,
            "warnings": ";".join(self.warnings),
        }


def format_wix_content(content_text: str, rich_content: str, settings: Settings) -> ContentFormatResult:
    content_text = fix_mojibake(content_text or "")
    rich_content = fix_mojibake(rich_content or "")
    warnings: list[str] = []

    if settings.content_format_mode == "richcontent_first":
        rich_paragraphs, rich_warnings = paragraphs_from_rich_content(rich_content)
        warnings.extend(rich_warnings)

        if len(rich_paragraphs) >= 2:
            return _result_from_paragraphs(
                rich_paragraphs,
                "richContent",
                warnings,
                original_length=len(rich_content or content_text),
            )

        if len(rich_paragraphs) == 1 and settings.content_paragraph_heuristic:
            # Single richContent block - try to split it using sentence heuristic.
            single_text = visible_inline_text(rich_paragraphs[0])
            if single_text:
                split_paragraphs, split_warnings = paragraphs_from_content_text(single_text, settings)
                warnings.extend(split_warnings)
                if len(split_paragraphs) >= 2:
                    return _result_from_paragraphs(
                        split_paragraphs,
                        "richContent_split",
                        warnings,
                        original_length=len(rich_content),
                    )
            # Cannot split further - return the single richContent paragraph as-is.
            if len(rich_paragraphs) == 1 and not clean_text(content_text):
                return _result_from_paragraphs(
                    rich_paragraphs,
                    "richContent",
                    warnings + ["richContent_single_paragraph"],
                    original_length=len(rich_content),
                )

    if settings.content_paragraph_heuristic:
        paragraphs, heuristic_warnings = paragraphs_from_content_text(content_text, settings)
        warnings.extend(heuristic_warnings)
        return _result_from_paragraphs(paragraphs, "contentText_heuristic", warnings, original_length=len(content_text))

    paragraphs = split_existing_paragraphs(content_text)
    return _result_from_paragraphs(paragraphs, "contentText", warnings, original_length=len(content_text))


def preview_content_format(input_file: Path, settings: Settings, limit: int = 5) -> dict[str, Any]:
    decoded = decode_text_file(input_file)
    reader = csv.DictReader(io.StringIO(decoded.text))
    previews: list[dict[str, Any]] = []
    report_rows: list[dict[str, Any]] = []

    for index, row in enumerate(reader, start=1):
        if index > limit:
            break
        wix_id = clean_text(row.get("id") or row.get("wix_id"))
        title = fix_mojibake(clean_text(row.get("title")))
        content_text = str(row.get("contentText") or row.get("content") or "")
        rich_content = str(row.get("richContent") or row.get("page_content") or "")
        result = format_wix_content(content_text, rich_content, settings)
        report_rows.append(result.report_row(wix_id, title))
        previews.append(
            {
                "title": title,
                "original_excerpt": abbreviate(clean_text(fix_mojibake(content_text)), 350),
                "html_generated": result.html,
                "paragraphs_generated": result.paragraph_count,
                "content_source_used": result.source_used,
                "warnings": result.warnings,
            }
        )

    report_path = settings.output_dir / "content_format_report.csv"
    write_content_format_report(report_path, report_rows)
    return {
        "file": str(input_file),
        "limit": limit,
        "content_format_report": str(report_path),
        "previews": previews,
    }


def paragraphs_from_rich_content(raw: str) -> tuple[list[str], list[str]]:
    text = (raw or "").strip()
    if not text:
        return [], ["missing_richContent"]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], [f"richContent_json_error:{exc.msg}"]

    nodes = parsed.get("nodes") if isinstance(parsed, dict) else parsed
    if not isinstance(nodes, list):
        return [], ["richContent_missing_nodes"]

    paragraphs: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = clean_text(node.get("type")).upper()
        if node_type in {"PARAGRAPH", "HEADING", "BLOCKQUOTE"}:
            paragraph = collect_inline_html(node)
            if visible_inline_text(paragraph):
                paragraphs.append(paragraph)
        elif node_type in {"ORDERED_LIST", "BULLETED_LIST", "LIST"}:
            for item in node.get("nodes") or []:
                paragraph = collect_inline_html(item)
                if visible_inline_text(paragraph):
                    paragraphs.append(paragraph)
    return dedupe_adjacent(paragraphs), []


def collect_inline_html(node: Any) -> str:
    parts: list[str] = []
    _collect_inline_parts(node, parts)
    return normalize_inline_html("".join(parts))


def _collect_inline_parts(node: Any, parts: list[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _collect_inline_parts(item, parts)
        return
    if not isinstance(node, dict):
        return

    text_data = node.get("textData")
    if isinstance(text_data, dict):
        text = fix_mojibake(str(text_data.get("text") or ""))
        if text:
            escaped = html.escape(text)
            link_url = link_from_text_data(text_data)
            if link_url:
                escaped = f'<a href="{html.escape(link_url, quote=True)}">{escaped}</a>'
            parts.append(escaped)
    elif isinstance(node.get("text"), str):
        parts.append(html.escape(fix_mojibake(str(node.get("text")))))

    for child in node.get("nodes") or []:
        _collect_inline_parts(child, parts)


def link_from_text_data(text_data: dict[str, Any]) -> str:
    decorations = text_data.get("decorations")
    if not isinstance(decorations, list):
        return ""
    for decoration in decorations:
        if not isinstance(decoration, dict) or clean_text(decoration.get("type")).upper() != "LINK":
            continue
        link_data = decoration.get("linkData")
        if not isinstance(link_data, dict):
            continue
        for path in (("link", "url"), ("link", "external", "url"), ("url",)):
            current: Any = link_data
            for key in path:
                if not isinstance(current, dict):
                    current = None
                    break
                current = current.get(key)
            if isinstance(current, str) and current.strip():
                return current.strip()
    return ""


def paragraphs_from_content_text(text: str, settings: Settings) -> tuple[list[str], list[str]]:
    raw = normalize_plain_text(text)
    if not raw:
        return [], ["missing_content"]
    existing = split_existing_paragraphs(raw)
    if len(existing) > 1:
        return existing, []

    sentences = split_sentences(raw)
    if len(sentences) <= 1:
        return split_long_text(raw, settings.max_paragraph_chars), ["contentText_no_sentence_boundaries"]

    paragraphs: list[str] = []
    current: list[str] = []
    current_len = 0
    for sentence in sentences:
        starts_connector = starts_with_connector(sentence)
        should_flush = bool(
            current
            and (
                len(current) >= settings.max_sentences_per_paragraph
                or current_len + len(sentence) > settings.max_paragraph_chars
                or (starts_connector and len(current) >= settings.min_sentences_per_paragraph)
            )
        )
        if should_flush:
            paragraphs.append(" ".join(current).strip())
            current = []
            current_len = 0
        current.append(sentence)
        current_len += len(sentence) + 1
    if current:
        paragraphs.append(" ".join(current).strip())

    return rebalance_short_paragraphs(paragraphs, settings), []


def split_existing_paragraphs(text: str) -> list[str]:
    raw = normalize_plain_text(text)
    if not raw:
        return []
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n+", raw) if item.strip()]
    if len(paragraphs) == 1:
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if len(lines) > 1:
            paragraphs = lines
    return paragraphs


def split_sentences(text: str) -> list[str]:
    sentences = []
    for match in SENTENCE_RE.finditer(text):
        sentence = match.group(0).strip()
        if sentence:
            sentences.append(sentence)
    return sentences


def split_long_text(text: str, max_chars: int) -> list[str]:
    words = text.split()
    paragraphs: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        if current and current_len + len(word) + 1 > max_chars:
            paragraphs.append(" ".join(current))
            current = []
            current_len = 0
        current.append(word)
        current_len += len(word) + 1
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def rebalance_short_paragraphs(paragraphs: list[str], settings: Settings) -> list[str]:
    balanced: list[str] = []
    for paragraph in paragraphs:
        if (
            balanced
            and len(paragraph) < 120
            and not starts_with_connector(paragraph)
            and len(balanced[-1]) + len(paragraph) + 1 <= settings.max_paragraph_chars
        ):
            balanced[-1] = f"{balanced[-1]} {paragraph}"
        else:
            balanced.append(paragraph)
    return balanced


def _result_from_paragraphs(
    paragraphs: list[str],
    source: str,
    warnings: list[str],
    original_length: int,
) -> ContentFormatResult:
    clean_paragraphs = [normalize_inline_html(paragraph) for paragraph in paragraphs if visible_inline_text(paragraph)]
    if not clean_paragraphs:
        return ContentFormatResult("", source, [], warnings + ["no_paragraphs_generated"], original_length)
    html_output = "\n".join(f"<p>{paragraph}</p>" for paragraph in clean_paragraphs)
    return ContentFormatResult(html_output, source, clean_paragraphs, warnings, original_length)


def _rich_content_is_useful(paragraphs: list[str]) -> bool:
    """Return True when richContent already gives multiple usable paragraphs."""
    return len(paragraphs) >= 2


def normalize_plain_text(text: str) -> str:
    text = fix_mojibake(text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_inline_html(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def visible_inline_text(value: str) -> str:
    return clean_text(re.sub(r"<[^>]+>", "", html.unescape(value or "")))


def starts_with_connector(sentence: str) -> bool:
    normalized = clean_text(sentence)
    return any(normalized.startswith(connector) for connector in TEMPORAL_CONNECTORS)


def dedupe_adjacent(paragraphs: list[str]) -> list[str]:
    result: list[str] = []
    previous = ""
    for paragraph in paragraphs:
        if paragraph and paragraph != previous:
            result.append(paragraph)
            previous = paragraph
    return result


def abbreviate(value: str, max_chars: int) -> str:
    value = clean_text(value)
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."


def write_content_format_report(path: Path, rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CONTENT_FORMAT_REPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
