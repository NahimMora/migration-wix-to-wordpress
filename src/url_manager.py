"""URL analysis and SEO-safe path comparison."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .csv_loader import read_csv_rows
from .normalizer import clean_text, extract_old_path, slug_from_old_path, slugify_text


def analyze_urls(file_path: Path) -> dict[str, Any]:
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
        if parsed.query:
            query_strings += 1
        if parsed.path.startswith("/post/"):
            post_structure += 1
        else:
            other_structure += 1
        if any(char.isspace() for char in parsed.path):
            special_chars += 1
        try:
            parsed.path.encode("ascii")
        except UnicodeEncodeError:
            non_ascii += 1

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
    path = settings.permalink_structure.replace("%postname%", slug)
    if not path.startswith("/"):
        path = "/" + path
    return settings.production_site_url.rstrip("/") + path


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
