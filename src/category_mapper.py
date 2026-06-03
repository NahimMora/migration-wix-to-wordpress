"""Category mapping helpers."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .database import MigrationDB
from .normalizer import clean_text


REQUIRED_CATEGORY_COLUMNS = {"wix_category", "wp_category_id"}


def load_category_map(file_path: Path, db: MigrationDB) -> dict[str, Any]:
    if not file_path.exists():
        return {"loaded": 0, "warnings": [f"Category map not found: {file_path}"]}

    loaded = 0
    warnings: list[str] = []
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or [])
        missing = REQUIRED_CATEGORY_COLUMNS - headers
        if missing:
            return {"loaded": 0, "warnings": [f"Missing category columns: {', '.join(sorted(missing))}"]}
        for line_number, row in enumerate(reader, start=2):
            wix_category = clean_text(row.get("wix_category"))
            wp_category_id_raw = clean_text(row.get("wp_category_id"))
            wp_category_name = clean_text(row.get("wp_category_name"))
            if not wix_category or not wp_category_id_raw:
                warnings.append(f"Line {line_number}: empty category mapping")
                continue
            try:
                wp_category_id = int(wp_category_id_raw)
            except ValueError:
                warnings.append(f"Line {line_number}: invalid wp_category_id={wp_category_id_raw}")
                continue
            db.upsert_category(wix_category, wp_category_id, wp_category_name)
            loaded += 1
    return {"loaded": loaded, "warnings": warnings}


def resolve_category(db: MigrationDB, wix_category: str | None, default_category_id: int) -> tuple[int, str | None]:
    if wix_category:
        mapping = db.get_category(wix_category)
        if mapping:
            return int(mapping["wp_category_id"]), None
        return default_category_id, f"Category not mapped: {wix_category}. Using default ID {default_category_id}."
    return default_category_id, f"Empty category. Using default ID {default_category_id}."
