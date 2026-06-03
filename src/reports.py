"""CSV and JSON report writers."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .config import Settings
from .database import MigrationDB
from .seo_auditor import report_orphan_media, scan_local_references
from .url_manager import build_new_url, classify_url_status, redirect_reason


def write_csv(file_path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str] | None = None) -> int:
    rows = list(rows)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = sorted({key for row in rows for key in row.keys()})
    with file_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


def export_reports(settings: Settings, db: MigrationDB) -> dict[str, int]:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    counts = {
        "posts_report.csv": write_csv(settings.output_dir / "posts_report.csv", db.query("SELECT * FROM posts_migration ORDER BY id ASC")),
        "images_report.csv": write_csv(settings.output_dir / "images_report.csv", db.query("SELECT * FROM images_migration ORDER BY id ASC")),
        "errors_report.csv": write_csv(settings.output_dir / "errors_report.csv", db.query("SELECT * FROM errors ORDER BY id ASC")),
        "orphan_media_report.csv": write_csv(settings.output_dir / "orphan_media_report.csv", report_orphan_media(db)),
    }
    url_counts = export_url_reports(settings, db)
    counts.update(url_counts)
    return counts


def export_url_reports(settings: Settings, db: MigrationDB) -> dict[str, int]:
    posts = db.query("SELECT * FROM posts_migration ORDER BY id ASC")
    url_rows: list[dict[str, Any]] = []
    redirect_rows: list[dict[str, Any]] = []

    for post in posts:
        old_url = post.get("old_url")
        new_url = post.get("new_url") or _expected_new_url(settings, post)
        status = post.get("url_status") or classify_url_status(old_url, new_url)
        old_path = urlparse(old_url or "").path
        new_path = urlparse(new_url or "").path
        url_row = {
            "wix_id": post.get("wix_id"),
            "wp_post_id": post.get("wp_post_id"),
            "old_url": old_url,
            "new_url": new_url,
            "old_path": old_path,
            "new_path": new_path,
            "desired_slug": post.get("desired_slug"),
            "wp_slug_final": post.get("wp_slug_final"),
            "url_status": status,
        }
        url_rows.append(url_row)
        if status not in {"exact_match", "trailing_slash_only"}:
            redirect_rows.append(
                {
                    "old_path": old_path,
                    "new_path": new_path,
                    "old_url": old_url,
                    "new_url": new_url,
                    "reason": redirect_reason(status),
                    "wix_id": post.get("wix_id"),
                    "wp_post_id": post.get("wp_post_id"),
                }
            )

    return {
        "url_report.csv": write_csv(settings.output_dir / "url_report.csv", url_rows),
        "redirect_candidates.csv": write_csv(settings.output_dir / "redirect_candidates.csv", redirect_rows),
    }


def export_rows(settings: Settings, filename: str, rows: Iterable[dict[str, Any]], fieldnames: list[str] | None = None) -> int:
    return write_csv(settings.output_dir / filename, rows, fieldnames=fieldnames)


def export_migration_manifest(settings: Settings, db: MigrationDB) -> dict[str, Any]:
    posts_by_status = _counts(db, "posts_migration", "status")
    images_by_status = _counts(db, "images_migration", "status")
    urls_by_status = _url_status_counts(settings, db)
    local_references = scan_local_references(settings, db)
    manifest = {
        "posts": {
            "total": db.count("posts_migration"),
            "by_status": posts_by_status,
            "migrated": posts_by_status.get("created", 0),
            "failed": posts_by_status.get("failed", 0),
        },
        "images": {
            "total": db.count("images_migration"),
            "by_status": images_by_status,
            "unique_uploaded": images_by_status.get("uploaded", 0),
            "reused": images_by_status.get("reused_by_url", 0) + images_by_status.get("reused_by_hash", 0),
            "local_images_size_bytes": _folder_size(settings.images_dir),
        },
        "urls": {
            "by_status": urls_by_status,
            "exact": urls_by_status.get("exact_match", 0),
            "trailing_slash_only": urls_by_status.get("trailing_slash_only", 0),
            "changed": sum(count for status, count in urls_by_status.items() if status not in {"exact_match", "trailing_slash_only", ""}),
        },
        "local_references_found": len(local_references),
        "hostinger_checklist": [
            "Export WordPress MySQL database from local.",
            "Copy wp-content/uploads, active theme child and required plugins.",
            "Move the migration support plugin if migrated meta must remain editable through REST.",
            "Run serialized-safe search-replace from local URL to production URL.",
            "Regenerate permalinks and verify /post/%postname%/.",
            "Review URL and redirect candidate reports before DNS cutover.",
        ],
    }
    output_path = settings.output_dir / "migration_manifest.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=True), encoding="utf-8")
    return manifest


def _counts(db: MigrationDB, table: str, column: str) -> dict[str, int]:
    rows = db.query(f"SELECT COALESCE({column}, '') AS value, COUNT(*) AS total FROM {table} GROUP BY value")
    return {str(row["value"]): int(row["total"]) for row in rows}


def _folder_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for file_path in path.rglob("*"):
        if file_path.is_file():
            if file_path.name == ".gitkeep":
                continue
            total += file_path.stat().st_size
    return total


def _expected_new_url(settings: Settings, post: dict[str, Any]) -> str | None:
    slug = post.get("wp_slug_final") or post.get("desired_slug")
    if not slug:
        return None
    return build_new_url(settings, str(slug))


def _url_status_counts(settings: Settings, db: MigrationDB) -> dict[str, int]:
    counts: dict[str, int] = {}
    posts = db.query("SELECT old_url, new_url, desired_slug, wp_slug_final, url_status FROM posts_migration")
    for post in posts:
        status = post.get("url_status") or classify_url_status(post.get("old_url"), post.get("new_url") or _expected_new_url(settings, post))
        counts[status] = counts.get(status, 0) + 1
    return counts
