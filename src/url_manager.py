"""URL analysis and SEO-safe path comparison."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .config import Settings
from .csv_loader import read_csv_rows
from .normalizer import clean_text, extract_old_path, slug_from_old_path, slugify_text


PRE_MIGRATION_URL_RISK_FIELDS = [
    "wix_id",
    "old_url",
    "old_path",
    "desired_slug",
    "expected_new_path",
    "status",
    "needs_redirect",
    "reason",
]


def analyze_urls(file_path: Path, settings: Settings | None = None) -> dict[str, Any]:
    rows, warnings = read_csv_rows(file_path)
    urls = [clean_text(row.get("old_url")) for row in rows]
    non_empty = [url for url in urls if url]
    duplicates = [url for url, count in Counter(non_empty).items() if count > 1]

    post_structure = 0
    other_structure = 0
    query_strings = 0
    special_chars = 0
    non_ascii = 0
    invalid = 0

    for url in non_empty:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            invalid += 1
            continue
        path = unquote(parsed.path)
        if parsed.query:
            query_strings += 1
        if path.startswith("/post/"):
            post_structure += 1
        else:
            other_structure += 1
        if any(char.isspace() for char in path):
            special_chars += 1
        try:
            path.encode("ascii")
        except UnicodeEncodeError:
            non_ascii += 1

    risk_rows = pre_migration_url_risk_rows_from_rows(rows, settings) if settings else []
    risk_status_counts = Counter(row["status"] for row in risk_rows)

    return {
        "file": str(file_path),
        "total_rows": len(rows),
        "total_urls": len(non_empty),
        "empty_urls": len(rows) - len(non_empty),
        "post_structure": post_structure,
        "other_structure": other_structure,
        "query_strings": query_strings,
        "special_characters": special_chars,
        "non_ascii_paths": non_ascii,
        "duplicate_urls": len(duplicates),
        "invalid_urls": invalid,
        "pre_migration_url_risk_rows": len(risk_rows),
        "pre_migration_url_risk_needs_redirect": sum(1 for row in risk_rows if row["needs_redirect"]),
        "pre_migration_url_risk_status_counts": dict(sorted(risk_status_counts.items())),
        "warnings": warnings,
    }


def desired_slug_from_url_or_title(old_url: str | None, explicit_slug: str | None, title: str | None) -> str:
    explicit = clean_text(explicit_slug)
    if explicit:
        return slugify_text(explicit)
    old_path = extract_old_path(clean_text(old_url))
    slug = slug_from_old_path(old_path)
    if slug:
        return slugify_text(slug)
    return slugify_text(clean_text(title))


def build_new_url(settings: Settings, slug: str) -> str:
    path = build_new_path(settings, slug)
    if not path:
        return settings.production_site_url.rstrip("/")
    return settings.production_site_url.rstrip("/") + path


def build_new_path(settings: Settings, slug: str | None) -> str:
    cleaned_slug = clean_text(slug)
    if not cleaned_slug:
        return ""
    path = settings.permalink_structure.replace("%postname%", cleaned_slug)
    if not path.startswith("/"):
        path = "/" + path
    return path


def predict_url_change(settings: Settings, old_url: str | None, desired_slug: str | None) -> dict[str, Any]:
    old_path = decoded_old_path(old_url)
    expected_new_path = build_new_path(settings, desired_slug)

    if old_path and expected_new_path and old_path == expected_new_path:
        status = "exact_match"
        reason = "paths_match"
    elif old_path and expected_new_path and old_path.rstrip("/") == expected_new_path.rstrip("/"):
        status = "trailing_slash_only"
        reason = "trailing_slash_only"
    elif has_non_ascii(old_path) and not has_non_ascii(expected_new_path):
        status = "slug_sanitized"
        reason = "non_ascii_slug_sanitized"
    else:
        status = "path_changed"
        reason = "path_changed"

    return {
        "old_path": old_path,
        "expected_new_path": expected_new_path,
        "status": status,
        "needs_redirect": status in {"slug_sanitized", "path_changed"},
        "reason": reason,
    }


def pre_migration_url_risk_rows(file_path: Path, settings: Settings) -> list[dict[str, Any]]:
    rows, _warnings = read_csv_rows(file_path)
    return pre_migration_url_risk_rows_from_rows(rows, settings)


def pre_migration_url_risk_rows_from_rows(rows: list[dict[str, Any]], settings: Settings) -> list[dict[str, Any]]:
    report_rows: list[dict[str, Any]] = []
    for row in rows:
        old_url = clean_text(row.get("old_url"))
        desired_slug = desired_slug_from_url_or_title(old_url, row.get("slug"), row.get("title"))
        prediction = predict_url_change(settings, old_url, desired_slug)
        report_rows.append(
            {
                "wix_id": clean_text(row.get("wix_id")),
                "old_url": old_url,
                "old_path": prediction["old_path"],
                "desired_slug": desired_slug,
                "expected_new_path": prediction["expected_new_path"],
                "status": prediction["status"],
                "needs_redirect": prediction["needs_redirect"],
                "reason": prediction["reason"],
            }
        )
    return report_rows


def classify_url_status(old_url: str | None, new_url: str | None) -> str:
    if not old_url:
        return "missing_old_url"
    old_parsed = urlparse(old_url)
    if old_parsed.scheme not in {"http", "https"} or not old_parsed.netloc:
        return "invalid_old_url"
    if not new_url:
        return "error"

    old_path = old_parsed.path or "/"
    new_path = urlparse(new_url).path or "/"
    if old_path == new_path:
        return "exact_match"
    if old_path.rstrip("/") == new_path.rstrip("/"):
        return "trailing_slash_only"
    if old_path.startswith("/post/") and not new_path.startswith("/post/"):
        return "path_structure_changed"
    old_slug = old_path.rstrip("/").split("/")[-1]
    new_slug = new_path.rstrip("/").split("/")[-1]
    if old_slug != new_slug:
        return "changed_by_wordpress"
    return "path_structure_changed"


def redirect_reason(url_status: str) -> str:
    reasons = {
        "changed_by_wordpress": "WordPress changed the slug or resolved a conflict",
        "path_structure_changed": "The final permalink path differs from the old path",
        "invalid_old_url": "The source URL is invalid",
        "missing_old_url": "The source row has no old_url",
        "error": "The final URL could not be determined",
    }
    return reasons.get(url_status, url_status)


def decoded_old_path(old_url: str | None) -> str:
    raw = clean_text(old_url)
    if not raw:
        return ""
    parsed = urlparse(raw)
    path = parsed.path if parsed.scheme or parsed.netloc else raw
    if not path.startswith("/"):
        path = "/" + path
    return unquote(path or "/")


def has_non_ascii(value: str | None) -> bool:
    try:
        clean_text(value).encode("ascii")
    except UnicodeEncodeError:
        return True
    return False
