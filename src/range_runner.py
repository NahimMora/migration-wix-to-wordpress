"""Range preflight and resumable batch runner for Wix export files."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .author_mapper import load_author_map, resolve_author, verify_authors
from .category_mapper import load_category_map, resolve_category_detail
from .config import Settings
from .csv_loader import analyze_csv, import_csv, read_csv_rows
from .database import MigrationDB
from .image_manager import analyze_images
from .post_manager import PostMigrationManager
from .reports import export_reports, write_csv
from .url_manager import (
    PRE_MIGRATION_URL_RISK_FIELDS,
    analyze_urls,
    build_new_url,
    classify_url_status,
    pre_migration_url_risk_rows,
    redirect_reason,
)
from .wix_csv_normalizer import check_wix_csv_encoding, normalize_wix_csv


@dataclass(frozen=True)
class RangeSpec:
    input_dir: Path
    pattern: str
    start: int
    end: int
    run_id: str
    force: bool = False
    sample_files: int | None = None


def preflight_range(settings: Settings, db: MigrationDB, spec: RangeSpec) -> dict[str, Any]:
    files = expected_files(spec)
    found = [item for item in files if item["path"].exists()]
    missing = [item for item in files if not item["path"].exists()]
    sample = found[: spec.sample_files or len(found)]

    summaries: list[dict[str, Any]] = []
    total_rows = 0
    total_image_urls = 0
    total_invalid_urls = 0
    total_mojibake_patterns = 0
    for item in sample:
        input_file = item["path"]
        encoding = check_wix_csv_encoding(input_file)
        normalized_file = normalized_path(settings, item["index"])
        normalize_result = normalize_wix_csv(
            input_file,
            normalized_file,
            settings.output_dir / "normalize_wix_csv_report.csv",
            settings.output_dir / "encoding_report.csv",
            settings,
            settings.output_dir / "content_format_report.csv",
        )
        csv_result = analyze_csv(normalized_file, db)
        url_result = analyze_urls(normalized_file, settings)
        image_result = analyze_images(normalized_file)
        url_rows = pre_migration_url_risk_rows(normalized_file, settings)
        summaries.append(
            {
                "file": str(input_file),
                "encoding": encoding,
                "normalized": normalize_result,
                "csv": csv_result,
                "urls": url_result,
                "images": image_result,
                "redirect_candidates": sum(1 for row in url_rows if row["needs_redirect"]),
            }
        )
        total_rows += int(csv_result.get("total_rows", 0))
        total_image_urls += int(image_result.get("image_urls", 0))
        total_invalid_urls += int(url_result.get("invalid_urls", 0))
        total_mojibake_patterns += int(csv_result.get("mojibake_pattern_count", 0))

    category_map = load_category_map(settings.input_dir / "category_map.csv", db)
    author_check = verify_authors(settings)
    dangerous_config = dangerous_configuration(settings)
    free_space = shutil.disk_usage(settings.root_dir).free
    blockers = []
    if missing:
        blockers.append("missing_files")
    if total_mojibake_patterns:
        blockers.append("possible_mojibake_detected")
    if settings.stop_on_critical_url_errors and total_invalid_urls:
        blockers.append("invalid_urls")
    if settings.stop_on_unmapped_author and not author_check.get("ok"):
        blockers.append("author_mapping")

    return {
        "ok": not blockers,
        "run_id": spec.run_id,
        "mode": "preflight_only",
        "files_expected": len(files),
        "files_found": len(found),
        "files_missing": len(missing),
        "missing_files": [str(item["path"]) for item in missing],
        "sample_files_checked": len(sample),
        "estimated_total_posts": estimate_total_posts(total_rows, len(sample), len(found)),
        "sample_rows": total_rows,
        "sample_image_urls": total_image_urls,
        "sample_invalid_urls": total_invalid_urls,
        "category_map": category_map,
        "author_check": author_check,
        "free_disk_bytes": free_space,
        "dangerous_config": dangerous_config,
        "blockers": blockers,
        "summaries": summaries,
    }


def migrate_range(settings: Settings, db: MigrationDB, logger: Any, spec: RangeSpec) -> dict[str, Any]:
    _print_run_preflight_summary(settings, spec)
    db.upsert_run(spec.run_id, str(spec.input_dir), spec.pattern, spec.start, spec.end, status="running")
    manager = PostMigrationManager(settings, db, logger)
    run_dir = run_output_dir(settings, spec.run_id)
    (run_dir / "per_file").mkdir(parents=True, exist_ok=True)
    files = expected_files(spec)
    processed: list[dict[str, Any]] = []

    for item in files:
        index = item["index"]
        input_file = item["path"]
        existing = db.query_one(
            "SELECT * FROM migration_run_files WHERE run_id = ? AND file_index = ?",
            (spec.run_id, index),
        )
        if existing and existing.get("status") == "migrated" and not spec.force:
            processed.append({"file": str(input_file), "status": "skipped"})
            continue
        if not input_file.exists():
            db.upsert_run_file(spec.run_id, index, str(input_file), status="failed", error_message="missing_file")
            processed.append({"file": str(input_file), "status": "failed", "error": "missing_file"})
            continue

        try:
            result = process_range_file(settings, db, logger, manager, spec, input_file, index)
            processed.append(result)
        except Exception as exc:
            db.upsert_run_file(spec.run_id, index, str(input_file), status="failed", error_message=str(exc))
            db.update_run(spec.run_id, status="failed", data={"last_error": str(exc), "last_file": str(input_file)})
            raise

    db.update_run(spec.run_id, status="completed", finished_at=_now(), data={"processed": len(processed)})
    summary = export_run_report(settings, db, spec.run_id)
    return {"run_id": spec.run_id, "processed_files": processed, "summary": summary}


def process_range_file(
    settings: Settings,
    db: MigrationDB,
    logger: Any,
    manager: PostMigrationManager,
    spec: RangeSpec,
    input_file: Path,
    index: int,
) -> dict[str, Any]:
    file_hash = sha256_file(input_file)
    normalized_file = normalized_path(settings, index)
    per_file_dir = run_output_dir(settings, spec.run_id) / "per_file" / f"{index:04d}"
    per_file_dir.mkdir(parents=True, exist_ok=True)
    db.upsert_run_file(spec.run_id, index, str(input_file), file_hash=file_hash, status="pending", started_at=_now())

    normalize_result = normalize_wix_csv(
        input_file,
        normalized_file,
        per_file_dir / "normalize_wix_csv_report.csv",
        per_file_dir / "encoding_report.csv",
        settings,
        per_file_dir / "content_format_report.csv",
    )
    db.update_run_file(spec.run_id, index, status="normalized", rows_total=int(normalize_result.get("total_rows", 0)))
    csv_result = analyze_csv(normalized_file, db)
    url_result = analyze_urls(normalized_file, settings)
    image_result = analyze_images(normalized_file)
    url_rows = pre_migration_url_risk_rows(normalized_file, settings)
    write_csv(per_file_dir / "pre_migration_url_risk_report.csv", url_rows, PRE_MIGRATION_URL_RISK_FIELDS)
    write_json(per_file_dir / "analysis.json", {"csv": csv_result, "urls": url_result, "images": image_result})
    db.update_run_file(spec.run_id, index, status="analyzed", redirect_candidates=sum(1 for row in url_rows if row["needs_redirect"]))
    enforce_pre_migrate_thresholds(settings, db, normalized_file, csv_result, url_result)

    import_result = import_csv(normalized_file, settings, db, run_id=spec.run_id, source_file_index=index)
    db.update_run_file(spec.run_id, index, status="imported", rows_imported=int(import_result.get("imported", 0)))
    manager.dry_run(limit=int(import_result.get("imported", 0)) or None, source_file=str(normalized_file))
    summary = manager.migrate(source_file=str(normalized_file), run_id=spec.run_id)
    enforce_post_failure_rate(settings, summary.processed, summary.failed)
    image_counts = image_status_counts(db, spec.run_id)
    warn_on_image_failure_rate(settings, logger, image_counts)
    db.update_run_file(
        spec.run_id,
        index,
        status="migrated",
        rows_created=summary.created,
        rows_failed=summary.failed,
        images_uploaded=image_counts.get("uploaded", 0),
        images_reused=image_counts.get("reused_by_url", 0) + image_counts.get("reused_by_hash", 0),
        finished_at=_now(),
    )
    export_reports(settings, db)
    return {"file": str(input_file), "status": "migrated", "created": summary.created, "failed": summary.failed}


def resume_run(settings: Settings, db: MigrationDB, logger: Any, run_id: str) -> dict[str, Any]:
    run = db.get_run(run_id)
    if not run:
        raise ValueError(f"Run not found: {run_id}")
    spec = RangeSpec(Path(str(run["input_dir"])), str(run["pattern"]), int(run["start_index"]), int(run["end_index"]), run_id)
    return migrate_range(settings, db, logger, spec)


def run_status(db: MigrationDB, run_id: str) -> dict[str, Any]:
    run = db.get_run(run_id)
    files = db.list_run_files(run_id)
    return {"run": run, "files": files}


def export_run_report(settings: Settings, db: MigrationDB, run_id: str) -> dict[str, Any]:
    run_dir = run_output_dir(settings, run_id)
    global_dir = run_dir / "global"
    global_dir.mkdir(parents=True, exist_ok=True)
    posts = run_posts(db, run_id)
    images = run_images(db, run_id)
    report_counts = {
        "posts_report.csv": write_csv(global_dir / "posts_report.csv", posts),
        "images_report.csv": write_csv(global_dir / "images_report.csv", images),
        "errors_report.csv": write_csv(global_dir / "errors_report.csv", run_errors(db, run_id)),
        "url_report.csv": 0,
        "redirect_candidates.csv": 0,
        "authors_report.csv": write_csv(
            global_dir / "authors_report.csv",
            [
                {
                    "id": post.get("id"),
                    "wix_id": post.get("wix_id"),
                    "author_source_id": post.get("author_source_id"),
                    "author_source_slug": post.get("author_source_slug"),
                    "wp_author_id": post.get("wp_author_id"),
                    "source_file": post.get("source_file"),
                    "run_id": post.get("run_id"),
                }
                for post in posts
            ],
        ),
        "categories_report.csv": write_csv(
            global_dir / "categories_report.csv",
            [
                {
                    "id": post.get("id"),
                    "wix_id": post.get("wix_id"),
                    "category_source": post.get("category_source"),
                    "wp_category_id": post.get("wp_category_id"),
                    "source_file": post.get("source_file"),
                    "run_id": post.get("run_id"),
                }
                for post in posts
            ],
        ),
        "content_format_report.csv": copy_if_exists(settings.output_dir / "content_format_report.csv", global_dir / "content_format_report.csv"),
    }
    url_rows, redirect_rows = run_url_reports(settings, posts)
    report_counts["url_report.csv"] = write_csv(global_dir / "url_report.csv", url_rows)
    report_counts["redirect_candidates.csv"] = write_csv(global_dir / "redirect_candidates.csv", redirect_rows)
    files = db.list_run_files(run_id)
    summary = run_summary(db, run_id, files)
    (global_dir / "run_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=True), encoding="utf-8")
    return {"run_id": run_id, "global_dir": str(global_dir), "reports": report_counts, "summary": summary}


def enforce_pre_migrate_thresholds(settings: Settings, db: MigrationDB, normalized_file: Path, csv_result: dict[str, Any], url_result: dict[str, Any]) -> None:
    if csv_result.get("critical_warnings"):
        raise RuntimeError("Critical CSV warnings block migration: " + ", ".join(csv_result["critical_warnings"]))
    if settings.stop_on_critical_url_errors and int(url_result.get("invalid_urls", 0)):
        raise RuntimeError("Critical URL errors block migration for this file")
    if settings.stop_on_unmapped_category:
        load_category_map(settings.input_dir / "category_map.csv", db)
        rows, _warnings = read_csv_rows(normalized_file)
        unmapped = [
            row
            for row in rows
            if resolve_category_detail(db, row.get("category"), settings.default_category_id)["matched_by"] == "default"
        ]
        if unmapped:
            raise RuntimeError(f"Unmapped categories block migration: {len(unmapped)} rows")
    if settings.stop_on_unmapped_author:
        author_rows, _warnings = load_author_map(settings.input_dir / "author_map.csv")
        rows, _csv_warnings = read_csv_rows(normalized_file)
        unmapped_authors = [
            row
            for row in rows
            if resolve_author(author_rows, row.get("author_source_id"), row.get("author_source_slug"), settings)["matched_by"] == "default"
        ]
        if unmapped_authors:
            raise RuntimeError(f"Unmapped authors block migration: {len(unmapped_authors)} rows")


def enforce_post_failure_rate(settings: Settings, processed: int, failed: int) -> None:
    if processed <= 0:
        return
    failure_rate = failed / processed
    if failure_rate > settings.max_post_failure_rate:
        raise RuntimeError(f"Post failure rate {failure_rate:.2%} exceeds MAX_POST_FAILURE_RATE={settings.max_post_failure_rate:.2%}")


def warn_on_image_failure_rate(settings: Settings, logger: Any, image_counts: dict[str, int]) -> None:
    total = sum(image_counts.values())
    if total <= 0:
        return
    failed = sum(count for status, count in image_counts.items() if status.startswith("failed") or status in {"invalid_image"})
    failure_rate = failed / total
    if failure_rate > settings.max_image_failure_rate:
        logger.warning(
            "Image failure rate %.2f%% exceeds MAX_IMAGE_FAILURE_RATE=%.2f%%",
            failure_rate * 100,
            settings.max_image_failure_rate * 100,
        )


def expected_files(spec: RangeSpec) -> list[dict[str, Any]]:
    return [
        {"index": index, "path": spec.input_dir / spec.pattern.format(num=index)}
        for index in range(spec.start, spec.end + 1)
    ]


def normalized_path(settings: Settings, index: int) -> Path:
    return settings.normalized_dir / f"wix_posts_normalized_{index:04d}.csv"


def run_output_dir(settings: Settings, run_id: str) -> Path:
    return settings.output_dir / "runs" / run_id


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def estimate_total_posts(sample_rows: int, sample_files: int, found_files: int) -> int:
    if sample_files <= 0:
        return 0
    return round((sample_rows / sample_files) * found_files)


def dangerous_configuration(settings: Settings) -> dict[str, Any]:
    return {
        "default_post_status": settings.default_post_status,
        "allow_wordpress_writes": settings.allow_wordpress_writes,
        "confirm_publish_mode": settings.confirm_publish_mode,
        "publish_without_confirm": settings.default_post_status == "publish" and not settings.confirm_publish_mode,
    }


def image_status_counts(db: MigrationDB, run_id: str | None = None) -> dict[str, int]:
    if run_id:
        rows = db.query(
            """
            SELECT im.status, COUNT(*) AS total
            FROM images_migration im
            WHERE EXISTS (
                SELECT 1
                FROM posts_migration pm
                WHERE pm.run_id = ?
                  AND pm.featured_image_url = im.source_image_url
            )
            GROUP BY im.status
            """,
            (run_id,),
        )
    else:
        rows = db.query("SELECT status, COUNT(*) AS total FROM images_migration GROUP BY status")
    return {row["status"]: int(row["total"]) for row in rows}


def run_summary(db: MigrationDB, run_id: str, files: list[dict[str, Any]]) -> dict[str, Any]:
    run = db.get_run(run_id) or {}
    expected = 0
    if run.get("start_index") is not None and run.get("end_index") is not None:
        expected = max(0, int(run["end_index"]) - int(run["start_index"]) + 1)
    post_counts = {
        row["status"]: int(row["total"])
        for row in db.query("SELECT status, COUNT(*) AS total FROM posts_migration WHERE run_id = ? GROUP BY status", (run_id,))
    }
    image_counts = image_status_counts(db, run_id)
    return {
        "run_id": run_id,
        "files_expected": expected or len(files),
        "files_found": sum(1 for item in files if item.get("status") != "failed" or item.get("error_message") != "missing_file"),
        "files_missing": sum(1 for item in files if item.get("error_message") == "missing_file"),
        "posts_totals_by_status": post_counts,
        "posts_created": post_counts.get("created", 0),
        "posts_skipped": post_counts.get("skipped_existing", 0),
        "posts_failed": post_counts.get("failed", 0),
        "images_uploaded": image_counts.get("uploaded", 0),
        "images_reused": image_counts.get("reused_by_url", 0) + image_counts.get("reused_by_hash", 0),
        "images_failed": sum(count for status, count in image_counts.items() if status.startswith("failed")),
        "redirects_needed": sum(int(item.get("redirect_candidates") or 0) for item in files),
        "categories_default_used": run_error_count(db, run_id, "category_mapping"),
        "authors_default_used": run_error_count(db, run_id, "author_mapping"),
        "duration_seconds": _duration_seconds(run.get("started_at"), run.get("finished_at")),
        "last_file_processed": files[-1]["file_name"] if files else "",
        "final_status": run.get("status"),
    }


def run_posts(db: MigrationDB, run_id: str) -> list[dict[str, Any]]:
    return db.query("SELECT * FROM posts_migration WHERE run_id = ? ORDER BY id ASC", (run_id,))


def run_images(db: MigrationDB, run_id: str) -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT im.*
        FROM images_migration im
        WHERE EXISTS (
            SELECT 1
            FROM posts_migration pm
            WHERE pm.run_id = ?
              AND pm.featured_image_url = im.source_image_url
        )
        ORDER BY im.id ASC
        """,
        (run_id,),
    )


def run_errors(db: MigrationDB, run_id: str) -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT e.*
        FROM errors e
        WHERE EXISTS (
            SELECT 1
            FROM posts_migration pm
            WHERE pm.run_id = ?
              AND e.entity_type = 'post'
              AND e.entity_id = CAST(pm.id AS TEXT)
        )
        ORDER BY e.id ASC
        """,
        (run_id,),
    )


def run_error_count(db: MigrationDB, run_id: str, stage: str) -> int:
    row = db.query_one(
        """
        SELECT COUNT(*) AS total
        FROM errors e
        WHERE e.stage = ?
          AND EXISTS (
              SELECT 1
              FROM posts_migration pm
              WHERE pm.run_id = ?
                AND e.entity_type = 'post'
                AND e.entity_id = CAST(pm.id AS TEXT)
          )
        """,
        (stage, run_id),
    )
    return int(row["total"]) if row else 0


def run_url_reports(settings: Settings, posts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    url_rows: list[dict[str, Any]] = []
    redirect_rows: list[dict[str, Any]] = []
    for post in posts:
        old_url = post.get("old_url")
        new_url = post.get("new_url") or _expected_new_url(settings, post)
        status = post.get("url_status") or classify_url_status(old_url, new_url)
        old_path = urlparse(old_url or "").path
        new_path = urlparse(new_url or "").path
        url_rows.append(
            {
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
        )
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
    return url_rows, redirect_rows


def _expected_new_url(settings: Settings, post: dict[str, Any]) -> str | None:
    slug = post.get("wp_slug_final") or post.get("desired_slug")
    if not slug:
        return None
    return build_new_url(settings, str(slug))


def copy_if_exists(source: Path, target: Path) -> int:
    if not source.exists():
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return 1


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True, default=str), encoding="utf-8")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _duration_seconds(started_at: str | None, finished_at: str | None) -> float | None:
    if not started_at or not finished_at:
        return None
    try:
        from datetime import datetime
        fmt = "%Y-%m-%d %H:%M:%S"
        t0 = datetime.strptime(started_at[:19], fmt)
        t1 = datetime.strptime(finished_at[:19], fmt)
        return round((t1 - t0).total_seconds(), 1)
    except (ValueError, TypeError):
        return None


def _print_run_preflight_summary(settings: Settings, spec: RangeSpec) -> None:
    files = expected_files(spec)
    found = sum(1 for item in files if item["path"].exists())
    lines = [
        "",
        "=" * 60,
        "  MIGRATE-RANGE PRE-FLIGHT SUMMARY",
        "=" * 60,
        f"  Run ID             : {spec.run_id}",
        f"  Write mode         : {'ENABLED' if settings.allow_wordpress_writes else 'DISABLED'}",
        f"  Post status        : {settings.default_post_status}",
        f"  Confirm publish    : {settings.confirm_publish_mode}",
        f"  Files expected     : {len(files)}",
        f"  Files found        : {found}",
        f"  Files missing      : {len(files) - found}",
        f"  Input dir          : {spec.input_dir}",
        f"  Pattern            : {spec.pattern}",
        f"  Range              : {spec.start} → {spec.end}",
        f"  Force reprocess    : {spec.force}",
        "=" * 60,
        "",
    ]
    print("\n".join(lines))
