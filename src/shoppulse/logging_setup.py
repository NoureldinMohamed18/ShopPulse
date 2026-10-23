"""Logging: human-readable console output + a rotating file for later debugging."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_HANDLER_TAG = "_shoppulse_handler"


def configure_logging(logs_dir: Path | None = None, level: str = "INFO") -> None:
    """Idempotent: calling it twice does not duplicate handlers."""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(_FORMAT)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    setattr(console, _HANDLER_TAG, True)
    root.addHandler(console)

    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            logs_dir / "shoppulse.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        setattr(file_handler, _HANDLER_TAG, True)
        root.addHandler(file_handler)

    # keep third-party libraries quiet
    for noisy in ("urllib3", "matplotlib", "PIL", "werkzeug"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
