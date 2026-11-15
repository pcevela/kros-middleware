"""KROS webhook signature: Base64(HMAC-SHA256(key=UTF-16LE(secret), msg=UTF-16LE(body)))."""

from __future__ import annotations

import base64
import hashlib
import hmac

SIGNATURE_HEADER = "x-kros-signature-256"


def compute_signature(body: str, secret: str) -> str:
    """Return the signature KROS would send for ``body``.

    Both the key and the payload are encoded as UTF-16LE (no BOM), exactly as
    ``Encoding.Unicode`` in the KROS reference C# implementation.
    """
    digest = hmac.new(secret.encode("utf-16-le"), body.encode("utf-16-le"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_signature(raw_body: bytes, received: str | None, secret: str) -> bool:
    """Constant-time verification of a received signature against the raw request body."""
    if not received:
        return False
    try:
        body = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    try:
        received_bytes = base64.b64decode(received.strip(), validate=True)
    except (ValueError, TypeError):
        return False
    expected_bytes = base64.b64decode(compute_signature(body, secret))
    return hmac.compare_digest(expected_bytes, received_bytes)
