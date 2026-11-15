from __future__ import annotations

import pytest

from kros_middleware.config import ConfigError, load_settings
from kros_middleware.messages import Messages
from kros_middleware.policy import PolicyError, canonical_path, compile_rule, looks_like_send, send_invoice_id
from kros_middleware.signature import compute_signature, verify_signature

from .conftest import REAL_TOKEN, base_env

# Vector computed independently with Node.js Buffer.from(s,'utf16le') and with the
# PHP reference from the KROS documentation (mb_convert_encoding UTF-16LE).
VECTOR_BODY = (
    '{"companyId":119919,"entityType":2,"results":{"entities":[{"index":0,"status":201,'
    '"data":{"name":"Žltý kôň – €"}}]},"status":200,"requestId":"50304274-197c-40e9-8804-84e23e28efa2"}'
)
VECTOR_KEY = "aG9zZXJhbmRvbXNlY3JldA==-test-secret-0123456789"
VECTOR_SIG = "rpjB2afynmuNiwYriMycJoAp2z7QoPQ4qk/sMNWPizo="


def test_signature_matches_reference_vector():
    assert compute_signature(VECTOR_BODY, VECTOR_KEY) == VECTOR_SIG
    assert verify_signature(VECTOR_BODY.encode("utf-8"), VECTOR_SIG, VECTOR_KEY)


@pytest.mark.parametrize(
    "bad",
    [None, "", "not-base64!!", VECTOR_SIG[:-4] + "AAAA", " " + VECTOR_SIG.lower()],
)
def test_signature_rejects(bad):
    assert not verify_signature(VECTOR_BODY.encode("utf-8"), bad, VECTOR_KEY)


def test_signature_rejects_utf8_variant():
    import base64
    import hashlib
    import hmac

    utf8 = base64.b64encode(hmac.new(VECTOR_KEY.encode(), VECTOR_BODY.encode(), hashlib.sha256).digest()).decode()
    assert not verify_signature(VECTOR_BODY.encode("utf-8"), utf8, VECTOR_KEY)


def test_settings_repr_has_no_secrets():
    text = repr(load_settings(base_env()))
    assert REAL_TOKEN not in text
    assert "webhook-secret" not in text
    assert "approver-password" not in text


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"KM_UPSTREAM_TOKEN": "  "}, "UPSTREAM_TOKEN"),
        ({"KM_RETENTION_DAYS": "30"}, "floor"),
        ({"KM_SEND_MODE": "auto"}, "SEND_AUTO_ACKNOWLEDGED"),
        ({"KM_APPROVER_CREDENTIALS": ""}, "APPROVER_CREDENTIALS"),
        ({"KM_APPROVAL_AUTH": "none"}, "ACKNOWLEDGED"),
        ({"KM_WEBHOOK_SECRET": "short"}, "32 characters"),
        ({"KM_UPSTREAM_RATE_PER_SECOND": "nan"}, "finite"),
        ({"KM_UPSTREAM_BASE_URL": "http://kros.example"}, "https"),
        ({"KM_PROXY_PREFIX": "/hooks/../api"}, "plain segments"),
        ({"KM_WEBHOOK_PATH": "/api/hook"}, "overlap"),
        ({"KM_CLIENT_TOKENS": "agent:short"}, "32 characters"),
        ({"KM_CLIENT_TOKENS": "agent:" + "x" * 40 + ":read+admin"}, "scopes"),
    ],
)
def test_config_validation(overrides, message):
    with pytest.raises(ConfigError, match=message):
        load_settings(base_env(**overrides))


def test_config_allows_retention_override_and_hashed_tokens():
    import hashlib

    digest = hashlib.sha256(b"x" * 40).hexdigest()
    settings = load_settings(
        base_env(
            KM_RETENTION_DAYS="30",
            KM_RETENTION_ALLOW_BELOW_FLOOR="true",
            KM_CLIENT_TOKENS=f"agent:sha256${digest}:read",
        )
    )
    assert settings.retention_days == 30
    assert settings.clients[0].token_sha256 == hashlib.sha256(b"x" * 40).digest()


def test_secret_file(tmp_path):
    secret = tmp_path / "token"
    secret.write_text("﻿from-file-token-0123456789\n", encoding="utf-8")
    env = base_env()
    del env["KM_UPSTREAM_TOKEN"]
    env["KM_UPSTREAM_TOKEN_FILE"] = str(secret)
    assert load_settings(env).upstream_token == "from-file-token-0123456789"


def test_core_hash_fields_always_present():
    settings = load_settings(base_env(KM_APPROVAL_HASH_FIELDS="dueDate"))
    for field in ("partner", "items", "prices", "bankAccount"):
        assert field in settings.approval_hash_fields


@pytest.mark.parametrize(
    "raw",
    [
        b"/api//invoices",
        b"/api/%2e%2e/x",
        b"/api/invoices/../auth",
        b"/api/a%2Fb",
        b"/api\\x",
        b"/api/;x",
        b"/ap\xc3\xad",
    ],
)
def test_canonical_path_rejects(raw):
    with pytest.raises(PolicyError):
        canonical_path(raw)


def test_canonical_path_trailing_slash():
    assert canonical_path(b"/api/invoices/") == "/api/invoices"


def test_rules():
    rule = compile_rule("GET /api/invoices/{id}")
    assert rule.matches("GET", "/api/invoices/12")
    assert not rule.matches("GET", "/api/invoices/abc")
    assert not rule.matches("POST", "/api/invoices/12")
    assert not rule.matches("GET", "/api/invoices/12/send")
    with pytest.raises(PolicyError):
        compile_rule("DELETE /api/x")
    with pytest.raises(PolicyError):
        compile_rule("GET /api/**/x")


def test_send_detection():
    assert send_invoice_id("POST", "/api/invoices/5/send") == 5
    assert send_invoice_id("POST", "/api/Invoices/5/SEND") == 5
    assert send_invoice_id("GET", "/api/invoices/5/send") is None
    assert looks_like_send("/api/proforma-invoices/5/Send")
    with pytest.raises(PolicyError):
        send_invoice_id("POST", "/api/invoices/9999999999999999999/send")


def test_messages_cannot_reach_attributes(tmp_path):
    custom = tmp_path / "m.json"
    custom.write_text('{"approval_requested": {"title": "${number.__class__}", "body": "{number.__class__}"}}')
    title, body = Messages(str(custom)).render("approval_requested", {"number": "X\nY"})
    assert "__class__" in title  # left untouched, not evaluated
    assert "class 'str'" not in title + body
