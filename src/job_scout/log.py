"""Logging to stderr in a format that reads well in a terminal and in journald."""

from __future__ import annotations

import logging

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level, format=FORMAT, datefmt="%Y-%m-%dT%H:%M:%S%z", force=True)
    # SQL echo is opt-in via sqlalchemy's own logger; keep httpx request lines at DEBUG.
    numeric = logging.getLevelNamesMapping()[level]
    logging.getLogger("httpx").setLevel(max(numeric, logging.WARNING))
