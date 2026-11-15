"""Notification texts.

Defaults are English; every message can be replaced through a JSON file given
in ``KM_MESSAGES_FILE``::

    {"approval_requested": {"title": "...", "body": "..."}, ...}

Templates use ``$name`` placeholders (``string.Template``), which cannot
reach object attributes, so untrusted values cannot be used to read internals.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template
from typing import Any

from .config import NOTIFY_EVENTS, ConfigError

DEFAULT_MESSAGES: dict[str, dict[str, str]] = {
    "approval_requested": {
        "title": "Approve sending invoice $number?",
        "body": (
            "Client $client asks to send invoice $number to $customer.\n"
            "Total: $total $currency (due $due_date)\n"
            "IBAN: $iban$iban_warning\n"
            "Recipients: $recipients\n"
            "E-invoice (Peppol): $einvoice\n"
            "Review before $expires_at: $link"
        ),
    },
    "approval_decided": {
        "title": "Invoice $number: $decision",
        "body": "Approval $approval_id for invoice $number ($customer) is now: $decision.\n$detail",
    },
    "upload_result_ok": {
        "title": "KROS processed request $request_id",
        "body": "$count document(s) processed without problems.",
    },
    "upload_result_problem": {
        "title": "KROS reported problems for request $request_id",
        "body": "Status $status. Problems:\n$problems",
    },
    "send_status_ok": {
        "title": "Invoice $number: $send_status via $channel",
        "body": "Invoice $number (id $invoice_id) reported $send_status on channel $channel at $occurred_at.",
    },
    "send_status_problem": {
        "title": "Invoice $number: $send_status via $channel",
        "body": "Invoice $number (id $invoice_id) reported $send_status on channel $channel at $occurred_at.\n"
        "Error code: $error_code\n$problems",
    },
    "signature_invalid": {
        "title": "Rejected webhook with an invalid signature",
        "body": "A request to the webhook endpoint from $remote failed signature verification. "
        "Further alerts are suppressed for $suppress_minutes minutes.",
    },
    "retention_run": {
        "title": "Retention purge finished",
        "body": "Removed: $counts",
    },
}

# Control characters, line/paragraph separators and bidirectional overrides.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029\u200e\u200f\u202a-\u202e\u2066-\u2069]")
MAX_VALUE_LENGTH = 300


def clean(value: Any) -> str:
    """Make an untrusted value safe to embed in a plain-text notification.

    Values are always flattened to one line, so data from an invoice can never
    add lines that look like part of the message (a fake IBAN line, a fake link).
    """
    text = "" if value is None else str(value)
    text = _CONTROL_RE.sub(" ", text)
    if len(text) > MAX_VALUE_LENGTH:
        text = text[: MAX_VALUE_LENGTH - 1] + "…"
    return text


class Messages:
    def __init__(self, overrides_file: str | None = None) -> None:
        self.templates = {k: dict(v) for k, v in DEFAULT_MESSAGES.items()}
        if overrides_file:
            try:
                data = json.loads(Path(overrides_file).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ConfigError(f"KM_MESSAGES_FILE: cannot load {overrides_file!r}: {exc}") from exc
            if not isinstance(data, dict):
                raise ConfigError("KM_MESSAGES_FILE must contain a JSON object")
            for event, parts in data.items():
                if event not in NOTIFY_EVENTS or not isinstance(parts, dict):
                    raise ConfigError(f"KM_MESSAGES_FILE: unknown event {event!r}")
                for key in ("title", "body"):
                    if key in parts:
                        if not isinstance(parts[key], str):
                            raise ConfigError(f"KM_MESSAGES_FILE: {event}.{key} must be a string")
                        self.templates[event][key] = parts[key]

    def render(self, event: str, values: dict[str, Any]) -> tuple[str, str]:
        safe = {k: clean(v) for k, v in values.items()}
        template = self.templates[event]
        title = Template(template["title"]).safe_substitute(safe)
        body = Template(template["body"]).safe_substitute(safe)
        # Titles end up in e-mail headers and ntfy headers: keep them on one line.
        return clean(" ".join(title.splitlines())), body
