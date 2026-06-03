"""Encoding and mojibake diagnostics for Wix CSV inputs."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SUSPICIOUS_PATTERNS = (
    "\u00c3",
    "\u00c2",
    "\u00e2\u20ac",
    "\ufffd",
)

DIRECT_REPLACEMENTS = {
    "\u00e2\u20ac\u0153": "\u201c",
    "\u00e2\u20ac\u009d": "\u201d",
    "\u00e2\u20ac\ufffd": "\u201d",
    "\u00e2\u20ac\u02dc": "\u2018",
    "\u00e2\u20ac\u2122": "\u2019",
    "\u00e2\u20ac\u201c": "\u2013",
    "\u00e2\u20ac\u201d": "\u2014",
    "\u00e2\u20ac\u00a6": "\u2026",
    "\u00c2\u00a0": " ",
    "\u00c2 ": " ",
}

CRITICAL_TEXT_COLUMNS = (
    "title",
    "content",
    "contentText",
    "richContent",
    "excerpt",
    "slug",
    "old_url",
    "publicUrl",
    "tags",
    "hashtags",
    "tagIds",
    "page_content",
)


@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding_detected: str
    encoding_used: str
    tried_encodings: list[str]
    decode_errors: dict[str, str]


def decode_text_file(file_path: Path) -> DecodedText:
    raw = file_path.read_bytes()
    tried: list[str] = []
    errors: dict[str, str] = {}

    for encoding in ("utf-8-sig", "utf-8"):
        tried.append(encoding)
        try:
            text = raw.decode(encoding)
            return DecodedText(text, encoding, encoding, tried, errors)
        except UnicodeDecodeError as exc:
            errors[encoding] = str(exc)

    for encoding in ("cp1252", "latin1"):
        tried.append(encoding)
        try:
            text = raw.decode(encoding)
            return DecodedText(text, encoding, encoding, tried, errors)
        except UnicodeDecodeError as exc:
            errors[encoding] = str(exc)

    text = raw.decode("utf-8", errors="replace")
    return DecodedText(text, "utf-8-replace", "utf-8-replace", tried, errors)


def count_mojibake_patterns(text: Any) -> int:
    if not isinstance(text, str) or not text:
        return 0
    return sum(text.count(pattern) for pattern in SUSPICIOUS_PATTERNS)


def fix_mojibake(text: Any) -> Any:
    if not isinstance(text, str) or not text:
        return text

    candidate = apply_direct_replacements(text)
    best = candidate
    best_score = count_mojibake_patterns(best)

    for encoding in ("cp1252", "latin1"):
        try:
            repaired = candidate.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        repaired = apply_direct_replacements(repaired)
        score = count_mojibake_patterns(repaired)
        if score < best_score:
            best = repaired
            best_score = score

    return apply_direct_replacements(best).replace("\u00a0", " ").replace("\u00c2", "")


def apply_direct_replacements(text: str) -> str:
    fixed = text
    for bad, good in DIRECT_REPLACEMENTS.items():
        fixed = fixed.replace(bad, good)
    return fixed


def analyze_text_for_mojibake(text: str) -> dict[str, Any]:
    fixed = fix_mojibake(text)
    return {
        "mojibake_detected": count_mojibake_patterns(text) > 0,
        "pattern_count_before": count_mojibake_patterns(text),
        "pattern_count_after": count_mojibake_patterns(fixed),
    }


def analyze_csv_text_for_mojibake(text: str, columns: Iterable[str] = CRITICAL_TEXT_COLUMNS) -> dict[str, Any]:
    columns_set = set(columns)
    rows_affected: set[int] = set()
    columns_affected: set[str] = set()

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return {
            "rows_affected": 0,
            "columns_affected": [],
            "pattern_count_before": count_mojibake_patterns(text),
            "pattern_count_after": count_mojibake_patterns(fix_mojibake(text)),
        }

    for row_number, row in enumerate(reader, start=2):
        for column, value in row.items():
            if column not in columns_set:
                continue
            if count_mojibake_patterns(value or "") > 0:
                rows_affected.add(row_number)
                columns_affected.add(column)

    return {
        "rows_affected": len(rows_affected),
        "columns_affected": sorted(columns_affected),
        "pattern_count_before": count_mojibake_patterns(text),
        "pattern_count_after": count_mojibake_patterns(fix_mojibake(text)),
    }


def read_csv_dicts_with_encoding(file_path: Path) -> tuple[DecodedText, csv.DictReader]:
    decoded = decode_text_file(file_path)
    return decoded, csv.DictReader(io.StringIO(decoded.text))
