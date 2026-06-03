"""Category mapping helpers."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .database import MigrationDB
from .encoding_utils import fix_mojibake
from .normalizer import clean_text


REQUIRED_CATEGORY_COLUMNS = {"wix_category", "wp_category_id"}
ALIAS_COLUMNS = ("wix_category", "wix_category_id", "wix_category_slug")
ALIAS_MATCH_TYPES = {
    "wix_category": "wix_category_name",
    "wix_category_id": "wix_category_id",
    "wix_category_slug": "wix_category_slug",
}


def load_category_map(file_path: Path, db: MigrationDB) -> dict[str, Any]:
    if not file_path.exists():
        return {"loaded": 0, "warnings": [f"Category map not found: {file_path}"]}

    loaded_rows = 0
    loaded_aliases = 0
    skipped_rows = 0
    warnings: list[str] = []
    seen_aliases: dict[str, int] = {}
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or [])
        missing = REQUIRED_CATEGORY_COLUMNS - headers
        if missing:
            return {"loaded": 0, "warnings": [f"Missing category columns: {', '.join(sorted(missing))}"]}
        db.clear_categories()
        for line_number, row in enumerate(reader, start=2):
            alias_entries = category_alias_entries(row)
            aliases = [entry["value"] for entry in alias_entries]
            wp_category_id_raw = clean_text(row.get("wp_category_id"))
            wp_category_name = fixed_clean_text(row.get("wp_category_name")) or fixed_clean_text(row.get("wix_category"))
            if not aliases:
                skipped_rows += 1
                warnings.append(f"Line {line_number}: no Wix category alias")
                continue
            if not wp_category_id_raw:
                skipped_rows += 1
                warnings.append(
                    f"Line {line_number}: missing wp_category_id for aliases {', '.join(aliases)}"
                )
                continue
            try:
                wp_category_id = int(wp_category_id_raw)
            except ValueError:
                skipped_rows += 1
                warnings.append(f"Line {line_number}: invalid wp_category_id={wp_category_id_raw}")
                continue
            for entry in alias_entries:
                alias = entry["value"]
                previous = seen_aliases.get(alias)
                if previous is not None and previous != wp_category_id:
                    warnings.append(
                        f"Line {line_number}: alias {alias} remapped from WP category {previous} to {wp_category_id}"
                    )
                seen_aliases[alias] = wp_category_id
                db.upsert_category(alias, wp_category_id, wp_category_name, entry["matched_by"])
                loaded_aliases += 1
            loaded_rows += 1
    return {
        "loaded": loaded_aliases,
        "loaded_rows": loaded_rows,
        "loaded_aliases": loaded_aliases,
        "skipped_rows": skipped_rows,
        "warnings": warnings,
    }


def resolve_category(db: MigrationDB, wix_category: str | None, default_category_id: int) -> tuple[int, str | None]:
    resolution = resolve_category_detail(db, wix_category, default_category_id)
    return int(resolution["wp_category_id"]), resolution["warning"]


def resolve_category_detail(db: MigrationDB, wix_category: str | None, default_category_id: int) -> dict[str, Any]:
    source_category = fixed_clean_text(wix_category)
    default_mapping = db.get_category_by_wp_id(int(default_category_id))
    default_category_name = default_mapping.get("wp_category_name") if default_mapping else ""

    if source_category:
        for candidate in resolve_candidates(source_category):
            mapping = db.get_category(candidate)
            if mapping:
                return {
                    "source_category": source_category,
                    "matched_by": mapping.get("wix_category_alias_type") or infer_match_type(candidate),
                    "wp_category_id": int(mapping["wp_category_id"]),
                    "wp_category_name": mapping.get("wp_category_name") or "",
                    "warning": None,
                }
        warning = f"Category not mapped: {source_category}. Using default ID {default_category_id}."
    else:
        warning = f"Empty category. Using default ID {default_category_id}."

    return {
        "source_category": source_category,
        "matched_by": "default",
        "wp_category_id": int(default_category_id),
        "wp_category_name": default_category_name or "",
        "warning": warning,
    }


def category_aliases(row: dict[str, Any]) -> list[str]:
    return [entry["value"] for entry in category_alias_entries(row)]


def category_alias_entries(row: dict[str, Any]) -> list[dict[str, str]]:
    aliases: list[str] = []
    entries: list[dict[str, str]] = []
    for column in ALIAS_COLUMNS:
        value = fixed_clean_text(row.get(column))
        if value and value not in aliases:
            aliases.append(value)
            entries.append({"value": value, "matched_by": ALIAS_MATCH_TYPES[column]})
    return entries


def resolve_candidates(value: str) -> list[str]:
    cleaned = clean_text(value)
    fixed = fixed_clean_text(value)
    candidates: list[str] = []
    for candidate in (cleaned, fixed):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def fixed_clean_text(value: Any) -> str:
    return clean_text(fix_mojibake(value))


def infer_match_type(value: str) -> str:
    cleaned = fixed_clean_text(value)
    if len(cleaned) == 36 and cleaned.count("-") == 4:
        return "wix_category_id"
    if cleaned and cleaned.lower() == cleaned and " " not in cleaned:
        return "wix_category_slug"
    return "wix_category_name"
