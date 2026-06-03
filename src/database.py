"""SQLite persistence for idempotent and resumable migrations."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS posts_migration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wix_id TEXT,
    old_url TEXT,
    old_path TEXT,
    desired_slug TEXT,
    wp_post_id INTEGER,
    wp_slug_final TEXT,
    new_url TEXT,
    title TEXT,
    source_date TEXT,
    wp_date TEXT,
    category_source TEXT,
    wp_category_id INTEGER,
    featured_image_url TEXT,
    featured_media_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    url_status TEXT,
    migration_batch TEXT,
    error_message TEXT,
    raw_payload TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_wix_id
ON posts_migration(wix_id)
WHERE wix_id IS NOT NULL AND wix_id != '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_old_url
ON posts_migration(old_url)
WHERE old_url IS NOT NULL AND old_url != '';

CREATE INDEX IF NOT EXISTS idx_posts_status ON posts_migration(status);
CREATE INDEX IF NOT EXISTS idx_posts_desired_slug ON posts_migration(desired_slug);

CREATE TABLE IF NOT EXISTS images_migration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_image_url TEXT,
    local_path TEXT,
    normalized_path TEXT,
    file_hash_original TEXT,
    file_hash_normalized TEXT,
    file_size_kb INTEGER,
    width INTEGER,
    height INTEGER,
    mime_type TEXT,
    http_status_code INTEGER,
    final_url TEXT,
    content_type_received TEXT,
    content_length_received INTEGER,
    downloaded_bytes INTEGER,
    download_error_type TEXT,
    download_error_detail TEXT,
    wp_media_id INTEGER,
    wp_media_url TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_images_source_url
ON images_migration(source_image_url)
WHERE source_image_url IS NOT NULL AND source_image_url != '';

CREATE INDEX IF NOT EXISTS idx_images_hash_original ON images_migration(file_hash_original);
CREATE INDEX IF NOT EXISTS idx_images_hash_normalized ON images_migration(file_hash_normalized);
CREATE INDEX IF NOT EXISTS idx_images_status ON images_migration(status);

CREATE TABLE IF NOT EXISTS categories_mapping (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wix_category TEXT NOT NULL UNIQUE,
    wix_category_alias_type TEXT,
    wp_category_id INTEGER NOT NULL,
    wp_category_name TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    stage TEXT NOT NULL,
    error_message TEXT NOT NULL,
    raw_payload TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS csv_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    total_rows INTEGER NOT NULL DEFAULT 0,
    imported_rows INTEGER NOT NULL DEFAULT 0,
    skipped_rows INTEGER NOT NULL DEFAULT 0,
    warnings TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class MigrationDB:
    """Small SQLite repository wrapper with explicit migration states."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            _ensure_columns(
                conn,
                "images_migration",
                {
                    "http_status_code": "INTEGER",
                    "final_url": "TEXT",
                    "content_type_received": "TEXT",
                    "content_length_received": "INTEGER",
                    "downloaded_bytes": "INTEGER",
                    "download_error_type": "TEXT",
                    "download_error_detail": "TEXT",
                },
            )
            _ensure_columns(
                conn,
                "categories_mapping",
                {
                    "wix_category_alias_type": "TEXT",
                },
            )

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self.connect() as conn:
            conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def upsert_post_source(self, row: Mapping[str, Any]) -> int:
        """Insert or refresh a source post without overwriting WordPress IDs."""

        payload = _json(row.get("raw_payload"))
        values = {
            "wix_id": row.get("wix_id"),
            "old_url": row.get("old_url"),
            "old_path": row.get("old_path"),
            "desired_slug": row.get("desired_slug"),
            "title": row.get("title"),
            "source_date": row.get("source_date") or row.get("date"),
            "category_source": row.get("category_source") or row.get("category"),
            "wp_category_id": row.get("wp_category_id"),
            "featured_image_url": row.get("featured_image_url") or row.get("image_url"),
            "raw_payload": payload,
        }

        existing = self.find_post_source(values.get("wix_id"), values.get("old_url"))
        if existing:
            assignments = ", ".join(f"{key}=?" for key in values)
            params = list(values.values()) + [existing["id"]]
            self.execute(
                f"UPDATE posts_migration SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                params,
            )
            return int(existing["id"])

        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self.connect() as conn:
            cursor = conn.execute(
                f"INSERT INTO posts_migration ({columns}) VALUES ({placeholders})",
                list(values.values()),
            )
            return int(cursor.lastrowid)

    def find_post_source(self, wix_id: Any, old_url: Any) -> dict[str, Any] | None:
        if wix_id:
            row = self.query_one("SELECT * FROM posts_migration WHERE wix_id = ?", (str(wix_id),))
            if row:
                return row
        if old_url:
            return self.query_one("SELECT * FROM posts_migration WHERE old_url = ?", (str(old_url),))
        return None

    def find_created_post_by_slug(self, desired_slug: str, exclude_id: int | None = None) -> dict[str, Any] | None:
        params: list[Any] = [desired_slug]
        sql = """
            SELECT * FROM posts_migration
            WHERE desired_slug = ?
              AND wp_post_id IS NOT NULL
              AND status IN ('created', 'skipped_existing')
        """
        if exclude_id is not None:
            sql += " AND id != ?"
            params.append(exclude_id)
        sql += " ORDER BY id ASC LIMIT 1"
        return self.query_one(sql, tuple(params))

    def list_posts(
        self,
        statuses: Iterable[str] | None = None,
        limit: int | None = None,
        include_failed: bool = False,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = []
        if statuses:
            status_values = list(statuses)
            where.append(f"status IN ({', '.join('?' for _ in status_values)})")
            params.extend(status_values)
        elif not include_failed:
            where.append("status NOT IN ('created', 'skipped_existing')")

        sql = "SELECT * FROM posts_migration"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id ASC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return self.query(sql, tuple(params))

    def update_post(self, post_id: int, **fields: Any) -> None:
        self._update_table("posts_migration", post_id, fields)

    def get_image_by_url(self, source_url: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM images_migration WHERE source_image_url = ?", (source_url,))

    def get_image_by_hash(self, file_hash: str) -> dict[str, Any] | None:
        return self.query_one(
            """
            SELECT * FROM images_migration
            WHERE (file_hash_original = ? OR file_hash_normalized = ?)
              AND wp_media_id IS NOT NULL
            ORDER BY id ASC
            LIMIT 1
            """,
            (file_hash, file_hash),
        )

    def upsert_image(self, source_url: str, **fields: Any) -> int:
        existing = self.get_image_by_url(source_url)
        values = {"source_image_url": source_url, **fields}
        if existing:
            self.update_image(int(existing["id"]), **fields)
            return int(existing["id"])

        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self.connect() as conn:
            cursor = conn.execute(
                f"INSERT INTO images_migration ({columns}) VALUES ({placeholders})",
                list(values.values()),
            )
            return int(cursor.lastrowid)

    def update_image(self, image_id: int, **fields: Any) -> None:
        self._update_table("images_migration", image_id, fields)

    def upsert_category(
        self,
        wix_category: str,
        wp_category_id: int,
        wp_category_name: str = "",
        wix_category_alias_type: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO categories_mapping (wix_category, wix_category_alias_type, wp_category_id, wp_category_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(wix_category) DO UPDATE SET
                    wix_category_alias_type=excluded.wix_category_alias_type,
                    wp_category_id=excluded.wp_category_id,
                    wp_category_name=excluded.wp_category_name,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (wix_category, wix_category_alias_type, wp_category_id, wp_category_name),
            )

    def clear_categories(self) -> None:
        self.execute("DELETE FROM categories_mapping")

    def get_category(self, wix_category: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM categories_mapping WHERE wix_category = ?", (wix_category,))

    def get_category_by_wp_id(self, wp_category_id: int) -> dict[str, Any] | None:
        return self.query_one(
            """
            SELECT *
            FROM categories_mapping
            WHERE wp_category_id = ?
            ORDER BY
                CASE wix_category_alias_type
                    WHEN 'wix_category_name' THEN 0
                    WHEN 'wix_category_slug' THEN 1
                    WHEN 'wix_category_id' THEN 2
                    ELSE 3
                END,
                id ASC
            LIMIT 1
            """,
            (wp_category_id,),
        )

    def list_categories(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM categories_mapping ORDER BY wix_category ASC")

    def record_error(
        self,
        entity_type: str,
        entity_id: Any,
        stage: str,
        error_message: str,
        raw_payload: Any = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO errors (entity_type, entity_id, stage, error_message, raw_payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (entity_type, str(entity_id) if entity_id is not None else None, stage, error_message, _json(raw_payload)),
            )

    def record_audit(self, audit_type: str, severity: str, message: str, data: Any = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_results (audit_type, severity, message, data)
                VALUES (?, ?, ?, ?)
                """,
                (audit_type, severity, message, _json(data)),
            )

    def record_csv_import(
        self,
        file_path: str,
        total_rows: int,
        imported_rows: int,
        skipped_rows: int,
        warnings: Any = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO csv_imports (file_path, total_rows, imported_rows, skipped_rows, warnings)
                VALUES (?, ?, ?, ?, ?)
                """,
                (file_path, total_rows, imported_rows, skipped_rows, _json(warnings)),
            )

    def count(self, table: str, where: str = "", params: Sequence[Any] = ()) -> int:
        sql = f"SELECT COUNT(*) AS total FROM {table}"
        if where:
            sql += f" WHERE {where}"
        row = self.query_one(sql, params)
        return int(row["total"]) if row else 0

    def _update_table(self, table: str, item_id: int, fields: Mapping[str, Any]) -> None:
        if not fields:
            return
        clean_fields = {key: _json(value) if key in {"raw_payload", "data"} else value for key, value in fields.items()}
        assignments = ", ".join(f"{key}=?" for key in clean_fields)
        params = list(clean_fields.values()) + [item_id]
        self.execute(
            f"UPDATE {table} SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            params,
        )


def _json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True, default=str)


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Mapping[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for column, column_type in columns.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
