"""Logging configuration."""

import logging


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    # The telemetry adapters poll every second; request lines would drown everything else.
    logging.getLogger("httpx").setLevel(logging.WARNING)
