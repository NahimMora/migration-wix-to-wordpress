"""Normalize Wix category exports into category_map.csv-compatible rows."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .category_mapper import category_aliases, fixed_clean_text
from .encoding_utils import count_mojibake_patterns, decode_text_file, fix_mojibake
from .normalizer import clean_text


CATEGORY_MAP_COLUMNS = [
    "wix_category",
    "wix_category_id",
    "wix_category_slug",
    "wp_category_id",
    "wp_category_name",
    "wix_post_count",
    "description",
    "cover_image_url",
    "notes",
]


def normalize_wix_categories(
    input_file: Path,
    output_file: Path,
    existing_map_file: Path | None = None,
) -> dict[str, Any]:
    """Create a safe category mapping draft from a Wix categories JSON export."""

    decoded = decode_text_file(input_file)
    source_pattern_count = count_mojibake_patterns(decoded.text)
    try:
        payload = json.loads(decoded.text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid Wix categories JSON at pos {exc.pos}: {exc.msg}") from exc

    categories = _extract_categories(payload)
    existing_map = load_existing_wp_mappings(existing_map_file)
    rows: list[dict[str, Any]] = []
    rows_with_encoding_fixes = 0

    for category in categories:
        row, fixed = normalize_wix_category(category, existing_map)
        rows.append(row)
        if fixed:
            rows_with_encoding_fixes += 1

    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CATEGORY_MAP_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    output_text = output_file.read_text(encoding="utf-8")
    missing_wp_category_id = sum(1 for row in rows if not clean_text(row.get("wp_category_id")))
    return {
        "input": str(input_file),
        "output": str(output_file),
        "existing_map": str(existing_map_file) if existing_map_file else None,
        "encoding_detected": decoded.encoding_detected,
        "encoding_used": decoded.encoding_used,
        "total_categories": len(categories),
        "normalized_categories": len(rows),
        "with_wp_category_id": len(rows) - missing_wp_category_id,
        "missing_wp_category_id": missing_wp_category_id,
        "rows_with_encoding_fixes": rows_with_encoding_fixes,
        "mojibake_patterns_before": source_pattern_count,
        "mojibake_patterns_after": count_mojibake_patterns(output_text),
    }


def normalize_wix_category(
    category: dict[str, Any],
    existing_map: dict[str, dict[str, str]],
) -> tuple[dict[str, Any], bool]:
    raw_label = clean_text(category.get("label")) or clean_text(category.get("title"))
    raw_title = clean_text(category.get("title"))
    raw_slug = clean_text(category.get("slug"))
    raw_description = clean_text(category.get("description"))

    label = fixed_clean_text(raw_label)
    title = fixed_clean_text(raw_title)
    slug = fixed_clean_text(raw_slug)
    description = fixed_clean_text(raw_description)
    wix_category_id = clean_text(category.get("id"))
    cover_image_url = _cover_image_url(category)

    merged = first_existing_mapping(existing_map, [wix_category_id, label, slug, title])
    wp_category_id = merged.get("wp_category_id", "")
    wp_category_name = merged.get("wp_category_name", "") or label

    notes: list[str] = []
    if not wp_category_id:
        notes.append("pending_wp_category_id")
    if label != raw_label or title != raw_title or slug != raw_slug or description != raw_description:
        notes.append("mojibake_fixed")

    row = {
        "wix_category": label,
        "wix_category_id": wix_category_id,
        "wix_category_slug": slug,
        "wp_category_id": wp_category_id,
        "wp_category_name": wp_category_name,
        "wix_post_count": category.get("postCount", ""),
        "description": description,
        "cover_image_url": cover_image_url,
        "notes": "|".join(notes),
    }
    return row, "mojibake_fixed" in notes


def load_existing_wp_mappings(file_path: Path | None) -> dict[str, dict[str, str]]:
    if not file_path or not file_path.exists():
        return {}
    mappings: dict[str, dict[str, str]] = {}
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            wp_category_id = clean_text(row.get("wp_category_id"))
            if not wp_category_id:
                continue
            wp_category_name = fixed_clean_text(row.get("wp_category_name")) or fixed_clean_text(row.get("wix_category"))
            mapping = {
                "wp_category_id": wp_category_id,
                "wp_category_name": wp_category_name,
            }
            for alias in category_aliases(row):
                mappings.setdefault(alias, mapping)
    return mappings


def first_existing_mapping(known_mappings: dict[str, dict[str, str]], aliases: list[str]) -> dict[str, str]:
    candidates: list[str] = []
    for alias in aliases:
        fixed = fixed_clean_text(alias)
        if fixed and fixed not in candidates:
            candidates.append(fixed)
    for candidate in candidates:
        if candidate in known_mappings:
            return known_mappings[candidate]
    return {}


def _extract_categories(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        categories = payload.get("categories")
    else:
        categories = payload
    if not isinstance(categories, list):
        raise ValueError("Wix categories JSON must be a list or an object with a categories list")
    return [item for item in categories if isinstance(item, dict)]


def _cover_image_url(category: dict[str, Any]) -> str:
    cover_image = category.get("coverImage")
    if isinstance(cover_image, dict):
        return clean_text(fix_mojibake(cover_image.get("url")))
    return ""
