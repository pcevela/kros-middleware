"""Run the middleware with uvicorn: ``python -m kros_middleware``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys

import uvicorn

from .app import create_app
from .config import ConfigError, load_settings
from .db import Database, iso, utcnow

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


def uploads_command(settings, args: argparse.Namespace) -> int:
    """List uploads without a KROS result, or resolve one after checking the invoice in KROS."""

    async def run() -> int:
        db = Database(settings.db_path)
        db.open()
        try:
            if args.action == "list":
                rows = await db.list_open_uploads()
                if not rows:
                    print("no open uploads")
                for r in rows:
                    print(
                        f"{r['id']}\t{r['created_at']}\t{(r['doc_sequence'] or '-')} {r['doc_number']}\t"
                        f"requestId={r['request_id'] or '-'}"
                    )
                return 0
            if not await db.resolve_upload(args.id, iso(utcnow())):
                print(f"upload {args.id} is not open", file=sys.stderr)
                return 1
            await db.audit(
                {
                    "at": iso(utcnow()),
                    "client": "operator",
                    "method": "CLI",
                    "path": "uploads resolve",
                    "query": None,
                    "status": None,
                    "request_id": None,
                    "body_sha256": None,
                    "body": None,
                    "note": f"upload {args.id} resolved by hand: {args.reason[:500]}",
                }
            )
            print(f"upload {args.id} resolved")
            return 0
        finally:
            db.close()

    return asyncio.run(run())


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kros_middleware")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the server (default)")
    uploads = sub.add_parser("uploads", help="uploads that have no result from KROS yet")
    actions = uploads.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="list open uploads")
    resolve = actions.add_parser("resolve", help="close an open upload after checking the invoice in KROS")
    resolve.add_argument("id", type=int)
    resolve.add_argument("--reason", required=True, help="what you checked, recorded in the audit log")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.command == "uploads":
        return uploads_command(settings, args)
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
