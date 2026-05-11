"""
Centralized logging configuration.
All modules import get_logger() from here so log format is consistent.
"""

import logging
import sys
from typing import Optional


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    Return a logger with a consistent format.
    Call this at the top of every module:
        logger = get_logger(__name__)
    """
    logger = logging.getLogger(name or "wechat_collection")

    # Avoid adding duplicate handlers if the logger already exists
    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # Level is set by config; default to INFO here so the logger is usable
    # even before config is fully loaded.
    logger.setLevel(logging.INFO)

    return logger
