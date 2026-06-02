"""WordPress REST API client using Application Passwords."""

from __future__ import annotations

import mimetypes
import time
from pathlib import Path
from typing import Any

import requests
from requests import Response
from requests.auth import HTTPBasicAuth

from .config import Settings


RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
CRITICAL_STATUS_CODES = {401, 403}


class WordPressError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None, critical: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload
        self.critical = critical


class WordPressClient:
    """Thin REST client with retry behavior tuned for migration batches."""

    def __init__(self, settings: Settings, timeout: int = 30):
        self.settings = settings
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(settings.wp_username, settings.wp_app_password)

    def get_api_index(self) -> dict[str, Any]:
        return self.request("GET", self.settings.wp_api_root)

    def verify_posts_endpoint(self) -> dict[str, Any]:
        return self.request("GET", f"{self.settings.wp_v2_root}/posts", params={"per_page": 1})

    def get_posts(self, **params: Any) -> list[dict[str, Any]]:
        return self.request("GET", f"{self.settings.wp_v2_root}/posts", params=params)

    def get_post(self, post_id: int, context: str = "edit") -> dict[str, Any]:
        return self.request("GET", f"{self.settings.wp_v2_root}/posts/{post_id}", params={"context": context})

    def create_post(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"{self.settings.wp_v2_root}/posts", json=payload)

    def list_categories(self, per_page: int = 100) -> list[dict[str, Any]]:
        categories: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self.request(
                "GET",
                f"{self.settings.wp_v2_root}/categories",
                params={"per_page": per_page, "page": page},
            )
            if not batch:
                return categories
            categories.extend(batch)
            if len(batch) < per_page:
                return categories
            page += 1

    def upload_media(self, file_path: Path, mime_type: str | None = None, title: str | None = None) -> dict[str, Any]:
        guessed_mime = mime_type or mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        headers = {
            "Content-Disposition": f'attachment; filename="{file_path.name}"',
            "Content-Type": guessed_mime,
        }
        params = {"title": title} if title else None
        with file_path.open("rb") as handle:
            return self.request(
                "POST",
                f"{self.settings.wp_v2_root}/media",
                headers=headers,
                data=handle,
                params=params,
            )

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        last_error: Exception | None = None
        attempts = max(1, self.settings.max_retries + 1)

        for attempt in range(1, attempts + 1):
            if self.settings.request_delay:
                time.sleep(self.settings.request_delay)
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
                return self._handle_response(response, method, url)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt >= attempts:
                    raise WordPressError(f"WordPress request failed after retries: {exc}") from exc
                time.sleep(self.settings.retry_backoff_seconds * attempt)
            except WordPressError as exc:
                last_error = exc
                if exc.critical or exc.status_code not in RETRY_STATUS_CODES or attempt >= attempts:
                    raise
                time.sleep(self.settings.retry_backoff_seconds * attempt)

        raise WordPressError(f"WordPress request failed: {last_error}")

    def _handle_response(self, response: Response, method: str, url: str) -> Any:
        payload = _json_or_text(response)
        status_code = response.status_code
        if 200 <= status_code < 300:
            return payload

        critical = status_code in CRITICAL_STATUS_CODES
        if status_code == 404 and "/wp-json" in url:
            critical = True
        message = f"{method} {url} returned HTTP {status_code}"
        if isinstance(payload, dict) and payload.get("message"):
            message += f": {payload['message']}"
        raise WordPressError(message, status_code=status_code, payload=payload, critical=critical)


def _json_or_text(response: Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text
