"""Dry-run and migration orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from logging import Logger
from pathlib import Path
from typing import Any, Iterable

from .config import Settings
from .csv_loader import import_csv
from .database import MigrationDB
from .html_cleaner import clean_html
from .image_manager import ImageManager, ImageResult
from .normalizer import clean_text
from .url_manager import build_new_url, classify_url_status
from .validators import validate_default_post_status, validate_source_post
from .wordpress_client import WordPressClient, WordPressError


@dataclass(frozen=True)
class MigrationSummary:
    processed: int
    created: int
    skipped: int
    failed: int


class PostMigrationManager:
    def __init__(self, settings: Settings, db: MigrationDB, logger: Logger):
        self.settings = settings
        self.db = db
        self.logger = logger
        self.image_manager = ImageManager(settings, db)

    def dry_run(self, limit: int = 10, source_file: str | None = None) -> list[dict[str, Any]]:
        self._load_source_if_needed(source_file, limit)
        warnings = validate_default_post_status(self.settings.default_post_status)
        for warning in warnings:
            self.db.record_audit("dry_run", "warning", warning)
            self.logger.warning(warning)

        posts = self.db.list_posts(statuses=["pending", "retry_pending", "failed", "dry_run_valid"], limit=limit)
        results: list[dict[str, Any]] = []
        for post in posts:
            raw_payload = _json_loads(post.get("raw_payload"))
            validation_warnings = validate_source_post(raw_payload, post)
            image_plan = self.image_manager.dry_run_plan(post.get("featured_image_url"))
            payload = self._build_post_payload(post, raw_payload, image_result=image_plan, dry_run=True)
            status = "dry_run_valid" if not validation_warnings else "failed"

            self.db.update_post(
                int(post["id"]),
                status=status,
                error_message="; ".join(validation_warnings) if validation_warnings else None,
            )
            result = {
                "post_id": post["id"],
                "wix_id": post.get("wix_id"),
                "old_url": post.get("old_url"),
                "desired_slug": post.get("desired_slug"),
                "status": status,
                "warnings": validation_warnings,
                "post_payload": payload,
                "image_plan": image_plan.plan,
            }
            results.append(result)
            self.logger.info("[Dry-run] wix_id=%s slug=%s status=%s", post.get("wix_id"), post.get("desired_slug"), status)
        self.db.record_audit("dry_run", "info", "Dry-run completed", {"processed": len(results)})
        return results

    def migrate(
        self,
        limit: int | None = None,
        batch_size: int | None = None,
        statuses: Iterable[str] | None = None,
    ) -> MigrationSummary:
        client = WordPressClient(self.settings)
        selected_statuses = list(statuses or ["dry_run_valid", "pending", "retry_pending"])
        max_posts = limit or batch_size or self.settings.batch_size
        posts = self.db.list_posts(statuses=selected_statuses, limit=max_posts)

        processed = created = skipped = failed = 0
        created_count = self.db.count("posts_migration", "status = 'created'")
        batch_label = f"batch-{created_count // max(1, self.settings.batch_size) + 1}"
        self.logger.info("[Batch %s] Processing %s posts", batch_label, len(posts))

        for post in posts:
            processed += 1
            if post.get("wp_post_id") and post.get("status") == "created":
                skipped += 1
                continue

            raw_payload = _json_loads(post.get("raw_payload"))
            validation_warnings = validate_source_post(raw_payload, post)
            if validation_warnings:
                failed += 1
                self._mark_post_failed(post, "validation", "; ".join(validation_warnings), raw_payload)
                continue

            image_result = self._handle_image(post, client)
            if image_result.error and not self.settings.create_post_if_image_fails:
                failed += 1
                self._mark_post_failed(post, "image", image_result.error, raw_payload)
                continue

            payload = self._build_post_payload(post, raw_payload, image_result=image_result, dry_run=False)
            try:
                wp_post = client.create_post(payload)
                final_slug = wp_post.get("slug") or post.get("desired_slug")
                new_url = build_new_url(self.settings, final_slug)
                url_status = classify_url_status(post.get("old_url"), new_url)
                self.db.update_post(
                    int(post["id"]),
                    wp_post_id=wp_post.get("id"),
                    wp_slug_final=final_slug,
                    new_url=new_url,
                    wp_date=wp_post.get("date"),
                    featured_media_id=image_result.wp_media_id,
                    status="created",
                    url_status=url_status,
                    migration_batch=batch_label,
                    error_message=None,
                )
                created += 1
                self.logger.info(
                    "[Post created] wix_id=%s wp_id=%s url_status=%s",
                    post.get("wix_id"),
                    wp_post.get("id"),
                    url_status,
                )
            except WordPressError as exc:
                failed += 1
                status = "image_uploaded_post_failed" if image_result.wp_media_id else "failed"
                self.db.update_post(int(post["id"]), status=status, error_message=str(exc), migration_batch=batch_label)
                self.db.record_error("post", post.get("id"), "create_post", str(exc), exc.payload)
                self.logger.error("[Post failed] wix_id=%s error=%s", post.get("wix_id"), exc)
                if exc.critical:
                    raise

        summary = MigrationSummary(processed=processed, created=created, skipped=skipped, failed=failed)
        self.db.record_audit("migration", "info", "Migration batch completed", summary.__dict__)
        return summary

    def retry_failed(self, limit: int | None = None) -> MigrationSummary:
        return self.migrate(limit=limit, statuses=["failed", "retry_pending", "image_uploaded_post_failed"])

    def _load_source_if_needed(self, source_file: str | None, limit: int | None) -> None:
        if self.db.count("posts_migration") > 0:
            return
        file_path = self.settings.input_dir / "wix_posts_sample.csv"
        if source_file:
            candidate = Path(source_file)
            file_path = candidate if candidate.is_absolute() else self.settings.root_dir / candidate
        if file_path.exists():
            import_csv(file_path, self.settings, self.db, limit=limit)

    def _handle_image(self, post: dict[str, Any], client: WordPressClient) -> ImageResult:
        image_url = clean_text(post.get("featured_image_url"))
        if not image_url:
            return ImageResult(source_url="", status="missing_image")
        try:
            return self.image_manager.prepare_and_upload(image_url, client, title=post.get("title"))
        except Exception as exc:
            self.db.record_error("image", post.get("id"), "prepare_image", str(exc), {"image_url": image_url})
            return ImageResult(source_url=image_url, status="failed_download", error=str(exc))

    def _build_post_payload(
        self,
        post: dict[str, Any],
        raw_payload: dict[str, Any],
        image_result: ImageResult,
        dry_run: bool,
    ) -> dict[str, Any]:
        content = raw_payload.get("content_clean") or clean_html(
            clean_text(raw_payload.get("content")) or clean_text(raw_payload.get("page_content"))
        )
        payload: dict[str, Any] = {
            "title": post.get("title"),
            "content": content,
            "status": self.settings.default_post_status,
            "slug": post.get("desired_slug"),
            "categories": [int(post.get("wp_category_id") or self.settings.default_category_id)],
            "author": int(raw_payload.get("author_id") or self.settings.default_author_id),
            "meta": {
                "_wix_id": post.get("wix_id") or "",
                "_wix_old_url": post.get("old_url") or "",
                "_migration_batch": "dry-run" if dry_run else "",
            },
        }
        if post.get("source_date"):
            payload["date"] = post.get("source_date")
        excerpt = clean_text(raw_payload.get("excerpt"))
        if excerpt:
            payload["excerpt"] = excerpt
        if image_result.wp_media_id:
            payload["featured_media"] = image_result.wp_media_id
        return payload

    def _mark_post_failed(self, post: dict[str, Any], stage: str, error: str, raw_payload: dict[str, Any]) -> None:
        self.db.update_post(int(post["id"]), status="failed", error_message=error)
        self.db.record_error("post", post.get("id"), stage, error, raw_payload)


def _json_loads(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    except json.JSONDecodeError:
        return {}
