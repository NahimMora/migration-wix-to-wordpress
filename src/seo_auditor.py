"""SEO-oriented reports and post-import verification helpers."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .csv_loader import read_csv_rows
from .database import MigrationDB
from .html_cleaner import extract_internal_links, visible_text
from .normalizer import clean_text, normalize_wix_row
from .url_manager import build_new_url
from .wordpress_client import WordPressClient, WordPressError


LOCAL_REFERENCE_PATTERNS = ("localhost", ".local", "127.0.0.1", "C:\\", "c:\\")


def analyze_internal_links(file_path: Path, settings: Settings) -> list[dict[str, Any]]:
    rows, _warnings = read_csv_rows(file_path)
    report_rows: list[dict[str, Any]] = []
    production_host = urlparse(settings.production_site_url).netloc
    for row in rows:
        html = clean_text(row.get("content")) or clean_text(row.get("page_content"))
        for link in extract_internal_links(html, settings.production_site_url):
            parsed = urlparse(link)
            if parsed.netloc and parsed.netloc != production_host:
                continue
            suggested = settings.production_site_url.rstrip("/") + (parsed.path or "/")
            report_rows.append(
                {
                    "wix_id": clean_text(row.get("wix_id")),
                    "old_internal_link": link,
                    "suggested_new_link": suggested,
                    "status": "candidate_review",
                }
            )
    return report_rows


def find_duplicate_posts(file_path: Path) -> list[dict[str, Any]]:
    rows, _warnings = read_csv_rows(file_path)
    normalized = [normalize_wix_row(row) for row in rows]
    checks = {
        "wix_id": Counter(item.get("wix_id") for item in normalized if item.get("wix_id")),
        "old_url": Counter(item.get("old_url") for item in normalized if item.get("old_url")),
        "desired_slug": Counter(item.get("desired_slug") for item in normalized if item.get("desired_slug")),
    }
    title_date_counter: Counter[tuple[str, str | None]] = Counter(
        (item.get("title") or "", item.get("source_date")) for item in normalized if item.get("title")
    )

    report_rows: list[dict[str, Any]] = []
    for index, item in enumerate(normalized, start=2):
        duplicate_reasons = [
            field
            for field, counter in checks.items()
            if item.get(field) and counter[item.get(field)] > 1
        ]
        title_date = (item.get("title") or "", item.get("source_date"))
        if title_date_counter[title_date] > 1:
            duplicate_reasons.append("title+date")
        if duplicate_reasons:
            report_rows.append(
                {
                    "line": index,
                    "wix_id": item.get("wix_id"),
                    "old_url": item.get("old_url"),
                    "desired_slug": item.get("desired_slug"),
                    "title": item.get("title"),
                    "source_date": item.get("source_date"),
                    "duplicate_by": ",".join(duplicate_reasons),
                }
            )
    return report_rows


def scan_local_references(settings: Settings, db: MigrationDB) -> list[dict[str, Any]]:
    report_rows: list[dict[str, Any]] = []
    posts = db.query("SELECT id, wix_id, old_url, raw_payload FROM posts_migration ORDER BY id ASC")
    for post in posts:
        payload = post.get("raw_payload") or ""
        for pattern in LOCAL_REFERENCE_PATTERNS:
            if pattern in payload:
                report_rows.append(
                    {
                        "entity_type": "post",
                        "entity_id": post["id"],
                        "wix_id": post.get("wix_id"),
                        "old_url": post.get("old_url"),
                        "pattern": pattern,
                        "status": "review_required",
                    }
                )

    for output_file in settings.output_dir.glob("*"):
        if not output_file.is_file():
            continue
        try:
            content = output_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in LOCAL_REFERENCE_PATTERNS:
            if pattern in content:
                report_rows.append(
                    {
                        "entity_type": "report",
                        "entity_id": str(output_file),
                        "wix_id": "",
                        "old_url": "",
                        "pattern": pattern,
                        "status": "review_required",
                    }
                )
    return report_rows


def report_orphan_media(db: MigrationDB) -> list[dict[str, Any]]:
    used_media_ids = {
        int(row["featured_media_id"])
        for row in db.query("SELECT featured_media_id FROM posts_migration WHERE featured_media_id IS NOT NULL")
    }
    images = db.query("SELECT * FROM images_migration WHERE wp_media_id IS NOT NULL ORDER BY id ASC")
    report_rows = []
    for image in images:
        media_id = int(image["wp_media_id"])
        if media_id not in used_media_ids:
            report_rows.append(
                {
                    "source_image_url": image.get("source_image_url"),
                    "wp_media_id": media_id,
                    "wp_media_url": image.get("wp_media_url"),
                    "status": image.get("status"),
                    "reason": "uploaded media is not referenced by migrated posts",
                }
            )
    return report_rows


def verify_import_sample(settings: Settings, db: MigrationDB, client: WordPressClient, limit: int) -> list[dict[str, Any]]:
    posts = db.query(
        """
        SELECT * FROM posts_migration
        WHERE status = 'created' AND wp_post_id IS NOT NULL
        ORDER BY id ASC
        LIMIT ?
        """,
        (limit,),
    )
    report_rows: list[dict[str, Any]] = []
    for post in posts:
        checks = defaultdict(lambda: "not_checked")
        try:
            wp_post = client.get_post(int(post["wp_post_id"]))
            checks["http"] = "ok"
            rendered_title = _nested(wp_post, "title", "rendered") or _nested(wp_post, "title", "raw")
            rendered_content = _nested(wp_post, "content", "rendered") or _nested(wp_post, "content", "raw")
            checks["title_present"] = "ok" if visible_text(rendered_title) else "failed"
            checks["content_present"] = "ok" if visible_text(rendered_content) else "failed"
            checks["featured_media"] = "ok" if not post.get("featured_image_url") or wp_post.get("featured_media") else "failed"
            checks["meta_wix_id"] = "ok" if _nested(wp_post, "meta", "_wix_id") else "failed"
            checks["meta_old_url"] = "ok" if _nested(wp_post, "meta", "_wix_old_url") else "failed"
        except WordPressError as exc:
            checks["http"] = f"failed:{exc.status_code}"

        report_rows.append(
            {
                "wix_id": post.get("wix_id"),
                "old_url": post.get("old_url"),
                "wp_post_id": post.get("wp_post_id"),
                "new_url": post.get("new_url") or build_new_url(settings, post.get("wp_slug_final") or post.get("desired_slug") or ""),
                "url_status": post.get("url_status"),
                "checks": json.dumps(dict(checks), ensure_ascii=True),
            }
        )
    return report_rows


def _nested(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current
