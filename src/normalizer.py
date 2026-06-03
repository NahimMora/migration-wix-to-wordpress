"""Source row normalization helpers."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import urlparse


SLUG_SAFE_RE = re.compile(r"[^a-z0-9-]+")
WHITESPACE_RE = re.compile(r"\s+")


def normalize_wix_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return canonical fields used by SQLite and WordPress payload builders."""

    old_url = clean_text(row.get("old_url"))
    old_path = extract_old_path(old_url)
    explicit_slug = clean_text(row.get("slug"))
    desired_slug = slugify_text(explicit_slug or slug_from_old_path(old_path) or clean_text(row.get("title")))

    return {
        "wix_id": clean_text(row.get("wix_id")) or None,
        "old_url": old_url or None,
        "old_path": old_path or None,
        "desired_slug": desired_slug or None,
        "title": clean_text(row.get("title")) or "(sin titulo)",
        "source_date": normalize_date(row.get("date")),
        "category_source": clean_text(row.get("category")) or None,
        "author_source_id": clean_text(row.get("author_source_id")) or None,
        "author_source_slug": clean_text(row.get("author_source_slug")) or None,
        "wp_author_id": clean_text(row.get("author")) or None,
        "featured_image_url": clean_text(row.get("image_url")) or None,
        "raw_payload": dict(row),
    }


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return WHITESPACE_RE.sub(" ", str(value).strip())


def normalize_date(value: Any) -> str | None:
    raw = clean_text(value)
    if not raw:
        return None
    candidates = [
        raw,
        raw.replace("Z", "+00:00"),
        raw.replace("/", "-"),
    ]
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y",
    ]
    for candidate in candidates:
        try:
            return datetime.fromisoformat(candidate).isoformat(sep=" ")
        except ValueError:
            pass
        for fmt in formats:
            try:
                return datetime.strptime(candidate, fmt).isoformat(sep=" ")
            except ValueError:
                continue
    return raw


def extract_old_path(old_url: str) -> str:
    if not old_url:
        return ""
    parsed = urlparse(old_url)
    path = parsed.path if parsed.scheme or parsed.netloc else old_url
    if not path.startswith("/"):
        path = "/" + path
    return path or "/"


def slug_from_old_path(old_path: str) -> str:
    if not old_path:
        return ""
    path = old_path.rstrip("/")
    if not path:
        return ""
    return path.split("/")[-1]


def slugify_text(value: str) -> str:
    text = clean_text(value).lower()
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.replace("_", "-")
    text = SLUG_SAFE_RE.sub("-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text


def source_key(row: Mapping[str, Any]) -> str:
    wix_id = clean_text(row.get("wix_id"))
    if wix_id:
        return wix_id
    old_url = clean_text(row.get("old_url"))
    if old_url:
        return old_url
    return ""
