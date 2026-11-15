"""Run the middleware with uvicorn: ``python -m kros_middleware``."""

from __future__ import annotations

import logging
import re
import sys

import uvicorn

from .app import create_app
from .config import ConfigError, load_settings

_REDACT_PATTERNS = (
    (re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE), r"\1[redacted]"),
    (re.compile(r"(bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE), r"\1[redacted]"),
    (re.compile(r"(basic\s+)[A-Za-z0-9+/=]+", re.IGNORECASE), r"\1[redacted]"),
)


class RedactingFilter(logging.Filter):
    """Removes tokens from log records (e.g. approval links in access logs)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = (
                tuple(_redact(a) if isinstance(a, str) else a for a in record.args)
                if isinstance(record.args, tuple)
                else record.args
            )
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        return True


def _redact(text: str) -> str:
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    redactor = RedactingFilter()
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "kros_middleware", "httpx"):
        logger = logging.getLogger(name)
        logger.addFilter(redactor)
        for handler in logger.handlers:
            handler.addFilter(redactor)
    # httpx logs full request URLs at INFO; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host=settings.listen_host,
        port=settings.listen_port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        server_header=False,
        date_header=False,
        log_config=None,
        log_level=settings.log_level,
        timeout_graceful_shutdown=20,
    )
    # log_config=None: uvicorn's loggers propagate to the root handler, which carries the redaction filter.
    uvicorn.Server(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
