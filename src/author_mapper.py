"""Author mapping from Wix member IDs/slugs to WordPress user IDs."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .config import Settings
from .normalizer import clean_text
from .wordpress_client import WordPressClient, WordPressError


AUTHOR_MAP_COLUMNS = [
    "wix_member_id",
    "wix_member_slug",
    "wix_display_name",
    "wp_user_id",
    "wp_user_login",
    "wp_display_name",
    "is_default",
    "notes",
]


def load_author_map(file_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not file_path.exists():
        return [], [f"Author map not found: {file_path}"]
    rows: list[dict[str, str]] = []
    warnings: list[str] = []
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or [])
        missing = set(AUTHOR_MAP_COLUMNS) - headers
        if missing:
            return [], [f"Missing author map columns: {', '.join(sorted(missing))}"]
        for line_number, row in enumerate(reader, start=2):
            clean_row = {key: clean_text(row.get(key)) for key in AUTHOR_MAP_COLUMNS}
            if not any(clean_row.values()):
                continue
            raw_wp_id = clean_row["wp_user_id"]
            if not raw_wp_id:
                warnings.append(f"Line {line_number}: missing wp_user_id - fill with real WordPress user ID")
            elif not raw_wp_id.isdigit():
                label = "placeholder - replace with real WordPress user ID" if raw_wp_id.startswith("ID_") else f"invalid value={raw_wp_id}"
                warnings.append(f"Line {line_number}: wp_user_id is a {label}")
            rows.append(clean_row)
    return rows, warnings


def resolve_author(
    author_rows: list[dict[str, str]],
    source_author_id: str | None,
    source_author_slug: str | None,
    settings: Settings,
) -> dict[str, Any]:
    source_author_id = clean_text(source_author_id)
    source_author_slug = clean_text(source_author_slug)

    for row in author_rows:
        if source_author_id and clean_text(row.get("wix_member_id")) == source_author_id and clean_text(row.get("wp_user_id")).isdigit():
            return _author_resolution(row, source_author_id, source_author_slug, "wix_member_id", None, settings)
    for row in author_rows:
        if source_author_slug and clean_text(row.get("wix_member_slug")) == source_author_slug and clean_text(row.get("wp_user_id")).isdigit():
            return _author_resolution(row, source_author_id, source_author_slug, "wix_member_slug", None, settings)

    default_row = default_author_row(author_rows)
    warning = None
    if source_author_id or source_author_slug:
        warning = "Author not mapped. Using default author."
    if default_row and clean_text(default_row.get("wp_user_id")).isdigit():
        return _author_resolution(default_row, source_author_id, source_author_slug, "default", warning, settings)

    return {
        "source_author_id": source_author_id,
        "source_author_slug": source_author_slug,
        "matched_by": "default",
        "wp_user_id": settings.default_author_id,
        "wp_display_name": "",
        "warning": warning or "No default author row found. Using DEFAULT_AUTHOR_ID.",
    }


def verify_authors(settings: Settings) -> dict[str, Any]:
    author_rows, map_warnings = load_author_map(settings.input_dir / "author_map.csv")
    result: dict[str, Any] = {
        "ok": False,
        "author_map": str(settings.input_dir / "author_map.csv"),
        "rows": len(author_rows),
        "default_rows": 0,
        "valid_wp_user_ids": [],
        "missing_wp_user_ids": [],
        "warnings": list(map_warnings),
        "invalid_author_map": bool(map_warnings),
        "users_probe_ok": False,
    }

    default_rows = [row for row in author_rows if is_default(row)]
    result["default_rows"] = len(default_rows)
    if len(default_rows) != 1:
        result["warnings"].append("author_map must contain exactly one is_default=1 row")

    try:
        users = list_wordpress_users(settings)
        result["users_probe_ok"] = True
    except WordPressError as exc:
        result["warnings"].append(f"WordPress users probe failed: status={exc.status_code} message={exc}")
        return result

    users_by_id = {int(user["id"]): user for user in users if str(user.get("id", "")).isdigit()}
    for row in author_rows:
        raw_id = clean_text(row.get("wp_user_id"))
        if not raw_id or not raw_id.isdigit():
            continue
        wp_user_id = int(raw_id)
        if wp_user_id in users_by_id:
            result["valid_wp_user_ids"].append(wp_user_id)
        else:
            result["missing_wp_user_ids"].append(wp_user_id)
            result["warnings"].append(f"wp_user_id does not exist in WordPress: {wp_user_id}")

    result["valid_wp_user_ids"] = sorted(set(result["valid_wp_user_ids"]))
    result["missing_wp_user_ids"] = sorted(set(result["missing_wp_user_ids"]))
    result["ok"] = bool(
        result["users_probe_ok"]
        and len(default_rows) == 1
        and not result["invalid_author_map"]
        and not result["missing_wp_user_ids"]
    )
    return result


def list_wordpress_users(settings: Settings) -> list[dict[str, Any]]:
    client = WordPressClient(settings)
    users = client.list_users(context="edit")
    return [safe_user_summary(user) for user in users]


def safe_user_summary(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user.get("id"),
        "username": user.get("slug") or user.get("username"),
        "login": user.get("slug") or user.get("username"),
        "display_name": user.get("name"),
        "roles": user.get("roles", []),
    }


def default_author_row(author_rows: list[dict[str, str]]) -> dict[str, str] | None:
    defaults = [row for row in author_rows if is_default(row)]
    return defaults[0] if len(defaults) == 1 else None


def is_default(row: dict[str, str]) -> bool:
    return clean_text(row.get("is_default")).lower() in {"1", "true", "yes", "y"}


def _author_resolution(
    row: dict[str, str],
    source_author_id: str,
    source_author_slug: str,
    matched_by: str,
    warning: str | None,
    settings: Settings,
) -> dict[str, Any]:
    raw_wp_user_id = clean_text(row.get("wp_user_id"))
    return {
        "source_author_id": source_author_id,
        "source_author_slug": source_author_slug,
        "matched_by": matched_by,
        "wp_user_id": int(raw_wp_user_id) if raw_wp_user_id.isdigit() else settings.default_author_id,
        "wp_display_name": clean_text(row.get("wp_display_name")),
        "warning": warning,
    }
