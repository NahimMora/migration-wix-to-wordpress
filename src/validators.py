"""Small validation helpers used before dry-run and migration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .html_cleaner import visible_text
from .normalizer import clean_text


ALLOWED_POST_STATUSES = {"draft", "pending", "private", "publish"}


def validate_default_post_status(status: str) -> list[str]:
    if status not in ALLOWED_POST_STATUSES:
        return [f"DEFAULT_POST_STATUS={status} is not a standard WordPress status"]
    if status == "publish":
        return ["DEFAULT_POST_STATUS=publish is risky. Use draft until controlled tests pass."]
    return []


def validate_source_post(raw_payload: Mapping[str, Any], normalized_post: Mapping[str, Any]) -> list[str]:
    warnings: list[str] = []
    if not normalized_post.get("wix_id") and not normalized_post.get("old_url"):
        warnings.append("Missing wix_id and old_url")
    if not clean_text(normalized_post.get("title")):
        warnings.append("Missing title")
    content = clean_text(raw_payload.get("content_clean")) or clean_text(raw_payload.get("content")) or clean_text(raw_payload.get("page_content"))
    if not visible_text(content):
        warnings.append("Empty or suspicious content")
    if not normalized_post.get("desired_slug"):
        warnings.append("Missing desired slug")
    if not normalized_post.get("wp_category_id"):
        warnings.append("Missing WordPress category ID")
    return warnings


def require_file(file_path: Path) -> Path:
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    return file_path


def positive_int(value: int | None, default: int) -> int:
    if value is None:
        return default
    if value <= 0:
        raise ValueError("Value must be positive")
    return value
