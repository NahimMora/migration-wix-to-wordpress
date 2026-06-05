"""Command-line interface for the Wix to WordPress migration toolkit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .category_mapper import load_category_map
from .config import Settings, load_settings
from .content_formatter import preview_content_format
from .author_mapper import list_wordpress_users, verify_authors
from .csv_loader import analyze_csv, import_csv
from .database import MigrationDB
from .image_manager import analyze_images, test_image_download
from .logger import setup_logging
from .meta_support import test_meta_draft_dry_run, verify_meta_support
from .post_manager import PostMigrationManager
from .preflight import verify_categories, verify_wordpress
from .preflight import scan_existing_wordpress
from .range_runner import (
    RangeSpec,
    check_run_integrity,
    clear_run_error,
    export_run_report,
    migrate_range,
    preflight_range,
    repair_run_status,
    resume_run,
    run_status,
    sqlite_health,
)
from .reports import export_migration_manifest, export_reports, export_rows
from .seo_auditor import (
    analyze_internal_links,
    find_duplicate_posts,
    report_orphan_media,
    scan_local_references,
    verify_import_sample,
)
from .url_manager import PRE_MIGRATION_URL_RISK_FIELDS, analyze_urls, pre_migration_url_risk_rows
from .validators import positive_int, require_file
from .wordpress_client import WordPressClient
from .wix_category_normalizer import normalize_wix_categories
from .wix_csv_normalizer import check_wix_csv_encoding, normalize_wix_csv


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings()
    logger = setup_logging(settings.log_file)
    db = MigrationDB(settings.db_path)
    db.initialize()

    try:
        result = dispatch(args, settings, db, logger)
    except Exception as exc:
        logger.error("Command failed: %s", exc)
        return 1

    if result is not None:
        print_json(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.main", description="Wix to WordPress migration toolkit")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db")

    analyze_csv_parser = subparsers.add_parser("analyze-csv")
    analyze_csv_parser.add_argument("--file", required=True)

    normalize_wix_parser = subparsers.add_parser("normalize-wix-csv")
    normalize_wix_parser.add_argument("--file", required=True)
    normalize_wix_parser.add_argument("--output", required=True)

    preview_content_parser = subparsers.add_parser("preview-content-format")
    preview_content_parser.add_argument("--file", required=True)
    preview_content_parser.add_argument("--limit", type=int, default=5)

    normalize_wix_categories_parser = subparsers.add_parser("normalize-wix-categories")
    normalize_wix_categories_parser.add_argument("--file", required=True)
    normalize_wix_categories_parser.add_argument("--output", required=True)
    normalize_wix_categories_parser.add_argument("--existing-map")

    check_encoding_parser = subparsers.add_parser("check-encoding")
    check_encoding_parser.add_argument("--file", required=True)

    analyze_urls_parser = subparsers.add_parser("analyze-urls")
    analyze_urls_parser.add_argument("--file", required=True)

    analyze_images_parser = subparsers.add_parser("analyze-images")
    analyze_images_parser.add_argument("--file", required=True)

    test_image_parser = subparsers.add_parser("test-image-download")
    test_image_parser.add_argument("--url", required=True)

    subparsers.add_parser("verify-wordpress")
    subparsers.add_parser("verify-categories")
    subparsers.add_parser("verify-authors")
    subparsers.add_parser("list-wordpress-users")
    subparsers.add_parser("verify-meta-support")

    test_meta_parser = subparsers.add_parser("test-meta-draft")
    test_meta_parser.add_argument("--dry-run", action="store_true")

    import_parser = subparsers.add_parser("import-csv")
    import_parser.add_argument("--file", required=True)
    import_parser.add_argument("--limit", type=int)

    dry_run_parser = subparsers.add_parser("dry-run")
    dry_run_parser.add_argument("--limit", type=int, default=10)
    dry_run_parser.add_argument("--file")

    migrate_parser = subparsers.add_parser("migrate")
    migrate_parser.add_argument("--limit", type=int)
    migrate_parser.add_argument("--batch-size", type=int)

    preflight_range_parser = subparsers.add_parser("preflight-range")
    add_range_args(preflight_range_parser, require_run_id=False)
    preflight_range_parser.add_argument("--sample-files", type=int, default=5)

    migrate_range_parser = subparsers.add_parser("migrate-range")
    add_range_args(migrate_range_parser, require_run_id=True)
    migrate_range_parser.add_argument("--force", action="store_true")

    resume_parser = subparsers.add_parser("resume-run")
    resume_parser.add_argument("--run-id", required=True)

    status_parser = subparsers.add_parser("run-status")
    status_parser.add_argument("--run-id", required=True)

    export_run_parser = subparsers.add_parser("export-run-report")
    export_run_parser.add_argument("--run-id", required=True)

    integrity_parser = subparsers.add_parser("check-run-integrity")
    integrity_parser.add_argument("--run-id", required=True)
    integrity_parser.add_argument("--fast", action="store_true", help="Fast mode: GROUP BY query, skip image subqueries")

    repair_parser = subparsers.add_parser("repair-run-status")
    repair_parser.add_argument("--run-id", required=True)

    clear_error_parser = subparsers.add_parser("clear-run-error")
    clear_error_parser.add_argument("--run-id", required=True)

    subparsers.add_parser("sqlite-health")

    retry_parser = subparsers.add_parser("retry-failed")
    retry_parser.add_argument("--limit", type=int)

    repair_images_parser = subparsers.add_parser("repair-missing-images")
    repair_images_parser.add_argument("--limit", type=int)

    subparsers.add_parser("export-reports")
    subparsers.add_parser("scan-local-references")
    subparsers.add_parser("report-orphan-media")

    verify_import_parser = subparsers.add_parser("verify-import")
    verify_import_parser.add_argument("--limit", type=int, default=100)

    subparsers.add_parser("export-migration-manifest")

    scan_existing_parser = subparsers.add_parser("scan-existing-wordpress")
    scan_existing_parser.add_argument("--limit", type=int, default=100)

    cleanup_parser = subparsers.add_parser("cleanup-test-batch")
    cleanup_parser.add_argument("--batch", required=True)
    cleanup_parser.add_argument("--dry-run", action="store_true", required=True)
    return parser


def dispatch(args: argparse.Namespace, settings: Settings, db: MigrationDB, logger: Any) -> Any:
    command = args.command
    if command == "init-db":
        category_result = load_category_map(settings.input_dir / "category_map.csv", db)
        return {"db": str(settings.db_path), "initialized": True, "category_map": category_result}

    if command == "analyze-csv":
        file_path = require_file(resolve_file(settings, args.file))
        return analyze_csv(file_path, db)

    if command == "normalize-wix-csv":
        file_path = require_file(resolve_file(settings, args.file))
        output_path = resolve_file(settings, args.output)
        report_path = settings.output_dir / "normalize_wix_csv_report.csv"
        encoding_report_path = settings.output_dir / "encoding_report.csv"
        content_report_path = settings.output_dir / "content_format_report.csv"
        result = normalize_wix_csv(file_path, output_path, report_path, encoding_report_path, settings, content_report_path)
        db.record_audit("csv", "info", "Wix CSV normalization completed", result)
        return result

    if command == "preview-content-format":
        file_path = require_file(resolve_file(settings, args.file))
        return preview_content_format(file_path, settings, positive_int(args.limit, 5))

    if command == "normalize-wix-categories":
        file_path = require_file(resolve_file(settings, args.file))
        output_path = resolve_file(settings, args.output)
        existing_map = resolve_file(settings, args.existing_map) if args.existing_map else None
        result = normalize_wix_categories(file_path, output_path, existing_map)
        db.record_audit("categories", "info", "Wix category normalization completed", result)
        return result

    if command == "check-encoding":
        file_path = require_file(resolve_file(settings, args.file))
        return check_wix_csv_encoding(file_path)

    if command == "analyze-urls":
        file_path = require_file(resolve_file(settings, args.file))
        result = analyze_urls(file_path, settings)
        url_risk_rows = pre_migration_url_risk_rows(file_path, settings)
        url_risk_count = export_rows(
            settings,
            "pre_migration_url_risk_report.csv",
            url_risk_rows,
            fieldnames=PRE_MIGRATION_URL_RISK_FIELDS,
        )
        internal_rows = analyze_internal_links(file_path, settings)
        duplicate_rows = find_duplicate_posts(file_path)
        export_rows(settings, "internal_links_report.csv", internal_rows)
        export_rows(settings, "duplicate_posts_report.csv", duplicate_rows)
        db.record_audit("urls", "info", "URL analysis completed", result)
        return {
            **result,
            "pre_migration_url_risk_report.csv": url_risk_count,
            "internal_links_report_rows": len(internal_rows),
            "duplicate_posts_report_rows": len(duplicate_rows),
        }

    if command == "analyze-images":
        file_path = require_file(resolve_file(settings, args.file))
        result = analyze_images(file_path)
        db.record_audit("images", "info", "Image analysis completed", result)
        return result

    if command == "test-image-download":
        result = test_image_download(args.url, settings)
        db.record_audit("images", "info" if result.get("ok") else "warning", "Single image download test completed", result)
        return result

    if command == "verify-wordpress":
        return verify_wordpress(settings, db, logger)

    if command == "verify-categories":
        load_category_map(settings.input_dir / "category_map.csv", db)
        return {"ok": verify_categories(settings, db, logger)}

    if command == "list-wordpress-users":
        return {"users": list_wordpress_users(settings)}

    if command == "verify-authors":
        return verify_authors(settings)

    if command == "verify-meta-support":
        result = verify_meta_support(settings, db)
        db.record_audit("wordpress_meta", "info" if result.get("ok") else "warning", "WordPress meta support verification completed", result)
        return result

    if command == "test-meta-draft":
        if not args.dry_run:
            raise ValueError("test-meta-draft refuses to run without --dry-run")
        return test_meta_draft_dry_run(settings, db)

    if command == "import-csv":
        file_path = require_file(resolve_file(settings, args.file))
        return import_csv(file_path, settings, db, limit=args.limit)

    if command == "dry-run":
        manager = PostMigrationManager(settings, db, logger)
        return manager.dry_run(limit=positive_int(args.limit, 10), source_file=args.file)

    if command == "migrate":
        manager = PostMigrationManager(settings, db, logger)
        summary = manager.migrate(limit=args.limit, batch_size=args.batch_size)
        return summary.__dict__

    if command == "preflight-range":
        spec = range_spec_from_args(settings, args, sample_files=positive_int(args.sample_files, 5))
        return preflight_range(settings, db, spec)

    if command == "migrate-range":
        spec = range_spec_from_args(settings, args, force=args.force)
        return migrate_range(settings, db, logger, spec)

    if command == "resume-run":
        return resume_run(settings, db, logger, args.run_id)

    if command == "run-status":
        return run_status(db, args.run_id)

    if command == "export-run-report":
        return export_run_report(settings, db, args.run_id)

    if command == "check-run-integrity":
        return check_run_integrity(settings, db, args.run_id, fast=getattr(args, "fast", False))

    if command == "repair-run-status":
        return repair_run_status(settings, db, args.run_id)

    if command == "clear-run-error":
        return clear_run_error(db, args.run_id)

    if command == "sqlite-health":
        return sqlite_health(db)

    if command == "retry-failed":
        manager = PostMigrationManager(settings, db, logger)
        summary = manager.retry_failed(limit=args.limit)
        return summary.__dict__

    if command == "repair-missing-images":
        manager = PostMigrationManager(settings, db, logger)
        return manager.repair_missing_images(limit=args.limit)

    if command == "export-reports":
        counts = export_reports(settings, db)
        manifest = export_migration_manifest(settings, db)
        return {"reports": counts, "manifest": manifest}

    if command == "scan-local-references":
        rows = scan_local_references(settings, db)
        count = export_rows(settings, "local_references_report.csv", rows)
        return {"local_references_report.csv": count}

    if command == "report-orphan-media":
        rows = report_orphan_media(db)
        count = export_rows(settings, "orphan_media_report.csv", rows)
        return {"orphan_media_report.csv": count}

    if command == "verify-import":
        client = WordPressClient(settings)
        rows = verify_import_sample(settings, db, client, positive_int(args.limit, 100))
        count = export_rows(settings, "verify_import_report.csv", rows)
        return {"verify_import_report.csv": count}

    if command == "export-migration-manifest":
        return export_migration_manifest(settings, db)

    if command == "scan-existing-wordpress":
        rows = scan_existing_wordpress(settings, db, logger, positive_int(args.limit, 100))
        count = export_rows(settings, "existing_wordpress_posts_report.csv", rows)
        return {"existing_wordpress_posts_report.csv": count, "rows": rows}

    if command == "cleanup-test-batch":
        if not args.dry_run:
            raise ValueError("cleanup-test-batch refuses to run without --dry-run in this version")
        manager = PostMigrationManager(settings, db, logger)
        return manager.cleanup_test_batch_preview(args.batch)

    raise ValueError(f"Unknown command: {command}")


def resolve_file(settings: Settings, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return settings.root_dir / path


def add_range_args(parser: argparse.ArgumentParser, require_run_id: bool) -> None:
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--pattern", required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--run-id", required=require_run_id)


def range_spec_from_args(
    settings: Settings,
    args: argparse.Namespace,
    force: bool = False,
    sample_files: int | None = None,
) -> RangeSpec:
    input_dir = resolve_file(settings, args.input_dir)
    return RangeSpec(
        input_dir=input_dir,
        pattern=args.pattern,
        start=positive_int(args.start, 1),
        end=positive_int(args.end, 1),
        run_id=args.run_id or "preflight_range",
        force=force,
        sample_files=sample_files,
    )


def print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=True, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
