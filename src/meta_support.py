"""Safe diagnostics for WordPress REST meta field support."""

from __future__ import annotations

from typing import Any

from .config import Settings, missing_wp_credentials
from .database import MigrationDB
from .normalizer import clean_text
from .wordpress_client import WordPressClient


REQUIRED_META_FIELDS = ("_wix_id", "_wix_old_url", "_migration_batch")
MIGRATION_PLUGIN_SLUG = "holasalta-migration-support/holasalta-migration-support"
META_SUPPORT_ROUTE = "holasalta-migration/v1/meta-support"


def verify_meta_support(settings: Settings, db: MigrationDB) -> dict[str, Any]:
    """Use GET-only probes to verify migration meta exposure in WordPress REST."""

    result: dict[str, Any] = {
        "ok": False,
        "uses_only_get": True,
        "required_meta_fields": list(REQUIRED_META_FIELDS),
        "missing_credentials": list(missing_wp_credentials(settings)),
        "authenticated_user": {},
        "plugin": {
            "endpoint_status": None,
            "active": False,
            "found": False,
            "matches": [],
        },
        "custom_meta_support_route": {
            "status_code": None,
            "ok": False,
            "fields": {},
        },
        "post_context_edit_probe": {
            "status_code": None,
            "post_id": None,
            "meta_keys": [],
            "fields_exposed": {},
        },
        "diagnosis": [],
    }
    if result["missing_credentials"]:
        result["diagnosis"].append("Missing WordPress credentials; meta support cannot be verified.")
        return result

    client = WordPressClient(settings)
    user_status, user_payload = client.get_current_user_probe()
    result["authenticated_user"] = summarize_user_probe(user_status, user_payload)

    plugin_status, plugin_payload = client.raw_request(
        "GET",
        f"{settings.wp_v2_root}/plugins",
        params={"context": "edit", "per_page": 100},
    )
    result["plugin"]["endpoint_status"] = plugin_status
    if isinstance(plugin_payload, list):
        matches = [
            summarize_plugin(item)
            for item in plugin_payload
            if clean_text(item.get("plugin")) == MIGRATION_PLUGIN_SLUG
            or "holasalta migration support" in clean_text(item.get("name")).lower()
        ]
        result["plugin"]["matches"] = matches
        result["plugin"]["found"] = bool(matches)
        result["plugin"]["active"] = any(item.get("status") == "active" for item in matches)
    elif plugin_status in {401, 403}:
        result["diagnosis"].append("/wp/v2/plugins is not readable by this user; cannot confirm plugin activation from core plugin endpoint.")
    else:
        result["diagnosis"].append(f"/wp/v2/plugins probe returned HTTP {plugin_status}.")

    route_status, route_payload = client.raw_request("GET", f"{settings.wp_api_root}/{META_SUPPORT_ROUTE}")
    route = result["custom_meta_support_route"]
    route["status_code"] = route_status
    if isinstance(route_payload, dict):
        route["ok"] = bool(route_status == 200 and route_payload.get("ok"))
        route["fields"] = safe_meta_route_fields(route_payload.get("fields"))
        if route_payload.get("plugin_active"):
            result["plugin"]["active"] = True
            result["plugin"]["found"] = True
    if route_status == 404:
        result["diagnosis"].append("Migration meta support route not found; plugin is inactive, missing, or older than the diagnostic version.")
    elif route_status not in {200, None} and not route["ok"]:
        result["diagnosis"].append(f"Migration meta support route returned HTTP {route_status}.")

    post_probe = probe_post_meta_exposure(settings, db, client)
    result["post_context_edit_probe"] = post_probe

    route_fields = route["fields"]
    route_exposed = {
        field: bool(route_fields.get(field, {}).get("registered") and route_fields.get(field, {}).get("show_in_rest"))
        for field in REQUIRED_META_FIELDS
    }
    post_exposed = post_probe.get("fields_exposed", {})
    fields_ok = all(route_exposed.values()) if route_fields else all(post_exposed.get(field) for field in REQUIRED_META_FIELDS)

    if not result["plugin"]["active"]:
        result["diagnosis"].append("holasalta-migration-support is not active in WordPress.")
    if route_fields and not all(route_exposed.values()):
        missing = [field for field, ok in route_exposed.items() if not ok]
        result["diagnosis"].append("Meta fields are not registered with show_in_rest=true: " + ", ".join(missing))
    if post_probe["status_code"] == 200 and not all(post_exposed.get(field) for field in REQUIRED_META_FIELDS):
        missing = [field for field in REQUIRED_META_FIELDS if not post_exposed.get(field)]
        result["diagnosis"].append("GET /wp/v2/posts/{id}?context=edit does not expose meta fields: " + ", ".join(missing))

    result["ok"] = bool(
        result["authenticated_user"].get("status_code") == 200
        and result["plugin"]["active"]
        and fields_ok
        and not [item for item in result["diagnosis"] if "not active" in item or "not expose" in item or "not registered" in item]
    )
    return result


def test_meta_draft_dry_run(settings: Settings, db: MigrationDB) -> dict[str, Any]:
    """Build the exact meta payload shape without making any WordPress request."""

    source = db.query_one(
        """
        SELECT wix_id, old_url, desired_slug, title
        FROM posts_migration
        WHERE wix_id IS NOT NULL OR old_url IS NOT NULL
        ORDER BY id ASC
        LIMIT 1
        """
    ) or {}
    wix_id = clean_text(source.get("wix_id")) or "meta-dry-run-wix-id"
    old_url = clean_text(source.get("old_url")) or "https://example.invalid/post/meta-dry-run"
    title = clean_text(source.get("title")) or "Meta support dry-run"
    slug = clean_text(source.get("desired_slug")) or "meta-support-dry-run"

    payload = {
        "title": title,
        "content": "<p>Dry-run only. This payload is not sent to WordPress.</p>",
        "status": "draft",
        "slug": slug,
        "meta": {
            "_wix_id": wix_id,
            "_wix_old_url": old_url,
            "_migration_batch": "meta-dry-run",
        },
    }
    return {
        "dry_run": True,
        "will_create_post": False,
        "will_send_request": False,
        "method_if_enabled": "POST",
        "endpoint_if_enabled": f"{settings.wp_v2_root}/posts",
        "allow_wordpress_writes": settings.allow_wordpress_writes,
        "required_meta_fields": list(REQUIRED_META_FIELDS),
        "post_payload": payload,
        "note": "No WordPress request was sent.",
    }


def probe_post_meta_exposure(settings: Settings, db: MigrationDB, client: WordPressClient) -> dict[str, Any]:
    row = db.query_one(
        """
        SELECT wp_post_id
        FROM posts_migration
        WHERE wp_post_id IS NOT NULL
        ORDER BY id ASC
        LIMIT 1
        """
    )
    post_id = int(row["wp_post_id"]) if row and row.get("wp_post_id") else None
    if post_id is None:
        posts = client.list_posts(limit=1, context="edit", status="any")
        if posts:
            post_id = int(posts[0]["id"])
    result: dict[str, Any] = {
        "status_code": None,
        "post_id": post_id,
        "meta_keys": [],
        "fields_exposed": {field: False for field in REQUIRED_META_FIELDS},
    }
    if post_id is None:
        result["note"] = "No post available for context=edit meta exposure probe."
        return result

    status, payload = client.raw_request(
        "GET",
        f"{settings.wp_v2_root}/posts/{post_id}",
        params={"context": "edit", "_fields": "id,meta"},
    )
    result["status_code"] = status
    meta = payload.get("meta") if isinstance(payload, dict) else None
    if isinstance(meta, dict):
        result["meta_keys"] = sorted(meta.keys())
        result["fields_exposed"] = {field: field in meta for field in REQUIRED_META_FIELDS}
    return result


def safe_meta_route_fields(fields: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(fields, dict):
        return {}
    safe: dict[str, dict[str, Any]] = {}
    for field in REQUIRED_META_FIELDS:
        value = fields.get(field)
        if not isinstance(value, dict):
            safe[field] = {"registered": False, "show_in_rest": False}
            continue
        safe[field] = {
            "registered": bool(value.get("registered")),
            "single": bool(value.get("single")),
            "type": value.get("type"),
            "show_in_rest": bool(value.get("show_in_rest")),
            "auth_callback": value.get("auth_callback"),
            "object_subtype": value.get("object_subtype"),
        }
    return safe


def summarize_plugin(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "plugin": item.get("plugin"),
        "name": item.get("name"),
        "status": item.get("status"),
        "version": item.get("version"),
    }


def summarize_user_probe(status_code: int | None, payload: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {"status_code": status_code}
    if isinstance(payload, dict):
        summary["id"] = payload.get("id")
        summary["roles"] = payload.get("roles", [])
        summary["capabilities"] = {
            key: bool(payload.get("capabilities", {}).get(key))
            for key in ("edit_posts", "edit_others_posts", "manage_options")
            if isinstance(payload.get("capabilities"), dict)
        }
    return summary
