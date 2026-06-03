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
from requests import Response

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


@dataclass(frozen=True)
class ImageDownloadProbe:
    source_url: str
    ok: bool
    status_code: int | None = None
    final_url: str | None = None
    content_type: str | None = None
    content_length: int | None = None
    downloaded_bytes: int = 0
    seems_image: bool = False
    valid_image: bool = False
    width: int | None = None
    height: int | None = None
    file_hash: str | None = None
    local_path: Path | None = None
    error_type: str | None = None
    error_detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "ok": self.ok,
            "status_code": self.status_code,
            "final_url": self.final_url,
            "content_type": self.content_type,
            "content_length": self.content_length,
            "downloaded_bytes": self.downloaded_bytes,
            "seems_image": self.seems_image,
            "valid_image": self.valid_image,
            "width": self.width,
            "height": self.height,
            "hash": self.file_hash,
            "local_path": str(self.local_path) if self.local_path else None,
            "error_type": self.error_type,
            "error_detail": self.error_detail,
        }


class ImageDownloadError(RuntimeError):
    def __init__(self, probe: ImageDownloadProbe):
        super().__init__(probe.error_detail or probe.error_type or "image_download_failed")
        self.probe = probe


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
        probe = download_image_for_diagnostics(source_url, self.settings, destination=destination)
        self._store_download_probe(image_id, probe)
        if not probe.ok or not probe.local_path:
            detail = probe.error_detail or probe.error_type or "download failed"
            self.db.update_image(image_id, status="failed_download", error_message=detail)
            self.db.record_error(
                "image",
                image_id,
                "download_image",
                detail,
                {"source_url": source_url, "download_probe": probe.as_dict(), "root_error": True},
            )
            raise ImageDownloadError(probe)
        return probe.local_path

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

    def _store_download_probe(self, image_id: int, probe: ImageDownloadProbe) -> None:
        self.db.update_image(
            image_id,
            http_status_code=probe.status_code,
            final_url=probe.final_url,
            content_type_received=probe.content_type,
            content_length_received=probe.content_length,
            downloaded_bytes=probe.downloaded_bytes,
            download_error_type=probe.error_type,
            download_error_detail=probe.error_detail,
        )


def test_image_download(source_url: str, settings: Settings) -> dict[str, Any]:
    destination = _diagnostic_destination(source_url, settings)
    return download_image_for_diagnostics(source_url, settings, destination=destination).as_dict()


def download_image_for_diagnostics(
    source_url: str,
    settings: Settings,
    destination: Path | None = None,
) -> ImageDownloadProbe:
    source_url = clean_text(source_url)
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"}:
        return ImageDownloadProbe(
            source_url=source_url,
            ok=False,
            error_type="invalid_url",
            error_detail="URL must start with http:// or https://",
        )

    headers = {
        "User-Agent": settings.image_download_user_agent,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        response = requests.get(
            source_url,
            stream=True,
            timeout=settings.image_download_timeout,
            allow_redirects=True,
            headers=headers,
        )
    except requests.Timeout as exc:
        return _failed_probe(source_url, "timeout", str(exc))
    except requests.exceptions.SSLError as exc:
        return _failed_probe(source_url, "ssl_error", str(exc))
    except requests.exceptions.InvalidURL as exc:
        return _failed_probe(source_url, "invalid_url", str(exc))
    except requests.exceptions.ConnectionError as exc:
        detail = str(exc)
        error_type = "dns_error" if _looks_like_dns_error(detail) else "connection_error"
        return _failed_probe(source_url, error_type, detail)
    except requests.RequestException as exc:
        return _failed_probe(source_url, "request_error", str(exc))

    with response:
        return _download_response(source_url, settings, response, destination)


def _download_response(
    source_url: str,
    settings: Settings,
    response: Response,
    destination: Path | None,
) -> ImageDownloadProbe:
    status_code = response.status_code
    final_url = response.url
    content_type = response.headers.get("Content-Type")
    content_length = _int_header(response.headers.get("Content-Length"))
    base = {
        "source_url": source_url,
        "status_code": status_code,
        "final_url": final_url,
        "content_type": content_type,
        "content_length": content_length,
    }

    if status_code != 200:
        return ImageDownloadProbe(
            **base,
            ok=False,
            error_type=_http_error_type(status_code),
            error_detail=f"HTTP {status_code} while downloading image",
        )

    if _is_html_content_type(content_type):
        return ImageDownloadProbe(
            **base,
            ok=False,
            error_type="html_response",
            error_detail=f"Expected image but received Content-Type={content_type}",
        )

    if _is_definitely_non_image_content_type(content_type):
        return ImageDownloadProbe(
            **base,
            ok=False,
            error_type="non_image_content_type",
            error_detail=f"Expected image but received Content-Type={content_type}",
        )

    destination = destination or _diagnostic_destination(source_url, settings)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if content_type and destination.suffix == ".bin":
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            destination = destination.with_suffix(guessed)

    downloaded_bytes = 0
    first_bytes = b""
    try:
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                if not first_bytes:
                    first_bytes = chunk[:512]
                downloaded_bytes += len(chunk)
                handle.write(chunk)
    except OSError as exc:
        return ImageDownloadProbe(
            **base,
            ok=False,
            downloaded_bytes=downloaded_bytes,
            error_type="write_error",
            error_detail=str(exc),
        )

    if _looks_like_html(first_bytes):
        _delete_if_exists(destination)
        return ImageDownloadProbe(
            **base,
            ok=False,
            downloaded_bytes=downloaded_bytes,
            seems_image=False,
            error_type="html_response",
            error_detail="Downloaded body looks like HTML, not an image",
        )

    info = inspect_image(destination)
    if not info.valid:
        return ImageDownloadProbe(
            **base,
            ok=False,
            downloaded_bytes=downloaded_bytes,
            seems_image=_looks_like_image(first_bytes, content_type),
            valid_image=False,
            file_hash=info.file_hash,
            local_path=destination,
            error_type="invalid_image",
            error_detail=info.error,
        )

    return ImageDownloadProbe(
        **base,
        ok=True,
        downloaded_bytes=downloaded_bytes,
        seems_image=True,
        valid_image=True,
        width=info.width,
        height=info.height,
        file_hash=info.file_hash,
        local_path=destination,
    )


def _extension_from_url(source_url: str) -> str:
    suffix = Path(urlparse(source_url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return suffix
    return ".bin"


def _diagnostic_destination(source_url: str, settings: Settings) -> Path:
    extension = _extension_from_url(source_url)
    filename = "test-" + hashlib.sha1(source_url.encode("utf-8")).hexdigest() + extension
    return settings.images_dir / filename


def _failed_probe(source_url: str, error_type: str, error_detail: str) -> ImageDownloadProbe:
    return ImageDownloadProbe(source_url=source_url, ok=False, error_type=error_type, error_detail=error_detail)


def _http_error_type(status_code: int) -> str:
    if status_code == 403:
        return "forbidden"
    if status_code == 404:
        return "not_found"
    if 400 <= status_code < 500:
        return "client_error"
    if 500 <= status_code:
        return "server_error"
    return "http_error"


def _int_header(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _is_html_content_type(content_type: str | None) -> bool:
    return bool(content_type and "text/html" in content_type.lower())


def _is_definitely_non_image_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    mime = content_type.split(";")[0].strip().lower()
    if mime.startswith("image/"):
        return False
    return mime.startswith(("text/", "application/json", "application/xml", "text/xml"))


def _looks_like_html(data: bytes) -> bool:
    sample = data.lstrip().lower()
    return sample.startswith((b"<!doctype html", b"<html", b"<?xml"))


def _looks_like_image(data: bytes, content_type: str | None) -> bool:
    if content_type and content_type.lower().split(";")[0].strip().startswith("image/"):
        return True
    return data.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"RIFF", b"GIF87a", b"GIF89a"))


def _looks_like_dns_error(detail: str) -> bool:
    lowered = detail.lower()
    return "name or service not known" in lowered or "getaddrinfo failed" in lowered or "nameresolutionerror" in lowered


def _delete_if_exists(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
