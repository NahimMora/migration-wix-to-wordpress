"""Preflight checks that do not mutate WordPress content."""

from __future__ import annotations

from logging import Logger
from typing import Any

from .config import Settings, missing_wp_credentials
from .database import MigrationDB
from .wordpress_client import WordPressClient, WordPressError


def verify_wordpress(settings: Settings, db: MigrationDB, logger: Logger) -> bool:
    missing = list(missing_wp_credentials(settings))
    if missing:
        message = "Missing WordPress credentials: " + ", ".join(missing)
        logger.error(message)
        db.record_audit("wordpress", "critical", message)
        return False

    client = WordPressClient(settings)
    try:
        api_index = client.get_api_index()
        current_user = client.get_current_user()
        posts_probe = client.verify_posts_endpoint()
        categories_probe = client.list_categories()
    except WordPressError as exc:
        logger.error("WordPress verification failed: %s", exc)
        db.record_audit("wordpress", "critical" if exc.critical else "error", str(exc), exc.payload)
        return False

    namespaces = api_index.get("namespaces", []) if isinstance(api_index, dict) else []
    user_id = current_user.get("id") if isinstance(current_user, dict) else None
    logger.info(
        "WordPress REST API reachable. authenticated_user_id=%s namespaces=%s posts_probe=%s categories=%s",
        user_id,
        namespaces,
        bool(posts_probe is not None),
        len(categories_probe),
    )
    db.record_audit(
        "wordpress",
        "info",
        "WordPress REST API reachable",
        {
            "namespaces": namespaces,
            "authenticated_user_id": user_id,
            "posts_endpoint": bool(posts_probe is not None),
            "categories_endpoint": True,
            "categories_count": len(categories_probe),
        },
    )
    return True


def scan_existing_wordpress(settings: Settings, db: MigrationDB, logger: Logger, limit: int = 100) -> list[dict[str, Any]]:
    missing = list(missing_wp_credentials(settings))
    if missing:
        message = "Missing WordPress credentials: " + ", ".join(missing)
        logger.error(message)
        db.record_audit("wordpress_scan", "critical", message)
        return []

    client = WordPressClient(settings)
    try:
        posts = client.list_posts(limit=limit, context="edit", status="any")
    except WordPressError as exc:
        logger.error("Existing WordPress scan failed: %s", exc)
        db.record_audit("wordpress_scan", "critical" if exc.critical else "error", str(exc), exc.payload)
        return []

    rows: list[dict[str, Any]] = []
    for post in posts:
        meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
        title = post.get("title") if isinstance(post.get("title"), dict) else {}
        rows.append(
            {
                "wp_post_id": post.get("id"),
                "slug": post.get("slug"),
                "status": post.get("status"),
                "date": post.get("date"),
                "link": post.get("link"),
                "title": title.get("raw") or title.get("rendered") or "",
                "meta_wix_id": meta.get("_wix_id", ""),
                "meta_old_url": meta.get("_wix_old_url", ""),
                "migration_batch": meta.get("_migration_batch", ""),
            }
        )
    db.record_audit("wordpress_scan", "info", "Existing WordPress scan completed", {"rows": len(rows)})
    return rows


def verify_categories(settings: Settings, db: MigrationDB, logger: Logger) -> bool:
    missing = list(missing_wp_credentials(settings))
    if missing:
        message = "Missing WordPress credentials: " + ", ".join(missing)
        logger.error(message)
        db.record_audit("categories", "critical", message)
        return False

    client = WordPressClient(settings)
    mappings = db.list_categories()
    if not mappings:
        message = "No category mappings loaded in SQLite. Run import-csv or load category_map.csv first."
        logger.warning(message)
        db.record_audit("categories", "warning", message)
        return False

    try:
        wp_categories = client.list_categories()
    except WordPressError as exc:
        logger.error("Category verification failed: %s", exc)
        db.record_audit("categories", "critical" if exc.critical else "error", str(exc), exc.payload)
        return False

    existing_ids = {int(item["id"]) for item in wp_categories}
    ok = True
    for mapping in mappings:
        wp_id = int(mapping["wp_category_id"])
        if wp_id not in existing_ids:
            ok = False
            message = f"Mapped category ID does not exist in WordPress: wix={mapping['wix_category']} wp_id={wp_id}"
            logger.warning(message)
            db.record_audit("categories", "warning", message, mapping)

    if ok:
        logger.info("All mapped categories exist in WordPress.")
        db.record_audit("categories", "info", "All mapped categories exist in WordPress", {"count": len(mappings)})
    return ok
