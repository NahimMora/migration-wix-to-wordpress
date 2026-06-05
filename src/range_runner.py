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
    rows_imported = int(import_result.get("imported", 0))
    db.update_run_file(spec.run_id, index, status="imported", rows_imported=rows_imported)
    manager.dry_run(limit=rows_imported or None, source_file=str(normalized_file))

    total_created = total_failed = total_skipped = total_processed = 0
    while True:
        remaining = _count_posts_for_file(db, settings, spec.run_id, index, ["dry_run_valid", "pending", "retry_pending"])
        if remaining == 0:
            break
        batch_summary = manager.migrate(source_file=str(normalized_file), run_id=spec.run_id)
        total_created += batch_summary.created
        total_failed += batch_summary.failed
        total_skipped += batch_summary.skipped
        total_processed += batch_summary.processed
        enforce_post_failure_rate(settings, batch_summary.processed, batch_summary.failed)
        if batch_summary.processed == 0:
            break  # No progress made — avoid infinite loop on unexpected stall

    remaining_after = _count_posts_for_file(db, settings, spec.run_id, index, ["dry_run_valid", "pending", "retry_pending"])
    file_status = "migrated" if remaining_after == 0 else "partial"
    image_counts = image_status_counts(db, spec.run_id)
    warn_on_image_failure_rate(settings, logger, image_counts)
    db.update_run_file(
        spec.run_id,
        index,
        status=file_status,
        rows_created=total_created,
        rows_failed=total_failed,
        images_uploaded=image_counts.get("uploaded", 0),
        images_reused=image_counts.get("reused_by_url", 0) + image_counts.get("reused_by_hash", 0),
        finished_at=_now(),
    )
    export_reports(settings, db)
    return {"file": str(input_file), "status": file_status, "created": total_created, "failed": total_failed}


def resume_run(settings: Settings, db: MigrationDB, logger: Any, run_id: str) -> dict[str, Any]:
    run = db.get_run(run_id)
    if not run:
        raise ValueError(f"Run not found: {run_id}")

    status = str(run.get("status") or "")
    recovery_note: str | None = None
    if status == "failed":
        files = db.list_run_files(run_id)
        partial_files = [f for f in files if f.get("status") == "partial"]
        recovery_note = (
            f"Run was in 'failed' state. Auto-recovering: {len(partial_files)} partial file(s) detected. "
            "Status reset to 'running'. Resume proceeding."
        )
        db.update_run(run_id, status="running")

    spec = RangeSpec(Path(str(run["input_dir"])), str(run["pattern"]), int(run["start_index"]), int(run["end_index"]), run_id)
    result = migrate_range(settings, db, logger, spec)
    if recovery_note:
        result["recovery_note"] = recovery_note
    return result


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


INTEGRITY_REPORT_FIELDS = [
    "run_id",
    "file_index",
    "file_name",
    "file_status",
    "rows_imported",
    "rows_created_record",
    "rows_created_real",
    "rows_pending_real",
    "rows_failed_real",
    "rows_total_real",
    "images_uploaded_record",
    "images_uploaded_real",
    "images_reused_record",
    "images_reused_real",
    "issue_types",
    "has_issues",
]

_CREATED_STATUSES = {"created", "skipped_existing"}
_PENDING_STATUSES = {"dry_run_valid", "pending", "retry_pending"}
_FAILED_STATUSES = {"failed", "image_uploaded_post_failed"}


def check_run_integrity(settings: Settings, db: MigrationDB, run_id: str, fast: bool = False) -> dict[str, Any]:
    """Audit a run for files incorrectly marked migrated, pending posts, and count mismatches.

    fast=True: single GROUP BY query instead of per-file COUNT queries; skips image subqueries.
    fast=False: full per-file counts including image correlation (can be slow on large datasets).
    """
    run_dir = run_output_dir(settings, run_id)
    global_dir = run_dir / "global"
    global_dir.mkdir(parents=True, exist_ok=True)
    files = db.list_run_files(run_id)
    issues: list[dict[str, Any]] = []

    # Fast mode: resolve all post counts in one GROUP BY query indexed by file_index.
    fast_counts: dict[int, dict[str, int]] = {}
    if fast:
        count_rows = db.query(
            """
            SELECT source_file_index, status, COUNT(*) AS cnt
            FROM posts_migration
            WHERE run_id = ? AND source_file_index IS NOT NULL
            GROUP BY source_file_index, status
            """,
            (run_id,),
        )
        for cr in count_rows:
            fi = int(cr["source_file_index"])
            if fi not in fast_counts:
                fast_counts[fi] = {}
            fast_counts[fi][str(cr["status"])] = int(cr["cnt"])

    for file_rec in files:
        file_index = int(file_rec["file_index"])
        file_name = str(file_rec.get("file_name") or "")
        file_status = str(file_rec.get("status") or "")
        rows_imported = int(file_rec.get("rows_imported") or 0)
        rows_created_record = int(file_rec.get("rows_created") or 0)
        images_uploaded_record = int(file_rec.get("images_uploaded") or 0)
        images_reused_record = int(file_rec.get("images_reused") or 0)

        if fast:
            fc = fast_counts.get(file_index, {})
            real_created = sum(fc.get(s, 0) for s in _CREATED_STATUSES)
            real_pending = sum(fc.get(s, 0) for s in _PENDING_STATUSES)
            real_failed = sum(fc.get(s, 0) for s in _FAILED_STATUSES)
            images_uploaded_real = images_uploaded_record  # not checked in fast mode
            images_reused_real = images_reused_record
        else:
            real_created = _count_posts_for_file(db, settings, run_id, file_index, list(_CREATED_STATUSES))
            real_pending = _count_posts_for_file(db, settings, run_id, file_index, list(_PENDING_STATUSES))
            real_failed = _count_posts_for_file(db, settings, run_id, file_index, list(_FAILED_STATUSES))
            source_file_str = str(normalized_path(settings, file_index))
            img_uploaded_row = db.query_one(
                """
                SELECT COUNT(*) AS total FROM images_migration im
                WHERE im.status = 'uploaded'
                  AND EXISTS (
                      SELECT 1 FROM posts_migration pm
                      WHERE (pm.source_file = ? OR (pm.run_id = ? AND pm.source_file_index = ?))
                        AND pm.featured_image_url = im.source_image_url
                  )
                """,
                (source_file_str, run_id, file_index),
            )
            img_reused_row = db.query_one(
                """
                SELECT COUNT(*) AS total FROM images_migration im
                WHERE im.status IN ('reused_by_url', 'reused_by_hash')
                  AND EXISTS (
                      SELECT 1 FROM posts_migration pm
                      WHERE (pm.source_file = ? OR (pm.run_id = ? AND pm.source_file_index = ?))
                        AND pm.featured_image_url = im.source_image_url
                  )
                """,
                (source_file_str, run_id, file_index),
            )
            images_uploaded_real = int(img_uploaded_row["total"]) if img_uploaded_row else 0
            images_reused_real = int(img_reused_row["total"]) if img_reused_row else 0

        real_total = real_created + real_pending + real_failed

        issue_types: list[str] = []
        if file_status == "migrated" and real_pending > 0:
            issue_types.append("migrated_but_has_pending_posts")
        if file_status == "migrated" and rows_created_record < rows_imported and real_pending > 0:
            issue_types.append("migrated_but_rows_created_lt_rows_imported")
        if rows_created_record != real_created:
            issue_types.append("rows_created_mismatch")
        if real_created > rows_imported and rows_imported > 0:
            issue_types.append("rows_created_gt_rows_imported")
        if file_status == "partial":
            issue_types.append("partial_file")
        if not fast and images_uploaded_record != images_uploaded_real:
            issue_types.append("images_uploaded_mismatch")

        issues.append({
            "run_id": run_id,
            "file_index": file_index,
            "file_name": file_name,
            "file_status": file_status,
            "rows_imported": rows_imported,
            "rows_created_record": rows_created_record,
            "rows_created_real": real_created,
            "rows_pending_real": real_pending,
            "rows_failed_real": real_failed,
            "rows_total_real": real_total,
            "images_uploaded_record": images_uploaded_record,
            "images_uploaded_real": images_uploaded_real,
            "images_reused_record": images_reused_record,
            "images_reused_real": images_reused_real,
            "issue_types": "; ".join(issue_types) if issue_types else "ok",
            "has_issues": bool(issue_types),
        })

    integrity_report_path = global_dir / "run_integrity_report.csv"
    write_csv(integrity_report_path, issues, INTEGRITY_REPORT_FIELDS)
    files_with_issues = [row for row in issues if row["has_issues"]]
    return {
        "run_id": run_id,
        "mode": "fast" if fast else "full",
        "files_checked": len(files),
        "files_with_issues": len(files_with_issues),
        "report": str(integrity_report_path),
        "issues": issues,
    }


def clear_run_error(db: MigrationDB, run_id: str) -> dict[str, Any]:
    """Reset a failed run to paused/recoverable state. No WordPress writes."""
    run = db.get_run(run_id)
    if not run:
        raise ValueError(f"Run not found: {run_id}")

    old_status = str(run.get("status") or "")
    if old_status != "failed":
        return {
            "run_id": run_id,
            "old_status": old_status,
            "new_status": old_status,
            "changed": False,
            "message": f"Run is not in failed state (current: {old_status}). No change needed.",
        }

    # Clear last_error from data blob.
    raw_data = run.get("data") or {}
    if isinstance(raw_data, str):
        try:
            import json as _json_mod
            raw_data = _json_mod.loads(raw_data)
        except Exception:
            raw_data = {}
    if isinstance(raw_data, dict):
        raw_data.pop("last_error", None)

    db.update_run(run_id, status="paused", data=raw_data)

    files = db.list_run_files(run_id)
    partial_files = [f["file_name"] for f in files if f.get("status") == "partial"]
    return {
        "run_id": run_id,
        "old_status": old_status,
        "new_status": "paused",
        "changed": True,
        "partial_files": partial_files,
        "message": (
            f"Run status cleared: failed → paused. "
            f"{len(partial_files)} partial file(s) remain. "
            "Run resume-run to continue."
        ),
    }


def sqlite_health(db: MigrationDB) -> dict[str, Any]:
    """Report SQLite health metrics. No WordPress writes."""
    integrity_row = db.query_one("PRAGMA integrity_check")
    integrity_result = integrity_row.get("integrity_check") if integrity_row else "unknown"

    journal_row = db.query_one("PRAGMA journal_mode")
    journal_mode = journal_row.get("journal_mode") if journal_row else "unknown"

    wal_checkpoint: dict[str, Any] | None = None
    if journal_mode == "wal":
        ckpt = db.query_one("PRAGMA wal_checkpoint(PASSIVE)")
        if ckpt:
            wal_checkpoint = dict(ckpt)

    page_size_row = db.query_one("PRAGMA page_size")
    page_count_row = db.query_one("PRAGMA page_count")
    page_size = int(page_size_row.get("page_size", 0)) if page_size_row else 0
    page_count = int(page_count_row.get("page_count", 0)) if page_count_row else 0
    size_bytes = page_size * page_count

    table_counts: dict[str, int] = {}
    for table in ["posts_migration", "images_migration", "migration_runs", "migration_run_files", "errors", "categories_mapping"]:
        row = db.query_one(f"SELECT COUNT(*) AS total FROM {table}")
        table_counts[table] = int(row["total"]) if row else 0

    return {
        "db_path": str(db.db_path),
        "integrity_check": integrity_result,
        "journal_mode": journal_mode,
        "wal_checkpoint": wal_checkpoint,
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / (1024 * 1024), 2) if size_bytes else 0,
        "table_counts": table_counts,
        "ok": integrity_result == "ok",
    }


def repair_run_status(settings: Settings, db: MigrationDB, run_id: str) -> dict[str, Any]:
    """Correct file statuses based on actual post counts. No WordPress writes."""
    files = db.list_run_files(run_id)
    repaired: list[dict[str, Any]] = []

    for file_rec in files:
        file_index = int(file_rec["file_index"])
        file_name = str(file_rec.get("file_name") or "")
        current_status = str(file_rec.get("status") or "")
        rows_imported = int(file_rec.get("rows_imported") or 0)

        real_created = _count_posts_for_file(db, settings, run_id, file_index, ["created", "skipped_existing"])
        real_pending = _count_posts_for_file(db, settings, run_id, file_index, ["dry_run_valid", "pending", "retry_pending"])
        real_failed = _count_posts_for_file(db, settings, run_id, file_index, ["failed", "image_uploaded_post_failed"])

        if real_pending > 0:
            if real_created > 0 or real_failed > 0:
                correct_status = "partial"
            elif rows_imported > 0:
                correct_status = "imported"
            else:
                correct_status = current_status
        elif real_created > 0 or real_failed > 0:
            correct_status = "migrated"
        else:
            correct_status = current_status

        if correct_status != current_status:
            db.update_run_file(
                run_id,
                file_index,
                status=correct_status,
                rows_created=real_created,
                rows_failed=real_failed,
            )
            repaired.append({
                "file_index": file_index,
                "file_name": file_name,
                "old_status": current_status,
                "new_status": correct_status,
                "real_pending": real_pending,
                "real_created": real_created,
                "real_failed": real_failed,
            })

    return {
        "run_id": run_id,
        "files_checked": len(files),
        "files_repaired": len(repaired),
        "repaired": repaired,
    }


def _count_posts_for_file(
    db: MigrationDB,
    settings: Settings,
    run_id: str,
    file_index: int,
    statuses: list[str],
) -> int:
    """Count posts for a file matching given statuses. Matches by source_file path or run_id+source_file_index."""
    source_file_str = str(normalized_path(settings, file_index))
    placeholders = ", ".join("?" * len(statuses))
    row = db.query_one(
        f"""
        SELECT COUNT(*) AS total FROM posts_migration
        WHERE status IN ({placeholders})
          AND (source_file = ? OR (run_id = ? AND source_file_index = ?))
        """,
        tuple(statuses) + (source_file_str, run_id, file_index),
    )
    return int(row["total"]) if row else 0


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
            # Only block on non-empty categories that aren't in the map.
            # Empty category means the post had no categoryIds in Wix — use default silently.
            if row.get("category")
            and resolve_category_detail(db, row.get("category"), settings.default_category_id)["matched_by"] == "default"
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
