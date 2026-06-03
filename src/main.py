"""Command-line interface for the Wix to WordPress migration toolkit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .category_mapper import load_category_map
from .config import Settings, load_settings
from .csv_loader import analyze_csv, import_csv
from .database import MigrationDB
from .image_manager import analyze_images, test_image_download
from .logger import setup_logging
from .post_manager import PostMigrationManager
from .preflight import verify_categories, verify_wordpress
from .preflight import scan_existing_wordpress
from .reports import export_migration_manifest, export_reports, export_rows
from .seo_auditor import (
    analyze_internal_links,
    find_duplicate_posts,
    report_orphan_media,
    scan_local_references,
    verify_import_sample,
)
from .url_manager import analyze_urls
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

    import_parser = subparsers.add_parser("import-csv")
    import_parser.add_argument("--file", required=True)
    import_parser.add_argument("--limit", type=int)

    dry_run_parser = subparsers.add_parser("dry-run")
    dry_run_parser.add_argument("--limit", type=int, default=10)
    dry_run_parser.add_argument("--file")

    migrate_parser = subparsers.add_parser("migrate")
    migrate_parser.add_argument("--limit", type=int)
    migrate_parser.add_argument("--batch-size", type=int)

    retry_parser = subparsers.add_parser("retry-failed")
    retry_parser.add_argument("--limit", type=int)

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
        result = normalize_wix_csv(file_path, output_path, report_path, encoding_report_path)
        db.record_audit("csv", "info", "Wix CSV normalization completed", result)
        return result

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
        result = analyze_urls(file_path)
        internal_rows = analyze_internal_links(file_path, settings)
        duplicate_rows = find_duplicate_posts(file_path)
        export_rows(settings, "internal_links_report.csv", internal_rows)
        export_rows(settings, "duplicate_posts_report.csv", duplicate_rows)
        db.record_audit("urls", "info", "URL analysis completed", result)
        return {**result, "internal_links_report_rows": len(internal_rows), "duplicate_posts_report_rows": len(duplicate_rows)}

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

    if command == "retry-failed":
        manager = PostMigrationManager(settings, db, logger)
        summary = manager.retry_failed(limit=args.limit)
        return summary.__dict__

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


def print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=True, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
