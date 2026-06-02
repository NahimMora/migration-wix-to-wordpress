"""Preflight checks that do not mutate WordPress content."""

from __future__ import annotations

from logging import Logger

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
        posts_probe = client.verify_posts_endpoint()
    except WordPressError as exc:
        logger.error("WordPress verification failed: %s", exc)
        db.record_audit("wordpress", "critical" if exc.critical else "error", str(exc), exc.payload)
        return False

    namespaces = api_index.get("namespaces", []) if isinstance(api_index, dict) else []
    logger.info("WordPress REST API reachable. namespaces=%s posts_probe=%s", namespaces, bool(posts_probe is not None))
    db.record_audit(
        "wordpress",
        "info",
        "WordPress REST API reachable",
        {"namespaces": namespaces, "posts_endpoint": bool(posts_probe is not None)},
    )
    return True


def verify_categories(settings: Settings, db: MigrationDB, logger: Logger) -> bool:
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
