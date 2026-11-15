"""ASGI application: restricting proxy, webhook receiver, results API and approval pages."""

from __future__ import annotations

import asyncio
import base64
import binascii
import collections
import contextlib
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import sqlite3
import time
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import approvals as ap
from .config import ClientToken, Settings, load_settings
from .db import Database, iso, utcnow
from .messages import Messages
from .notify import Notifier
from .policy import PolicyError, canonical_path, compile_rules, is_allowed, looks_like_send, send_invoice_id
from .signature import SIGNATURE_HEADER, verify_signature
from .upstream import Upstream
from .web import load_labels, load_template, render_page

log = logging.getLogger("kros_middleware")

MAX_JSON_DEPTH = 32
_QUERY_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
}
PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
OK_SEND_STATES = {"sent", "confirmed"}


# ---------------------------------------------------------------------------- helpers
class HTTPError(Exception):
    def __init__(self, status: int, code: str, detail: str | None = None, extra: dict[str, Any] | None = None):
        super().__init__(code)
        self.status, self.code, self.detail, self.extra = status, code, detail, extra or {}


def error_response(status: int, code: str, detail: str | None = None, /, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"errorCode": code}
    if detail:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(body, status_code=status, headers=SECURITY_HEADERS)


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate keys, including keys that differ only in case or Unicode form.

    The upstream (.NET) binds property names case-insensitively, so ``DocumentNumber``
    and ``documentNumber`` would mean the same field there.
    """
    result: dict[str, Any] = {}
    folded: set[str] = set()
    for key, value in pairs:
        norm = unicodedata.normalize("NFKC", key).casefold()
        if norm in folded:
            raise ValueError(f"duplicate key {key[:40]!r}")
        folded.add(norm)
        result[key] = value
    return result


def get_ci(doc: dict[str, Any], name: str) -> Any:
    """Case-insensitive property lookup (keys are unique after folding)."""
    target = name.casefold()
    for key, value in doc.items():
        if unicodedata.normalize("NFKC", key).casefold() == target:
            return value
    return None


def _depth(value: Any, level: int = 0) -> int:
    if level > MAX_JSON_DEPTH:
        return level
    if isinstance(value, dict):
        return max([_depth(v, level + 1) for v in value.values()] or [level + 1])
    if isinstance(value, list):
        return max([_depth(v, level + 1) for v in value] or [level + 1])
    return level


def parse_json_strict(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPError(400, "invalidJson", "body is not valid UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_no_constants,
            parse_float=_safe_float,
            parse_int=_safe_int,
        )
    except (ValueError, RecursionError) as exc:
        raise HTTPError(400, "invalidJson", str(exc)[:200]) from exc
    if _depth(value) > MAX_JSON_DEPTH:
        raise HTTPError(400, "invalidJson", "JSON nested too deeply")
    return value


def _safe_float(text: str) -> float:
    """Reject numbers that would not survive re-serialisation unchanged in meaning."""
    value = float(text)
    mantissa = text.lstrip("-").split("e")[0].split("E")[0]
    if "." in mantissa:
        mantissa = mantissa.rstrip("0")
    digits = mantissa.replace(".", "").lstrip("0")
    if not math.isfinite(value) or len(digits) > 15:
        raise ValueError(f"number {text[:40]} is out of range or too precise")
    return value


def _safe_int(text: str) -> int:
    value = int(text)
    if not -(2**63) <= value < 2**63:
        raise ValueError("integer out of the 64-bit range")
    return value


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not allowed")


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


async def read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise HTTPError(413, "bodyTooLarge")
        except ValueError as exc:
            raise HTTPError(400, "badContentLength") from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPError(413, "bodyTooLarge")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_query(raw: bytes) -> list[tuple[str, str]]:
    if not raw:
        return []
    try:
        text = raw.decode("ascii")
        pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True, max_num_fields=50)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPError(400, "invalidQuery") from exc
    seen: set[str] = set()
    for key, value in pairs:
        if not _QUERY_KEY_RE.fullmatch(key) or key.lower() in seen:
            raise HTTPError(400, "invalidQuery", "invalid or duplicate query parameter")
        if len(value) > 500 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise HTTPError(400, "invalidQuery", "invalid query value")
        seen.add(key.lower())
    return pairs


class FailureLimiter:
    """Counts authentication failures per remote address in a sliding one-minute window."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.hits: dict[str, collections.deque[float]] = {}

    def blocked(self, key: str) -> bool:
        window = self.hits.get(key)
        if not window:
            return False
        cutoff = time.monotonic() - 60
        while window and window[0] < cutoff:
            window.popleft()
        if not window:
            self.hits.pop(key, None)
            return False
        return len(window) >= self.per_minute

    def fail(self, key: str) -> None:
        self.hits.setdefault(key, collections.deque()).append(time.monotonic())
        if len(self.hits) > 10_000:  # bound memory under a distributed flood
            self.hits.pop(next(iter(self.hits)))


def remote_addr(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------- application state
class Core:
    def __init__(self, settings: Settings, db: Database, upstream: Upstream, notifier: Notifier) -> None:
        self.s = settings
        self.db = db
        self.upstream = upstream
        self.notifier = notifier
        self.allow_rules = compile_rules(settings.allow_rules)
        self.issue_rules = compile_rules(settings.issue_rules)
        # Separate realms: a client hammering bad tokens must not lock out the human approver.
        self.limiter = FailureLimiter(settings.auth_failures_per_minute)
        self.approver_limiter = FailureLimiter(settings.auth_failures_per_minute)
        # Bound concurrent JSON parsing of large bodies.
        self.body_slots = asyncio.Semaphore(4)
        self.template = load_template(settings.template_dir)
        self.labels = load_labels(settings.template_dir)
        self._retention_task: asyncio.Task | None = None

    # -------------------------------------------------------------- authentication
    def authenticate(self, request: Request) -> ClientToken:
        addr = remote_addr(request)
        if self.limiter.blocked(addr):
            raise HTTPError(429, "tooManyAuthFailures")
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            self.limiter.fail(addr)
            raise HTTPError(401, "unauthorized")
        digest = hashlib.sha256(token.strip().encode("utf-8")).digest()
        found: ClientToken | None = None
        for client in self.s.clients:  # compare against all, constant time each
            if hmac.compare_digest(digest, client.token_sha256):
                found = client
        if found is None:
            self.limiter.fail(addr)
            raise HTTPError(401, "unauthorized")
        return found

    def check_approver(self, request: Request) -> str:
        """Second factor for approval pages: HTTP Basic credentials the agent never has."""
        if self.s.approval_auth == "none":
            return "link"
        addr = remote_addr(request)
        if self.approver_limiter.blocked(addr):
            raise HTTPError(429, "tooManyAuthFailures")
        header = request.headers.get("authorization", "")
        scheme, _, encoded = header.partition(" ")
        user = password = ""
        if scheme.lower() == "basic":
            try:
                user, _, password = base64.b64decode(encoded.strip(), validate=True).decode("utf-8").partition(":")
            except (binascii.Error, UnicodeDecodeError):
                user = password = ""
        digest = hashlib.sha256(password.encode("utf-8")).digest()
        ok_user = None
        for cred in self.s.approver_credentials:
            if hmac.compare_digest(user.encode(), cred.username.encode()) & hmac.compare_digest(
                digest, cred.password_sha256
            ):
                ok_user = cred.username
        if ok_user is None:
            if header:
                self.approver_limiter.fail(addr)
            raise HTTPError(401, "approverAuthRequired")
        return ok_user

    def same_origin(self, request: Request) -> bool:
        """Reject cross-site form posts when the browser tells us the origin."""
        origin = request.headers.get("origin")
        if not origin or origin == "null":
            return origin is None
        mine = urlsplit(self.s.public_base_url)
        theirs = urlsplit(origin)
        return (theirs.scheme, theirs.netloc) == (mine.scheme, mine.netloc)

    # -------------------------------------------------------------- audit
    async def audit(
        self,
        client: str,
        method: str,
        path: str,
        query: list[tuple[str, str]],
        status: int | None,
        request_id: str | None,
        body: bytes | None,
        note: str | None,
    ) -> None:
        await self.db.audit(
            {
                "at": iso(utcnow()),
                "client": client,
                "method": method,
                "path": path,
                "query": "&".join(f"{k}={v}" for k, v in query) or None,
                "status": status,
                "request_id": request_id,
                "body_sha256": hashlib.sha256(body).hexdigest() if body else None,
                "body": body.decode("utf-8", "replace") if (body and self.s.audit_store_bodies) else None,
                "note": note,
            }
        )

    # -------------------------------------------------------------- proxy
    async def proxy(self, request: Request) -> Response:
        method = request.method
        if method not in ("GET", "POST"):
            raise HTTPError(405, "methodNotAllowed")
        client = self.authenticate(request)
        try:
            path = canonical_path(request.scope.get("raw_path") or request.url.path.encode())
        except PolicyError as exc:
            raise HTTPError(400, "invalidPath", str(exc)) from exc
        prefix = self.s.proxy_prefix
        if not path.startswith(prefix + "/"):
            raise HTTPError(404, "notFound")
        upstream_path = "/api" + path[len(prefix) :]
        query = parse_query(request.scope.get("query_string", b""))

        if method == "GET":
            if "read" not in client.scopes:
                raise HTTPError(403, "scopeRequired", "read")
            if not is_allowed(self.allow_rules, method, upstream_path):
                raise HTTPError(403, "routeNotAllowed")
            return await self.forward(client, method, upstream_path, query, None, request)

        # POST
        try:
            invoice_id = send_invoice_id(method, upstream_path)
        except PolicyError as exc:
            raise HTTPError(400, "invalidPath", str(exc)) from exc
        if invoice_id is None and looks_like_send(upstream_path):
            # Proforma or other send-like endpoints: never reachable through the proxy.
            raise HTTPError(403, "routeNotAllowed", "send-like endpoints only go through the approval flow")
        if invoice_id is not None:
            if "send" not in client.scopes:
                raise HTTPError(403, "scopeRequired", "send")
            if query:
                raise HTTPError(400, "invalidQuery", "send takes no query parameters")
            raw = await read_body(request, 64 * 1024)
            body = self.json_body(request, raw, allow_empty=True)
            return await self.handle_send(client, invoice_id, body)

        issuing = is_allowed(self.issue_rules, method, upstream_path)
        if issuing:
            if not self.s.issue_enabled:
                await self.audit(client.name, method, upstream_path, query, 403, None, None, "issuing disabled")
                raise HTTPError(403, "issuingDisabled")
        elif not is_allowed(self.allow_rules, method, upstream_path):
            raise HTTPError(403, "routeNotAllowed")
        if "issue" not in client.scopes:
            raise HTTPError(403, "scopeRequired", "issue")

        async with self.body_slots:
            raw = await read_body(request, self.s.max_body_bytes)
            self.require_json(request)
            body = await asyncio.to_thread(parse_json_strict, raw) if raw.strip() else None
        if body is None:
            raise HTTPError(400, "bodyRequired")
        if self.s.send_mode != "auto" and any(k.casefold() == "options" for k in _walk_keys(body)):
            await self.audit(client.name, method, upstream_path, query, 403, None, raw, "options rejected")
            raise HTTPError(
                403, "sendRequiresApproval", "'options' (e.g. options.send) is not accepted; request a send separately"
            )
        if issuing:
            allowed_keys = {"data", "options"} if self.s.send_mode == "auto" else {"data"}
            data = body.get("data") if isinstance(body, dict) else None
            if (
                not isinstance(body, dict)
                or not set(body) <= allowed_keys
                or not (isinstance(data, dict) or (isinstance(data, list) and all(isinstance(d, dict) for d in data)))
            ):
                raise HTTPError(400, "invalidBodyShape", 'expected {"data": object} or {"data": [objects]}')
        await self.check_frozen(body)
        payload = await asyncio.to_thread(_serialise, body)
        return await self.forward(client, method, upstream_path, query, payload, request)

    def json_body(self, request: Request, raw: bytes, allow_empty: bool) -> Any:
        if not raw.strip():
            if allow_empty:
                return None
            raise HTTPError(400, "bodyRequired")
        self.require_json(request)
        return parse_json_strict(raw)

    @staticmethod
    def require_json(request: Request) -> None:
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise HTTPError(415, "jsonRequired")

    async def check_frozen(self, body: Any) -> None:
        """Refuse to overwrite an invoice that is under approval or has been submitted for sending."""
        frozen = {ap.document_key(seq, num) for seq, num in await self.db.frozen_documents()}
        frozen.discard(None)
        if not frozen:
            return
        data = body.get("data") if isinstance(body, dict) else None
        for doc in data if isinstance(data, list) else [data]:
            number_value = get_ci(doc, "documentNumber") if isinstance(doc, dict) else None
            if number_value in (None, ""):
                continue  # no number: KROS assigns a new one, nothing is overwritten
            key = ap.document_key(get_ci(doc, "numberingSequence"), number_value)
            if key is None:
                raise HTTPError(400, "invalidDocumentNumber")
            sequence, number = key
            for frozen_sequence, frozen_number in frozen:
                # Conservative: an unknown sequence on either side counts as a match.
                if frozen_number == number and (not sequence or not frozen_sequence or sequence == frozen_sequence):
                    raise HTTPError(
                        409,
                        "invoiceFrozen",
                        f"document {str(number_value)[:40]} is awaiting approval or has been sent",
                    )

    async def forward(
        self,
        client: ClientToken,
        method: str,
        path: str,
        query: list[tuple[str, str]],
        payload: bytes | None,
        request: Request,
    ) -> Response:
        accept = request.headers.get("accept")
        if accept and (len(accept) > 200 or any(ord(c) < 32 for c in accept)):
            accept = None
        try:
            upstream = await self.upstream.request(method, path, query, payload, accept)
        except httpx.HTTPError as exc:
            if method != "GET":
                await self.audit(
                    client.name, method, path, query, None, None, payload, f"upstream error {type(exc).__name__}"
                )
            raise HTTPError(502, "upstreamUnavailable", type(exc).__name__) from exc
        content = upstream.content
        if method != "GET":
            request_id = None
            if upstream.status_code == 202:
                with contextlib.suppress(ValueError, AttributeError):
                    rid = upstream.json().get("requestId")
                    request_id = str(rid) if rid else None
            await self.audit(client.name, method, path, query, upstream.status_code, request_id, payload, None)
        headers = {k: v for k, v in upstream.headers.items() if k.lower() in Upstream.PASS_HEADERS}
        headers.update(SECURITY_HEADERS)
        return Response(content, status_code=upstream.status_code, headers=headers)

    # -------------------------------------------------------------- sending
    async def handle_send(self, client: ClientToken, invoice_id: int, body: Any) -> Response:
        path = f"/api/invoices/{invoice_id}/send"
        if self.s.send_mode == "off":
            await self.audit(client.name, "POST", path, [], 403, None, None, "sending disabled")
            raise HTTPError(403, "sendingDisabled")
        try:
            send_body = ap.validate_send_body(body)
        except ap.SendBodyError as exc:
            raise HTTPError(400, "invalidSendBody", str(exc)) from exc
        payload = json.dumps(send_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        if self.s.send_mode == "auto":
            try:
                upstream = await self.upstream.request("POST", path, None, payload)
            except httpx.HTTPError as exc:
                await self.audit(client.name, "POST", path, [], None, None, payload, "send outcome unknown")
                raise HTTPError(
                    502, "sendOutcomeUnknown", "do not retry blindly; check the send-status results first"
                ) from exc
            await self.audit(client.name, "POST", path, [], upstream.status_code, None, payload, "auto send")
            headers = {k: v for k, v in upstream.headers.items() if k.lower() in Upstream.PASS_HEADERS}
            headers.update(SECURITY_HEADERS)
            return Response(upstream.content, status_code=upstream.status_code, headers=headers)

        # approval mode
        now = utcnow()
        for expired in await self.db.expire_approvals(iso(now)):
            self.notify_decided(expired, "expired", "")
        existing = await self.db.open_approval_for_invoice(invoice_id, iso(now))
        if existing:
            raise HTTPError(
                409, "approvalAlreadyOpen", extra={"approvalId": existing["id"], "status": existing["status"]}
            )
        if await self.db.count_pending(iso(now)) >= self.s.approval_max_pending:
            raise HTTPError(429, "tooManyPendingApprovals")

        invoice = await self.fetch_invoice(invoice_id)
        summary = ap.summary(invoice, send_body, self.s.allowed_ibans)
        summary["previously_sent"] = await self.db.previous_send(invoice_id)
        if summary["iban_allowed"] is False:
            await self.audit(client.name, "POST", path, [], 422, None, payload, "iban not allowed")
            raise HTTPError(422, "ibanNotAllowed", "the invoice IBAN is not in KM_ALLOWED_IBANS")
        snap = ap.snapshot(invoice, self.s.approval_hash_fields)
        token = secrets.token_urlsafe(32)
        approval_id = str(uuid.uuid4())
        expires = now + timedelta(hours=self.s.approval_ttl_hours)
        row = {
            "id": approval_id,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "invoice_id": invoice_id,
            "client": client.name,
            "request_body": payload.decode("utf-8"),
            "summary": json.dumps(summary, ensure_ascii=False),
            "snapshot": json.dumps(snap, ensure_ascii=False, sort_keys=True),
            "content_hash": ap.content_hash(snap, send_body),
            "document_number": None if invoice.get("documentNumber") is None else str(invoice["documentNumber"]),
            "numbering_sequence": invoice.get("numberingSequence") or "",
            "created_at": iso(now),
            "expires_at": iso(expires),
        }
        try:
            await self.db.create_approval(row)
        except sqlite3.IntegrityError as exc:
            raise HTTPError(409, "approvalAlreadyOpen") from exc
        await self.audit(client.name, "POST", path, [], 202, None, payload, f"approval {approval_id} requested")
        link = f"{self.s.public_base_url}{self.s.approvals_prefix}/{approval_id}?token={token}"
        self.notifier.fire(
            "approval_requested",
            {
                **summary,
                "client": client.name,
                "recipients": ", ".join(summary["recipients"]) or "(KROS default: partner e-mail)",
                "einvoice": "yes" if summary["einvoice"] else "no",
                "iban_warning": "",
                "previously_sent": "",
                "expires_at": iso(expires),
                "approval_id": approval_id,
                "link": link,
            },
            link=link,
        )
        return JSONResponse(
            {"approvalId": approval_id, "status": "pendingApproval", "expiresAt": iso(expires)},
            status_code=202,
            headers=SECURITY_HEADERS,
        )

    async def fetch_invoice(self, invoice_id: int) -> dict[str, Any]:
        try:
            response = await self.upstream.request("GET", f"/api/invoices/{invoice_id}")
        except httpx.HTTPError as exc:
            raise HTTPError(502, "upstreamUnavailable", type(exc).__name__) from exc
        if response.status_code == 404:
            raise HTTPError(404, "invoiceNotFound")
        if response.status_code != 200:
            raise HTTPError(502, "invoiceFetchFailed", f"KROS returned {response.status_code}")
        try:
            data = response.json().get("data")
        except (ValueError, AttributeError) as exc:
            raise HTTPError(502, "invoiceFetchFailed", "unexpected response") from exc
        if not isinstance(data, dict):
            raise HTTPError(502, "invoiceFetchFailed", "unexpected response")
        return data

    def notify_decided(self, approval: dict[str, Any], decision: str, detail: str) -> None:
        summary = json.loads(approval.get("summary") or "{}")
        self.notifier.fire(
            "approval_decided",
            {
                "approval_id": approval["id"],
                "number": summary.get("number", approval.get("invoice_id")),
                "customer": summary.get("customer", ""),
                "decision": decision,
                "detail": detail,
            },
        )

    # -------------------------------------------------------------- approval pages
    async def load_approval_for_page(self, request: Request, approval_id: str, token: str | None) -> dict[str, Any]:
        if not _UUID_RE.fullmatch(approval_id) or not token or len(token) > 100:
            raise HTTPError(404, "notFound")
        approval = await self.db.get_approval(approval_id)
        if approval is None or not hmac.compare_digest(
            hashlib.sha256(token.encode()).hexdigest(), approval["token_hash"]
        ):
            if self.s.approval_auth == "none":  # with Basic auth the approver is already authenticated
                self.approver_limiter.fail(remote_addr(request))
            raise HTTPError(404, "notFound")
        return approval

    def page(
        self,
        approval: dict[str, Any],
        token: str | None,
        message: str | None = None,
        actions: bool = False,
        status: int = 200,
    ) -> HTMLResponse:
        summary = json.loads(approval.get("summary") or "{}")
        html = render_page(
            self.template,
            self.labels,
            summary,
            approval,
            f"{self.s.approvals_prefix}/{approval['id']}" if actions else None,
            token if actions else None,
            message,
        )
        headers = dict(SECURITY_HEADERS)
        headers["Content-Security-Policy"] = PAGE_CSP
        return HTMLResponse(html, status_code=status, headers=headers)

    async def approval_page(self, request: Request) -> Response:
        approver = self.check_approver(request)
        del approver
        approval_id = request.path_params["approval_id"]
        token = request.query_params.get("token")
        approval = await self.load_approval_for_page(request, approval_id, token)
        if approval["status"] == "pending" and approval["expires_at"] <= iso(utcnow()):
            await self.db.expire_approvals(iso(utcnow()))
            approval = await self.db.get_approval(approval_id) or approval
        return self.page(approval, token, actions=approval["status"] == "pending")

    async def approval_action(self, request: Request) -> Response:
        approver = self.check_approver(request)
        if not self.same_origin(request):
            raise HTTPError(403, "crossOriginRejected")
        approval_id = request.path_params["approval_id"]
        action = request.path_params["action"]
        raw = await read_body(request, 4096)
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            raise HTTPError(415, "formRequired")
        try:
            form = dict(parse_qsl(raw.decode("ascii"), keep_blank_values=True, max_num_fields=5))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPError(400, "invalidForm") from exc
        token = form.get("token")
        approval = await self.load_approval_for_page(request, approval_id, token)
        now = iso(utcnow())

        if action == "reject":
            if await self.db.transition_approval(approval_id, "pending", "rejected", now, approver):
                approval = await self.db.get_approval(approval_id) or approval
                self.notify_decided(approval, "rejected", f"by {approver}")
            return self.page(await self.db.get_approval(approval_id) or approval, token)

        # approve
        if not await self.db.transition_approval(
            approval_id, "pending", "approved", now, approver, require_unexpired=True
        ):
            return self.page(await self.db.get_approval(approval_id) or approval, token, status=409)
        return await self.execute_send(approval, token, approver)

    async def execute_send(self, approval: dict[str, Any], token: str | None, approver: str) -> Response:
        approval_id = approval["id"]
        invoice_id = approval["invoice_id"]
        send_body = json.loads(approval["request_body"] or "{}")
        # Re-check the content: the invoice must still be exactly what the approver saw.
        try:
            invoice = await self.fetch_invoice(invoice_id)
        except HTTPError as exc:
            await self.db.finish_approval(approval_id, "approved", "failed", None, f"re-fetch failed: {exc.code}")
            approval = await self.db.get_approval(approval_id) or approval
            self.notify_decided(approval, "failed", f"could not re-check the invoice ({exc.code}); nothing was sent")
            return self.page(approval, token, status=502)
        current_hash = ap.content_hash(ap.snapshot(invoice, self.s.approval_hash_fields), send_body)
        if not hmac.compare_digest(current_hash, approval["content_hash"] or ""):
            await self.db.finish_approval(approval_id, "approved", "stale", None, "invoice changed after the request")
            approval = await self.db.get_approval(approval_id) or approval
            self.notify_decided(approval, "stale", "the invoice changed after approval was requested; nothing was sent")
            return self.page(approval, token, status=409)
        if not await self.db.transition_approval(approval_id, "approved", "sending", iso(utcnow())):
            return self.page(await self.db.get_approval(approval_id) or approval, token, status=409)

        payload = json.dumps(send_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        path = f"/api/invoices/{invoice_id}/send"
        try:
            upstream = await self.upstream.request("POST", path, None, payload)
        except httpx.HTTPError as exc:
            await self.db.finish_approval(
                approval_id, "sending", "unknown", None, f"transport error: {type(exc).__name__}"
            )
            await self.audit(
                approver, "POST", path, [], None, None, payload, f"approval {approval_id}: outcome unknown"
            )
            approval = await self.db.get_approval(approval_id) or approval
            self.notify_decided(approval, "unknown", "the send request failed in transit; check KROS before retrying")
            return self.page(approval, token, status=502)
        status = "submitted" if upstream.status_code == 202 else "failed"
        await self.db.finish_approval(approval_id, "sending", status, upstream.status_code, upstream.text[:4000])
        await self.audit(approver, "POST", path, [], upstream.status_code, None, payload, f"approval {approval_id}")
        approval = await self.db.get_approval(approval_id) or approval
        self.notify_decided(approval, status, f"KROS answered {upstream.status_code}: {upstream.text[:300]}")
        return self.page(approval, token)

    # -------------------------------------------------------------- webhooks
    async def webhook(self, request: Request) -> Response:
        raw = await read_body(request, self.s.webhook_max_body_bytes)
        if self.s.webhook_secrets:
            received = request.headers.get(SIGNATURE_HEADER)
            if not any(verify_signature(raw, received, secret) for secret in self.s.webhook_secrets):
                addr = remote_addr(request)
                log.warning("webhook signature verification failed from %s", addr)
                if not self.notifier.suppress("signature_invalid", 600):
                    self.notifier.fire("signature_invalid", {"remote": addr, "suppress_minutes": 10})
                return Response(status_code=401, headers=SECURITY_HEADERS)
        event = parse_json_strict(raw)
        if not isinstance(event, dict):
            raise HTTPError(400, "invalidPayload")
        row = extract_event(event, raw)
        if await self.db.insert_event(row):
            self.notify_event(event, row)
        return JSONResponse({"ok": True}, headers=SECURITY_HEADERS)

    def notify_event(self, event: dict[str, Any], row: dict[str, Any]) -> None:
        results = event.get("results") if isinstance(event.get("results"), dict) else {}
        entities = [e for e in (results.get("entities") or []) if isinstance(e, dict)]
        if row["entity_type"] == 4 and entities:
            data = entities[0].get("data") if isinstance(entities[0].get("data"), dict) else {}
            ok = (row["send_status"] or "") in OK_SEND_STATES
            self.notifier.fire(
                "send_status_ok" if ok else "send_status_problem",
                {
                    "number": f"{data.get('numberingSequence') or ''} {data.get('documentNumber') or ''}".strip(),
                    "invoice_id": row["invoice_id"],
                    "send_status": row["send_status"],
                    "channel": row["channel"],
                    "occurred_at": row["occurred_at"],
                    "error_code": data.get("errorCode") or "",
                    "problems": _problems_text(entities),
                },
            )
            return
        if not row["request_id"]:
            return  # change made by a user in the KROS app, not by us
        bad = row["top_status"] != 200 or any((e.get("status") or 0) >= 400 or e.get("problems") for e in entities)
        self.notifier.fire(
            "upload_result_problem" if bad else "upload_result_ok",
            {
                "request_id": row["request_id"],
                "status": row["top_status"],
                "count": len(entities),
                "problems": _problems_text(entities),
            },
        )

    # -------------------------------------------------------------- results API
    async def results(self, request: Request, kind: str) -> Response:
        client = self.authenticate(request)
        ident = request.path_params.get("ident", "")
        everything = "events" in client.scopes
        if not everything and "results" not in client.scopes:
            raise HTTPError(403, "scopeRequired", "results")

        if kind == "requests":
            if not _UUID_RE.fullmatch(ident.lower()):
                raise HTTPError(404, "notFound")
            if not everything and await self.db.request_owner(ident) != client.name:
                raise HTTPError(404, "notFound")
            return JSONResponse(
                {"requestId": ident, "events": await self.db.events_by_request(ident)}, headers=SECURITY_HEADERS
            )
        if kind == "send-status":
            if not ident.isdigit() or len(ident) > 19:
                raise HTTPError(404, "notFound")
            invoice_id = int(ident)
            if not everything and not await self.db.invoice_touched_by(invoice_id, client.name):
                raise HTTPError(404, "notFound")
            events = await self.db.send_status_events(invoice_id)
            latest: dict[str, Any] = {}
            for e in events:  # ordered by occurredAt: last one per channel wins
                latest[e["channel"] or ""] = {"sendStatus": e["send_status"], "occurredAt": e["occurred_at"]}
            return JSONResponse(
                {"invoiceId": invoice_id, "latestByChannel": latest, "events": events}, headers=SECURITY_HEADERS
            )
        if kind == "approvals":
            if ident:
                approval = await self.db.get_approval(ident) if _UUID_RE.fullmatch(ident) else None
                if approval is None or (not everything and approval["client"] != client.name):
                    raise HTTPError(404, "notFound")
                return JSONResponse(public_approval(approval), headers=SECURITY_HEADERS)
            rows = await self.db.list_approvals(None if everything else client.name, 100)
            return JSONResponse({"approvals": [public_approval(r) for r in rows]}, headers=SECURITY_HEADERS)
        if kind == "events":
            if not everything:
                raise HTTPError(403, "scopeRequired", "events")
            q = request.query_params
            since = _normalise_since(q.get("since"))
            entity = q.get("entityType")
            try:
                limit = min(int(q.get("limit") or 100), 500)
                after_id = int(q.get("afterId") or 0)
                entity_type = int(entity) if entity else None
            except ValueError as exc:
                raise HTTPError(400, "invalidQuery") from exc
            events = await self.db.events_since(since, entity_type, limit, after_id)
            return JSONResponse({"events": events}, headers=SECURITY_HEADERS)
        raise HTTPError(404, "notFound")

    # -------------------------------------------------------------- retention
    async def retention_loop(self) -> None:
        while True:
            try:
                await self.retention_once()
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("retention run failed")
            await asyncio.sleep(self.s.retention_interval_hours * 3600)

    async def retention_once(self) -> dict[str, int]:
        for expired in await self.db.expire_approvals(iso(utcnow())):
            self.notify_decided(expired, "expired", "")
        counts = await self.db.purge(self.s.retention_days)
        log.info("retention purge removed %s", counts)
        self.notifier.fire("retention_run", {"counts": json.dumps(counts)})
        return counts


def _normalise_since(value: str | None) -> str:
    """Parse a client-supplied timestamp and render it in the exact stored format."""
    if not value:
        return iso(utcnow() - timedelta(days=1))
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPError(400, "invalidQuery", "since must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return iso(parsed)


def _serialise(body: Any) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def public_approval(row: dict[str, Any]) -> dict[str, Any]:
    result_body: Any = row.get("result_body")
    with contextlib.suppress(TypeError, ValueError):
        result_body = json.loads(result_body)
    return {
        "approvalId": row["id"],
        "invoiceId": row["invoice_id"],
        "status": row["status"],
        "createdAt": row["created_at"],
        "expiresAt": row["expires_at"],
        "decidedAt": row.get("decided_at"),
        "resultStatus": row.get("result_status"),
        "result": result_body,
        "summary": json.loads(row.get("summary") or "{}"),
    }


def _problems_text(entities: list[dict[str, Any]]) -> str:
    lines = []
    for entity in entities[:20]:
        problems = entity.get("problems")
        if problems:
            lines.append(f"#{entity.get('index')}: {json.dumps(problems, ensure_ascii=False)[:300]}")
    return " | ".join(lines) or "-"


def extract_event(event: dict[str, Any], raw: bytes) -> dict[str, Any]:
    results = event.get("results") if isinstance(event.get("results"), dict) else {}
    entities = [e for e in (results.get("entities") or []) if isinstance(e, dict)] if isinstance(results, dict) else []
    entity_type = event.get("entityType") if isinstance(event.get("entityType"), int) else None
    event_id = invoice_id = send_status = channel = occurred_at = None
    if entity_type == 4 and entities:
        data = entities[0].get("data") if isinstance(entities[0].get("data"), dict) else {}
        event_id = str(data["eventId"]) if data.get("eventId") else None
        invoice_id = data.get("id") if isinstance(data.get("id"), int) else None
        send_status = str(data.get("sendStatus") or "") or None
        channel = str(data.get("channel") or "") or None
        occurred_at = str(data.get("occurredAt") or "") or None
    request_id = event.get("requestId")
    body_hash = hashlib.sha256(raw).hexdigest()
    return {
        "received_at": iso(utcnow()),
        "dedupe_key": f"event:{event_id}" if event_id else f"sha256:{body_hash}",
        "entity_type": entity_type,
        "company_id": event.get("companyId") if isinstance(event.get("companyId"), int) else None,
        "request_id": str(request_id) if request_id else None,
        "top_status": event.get("status") if isinstance(event.get("status"), int) else None,
        "event_id": event_id,
        "invoice_id": invoice_id,
        "send_status": send_status,
        "channel": channel,
        "occurred_at": occurred_at,
        "payload": raw.decode("utf-8"),
    }


# ---------------------------------------------------------------------------- Starlette wiring
def create_app(
    settings: Settings | None = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    notify_http: httpx.AsyncClient | None = None,
    start_background: bool = True,
) -> Starlette:
    settings = settings or load_settings()
    db = Database(settings.db_path)
    upstream = Upstream(settings, transport=upstream_transport)
    notifier = Notifier(settings, Messages(settings.messages_file), http=notify_http)
    core = Core(settings, db, upstream, notifier)

    def guard(handler):
        async def endpoint(request: Request) -> Response:
            try:
                return await handler(request)
            except HTTPError as exc:
                if exc.code == "approverAuthRequired":
                    return Response(
                        status_code=401,
                        headers={**SECURITY_HEADERS, "WWW-Authenticate": 'Basic realm="approvals", charset="UTF-8"'},
                    )
                return error_response(exc.status, exc.code, exc.detail, **exc.extra)

        return endpoint

    async def health(request: Request) -> Response:
        ok = await db.ping()
        return JSONResponse(
            {"status": "ok" if ok else "degraded"}, status_code=200 if ok else 503, headers=SECURITY_HEADERS
        )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        db.open()
        interrupted = await db.mark_interrupted_sends()
        if interrupted:
            log.warning("%d send(s) were interrupted by a restart; their outcome is unknown", interrupted)
        if start_background:
            core._retention_task = asyncio.create_task(core.retention_loop())
        try:
            yield
        finally:
            if core._retention_task:
                core._retention_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await core._retention_task
            await notifier.drain()
            await upstream.aclose()
            db.close()

    def results_for(kind: str):
        async def endpoint(request: Request) -> Response:
            return await core.results(request, kind)

        return endpoint

    s = settings
    routes = [
        Route("/healthz", health, methods=["GET"]),
        Route(s.webhook_path, guard(core.webhook), methods=["POST"]),
        Route(s.proxy_prefix + "/{path:path}", guard(core.proxy), methods=["GET", "POST"]),
        Route(s.results_prefix + "/requests/{ident}", guard(results_for("requests")), methods=["GET"]),
        Route(s.results_prefix + "/invoices/{ident}/send-status", guard(results_for("send-status")), methods=["GET"]),
        Route(s.results_prefix + "/approvals", guard(results_for("approvals")), methods=["GET"]),
        Route(s.results_prefix + "/approvals/{ident}", guard(results_for("approvals")), methods=["GET"]),
        Route(s.results_prefix + "/events", guard(results_for("events")), methods=["GET"]),
        Route(s.approvals_prefix + "/{approval_id}", guard(core.approval_page), methods=["GET"]),
        Route(
            s.approvals_prefix + "/{approval_id}/{action:str}",
            guard(_only_actions(core.approval_action)),
            methods=["POST"],
        ),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.router.redirect_slashes = False  # never build redirects from the Host header
    app.state.core = core
    return app


def _only_actions(handler):
    async def endpoint(request: Request) -> Response:
        if request.path_params.get("action") not in ("approve", "reject"):
            raise HTTPError(404, "notFound")
        return await handler(request)

    return endpoint
