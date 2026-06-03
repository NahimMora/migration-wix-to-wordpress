"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent


def _str(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _trim_url(value: str) -> str:
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    db_path: Path
    input_dir: Path
    output_dir: Path
    images_dir: Path
    logs_dir: Path
    log_file: Path
    wp_base_url: str
    wp_username: str
    wp_app_password: str
    local_site_url: str
    production_site_url: str
    production_domain: str
    default_post_status: str
    default_author_id: int
    default_category_id: int
    allow_wordpress_writes: bool
    batch_size: int
    request_delay: float
    max_retries: int
    retry_backoff_seconds: float
    image_download_timeout: float
    image_download_user_agent: str
    create_post_if_image_fails: bool
    normalize_images: bool
    normalize_only_if_needed: bool
    recompress_all_images: bool
    max_image_width: int
    max_image_filesize_kb: int
    jpeg_quality: int
    keep_original_images: bool
    permalink_structure: str

    @property
    def wp_api_root(self) -> str:
        return f"{self.wp_base_url}/wp-json"

    @property
    def wp_v2_root(self) -> str:
        return f"{self.wp_api_root}/wp/v2"

    def ensure_runtime_dirs(self) -> None:
        for path in (self.db_path.parent, self.output_dir, self.images_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_settings(env_file: Path | None = None) -> Settings:
    """Load `.env` if present and return typed settings."""

    env_path = env_file or ROOT_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    settings = Settings(
        root_dir=ROOT_DIR,
        db_path=ROOT_DIR / "db" / "migration.sqlite",
        input_dir=ROOT_DIR / "data" / "input",
        output_dir=ROOT_DIR / "data" / "output",
        images_dir=ROOT_DIR / "data" / "images",
        logs_dir=ROOT_DIR / "logs",
        log_file=ROOT_DIR / "logs" / "migration.log",
        wp_base_url=_trim_url(_str("WP_BASE_URL", "http://holasalta.local")),
        wp_username=_str("WP_USERNAME", "admin"),
        wp_app_password=_str("WP_APP_PASSWORD", ""),
        local_site_url=_trim_url(_str("LOCAL_SITE_URL", "http://holasalta.local")),
        production_site_url=_trim_url(_str("PRODUCTION_SITE_URL", "https://holasalta.com")),
        production_domain=_trim_url(_str("PRODUCTION_DOMAIN", "https://holasalta.com")),
        default_post_status=_str("DEFAULT_POST_STATUS", "draft"),
        default_author_id=_int("DEFAULT_AUTHOR_ID", 1),
        default_category_id=_int("DEFAULT_CATEGORY_ID", 1),
        allow_wordpress_writes=_bool("ALLOW_WORDPRESS_WRITES", False),
        batch_size=_int("BATCH_SIZE", 100),
        request_delay=_float("REQUEST_DELAY", 0.2),
        max_retries=_int("MAX_RETRIES", 3),
        retry_backoff_seconds=_float("RETRY_BACKOFF_SECONDS", 2),
        image_download_timeout=_float("IMAGE_DOWNLOAD_TIMEOUT", 60),
        image_download_user_agent=_str(
            "IMAGE_DOWNLOAD_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
        ),
        create_post_if_image_fails=_bool("CREATE_POST_IF_IMAGE_FAILS", True),
        normalize_images=_bool("NORMALIZE_IMAGES", True),
        normalize_only_if_needed=_bool("NORMALIZE_ONLY_IF_NEEDED", True),
        recompress_all_images=_bool("RECOMPRESS_ALL_IMAGES", False),
        max_image_width=_int("MAX_IMAGE_WIDTH", 1400),
        max_image_filesize_kb=_int("MAX_IMAGE_FILESIZE_KB", 250),
        jpeg_quality=_int("JPEG_QUALITY", 82),
        keep_original_images=_bool("KEEP_ORIGINAL_IMAGES", False),
        permalink_structure=_str("PERMALINK_STRUCTURE", "/post/%postname%/"),
    )
    settings.ensure_runtime_dirs()
    return settings


def missing_wp_credentials(settings: Settings) -> Iterable[str]:
    if not settings.wp_base_url:
        yield "WP_BASE_URL"
    if not settings.wp_username:
        yield "WP_USERNAME"
    if not settings.wp_app_password or settings.wp_app_password.startswith("xxxx"):
        yield "WP_APP_PASSWORD"
