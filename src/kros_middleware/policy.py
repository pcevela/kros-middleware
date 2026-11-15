"""Request policy: path canonicalisation and method/path allow rules.

Rules have the form ``METHOD /path/pattern``. Pattern segments:

* ``{id}``   -- a numeric identifier (1-19 digits)
* ``{word}`` -- a lowercase word such as an evidence name (``invoices``)
* ``*``      -- any single safe segment
* ``**``     -- any remainder, only as the last segment (use sparingly)

Everything else is a literal, matched case-sensitively like KROS paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ALLOWED_METHODS = ("GET", "POST")
SEND_PATH_RE = re.compile(r"^/api/invoices/(?P<id>[0-9]{1,19})/send$", re.IGNORECASE)
INT64_MAX = 2**63 - 1
# Only these characters may appear in a forwarded path; everything else
# (percent-encoding, backslashes, control characters, ';', etc.) is rejected
# so the proxy and KROS can never interpret the same path differently.
_SAFE_PATH_RE = re.compile(r"^(/[A-Za-z0-9._~-]+)+/?$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~-]+$")


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    method: str
    pattern: str
    regex: re.Pattern[str]

    def matches(self, method: str, path: str) -> bool:
        return method == self.method and self.regex.fullmatch(path) is not None


def compile_rule(text: str) -> Rule:
    parts = text.split()
    if len(parts) != 2:
        raise PolicyError(f"invalid rule {text!r}: expected 'METHOD /path'")
    method, pattern = parts[0].upper(), parts[1]
    if method not in ALLOWED_METHODS:
        raise PolicyError(f"invalid rule {text!r}: unsupported method {method}")
    if not pattern.startswith("/") or pattern.endswith("/") or "//" in pattern:
        raise PolicyError(f"invalid rule {text!r}: pattern must be an absolute path without empty segments")
    segments = pattern[1:].split("/")
    regex_parts: list[str] = []
    for index, segment in enumerate(segments):
        if segment == "**":
            if index != len(segments) - 1:
                raise PolicyError(f"invalid rule {text!r}: '**' is only allowed as the last segment")
            regex_parts.append("(?:/[^/]+)*")
        elif segment == "*":
            regex_parts.append("/[^/]+")
        elif segment == "{id}":
            regex_parts.append("/[0-9]{1,18}")
        elif segment == "{word}":
            regex_parts.append("/[a-z][a-z-]{0,40}")
        elif segment in (".", "..") or not _SEGMENT_RE.fullmatch(segment) or "{" in segment or "}" in segment:
            raise PolicyError(f"invalid rule {text!r}: bad segment {segment!r}")
        else:
            regex_parts.append("/" + re.escape(segment))
    return Rule(method=method, pattern=pattern, regex=re.compile("".join(regex_parts)))


def compile_rules(texts: list[str]) -> list[Rule]:
    return [compile_rule(t) for t in texts]


def canonical_path(raw_path: bytes) -> str:
    """Validate the raw request path and return it as text.

    Raises PolicyError for anything that is not a plain path of safe segments:
    encoded characters, dot segments, empty segments, backslashes, etc.
    A single trailing slash is removed.
    """
    try:
        path = raw_path.decode("ascii")
    except UnicodeDecodeError as exc:
        raise PolicyError("non-ASCII path") from exc
    if not _SAFE_PATH_RE.fullmatch(path):
        raise PolicyError("path contains disallowed characters or empty segments")
    segments = path.strip("/").split("/")
    if any(s in (".", "..") for s in segments):
        raise PolicyError("dot segments are not allowed")
    return "/" + "/".join(segments)


def is_allowed(rules: list[Rule], method: str, path: str) -> bool:
    return any(rule.matches(method, path) for rule in rules)


def send_invoice_id(method: str, path: str) -> int | None:
    """Invoice id if this is (any case variant of) the KROS send endpoint."""
    if method != "POST":
        return None
    match = SEND_PATH_RE.fullmatch(path)
    if not match:
        return None
    value = int(match.group("id"))
    if value > INT64_MAX:
        raise PolicyError("invoice id out of range")
    return value


def looks_like_send(path: str) -> bool:
    """Any path with a 'send' segment (any case) - such writes only go through the approval flow."""
    return any(segment.casefold() == "send" for segment in path.split("/"))
