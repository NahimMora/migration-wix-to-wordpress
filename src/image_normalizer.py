"""Image inspection, hashing and conditional normalization."""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from .config import Settings


SUPPORTED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}


@dataclass(frozen=True)
class ImageInfo:
    path: Path
    file_hash: str | None
    file_size_kb: int
    width: int | None
    height: int | None
    mime_type: str | None
    valid: bool
    error: str | None = None


def hash_file(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_image(file_path: Path) -> ImageInfo:
    size_kb = max(1, round(file_path.stat().st_size / 1024))
    file_hash = hash_file(file_path)
    try:
        with Image.open(file_path) as image:
            image.verify()
        with Image.open(file_path) as image:
            width, height = image.size
            mime_type = Image.MIME.get(image.format) or mimetypes.guess_type(file_path.name)[0]
        return ImageInfo(
            path=file_path,
            file_hash=file_hash,
            file_size_kb=size_kb,
            width=width,
            height=height,
            mime_type=mime_type,
            valid=True,
        )
    except Exception as exc:
        return ImageInfo(
            path=file_path,
            file_hash=file_hash,
            file_size_kb=size_kb,
            width=None,
            height=None,
            mime_type=mimetypes.guess_type(file_path.name)[0],
            valid=False,
            error=str(exc),
        )


def normalization_reasons(info: ImageInfo, settings: Settings) -> list[str]:
    reasons: list[str] = []
    if settings.recompress_all_images:
        reasons.append("RECOMPRESS_ALL_IMAGES=true")
    if not info.valid:
        reasons.append("invalid_or_corrupt_image")
    if info.mime_type not in SUPPORTED_MIME_TYPES:
        reasons.append(f"unsupported_mime={info.mime_type}")
    if info.width and info.width > settings.max_image_width:
        reasons.append(f"width>{settings.max_image_width}")
    if info.file_size_kb > settings.max_image_filesize_kb:
        reasons.append(f"filesize_kb>{settings.max_image_filesize_kb}")
    return reasons


def normalize_image(file_path: Path, settings: Settings) -> Path:
    """Normalize only the copy used for upload; the original stays available."""

    with Image.open(file_path) as image:
        image = ImageOps.exif_transpose(image)
        if image.width > settings.max_image_width:
            ratio = settings.max_image_width / image.width
            new_size = (settings.max_image_width, max(1, round(image.height * ratio)))
            image = image.resize(new_size, Image.Resampling.LANCZOS)

        output_path = file_path.with_name(f"{file_path.stem}.normalized.jpg")
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        image.save(output_path, format="JPEG", quality=settings.jpeg_quality, optimize=True)
        return output_path
