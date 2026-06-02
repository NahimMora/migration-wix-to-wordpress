"""Logging setup for console and file output."""

from __future__ import annotations

import logging
import sys
from pathlib import Path


LOGGER_NAME = "wix_to_wordpress_migrator"


def setup_logging(log_file: Path, verbose: bool = True) -> logging.Logger:
    """Configure a reusable logger without duplicating handlers."""

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
