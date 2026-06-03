"""Preflight checks that do not mutate WordPress content."""

from __future__ import annotations

from logging import Logger
from typing import Any

from .config import Settings, missing_wp_credentials
from .database import MigrationDB
from .wordpress_client import WordPressClient, WordPressError


def verify_wordpress(settings: Settings, db: MigrationDB, logger: Logger) -> dict[str, Any]:
    """Run GET-only WordPress probes and require authenticated /users/me."""

    result: dict[str, Any] = {
        "ok": False,
        "api_reachable": False,
        "auth_ok": False,
        "posts_read_ok": False,
        "categories_read_ok": False,
        "authenticated_user_id": None,
        "namespaces": [],
        "posts_probe": False,
        "categories_count": 0,
        "users_me": {"status_code": None, "summary": None},
        "diagnosis": [],
    }

    missing = list(missing_wp_credentials(settings))
    if missing:
        message = "Missing WordPress credentials: " + ", ".join(missing)
        logger.error(message)
        result["diagnosis"].append(message)
        db.record_audit("wordpress", "critical", "WordPress verification failed", result)
        return result

    client = WordPressClient(settings)

    try:
        api_index = client.get_api_index()
        result["api_reachable"] = True
        result["namespaces"] = api_index.get("namespaces", []) if isinstance(api_index, dict) else []
    except WordPressError as exc:
        result["diagnosis"].append(_diagnose_wp_error(exc, "api_index"))
        logger.error("WordPress API index verification failed: %s", exc)
        db.record_audit("wordpress", "critical" if exc.critical else "error", "WordPress verification failed", result)
        return result

    users_status, users_payload = client.get_current_user_probe()
    users_summary = summarize_users_me_response(users_payload)
    user_id = users_payload.get("id") if isinstance(users_payload, dict) else None
    result["users_me"] = {"status_code": users_status, "summary": users_summary}
    result["authenticated_user_id"] = user_id
    result["auth_ok"] = bool(users_status == 200 and user_id is not None)

    if not result["auth_ok"]:
        result["diagnosis"].extend(diagnose_auth_failure(users_status, users_payload))

    try:
        posts_probe = client.verify_posts_endpoint()
        result["posts_read_ok"] = posts_probe is not None
        result["posts_probe"] = posts_probe is not None
    except WordPressError as exc:
        result["diagnosis"].append(_diagnose_wp_error(exc, "posts"))

    try:
        categories_probe = client.list_categories()
        result["categories_read_ok"] = True
        result["categories_count"] = len(categories_probe)
    except WordPressError as exc:
        result["diagnosis"].append(_diagnose_wp_error(exc, "categories"))

    result["ok"] = bool(
        result["api_reachable"]
        and result["auth_ok"]
        and result["posts_read_ok"]
        and result["categories_read_ok"]
    )

    if result["ok"]:
        logger.info(
            "WordPress REST API verified. authenticated_user_id=%s posts_read_ok=%s categories_read_ok=%s categories=%s",
            result["authenticated_user_id"],
            result["posts_read_ok"],
            result["categories_read_ok"],
            result["categories_count"],
        )
        severity = "info"
    else:
        logger.error(
            "WordPress REST API verification failed. api_reachable=%s auth_ok=%s posts_read_ok=%s categories_read_ok=%s users_me_status=%s users_me_summary=%s",
            result["api_reachable"],
            result["auth_ok"],
            result["posts_read_ok"],
            result["categories_read_ok"],
            users_status,
            users_summary,
        )
        severity = "error"

    db.record_audit("wordpress", severity, "WordPress verification completed", result)
    return result


def summarize_users_me_response(payload: Any) -> dict[str, Any]:
    """Return a safe summary without email, headers or credentials."""

    if isinstance(payload, dict):
        summary: dict[str, Any] = {
            "payload_type": "object",
            "id": payload.get("id"),
            "code": payload.get("code"),
            "message": payload.get("message"),
        }
        data = payload.get("data")
        if isinstance(data, dict):
            summary["data_status"] = data.get("status")
        if payload.get("roles"):
            summary["roles"] = payload.get("roles")
        return {key: value for key, value in summary.items() if value is not None}
    if isinstance(payload, list):
        return {"payload_type": "array", "items": len(payload)}
    if isinstance(payload, str):
        return {"payload_type": "text", "chars": len(payload)}
    if payload is None:
        return {"payload_type": "empty"}
    return {"payload_type": type(payload).__name__}


def diagnose_auth_failure(status_code: int | None, payload: Any) -> list[str]:
    code = payload.get("code") if isinstance(payload, dict) else None
    message = payload.get("message") if isinstance(payload, dict) else None
    diagnosis: list[str] = []

    if status_code in {401, 403}:
        diagnosis.append("auth_ok=false: WordPress rejected /users/me with 401/403.")
    elif status_code == 404:
        diagnosis.append("auth_ok=false: /wp/v2/users/me was not found or is blocked.")
    elif status_code == 200:
        diagnosis.append("auth_ok=false: /users/me returned 200 but no authenticated user ID.")
    elif status_code is None:
        diagnosis.append("auth_ok=false: /users/me did not respond.")
    else:
        diagnosis.append(f"auth_ok=false: /users/me returned unexpected HTTP {status_code}.")

    if code:
        diagnosis.append(f"users_me_code={code}")
    if message:
        diagnosis.append(f"users_me_message={message}")

    diagnosis.extend(
        [
            "Possible cause: Application Password incorrect or copied with wrong spaces.",
            "Possible cause: WP_USERNAME does not match the Application Password owner.",
            "Possible cause: server/proxy does not pass the Authorization header to PHP/WordPress.",
            "Possible cause: security plugin or server rule blocks /wp-json/wp/v2/users/me.",
            "Possible cause: authenticated user has insufficient REST permissions.",
        ]
    )
    return diagnosis


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


def _diagnose_wp_error(exc: WordPressError, probe: str) -> str:
    return f"{probe}_probe_failed: status={exc.status_code} message={exc}"
