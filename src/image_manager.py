"""Download, inspect, deduplicate and upload images."""

from __future__ import annotations

import hashlib
import mimetypes
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from .config import Settings
from .csv_loader import read_csv_rows
from .database import MigrationDB
from .image_normalizer import ImageInfo, hash_file, inspect_image, normalize_image, normalization_reasons
from .normalizer import clean_text
from .wordpress_client import WordPressClient, WordPressError


@dataclass(frozen=True)
class ImageResult:
    source_url: str
    status: str
    wp_media_id: int | None = None
    wp_media_url: str | None = None
    upload_path: Path | None = None
    error: str | None = None
    plan: dict[str, Any] | None = None


def analyze_images(file_path: Path) -> dict[str, Any]:
    rows, warnings = read_csv_rows(file_path)
    urls = [clean_text(row.get("image_url")) for row in rows]
    non_empty_urls = [url for url in urls if url]
    duplicates = [url for url, count in Counter(non_empty_urls).items() if count > 1]
    invalid_urls = [url for url in non_empty_urls if urlparse(url).scheme not in {"http", "https"}]
    return {
        "file": str(file_path),
        "total_rows": len(rows),
        "image_urls": len(non_empty_urls),
        "empty_image_urls": len(rows) - len(non_empty_urls),
        "unique_image_urls": len(set(non_empty_urls)),
        "duplicate_image_urls": len(duplicates),
        "invalid_image_urls": len(invalid_urls),
        "warnings": warnings,
    }


class ImageManager:
    def __init__(self, settings: Settings, db: MigrationDB):
        self.settings = settings
        self.db = db
        self.settings.images_dir.mkdir(parents=True, exist_ok=True)

    def dry_run_plan(self, source_url: str | None) -> ImageResult:
        source_url = clean_text(source_url)
        if not source_url:
            return ImageResult(source_url="", status="missing_image", plan={"will_upload": False})
        existing = self.db.get_image_by_url(source_url)
        if existing and existing.get("wp_media_id"):
            return ImageResult(
                source_url=source_url,
                status="reused_by_url",
                wp_media_id=int(existing["wp_media_id"]),
                wp_media_url=existing.get("wp_media_url"),
                plan={"will_upload": False, "reason": "existing source URL"},
            )
        return ImageResult(
            source_url=source_url,
            status="dry_run_valid",
            plan={
                "will_download": True,
                "will_hash": True,
                "will_upload_if_unique": True,
                "normalization": "conditional",
            },
        )

    def prepare_and_upload(self, source_url: str, client: WordPressClient, title: str | None = None) -> ImageResult:
        source_url = clean_text(source_url)
        if not source_url:
            return ImageResult(source_url="", status="missing_image")

        existing_by_url = self.db.get_image_by_url(source_url)
        if existing_by_url and existing_by_url.get("wp_media_id"):
            self.db.update_image(int(existing_by_url["id"]), status="reused_by_url")
            return ImageResult(
                source_url=source_url,
                status="reused_by_url",
                wp_media_id=int(existing_by_url["wp_media_id"]),
                wp_media_url=existing_by_url.get("wp_media_url"),
            )

        image_id = self.db.upsert_image(source_url, status="pending")
        local_path = self._download(source_url, image_id)
        info = inspect_image(local_path)
        self._store_inspection(image_id, local_path, info, "downloaded")

        if not info.valid:
            self.db.update_image(image_id, status="invalid_image", error_message=info.error)
            return ImageResult(source_url=source_url, status="invalid_image", error=info.error)

        upload_path = local_path
        upload_info = info
        reasons = normalization_reasons(info, self.settings)
        should_normalize = self.settings.normalize_images and (
            self.settings.recompress_all_images or not self.settings.normalize_only_if_needed or bool(reasons)
        )

        if should_normalize:
            normalized_path = normalize_image(local_path, self.settings)
            upload_path = normalized_path
            upload_info = inspect_image(normalized_path)
            self.db.update_image(
                image_id,
                normalized_path=str(normalized_path),
                file_hash_normalized=upload_info.file_hash,
                status="normalized",
                mime_type=upload_info.mime_type,
                width=upload_info.width,
                height=upload_info.height,
                file_size_kb=upload_info.file_size_kb,
            )
        else:
            self.db.update_image(image_id, status="normalization_skipped")

        hash_for_dedupe = upload_info.file_hash or hash_file(upload_path)
        existing_by_hash = self.db.get_image_by_hash(hash_for_dedupe)
        if existing_by_hash and existing_by_hash.get("wp_media_id"):
            self.db.update_image(
                image_id,
                status="reused_by_hash",
                wp_media_id=existing_by_hash["wp_media_id"],
                wp_media_url=existing_by_hash.get("wp_media_url"),
            )
            return ImageResult(
                source_url=source_url,
                status="reused_by_hash",
                wp_media_id=int(existing_by_hash["wp_media_id"]),
                wp_media_url=existing_by_hash.get("wp_media_url"),
                upload_path=upload_path,
            )

        try:
            media = client.upload_media(upload_path, mime_type=upload_info.mime_type, title=title)
        except WordPressError as exc:
            self.db.update_image(image_id, status="failed_upload", error_message=str(exc))
            self.db.record_error("image", image_id, "upload_media", str(exc), exc.payload)
            return ImageResult(source_url=source_url, status="failed_upload", upload_path=upload_path, error=str(exc))

        media_id = int(media["id"])
        media_url = media.get("source_url") or media.get("guid", {}).get("rendered")
        self.db.update_image(
            image_id,
            status="uploaded",
            wp_media_id=media_id,
            wp_media_url=media_url,
            file_hash_normalized=hash_for_dedupe,
        )
        return ImageResult(
            source_url=source_url,
            status="uploaded",
            wp_media_id=media_id,
            wp_media_url=media_url,
            upload_path=upload_path,
        )

    def _download(self, source_url: str, image_id: int) -> Path:
        extension = _extension_from_url(source_url)
        filename = hashlib.sha1(source_url.encode("utf-8")).hexdigest() + extension
        destination = self.settings.images_dir / filename
        if destination.exists() and destination.stat().st_size > 0:
            return destination

        try:
            with requests.get(source_url, stream=True, timeout=60) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type")
                if content_type and extension == ".bin":
                    guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
                    if guessed:
                        destination = destination.with_suffix(guessed)
                with destination.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
        except Exception as exc:
            self.db.update_image(image_id, status="failed_download", error_message=str(exc))
            self.db.record_error("image", image_id, "download_image", str(exc), {"source_url": source_url})
            raise
        return destination

    def _store_inspection(self, image_id: int, local_path: Path, info: ImageInfo, status: str) -> None:
        self.db.update_image(
            image_id,
            local_path=str(local_path),
            file_hash_original=info.file_hash,
            file_size_kb=info.file_size_kb,
            width=info.width,
            height=info.height,
            mime_type=info.mime_type,
            status=status,
            error_message=info.error,
        )


def _extension_from_url(source_url: str) -> str:
    suffix = Path(urlparse(source_url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return suffix
    return ".bin"
