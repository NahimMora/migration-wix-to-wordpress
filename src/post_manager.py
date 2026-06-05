"""Dry-run and migration orchestration."""

from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from logging import Logger
from pathlib import Path
from typing import Any, Iterable

from .author_mapper import load_author_map, resolve_author
from .category_mapper import load_category_map, resolve_category_detail
from .config import Settings
from .csv_loader import import_csv
from .database import MigrationDB
from .html_cleaner import clean_html
from .image_manager import ImageDownloadError, ImageManager, ImageResult
from .normalizer import clean_text
from .url_manager import build_new_url, classify_url_status, predict_url_change
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
        resolved_source_file = self._load_source_if_needed(source_file, limit)
        category_map_result = load_category_map(self.settings.input_dir / "category_map.csv", self.db)
        author_rows, author_warnings = load_author_map(self.settings.input_dir / "author_map.csv")
        for warning in category_map_result.get("warnings", []):
            self.db.record_audit("dry_run", "warning", warning)
            self.logger.warning(warning)
        for warning in author_warnings:
            self.db.record_audit("dry_run", "warning", warning)
            self.logger.warning(warning)
        warnings = validate_default_post_status(self.settings.default_post_status)
        for warning in warnings:
            self.db.record_audit("dry_run", "warning", warning)
            self.logger.warning(warning)

        source_filter = None
        if source_file and resolved_source_file:
            resolved_value = str(resolved_source_file)
            if self.db.count("posts_migration", "source_file = ?", (resolved_value,)):
                source_filter = resolved_value
        posts = self.db.list_posts(
            statuses=["pending", "retry_pending", "failed", "dry_run_valid"],
            limit=limit,
            source_file=source_filter,
        )
        results: list[dict[str, Any]] = []
        for post in posts:
            raw_payload = _json_loads(post.get("raw_payload"))
            category_resolution = resolve_category_detail(
                self.db,
                post.get("category_source"),
                self.settings.default_category_id,
            )
            post_for_payload = {
                **post,
                "wp_category_id": category_resolution["wp_category_id"],
            }
            author_resolution = resolve_author(
                author_rows,
                post.get("author_source_id") or raw_payload.get("author_source_id"),
                post.get("author_source_slug") or raw_payload.get("author_source_slug"),
                self.settings,
            )
            post_for_payload["wp_author_id"] = author_resolution["wp_user_id"]
            raw_payload["author_id"] = author_resolution["wp_user_id"]
            validation_warnings = validate_source_post(raw_payload, post_for_payload)
            image_plan = self.image_manager.dry_run_plan(post.get("featured_image_url"))
            url_prediction = predict_url_change(self.settings, post.get("old_url"), post.get("desired_slug"))
            payload = self._build_post_payload(post_for_payload, raw_payload, image_result=image_plan, dry_run=True)
            status = "dry_run_valid" if not validation_warnings else "failed"

            self.db.update_post(
                int(post["id"]),
                status=status,
                wp_category_id=category_resolution["wp_category_id"],
                wp_author_id=author_resolution["wp_user_id"],
                error_message="; ".join(validation_warnings) if validation_warnings else None,
            )
            result = {
                "post_id": post["id"],
                "wix_id": post.get("wix_id"),
                "old_url": post.get("old_url"),
                "desired_slug": post.get("desired_slug"),
                "status": status,
                "warnings": validation_warnings,
                "category_resolution": category_resolution,
                "author_resolution": author_resolution,
                "url_prediction": url_prediction,
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
        source_file: str | None = None,
        run_id: str | None = None,
    ) -> MigrationSummary:
        self._validate_write_mode(source_file=source_file, run_id=run_id, limit=limit, batch_size=batch_size)
        if not self.settings.allow_wordpress_writes:
            raise RuntimeError(
                "WordPress writes are disabled. Set ALLOW_WORDPRESS_WRITES=true only after audit, verify and dry-run pass."
            )

        client = WordPressClient(self.settings)
        selected_statuses = list(statuses or ["dry_run_valid", "pending", "retry_pending"])
        max_posts = limit or batch_size or self.settings.batch_size
        posts = self.db.list_posts(statuses=selected_statuses, limit=max_posts, source_file=source_file, run_id=run_id)

        processed = created = skipped = failed = 0
        created_count = self.db.count("posts_migration", "status = 'created'")
        batch_label = f"batch-{created_count // max(1, self.settings.batch_size) + 1}"
        self.logger.info("[Batch %s] Processing %s posts", batch_label, len(posts))

        # Launch image downloads in background while the main loop processes posts
        prefetch_futures: dict[str, Future[None]] = {}
        _prefetch_executor: ThreadPoolExecutor | None = None
        _prefetch_workers = self.settings.image_prefetch_workers
        if _prefetch_workers > 0:
            _prefetch_executor = ThreadPoolExecutor(max_workers=_prefetch_workers)
            for _post in posts:
                _url = clean_text(_post.get("featured_image_url")) or ""
                if _url and _url not in prefetch_futures:
                    prefetch_futures[_url] = _prefetch_executor.submit(
                        self.image_manager.prefetch_one, _url
                    )

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

            existing_slug_post = self.db.find_created_post_by_slug(str(post.get("desired_slug") or ""), int(post["id"]))
            if existing_slug_post:
                skipped += 1
                message = f"desired_slug already created in post_id={existing_slug_post['id']}"
                self.db.update_post(
                    int(post["id"]),
                    status="skipped_existing",
                    wp_post_id=existing_slug_post.get("wp_post_id"),
                    wp_slug_final=existing_slug_post.get("wp_slug_final"),
                    new_url=existing_slug_post.get("new_url"),
                    url_status="duplicate_slug_changed",
                    error_message=message,
                    migration_batch=batch_label,
                )
                self.db.record_error("post", post.get("id"), "duplicate_slug", message, raw_payload)
                self.logger.warning("[Post skipped] wix_id=%s %s", post.get("wix_id"), message)
                continue

            # Wait for this post's prefetched image before proceeding
            _pf_url = clean_text(post.get("featured_image_url")) or ""
            if _pf_url in prefetch_futures:
                try:
                    prefetch_futures[_pf_url].result(timeout=self.settings.image_download_timeout + 30)
                except Exception:
                    pass

            image_result = self._handle_image(post, client)
            if image_result.error and not self.settings.create_post_if_image_fails:
                failed += 1
                self._mark_post_failed(post, "image", image_result.error, raw_payload)
                continue

            payload = self._build_post_payload(
                post,
                raw_payload,
                image_result=image_result,
                dry_run=False,
                migration_batch=batch_label,
            )
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

        if _prefetch_executor is not None:
            _prefetch_executor.shutdown(wait=False)

        summary = MigrationSummary(processed=processed, created=created, skipped=skipped, failed=failed)
        self.db.record_audit("migration", "info", "Migration batch completed", summary.__dict__)
        return summary

    def retry_failed(self, limit: int | None = None) -> MigrationSummary:
        return self.migrate(limit=limit, statuses=["failed", "retry_pending", "image_uploaded_post_failed"])

    def repair_missing_images(self, limit: int | None = None) -> dict[str, Any]:
        """Retry image downloads for posts already created but missing their featured image.

        For each qualifying post, re-attempts the download, uploads to WordPress, and
        PATCHes the existing post to set featured_media.
        """
        if not self.settings.allow_wordpress_writes:
            raise RuntimeError(
                "WordPress writes are disabled. Set ALLOW_WORDPRESS_WRITES=true to use repair-missing-images."
            )
        sql = """
            SELECT id, wp_post_id, featured_image_url, title, wix_id
            FROM posts_migration
            WHERE status = 'created'
              AND featured_media_id IS NULL
              AND featured_image_url IS NOT NULL
              AND featured_image_url != ''
              AND wp_post_id IS NOT NULL
            ORDER BY id
        """
        if limit:
            sql += f" LIMIT {limit}"
        posts = self.db.query(sql)
        client = WordPressClient(self.settings)
        repaired = failed = skipped = 0
        for post in posts:
            image_url = clean_text(post.get("featured_image_url"))
            if not image_url:
                skipped += 1
                continue
            image_result = self._handle_image(post, client)
            if not image_result.wp_media_id:
                failed += 1
                self.logger.warning(
                    "[repair-image] wix_id=%s failed: %s", post.get("wix_id"), image_result.error
                )
                continue
            wp_post_id = int(post["wp_post_id"])
            try:
                client.update_post(wp_post_id, {"featured_media": image_result.wp_media_id})
                self.db.update_post(int(post["id"]), featured_media_id=image_result.wp_media_id)
                repaired += 1
                self.logger.info(
                    "[repair-image] wix_id=%s wp_id=%s media_id=%s",
                    post.get("wix_id"), wp_post_id, image_result.wp_media_id,
                )
            except WordPressError as exc:
                failed += 1
                self.db.record_error("post", post.get("id"), "repair_featured_media", str(exc), exc.payload)
                self.logger.error("[repair-image] patch failed wix_id=%s error=%s", post.get("wix_id"), exc)
        result = {"total": len(posts), "repaired": repaired, "failed": failed, "skipped": skipped}
        self.db.record_audit("repair_missing_images", "info", "Repair completed", result)
        return result

    def cleanup_test_batch_preview(self, batch: str) -> dict[str, Any]:
        posts = self.db.query(
            """
            SELECT id, wix_id, old_url, wp_post_id, new_url, status, migration_batch
            FROM posts_migration
            WHERE migration_batch = ?
            ORDER BY id ASC
            """,
            (batch,),
        )
        media_ids = [
            row["featured_media_id"]
            for row in self.db.query(
                """
                SELECT featured_media_id
                FROM posts_migration
                WHERE migration_batch = ? AND featured_media_id IS NOT NULL
                ORDER BY id ASC
                """,
                (batch,),
            )
        ]
        return {
            "batch": batch,
            "dry_run": True,
            "will_delete_posts": False,
            "will_delete_media": False,
            "candidate_posts": posts,
            "candidate_media_ids": media_ids,
            "note": "Preview only. No WordPress content was modified.",
        }

    def _load_source_if_needed(self, source_file: str | None, limit: int | None) -> Path | None:
        file_path = self._resolve_source_file(source_file)
        if source_file and file_path.exists() and not self.db.count("posts_migration", "source_file = ?", (str(file_path),)):
            import_csv(file_path, self.settings, self.db, limit=limit)
            return file_path
        if self.db.count("posts_migration") > 0:
            return file_path
        if file_path.exists():
            import_csv(file_path, self.settings, self.db, limit=limit)
            return file_path
        return None

    def _resolve_source_file(self, source_file: str | None) -> Path:
        if source_file:
            candidate = Path(source_file)
            return candidate if candidate.is_absolute() else self.settings.root_dir / candidate
        return self.settings.input_dir / "wix_posts_sample.csv"

    def _handle_image(self, post: dict[str, Any], client: WordPressClient) -> ImageResult:
        image_url = clean_text(post.get("featured_image_url"))
        if not image_url:
            return ImageResult(source_url="", status="missing_image")
        try:
            return self.image_manager.prepare_and_upload(image_url, client, title=post.get("title"))
        except ImageDownloadError as exc:
            return ImageResult(
                source_url=image_url,
                status="failed_download",
                error=exc.probe.error_detail or exc.probe.error_type or str(exc),
            )
        except Exception as exc:
            self.db.record_error(
                "image",
                post.get("id"),
                "prepare_image_unexpected",
                str(exc),
                {"image_url": image_url, "derived_from": "prepare_image"},
            )
            return ImageResult(source_url=image_url, status="failed_download", error=str(exc))

    def _build_post_payload(
        self,
        post: dict[str, Any],
        raw_payload: dict[str, Any],
        image_result: ImageResult,
        dry_run: bool,
        migration_batch: str = "",
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
            "author": int(post.get("wp_author_id") or raw_payload.get("author_id") or self.settings.default_author_id),
            "meta": {
                "_wix_id": post.get("wix_id") or "",
                "_wix_old_url": post.get("old_url") or "",
                "_migration_batch": "dry-run" if dry_run else migration_batch,
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

    def _validate_write_mode(
        self,
        source_file: str | None = None,
        run_id: str | None = None,
        limit: int | None = None,
        batch_size: int | None = None,
    ) -> None:
        selected_statuses = ["dry_run_valid", "pending", "retry_pending"]
        estimated_rows = self.db.count(
            "posts_migration",
            "status IN ('dry_run_valid', 'pending', 'retry_pending')"
            + (" AND source_file = ?" if source_file else "")
            + (" AND run_id = ?" if run_id else ""),
            tuple(item for item in (source_file, run_id) if item),
        )
        summary = {
            "write_mode": "enabled" if self.settings.allow_wordpress_writes else "disabled",
            "post_status": self.settings.default_post_status,
            "confirm_publish_mode": self.settings.confirm_publish_mode,
            "input_mode": "range" if run_id else "single file" if source_file else "sqlite queue",
            "batch_size": batch_size or limit or self.settings.batch_size,
            "files_to_process": [source_file] if source_file else [],
            "estimated_rows": estimated_rows,
            "selected_statuses": selected_statuses,
        }
        self.logger.warning("Migration write-mode summary: %s", json.dumps(summary, ensure_ascii=True))
        if self.settings.default_post_status == "publish" and not self.settings.confirm_publish_mode:
            raise RuntimeError("Publishing mode requires CONFIRM_PUBLISH_MODE=true")


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
