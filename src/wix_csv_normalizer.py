"""Normalize raw Wix export CSVs into the migrator input schema."""

from __future__ import annotations

import csv
import html
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .encoding_utils import (
    analyze_csv_text_for_mojibake,
    count_mojibake_patterns,
    decode_text_file,
    fix_mojibake,
)


NORMALIZED_COLUMNS = [
    "wix_id",
    "title",
    "content",
    "date",
    "category",
    "image_url",
    "old_url",
    "author",
    "excerpt",
    "slug",
    "tags",
    "page_content",
]


@dataclass
class NormalizeStats:
    total_rows: int = 0
    normalized_rows: int = 0
    without_image: int = 0
    without_category: int = 0
    without_date: int = 0
    without_content: int = 0
    invalid_urls: int = 0
    media_json_errors: int = 0
    category_json_errors: int = 0
    rows_with_encoding_fixes: set[int] = field(default_factory=set)
    columns_with_encoding_fixes: set[str] = field(default_factory=set)
    rows: list[dict[str, Any]] = field(default_factory=list)

    def summary_rows(self) -> list[dict[str, Any]]:
        metrics = {
            "total_rows": self.total_rows,
            "normalized_rows": self.normalized_rows,
            "without_image": self.without_image,
            "without_category": self.without_category,
            "without_date": self.without_date,
            "without_content": self.without_content,
            "invalid_urls": self.invalid_urls,
            "media_json_errors": self.media_json_errors,
            "category_json_errors": self.category_json_errors,
        }
        return [
            {
                "record_type": "summary",
                "metric": key,
                "value": value,
                "row_number": "",
                "wix_id": "",
                "warnings": "",
                "extra_category_ids": "",
                "media_json_error": "",
                "category_json_error": "",
                "old_url": "",
                "image_url": "",
            }
            for key, value in metrics.items()
        ]


def normalize_wix_csv(
    input_file: Path,
    output_file: Path,
    report_file: Path,
    encoding_report_file: Path,
) -> dict[str, Any]:
    stats = NormalizeStats()
    decoded = decode_text_file(input_file)
    input_encoding_analysis = analyze_csv_text_for_mojibake(decoded.text)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.parent.mkdir(parents=True, exist_ok=True)
    encoding_report_file.parent.mkdir(parents=True, exist_ok=True)

    reader = csv.DictReader(io.StringIO(decoded.text))
    if not reader.fieldnames:
        raise ValueError(f"CSV has no header row: {input_file}")

    with output_file.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=NORMALIZED_COLUMNS)
        writer.writeheader()
        for row_number, row in enumerate(reader, start=2):
            stats.total_rows += 1
            normalized, report_row = normalize_wix_export_row(row, row_number)
            writer.writerow(normalized)
            stats.normalized_rows += 1
            stats.rows.append(report_row)
            _accumulate_stats(stats, report_row)

    _write_report(report_file, stats)
    output_text = output_file.read_text(encoding="utf-8")
    output_pattern_count = count_mojibake_patterns(output_text)
    _write_encoding_report(
        encoding_report_file,
        input_file,
        decoded.encoding_detected,
        decoded.encoding_used,
        input_encoding_analysis["pattern_count_before"],
        output_pattern_count,
        stats,
    )
    return {
        "input": str(input_file),
        "output": str(output_file),
        "report": str(report_file),
        "encoding_report": str(encoding_report_file),
        "encoding_detected": decoded.encoding_detected,
        "encoding_used": decoded.encoding_used,
        "mojibake_patterns_before": input_encoding_analysis["pattern_count_before"],
        "mojibake_patterns_after": output_pattern_count,
        "encoding_rows_affected": len(stats.rows_with_encoding_fixes),
        "encoding_columns_affected": sorted(stats.columns_with_encoding_fixes),
        "total_rows": stats.total_rows,
        "normalized_rows": stats.normalized_rows,
        "without_image": stats.without_image,
        "without_category": stats.without_category,
        "without_date": stats.without_date,
        "without_content": stats.without_content,
        "invalid_urls": stats.invalid_urls,
        "media_json_errors": stats.media_json_errors,
        "category_json_errors": stats.category_json_errors,
    }


def normalize_wix_export_row(row: dict[str, Any], row_number: int) -> tuple[dict[str, str], dict[str, Any]]:
    warnings: list[str] = []
    media_error = ""
    category_error = ""
    fixed_columns: set[str] = set()

    wix_id = _value(row, "id")
    title = _fixed_value(row, "title", fixed_columns)
    slug = _fixed_value(row, "slug", fixed_columns, preserve_whitespace=True).strip()
    excerpt = _fixed_value(row, "excerpt", fixed_columns)
    content_text = _fixed_value(row, "contentText", fixed_columns, preserve_whitespace=True)
    rich_content_raw = _value(row, "richContent", preserve_whitespace=True)
    rich_content, rich_content_fixed, rich_content_warning = fix_json_text_if_safe(rich_content_raw)
    if rich_content_fixed:
        fixed_columns.add("page_content")
    if rich_content_warning:
        warnings.append(rich_content_warning)
    content_source = "contentText"

    if not content_text.strip():
        extracted = extract_text_from_rich_content(rich_content)
        if extracted:
            content_text = extracted
            content_source = "richContent"

    content = text_to_simple_html(content_text)
    if not content:
        warnings.append("missing_content")

    categories, category_error = parse_category_ids(_value(row, "categoryIds", preserve_whitespace=True))
    category = categories[0] if categories else ""
    extra_categories = categories[1:]
    if not category:
        warnings.append("missing_category")
    if extra_categories:
        warnings.append("multiple_categories_first_used")

    image_url, media_error = extract_media_image_url(_value(row, "media", preserve_whitespace=True))
    if not image_url:
        warnings.append("missing_image")

    old_url = _fixed_value(row, "publicUrl", fixed_columns, preserve_whitespace=True).strip()
    if old_url and not _is_valid_url(old_url):
        warnings.append("invalid_old_url")

    date = _value(row, "firstPublishedDate")
    if not date:
        warnings.append("missing_date")

    normalized = {
        "wix_id": wix_id,
        "title": title,
        "content": content,
        "date": date,
        "category": category,
        "image_url": image_url,
        "old_url": old_url,
        "author": _fixed_value(row, "author", fixed_columns),
        "excerpt": excerpt,
        "slug": slug,
        "tags": extract_tags(row, fixed_columns),
        "page_content": rich_content,
    }

    report_row = {
        "record_type": "row",
        "metric": "",
        "value": "",
        "row_number": row_number,
        "wix_id": wix_id,
        "warnings": ";".join(warnings),
        "extra_category_ids": "|".join(extra_categories),
        "media_json_error": media_error,
        "category_json_error": category_error,
        "old_url": old_url,
        "image_url": image_url,
        "content_source": content_source,
        "encoding_fixed_columns": "|".join(sorted(fixed_columns)),
    }
    return normalized, report_row


def text_to_simple_html(text: str) -> str:
    raw = text.strip()
    if not raw:
        return ""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", raw) if part.strip()]
    if len(paragraphs) == 1:
        paragraphs = [part.strip() for part in raw.splitlines() if part.strip()]
    return "\n".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in paragraphs)


def extract_text_from_rich_content(raw: str) -> str:
    parsed, _error = _parse_jsonish(raw)
    if parsed is None:
        return ""
    texts: list[str] = []
    _collect_text_nodes(parsed, texts)
    return "\n\n".join(text for text in texts if text.strip())


def extract_media_image_url(raw: str) -> tuple[str, str]:
    parsed, error = _parse_jsonish(raw)
    if parsed is None:
        return "", error
    if isinstance(parsed, str):
        return (parsed.strip(), error) if parsed.strip().startswith(("http://", "https://")) else ("", error)
    candidates = [
        ("wixMedia", "image", "url"),
        ("image", "url"),
        ("url",),
        ("src",),
    ]
    for path in candidates:
        value = _get_path(parsed, path)
        if isinstance(value, str) and value.strip():
            return value.strip(), error
    found = _find_first_key(parsed, {"url", "src"})
    return (found.strip() if isinstance(found, str) else ""), error


def parse_category_ids(raw: str) -> tuple[list[str], str]:
    raw_text = (raw or "").strip()
    parsed, error = _parse_jsonish(raw)
    if parsed is None:
        fallback = [item.strip() for item in re.split(r"[|,;]", raw_text) if item.strip()]
        return fallback, error
    if isinstance(parsed, str) and not parsed.startswith(("{", "[")):
        return [item.strip() for item in re.split(r"[|,;]", parsed) if item.strip()], error
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()], error
    if isinstance(parsed, dict):
        for key in ("categoryIds", "ids", "items"):
            value = parsed.get(key)
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()], error
        found = _find_first_list(parsed)
        if found:
            return [str(item).strip() for item in found if str(item).strip()], error
    if isinstance(parsed, str) and parsed.strip():
        return [parsed.strip()], error
    return [], error


def extract_tags(row: dict[str, Any], fixed_columns: set[str] | None = None) -> str:
    values: list[str] = []
    for key in ("hashtags", "tagIds", "tags"):
        raw = _value(row, key, preserve_whitespace=True)
        if not raw:
            continue
        parsed, _error = _parse_jsonish(raw)
        if isinstance(parsed, list):
            for item in parsed:
                value = str(item).strip()
                fixed = fix_mojibake(value)
                if fixed != value and fixed_columns is not None:
                    fixed_columns.add("tags")
                if fixed:
                    values.append(fixed)
        elif isinstance(parsed, str):
            for part in re.split(r"[|,;]", parsed):
                value = part.strip()
                fixed = fix_mojibake(value)
                if fixed != value and fixed_columns is not None:
                    fixed_columns.add("tags")
                if fixed:
                    values.append(fixed)
        else:
            for part in re.split(r"[|,;]", raw):
                value = part.strip()
                fixed = fix_mojibake(value)
                if fixed != value and fixed_columns is not None:
                    fixed_columns.add("tags")
                if fixed:
                    values.append(fixed)
    return ",".join(dict.fromkeys(values))


def fix_json_text_if_safe(raw: str) -> tuple[str, bool, str]:
    text = raw or ""
    if not text.strip():
        return text, False, ""
    if not text.strip().startswith(("{", "[")):
        return text, False, "page_content_encoding_not_fixed_not_json" if count_mojibake_patterns(text) else ""
    parsed, error = _parse_jsonish(text)
    if parsed is None:
        return text, False, "page_content_encoding_not_fixed_invalid_json" if count_mojibake_patterns(text) else ""
    if isinstance(parsed, str):
        fixed = fix_mojibake(parsed)
        return fixed, fixed != parsed, ""
    fixed_value = _fix_json_value(parsed)
    if fixed_value == parsed:
        return text, False, ""
    try:
        return json.dumps(fixed_value, ensure_ascii=False), True, ""
    except (TypeError, ValueError):
        return text, False, "page_content_encoding_not_fixed_json_dump_failed"


def check_wix_csv_encoding(file_path: Path) -> dict[str, Any]:
    decoded = decode_text_file(file_path)
    analysis = analyze_csv_text_for_mojibake(decoded.text)
    return {
        "file": str(file_path),
        "encoding_detected": decoded.encoding_detected,
        "encoding_used": decoded.encoding_used,
        "tried_encodings": decoded.tried_encodings,
        "mojibake_detected": analysis["pattern_count_before"] > 0 or analysis["rows_affected"] > 0,
        "pattern_count_before": analysis["pattern_count_before"],
        "pattern_count_after_if_fixed": analysis["pattern_count_after"],
        "rows_affected": analysis["rows_affected"],
        "columns_affected": analysis["columns_affected"],
        "decode_errors": decoded.decode_errors,
    }


def _parse_jsonish(raw: str) -> tuple[Any, str]:
    text = (raw or "").strip()
    if not text:
        return None, ""
    if not text.startswith(("{", "[")):
        return text, ""
    try:
        return json.loads(text), ""
    except json.JSONDecodeError as exc:
        return None, f"{exc.msg} at pos {exc.pos}"


def _fix_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _fix_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_fix_json_value(item) for item in value]
    if isinstance(value, str):
        return fix_mojibake(value)
    return value


def _collect_text_nodes(value: Any, texts: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "text" and isinstance(item, str) and item.strip():
                texts.append(item.strip())
            else:
                _collect_text_nodes(item, texts)
    elif isinstance(value, list):
        for item in value:
            _collect_text_nodes(item, texts)


def _get_path(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _find_first_key(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and isinstance(item, str) and item.strip():
                return item
            found = _find_first_key(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_first_key(item, keys)
            if found:
                return found
    return None


def _find_first_list(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for item in value.values():
            found = _find_first_list(item)
            if found:
                return found
    return None


def _is_valid_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _value(row: dict[str, Any], key: str, preserve_whitespace: bool = False) -> str:
    value = row.get(key)
    if value is None:
        return ""
    text = str(value)
    if preserve_whitespace:
        return text
    return re.sub(r"\s+", " ", text.strip())


def _fixed_value(
    row: dict[str, Any],
    key: str,
    fixed_columns: set[str],
    preserve_whitespace: bool = False,
) -> str:
    original = _value(row, key, preserve_whitespace=preserve_whitespace)
    fixed = fix_mojibake(original)
    if fixed != original or count_mojibake_patterns(original):
        fixed_columns.add(_normalized_report_column(key))
    return fixed


def _normalized_report_column(key: str) -> str:
    mapping = {
        "contentText": "content",
        "publicUrl": "old_url",
        "hashtags": "tags",
        "tagIds": "tags",
    }
    return mapping.get(key, key)


def _accumulate_stats(stats: NormalizeStats, report_row: dict[str, Any]) -> None:
    warnings = set(str(report_row.get("warnings") or "").split(";"))
    if "missing_image" in warnings:
        stats.without_image += 1
    if "missing_category" in warnings:
        stats.without_category += 1
    if "missing_date" in warnings:
        stats.without_date += 1
    if "missing_content" in warnings:
        stats.without_content += 1
    if "invalid_old_url" in warnings:
        stats.invalid_urls += 1
    if report_row.get("media_json_error"):
        stats.media_json_errors += 1
    if report_row.get("category_json_error"):
        stats.category_json_errors += 1
    fixed_columns = [item for item in str(report_row.get("encoding_fixed_columns") or "").split("|") if item]
    if fixed_columns:
        stats.rows_with_encoding_fixes.add(int(report_row["row_number"]))
        stats.columns_with_encoding_fixes.update(fixed_columns)


def _write_report(report_file: Path, stats: NormalizeStats) -> None:
    fieldnames = [
        "record_type",
        "metric",
        "value",
        "row_number",
        "wix_id",
        "warnings",
        "extra_category_ids",
        "media_json_error",
        "category_json_error",
        "old_url",
        "image_url",
        "content_source",
        "encoding_fixed_columns",
    ]
    with report_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(stats.summary_rows())
        writer.writerows(stats.rows)


def _write_encoding_report(
    report_file: Path,
    input_file: Path,
    encoding_detected: str,
    encoding_used: str,
    pattern_count_before: int,
    pattern_count_after: int,
    stats: NormalizeStats,
) -> None:
    fieldnames = [
        "archivo",
        "encoding_detectado",
        "encoding_usado",
        "conteo_patrones_antes",
        "conteo_patrones_despues",
        "filas_afectadas",
        "columnas_afectadas",
    ]
    row = {
        "archivo": str(input_file),
        "encoding_detectado": encoding_detected,
        "encoding_usado": encoding_used,
        "conteo_patrones_antes": pattern_count_before,
        "conteo_patrones_despues": pattern_count_after,
        "filas_afectadas": len(stats.rows_with_encoding_fixes),
        "columnas_afectadas": "|".join(sorted(stats.columns_with_encoding_fixes)),
    }
    with report_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(row)
