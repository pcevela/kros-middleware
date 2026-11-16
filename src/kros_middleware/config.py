"""Configuration loaded exclusively from environment variables.

Every secret can be given either directly (``KM_FOO``) or as a path to a file
(``KM_FOO_FILE``), which is how Podman/Docker secrets are mounted. The file
variant wins when both are set. Nothing deployment-specific is hard-coded.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

PREFIX = "KM_"

SEND_MODES = ("off", "approval", "auto")
NOTIFY_CHANNELS = ("ntfy", "email")
NOTIFY_EVENTS = (
    "approval_requested",
    "approval_decided",
    "upload_result_ok",
    "upload_result_problem",
    "send_status_ok",
    "send_status_problem",
    "signature_invalid",
    "retention_run",
)
DEFAULT_NOTIFY_EVENTS = (
    "approval_requested",
    "approval_decided",
    "upload_result_problem",
    "send_status_ok",
    "send_status_problem",
    "signature_invalid",
)
RETENTION_FLOOR_DAYS = 90


class ConfigError(ValueError):
    """Raised for missing or invalid configuration."""


def _raw(name: str, environ: dict[str, str]) -> str | None:
    key = PREFIX + name
    file_key = key + "_FILE"
    path = environ.get(file_key)
    if path:
        try:
            # Secrets files often end with a newline; strip only line endings.
            value = Path(path).read_text(encoding="utf-8-sig").rstrip("\r\n")
        except OSError as exc:
            raise ConfigError(f"{file_key}: cannot read the file: {exc.strerror}") from exc
    else:
        value = environ.get(key)
    if value is None or not value.strip():
        return None
    return value


def _str(name: str, env: dict[str, str], default: str | None = None, required: bool = False) -> str | None:
    value = _raw(name, env)
    if value is None:
        if required:
            raise ConfigError(f"{PREFIX}{name} (or {PREFIX}{name}_FILE) is required")
        return default
    return value


def _int(name: str, env: dict[str, str], default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    value = _raw(name, env)
    if value is None:
        result = default
    else:
        try:
            result = int(value.strip())
        except ValueError as exc:
            raise ConfigError(f"{PREFIX}{name} must be an integer, got {value!r}") from exc
    if minimum is not None and result < minimum:
        raise ConfigError(f"{PREFIX}{name} must be >= {minimum}, got {result}")
    if maximum is not None and result > maximum:
        raise ConfigError(f"{PREFIX}{name} must be <= {maximum}, got {result}")
    return result


def _float(name: str, env: dict[str, str], default: float, minimum: float = 0.0, maximum: float = 3600.0) -> float:
    value = _raw(name, env)
    if value is None:
        return default
    try:
        result = float(value.strip())
    except ValueError as exc:
        raise ConfigError(f"{PREFIX}{name} must be a number, got {value!r}") from exc
    if not math.isfinite(result) or result <= minimum or result > maximum:
        raise ConfigError(f"{PREFIX}{name} must be a finite number in ({minimum}, {maximum}], got {value!r}")
    return result


def _bool(name: str, env: dict[str, str], default: bool) -> bool:
    value = _raw(name, env)
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{PREFIX}{name} must be a boolean (true/false), got {value!r}")


def _list(name: str, env: dict[str, str], default: tuple[str, ...] = ()) -> list[str]:
    value = _raw(name, env)
    if value is None:
        return list(default)
    return [part.strip() for part in re.split(r"[,\n]", value) if part.strip()]


def _https_url(name: str, value: str, allow_http: bool) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("https", "http") or not parts.netloc:
        raise ConfigError(f"{PREFIX}{name} must be an absolute http(s) URL")
    if parts.scheme == "http" and not allow_http:
        raise ConfigError(
            f"{PREFIX}{name} must use https (set {PREFIX}ALLOW_INSECURE_URLS=true only for local testing)"
        )
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ConfigError(f"{PREFIX}{name} must not contain credentials, a query string or a fragment")
    return value.rstrip("/")


def _path(name: str, env: dict[str, str], default: str) -> str:
    value = _str(name, env, default) or default
    if not re.fullmatch(r"(/[A-Za-z0-9._~-]+)+", value) or any(seg in (".", "..") for seg in value.split("/")):
        raise ConfigError(f"{PREFIX}{name} must be an absolute path of plain segments, got {value!r}")
    return value


SCOPES = ("read", "issue", "send", "results", "events")
DEFAULT_SCOPES = ("read", "results")


@dataclass(frozen=True)
class ClientToken:
    name: str
    token_sha256: bytes = field(repr=False)
    scopes: frozenset[str]


@dataclass(frozen=True)
class Credential:
    username: str
    password_sha256: bytes = field(repr=False)


@dataclass(frozen=True)
class NtfyConfig:
    url: str
    topic: str
    token: str | None = field(repr=False)
    priority: int
    priority_problem: int
    tags: list[str]


@dataclass(frozen=True)
class EmailConfig:
    host: str
    port: int
    security: str
    username: str | None
    password: str | None = field(repr=False)
    sender: str
    recipients: list[str]
    subject_prefix: str
    timeout: float


@dataclass(frozen=True)
class Settings:
    upstream_base_url: str
    upstream_token: str = field(repr=False)
    upstream_timeout: float
    upstream_rate_per_second: float
    upstream_burst: int
    upstream_rate_per_minute: int

    clients: list[ClientToken] = field(repr=False)
    auth_failures_per_minute: int

    public_base_url: str
    webhook_path: str
    webhook_secrets: list[str] = field(repr=False)
    webhook_allow_unsigned: bool
    webhook_max_body_bytes: int

    proxy_prefix: str
    results_prefix: str
    approvals_prefix: str
    allow_rules: list[str]
    issue_enabled: bool
    issue_rules: list[str]
    send_mode: str
    max_body_bytes: int

    approval_ttl_hours: int
    approval_max_pending: int
    approval_auth: str
    approver_credentials: list[Credential] = field(repr=False)
    approval_hash_fields: list[str]
    allowed_ibans: list[str]

    db_path: str
    retention_days: int
    retention_interval_hours: int
    audit_store_bodies: bool

    notify_channels: list[str]
    notify_events: list[str]
    ntfy: NtfyConfig | None
    email: EmailConfig | None
    messages_file: str | None
    template_dir: str | None

    listen_host: str
    listen_port: int
    forwarded_allow_ips: str
    log_level: str
    instance_name: str = field(default="kros-middleware")


def _secret_digest(secret: str, what: str) -> bytes:
    """Accept either a plain secret or ``sha256$<64 hex>`` and return the sha256 digest."""
    if secret.startswith("sha256$"):
        hexpart = secret[len("sha256$") :]
        if not re.fullmatch(r"[0-9a-fA-F]{64}", hexpart):
            raise ConfigError(f"{what}: sha256$ value must be 64 hex characters")
        return bytes.fromhex(hexpart)
    if len(secret) < 32:
        raise ConfigError(f"{what}: secret must be at least 32 characters (or given as sha256$<hex>)")
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _parse_clients(raw: str | None) -> list[ClientToken]:
    """Entries ``name:secret[:scope+scope]``, one per line or comma-separated."""
    if not raw:
        raise ConfigError(f"{PREFIX}CLIENT_TOKENS (or {PREFIX}CLIENT_TOKENS_FILE) is required")
    clients: list[ClientToken] = []
    seen_names: set[str] = set()
    seen_digests: set[bytes] = set()
    for entry in re.split(r"[,\n]", raw):
        entry = entry.strip()
        if not entry or entry.startswith("#"):
            continue
        parts = entry.split(":")
        if len(parts) not in (2, 3):
            raise ConfigError(f"{PREFIX}CLIENT_TOKENS: invalid entry; expected name:secret[:scopes]")
        name = parts[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
            raise ConfigError(f"{PREFIX}CLIENT_TOKENS: invalid client name {name!r}")
        digest = _secret_digest(parts[1].strip(), f"{PREFIX}CLIENT_TOKENS[{name}]")
        scopes = (
            frozenset(x.strip() for x in parts[2].split("+") if x.strip())
            if len(parts) == 3
            else frozenset(DEFAULT_SCOPES)
        )
        unknown = scopes - set(SCOPES)
        if unknown or not scopes:
            raise ConfigError(f"{PREFIX}CLIENT_TOKENS[{name}]: unknown or empty scopes {sorted(unknown)}")
        if name in seen_names or digest in seen_digests:
            raise ConfigError(f"{PREFIX}CLIENT_TOKENS: duplicate client name or secret ({name!r})")
        seen_names.add(name)
        seen_digests.add(digest)
        clients.append(ClientToken(name=name, token_sha256=digest, scopes=scopes))
    if not clients:
        raise ConfigError(f"{PREFIX}CLIENT_TOKENS contains no clients")
    return clients


def _parse_credentials(raw: str | None) -> list[Credential]:
    creds: list[Credential] = []
    for entry in re.split(r"[,\n]", raw or ""):
        entry = entry.strip()
        if not entry or entry.startswith("#"):
            continue
        user, sep, secret = entry.partition(":")
        if not sep or not re.fullmatch(r"[A-Za-z0-9._@-]{1,64}", user.strip()):
            raise ConfigError(f"{PREFIX}APPROVER_CREDENTIALS: invalid entry; expected user:password")
        creds.append(Credential(user.strip(), _secret_digest(secret.strip(), f"{PREFIX}APPROVER_CREDENTIALS[{user}]")))
    return creds


DEFAULT_ALLOW_RULES = (
    "GET /api/auth/check",
    "GET /api/invoices",
    "GET /api/invoices/{id}",
    "GET /api/invoices/{id}/reports/{id}",
    "GET /api/invoices/{id}/reports/{id}/previewLink",
    "GET /api/proforma-invoices",
    "GET /api/proforma-invoices/{id}",
    "GET /api/proforma-invoices/{id}/reports/{id}",
    "GET /api/received-orders",
    "GET /api/received-orders/{id}",
    "GET /api/delivery-notes",
    "GET /api/delivery-notes/{id}",
    "GET /api/expenses",
    "GET /api/expenses/{id}",
    "GET /api/payments",
    "GET /api/payments/accounts",
    "GET /api/numberingSequences",
    "GET /api/tags",
    "GET /api/warehouses",
    "GET /api/catalog-items",
    "GET /api/attachments/{word}/{id}",
    "GET /api/attachments/{word}/{id}/{id}/content",
)
DEFAULT_ISSUE_RULES = (
    "POST /api/invoices",
    "POST /api/invoices/batch",
    "POST /api/proforma-invoices",
    "POST /api/proforma-invoices/batch",
)
# Always part of the approval hash, whatever KM_APPROVAL_HASH_FIELDS says.
CORE_APPROVAL_HASH_FIELDS = (
    "documentNumber",
    "numberingSequence",
    "partner",
    "items",
    "prices",
    "bankAccount",
    "dueDate",
    "variableSymbol",
    "invoiceType",
)
DEFAULT_APPROVAL_HASH_FIELDS = (
    "documentNumber",
    "numberingSequence",
    "invoiceType",
    "partner",
    "items",
    "prices",
    "vatBreakdowns",
    "discount",
    "advancePaymentDeduction",
    "issueDate",
    "deliveryDate",
    "dueDate",
    "variableSymbol",
    "bankAccount",
    "paymentType",
    "culture",
    "vatPayerType",
    "mandatoryText",
    "mandatoryTextType",
    "creditedInvoiceNumber",
    "openingText",
    "closingText",
    "printedNote",
    "eInvoice.isEInvoice",
)


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if environ is None else environ)
    allow_http = _bool("ALLOW_INSECURE_URLS", env, False)

    upstream_base_url = _https_url(
        "UPSTREAM_BASE_URL", _str("UPSTREAM_BASE_URL", env, "https://api-economy.kros.sk"), allow_http
    )
    upstream_token = _str("UPSTREAM_TOKEN", env, required=True)
    public_base_url = _https_url("PUBLIC_BASE_URL", _str("PUBLIC_BASE_URL", env, required=True), allow_http)

    webhook_secrets = [x for x in (_str("WEBHOOK_SECRET", env), _str("WEBHOOK_SECRET_PREVIOUS", env)) if x]
    webhook_allow_unsigned = _bool("WEBHOOK_ALLOW_UNSIGNED", env, False)
    if any(len(x) < 32 for x in webhook_secrets):
        raise ConfigError(f"{PREFIX}WEBHOOK_SECRET must be at least 32 characters (KROS recommends a 256-bit key)")
    if not webhook_secrets and not webhook_allow_unsigned:
        raise ConfigError(
            f"{PREFIX}WEBHOOK_SECRET is required (set {PREFIX}WEBHOOK_ALLOW_UNSIGNED=true only for local testing)"
        )

    send_mode = (_str("SEND_MODE", env, "approval") or "approval").lower()
    if send_mode not in SEND_MODES:
        raise ConfigError(f"{PREFIX}SEND_MODE must be one of {', '.join(SEND_MODES)}, got {send_mode!r}")
    if send_mode == "auto" and not _bool("SEND_AUTO_ACKNOWLEDGED", env, False):
        raise ConfigError(
            f"{PREFIX}SEND_MODE=auto sends invoices to customers without human approval; "
            f"also set {PREFIX}SEND_AUTO_ACKNOWLEDGED=true to confirm"
        )

    approval_auth = (_str("APPROVAL_AUTH", env, "basic") or "basic").lower()
    if approval_auth not in ("basic", "none"):
        raise ConfigError(f"{PREFIX}APPROVAL_AUTH must be basic or none")
    approver_credentials = _parse_credentials(_str("APPROVER_CREDENTIALS", env))
    if send_mode == "approval":
        if approval_auth == "basic" and not approver_credentials:
            raise ConfigError(
                f"{PREFIX}APPROVER_CREDENTIALS is required: the approval link alone must not be enough to send "
                f"an invoice (or set {PREFIX}APPROVAL_AUTH=none if the approval path is protected otherwise)"
            )
        if approval_auth == "none" and not _bool("APPROVAL_AUTH_NONE_ACKNOWLEDGED", env, False):
            raise ConfigError(
                f"{PREFIX}APPROVAL_AUTH=none makes the link the only factor; protect {PREFIX}APPROVALS_PREFIX "
                f"in the reverse proxy and set {PREFIX}APPROVAL_AUTH_NONE_ACKNOWLEDGED=true"
            )

    retention_days = _int("RETENTION_DAYS", env, RETENTION_FLOOR_DAYS, minimum=1, maximum=36500)
    if retention_days < RETENTION_FLOOR_DAYS and not _bool("RETENTION_ALLOW_BELOW_FLOOR", env, False):
        raise ConfigError(
            f"{PREFIX}RETENTION_DAYS={retention_days} is below the {RETENTION_FLOOR_DAYS}-day floor; "
            f"set {PREFIX}RETENTION_ALLOW_BELOW_FLOOR=true to override"
        )

    notify_channels = [c.lower() for c in _list("NOTIFY_CHANNELS", env)]
    for channel in notify_channels:
        if channel not in NOTIFY_CHANNELS:
            raise ConfigError(f"{PREFIX}NOTIFY_CHANNELS: unknown channel {channel!r}")
    notify_events = _list("NOTIFY_EVENTS", env, DEFAULT_NOTIFY_EVENTS)
    for event in notify_events:
        if event not in NOTIFY_EVENTS:
            raise ConfigError(f"{PREFIX}NOTIFY_EVENTS: unknown event {event!r}")

    ntfy = None
    if "ntfy" in notify_channels:
        ntfy = NtfyConfig(
            url=_https_url("NTFY_URL", _str("NTFY_URL", env, required=True), allow_http),
            topic=_str("NTFY_TOPIC", env, required=True),
            token=_str("NTFY_TOKEN", env),
            priority=_int("NTFY_PRIORITY", env, 3, minimum=1, maximum=5),
            priority_problem=_int("NTFY_PRIORITY_PROBLEM", env, 4, minimum=1, maximum=5),
            tags=_list("NTFY_TAGS", env),
        )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ntfy.topic):
            raise ConfigError(f"{PREFIX}NTFY_TOPIC contains invalid characters")

    email = None
    if "email" in notify_channels:
        security = (_str("SMTP_SECURITY", env, "starttls") or "starttls").lower()
        if security not in ("starttls", "ssl", "none"):
            raise ConfigError(f"{PREFIX}SMTP_SECURITY must be starttls, ssl or none")
        if security == "none" and not allow_http:
            raise ConfigError(
                f"{PREFIX}SMTP_SECURITY=none sends approval links in clear text; "
                f"set {PREFIX}ALLOW_INSECURE_URLS=true to allow it"
            )
        recipients = _list("EMAIL_TO", env)
        if not recipients:
            raise ConfigError(f"{PREFIX}EMAIL_TO is required when e-mail notifications are enabled")
        email = EmailConfig(
            host=_str("SMTP_HOST", env, required=True),
            port=_int("SMTP_PORT", env, 465 if security == "ssl" else 587, minimum=1, maximum=65535),
            security=security,
            username=_str("SMTP_USERNAME", env),
            password=_str("SMTP_PASSWORD", env),
            sender=_str("EMAIL_FROM", env, required=True),
            recipients=recipients,
            subject_prefix=_str("EMAIL_SUBJECT_PREFIX", env, "[kros-middleware]") or "",
            timeout=_float("SMTP_TIMEOUT", env, 20.0),
        )

    settings = Settings(
        upstream_base_url=upstream_base_url,
        upstream_token=upstream_token,
        upstream_timeout=_float("UPSTREAM_TIMEOUT", env, 30.0),
        upstream_rate_per_second=_float("UPSTREAM_RATE_PER_SECOND", env, 5.0),
        upstream_burst=_int("UPSTREAM_BURST", env, 10, minimum=1),
        upstream_rate_per_minute=_int("UPSTREAM_RATE_PER_MINUTE", env, 240, minimum=1),
        clients=_parse_clients(_str("CLIENT_TOKENS", env)),
        auth_failures_per_minute=_int("AUTH_FAILURES_PER_MINUTE", env, 10, minimum=1),
        public_base_url=public_base_url,
        webhook_path=_path("WEBHOOK_PATH", env, "/webhooks/kros"),
        webhook_secrets=webhook_secrets,
        webhook_allow_unsigned=webhook_allow_unsigned,
        webhook_max_body_bytes=_int("WEBHOOK_MAX_BODY_BYTES", env, 1_048_576, minimum=1024),
        proxy_prefix=_path("PROXY_PREFIX", env, "/api"),
        results_prefix=_path("RESULTS_PREFIX", env, "/results"),
        approvals_prefix=_path("APPROVALS_PREFIX", env, "/approvals"),
        allow_rules=_list("ALLOW_RULES", env, DEFAULT_ALLOW_RULES),
        issue_enabled=_bool("ISSUE_ENABLED", env, False),
        issue_rules=_list("ISSUE_RULES", env, DEFAULT_ISSUE_RULES),
        send_mode=send_mode,
        max_body_bytes=_int("MAX_BODY_BYTES", env, 1024 * 1024, minimum=1024, maximum=12 * 1024 * 1024),
        approval_ttl_hours=_int("APPROVAL_TTL_HOURS", env, 24, minimum=1, maximum=24 * 14),
        approval_max_pending=_int("APPROVAL_MAX_PENDING", env, 20, minimum=1),
        approval_auth=approval_auth,
        approver_credentials=approver_credentials,
        # Extra fields are added; the defaults (everything shown on the approval page) always stay bound.
        approval_hash_fields=sorted(
            set(_list("APPROVAL_HASH_FIELDS", env)) | set(DEFAULT_APPROVAL_HASH_FIELDS) | set(CORE_APPROVAL_HASH_FIELDS)
        ),
        allowed_ibans=[re.sub(r"\s+", "", x).upper() for x in _list("ALLOWED_IBANS", env)],
        db_path=_str("DB_PATH", env, "/data/kros-middleware.sqlite3") or "",
        retention_days=retention_days,
        retention_interval_hours=_int("RETENTION_INTERVAL_HOURS", env, 24, minimum=1, maximum=24 * 30),
        audit_store_bodies=_bool("AUDIT_STORE_BODIES", env, False),
        notify_channels=notify_channels,
        notify_events=notify_events,
        ntfy=ntfy,
        email=email,
        messages_file=_str("MESSAGES_FILE", env),
        template_dir=_str("TEMPLATE_DIR", env),
        listen_host=_str("LISTEN_HOST", env, "0.0.0.0") or "0.0.0.0",  # noqa: S104 - container-internal bind
        listen_port=_int("LISTEN_PORT", env, 8080, minimum=1, maximum=65535),
        forwarded_allow_ips=_str("FORWARDED_ALLOW_IPS", env, "127.0.0.1") or "127.0.0.1",
        log_level=(_str("LOG_LEVEL", env, "info") or "info").lower(),
        instance_name=_str("INSTANCE_NAME", env, "kros-middleware") or "kros-middleware",
    )

    prefixes = [settings.proxy_prefix, settings.results_prefix, settings.approvals_prefix, settings.webhook_path]
    for i, a in enumerate(prefixes):
        for b in prefixes[i + 1 :]:
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                raise ConfigError(f"path prefixes overlap: {a!r} and {b!r}")
    if settings.send_mode == "auto":  # pragma: no cover - logging only
        log.warning("%sSEND_MODE=auto: invoices will be sent to customers without human approval", PREFIX)
    if settings.retention_days < RETENTION_FLOOR_DAYS:
        log.warning(
            "%sRETENTION_DAYS=%d is below the %d-day floor", PREFIX, settings.retention_days, RETENTION_FLOOR_DAYS
        )
    return settings
