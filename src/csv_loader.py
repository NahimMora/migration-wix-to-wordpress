"""CSV analysis and source import into SQLite."""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any

from .category_mapper import load_category_map, resolve_category
from .config import Settings
from .database import MigrationDB
from .html_cleaner import clean_html, visible_text
from .normalizer import clean_text, normalize_wix_row, source_key


EXPECTED_COLUMNS = {
    "wix_id",
    "title",
    "content",
    "date",
    "category",
    "image_url",
    "old_url",
    "author",
}
OPTIONAL_COLUMNS = {"slug", "excerpt", "tags", "page_content"}
COLUMN_ALIASES = {
    "post_title": "title",
    "body": "content",
    "published_date": "date",
    "published": "date",
    "featured_image": "image_url",
    "url": "old_url",
    "link": "old_url",
    "id": "wix_id",
}


def read_csv_rows(file_path: Path, limit: int | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return rows, ["CSV has no header row"]
        for index, row in enumerate(reader, start=1):
            rows.append(normalize_headers(row))
            if limit and index >= limit:
                break
    return rows, warnings


def normalize_headers(row: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in row.items():
        clean_key = clean_text(key).lower()
        canonical_key = COLUMN_ALIASES.get(clean_key, clean_key)
        normalized[canonical_key] = value
    return normalized


def analyze_csv(file_path: Path, db: MigrationDB | None = None) -> dict[str, Any]:
    rows, warnings = read_csv_rows(file_path)
    headers = set(rows[0].keys()) if rows else set()
    missing_columns = sorted(EXPECTED_COLUMNS - headers)
    extra_columns = sorted(headers - EXPECTED_COLUMNS - OPTIONAL_COLUMNS)

    wix_ids = [clean_text(row.get("wix_id")) for row in rows if clean_text(row.get("wix_id"))]
    old_urls = [clean_text(row.get("old_url")) for row in rows if clean_text(row.get("old_url"))]
    duplicate_wix_ids = [item for item, count in Counter(wix_ids).items() if count > 1]
    duplicate_old_urls = [item for item, count in Counter(old_urls).items() if count > 1]

    empty_content = 0
    empty_title = 0
    missing_source_key = 0
    missing_wix_id = 0
    missing_old_url = 0

    for row in rows:
        if not clean_text(row.get("title")):
            empty_title += 1
        content = clean_text(row.get("content")) or clean_text(row.get("page_content"))
        if not visible_text(content):
            empty_content += 1
        if not clean_text(row.get("wix_id")):
            missing_wix_id += 1
        if not clean_text(row.get("old_url")):
            missing_old_url += 1
        if not source_key(row):
            missing_source_key += 1

    result = {
        "file": str(file_path),
        "total_rows": len(rows),
        "columns": sorted(headers),
        "missing_expected_columns": missing_columns,
        "extra_columns": extra_columns,
        "missing_wix_id": missing_wix_id,
        "missing_old_url": missing_old_url,
        "missing_source_key": missing_source_key,
        "empty_title": empty_title,
        "empty_content": empty_content,
        "duplicate_wix_ids": len(duplicate_wix_ids),
        "duplicate_old_urls": len(duplicate_old_urls),
        "warnings": warnings,
    }

    if db:
        severity = "warning" if missing_columns or duplicate_wix_ids or duplicate_old_urls else "info"
        db.record_audit("csv", severity, "CSV analysis completed", result)
    return result


def import_csv(file_path: Path, settings: Settings, db: MigrationDB, limit: int | None = None) -> dict[str, Any]:
    category_result = load_category_map(settings.input_dir / "category_map.csv", db)
    rows, warnings = read_csv_rows(file_path, limit=limit)
    imported = 0
    skipped = 0

    for row in rows:
        normalized = normalize_wix_row(row)
        if not normalized.get("wix_id") and not normalized.get("old_url"):
            skipped += 1
            db.record_error("post", None, "import_csv", "Missing wix_id and old_url", row)
            continue

        content = clean_text(row.get("content")) or clean_text(row.get("page_content"))
        row["content_clean"] = clean_html(content)
        wp_category_id, category_warning = resolve_category(
            db,
            normalized.get("category_source"),
            settings.default_category_id,
        )
        normalized["wp_category_id"] = wp_category_id
        normalized["raw_payload"] = row
        post_id = db.upsert_post_source(normalized)
        imported += 1
        if category_warning:
            db.record_error("post", post_id, "category_mapping", category_warning, row)

    result = {
        "file": str(file_path),
        "imported": imported,
        "skipped": skipped,
        "category_map": category_result,
        "warnings": warnings + category_result.get("warnings", []),
    }
    db.record_csv_import(str(file_path), len(rows), imported, skipped, result["warnings"])
    db.record_audit("csv", "info", "CSV import completed", result)
    return result
