from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx

from kros_middleware.signature import compute_signature

from .conftest import AGENT_TOKEN, PUBLIC, READER_TOKEN, REAL_TOKEN, WEBHOOK_SECRET, Harness, invoice

# ---------------------------------------------------------------------------- proxy


async def test_get_forwarded_with_real_token_only(harness: Harness):
    r = await harness.client.get("/api/invoices?Top=10", headers=harness.bearer())
    assert r.status_code == 200
    call = harness.kros.calls[-1]
    assert call.headers["authorization"] == f"Bearer {REAL_TOKEN}"
    assert call.url.params["Top"] == "10"
    assert "set-cookie" not in r.headers and "server" not in r.headers
    assert r.headers["x-rate-limit-remaining"] == "299"
    assert REAL_TOKEN not in r.text


async def test_auth_required(harness: Harness):
    assert (await harness.client.get("/api/invoices")).status_code == 401
    assert (await harness.client.get("/api/invoices", headers=harness.bearer("x" * 40))).status_code == 401
    assert not harness.kros.calls


async def test_auth_failures_are_throttled(harness: Harness):
    for _ in range(10):
        await harness.client.get("/api/invoices", headers=harness.bearer("y" * 40))
    r = await harness.client.get("/api/invoices", headers=harness.bearer())
    assert r.status_code == 429


async def test_unlisted_route_denied(harness: Harness):
    r = await harness.client.get("/api/movements/issues", headers=harness.bearer())
    assert r.status_code == 403
    assert not harness.kros.calls


async def test_path_tricks_denied(harness: Harness):
    for path in ("/api//invoices", "/api/invoices/%2e%2e/auth/check", "/api/invoices%2F1"):
        r = await harness.client.get(path, headers=harness.bearer())
        assert r.status_code in (400, 404), path
    assert not harness.kros.calls


async def test_duplicate_query_params_rejected(harness: Harness):
    r = await harness.client.get("/api/invoices?Top=1&top=100", headers=harness.bearer())
    assert r.status_code == 400


async def test_other_methods_rejected(harness: Harness):
    r = await harness.client.request("DELETE", "/api/invoices/1", headers=harness.bearer())
    assert r.status_code == 405


async def test_issue_requires_flag_and_scope(make_harness):
    async with make_harness(KM_ISSUE_ENABLED="false") as h:
        r = await h.post_json("/api/invoices", {"data": invoice(None)})
        assert r.status_code == 403 and r.json()["errorCode"] == "issuingDisabled"
    async with make_harness() as h:
        r = await h.post_json("/api/invoices", {"data": invoice(None)}, token=READER_TOKEN)
        assert r.status_code == 403 and r.json()["errorCode"] == "scopeRequired"
        r = await h.post_json("/api/invoices", {"data": invoice(None)})
        assert r.status_code == 202
        assert r.json()["requestId"]


async def test_options_send_smuggling_rejected(harness: Harness):
    bodies = [
        {"data": invoice(None), "options": {"send": True}},
        {"data": invoice(None), "OPTIONS": {"send": True}},
        {"data": [invoice(None)], "options": {"send": False}},
        {"data": {"nested": {"options": {"send": True}}}},
    ]
    for body in bodies:
        r = await harness.post_json("/api/invoices/batch", body)
        assert r.status_code == 403, body
    raw = '{"data": {}, "options": {"send": false}, "options": {"send": true}}'
    r = await harness.client.post(
        "/api/invoices", content=raw, headers={**harness.bearer(), "Content-Type": "application/json"}
    )
    assert r.status_code in (400, 403)
    r = await harness.client.post(
        "/api/invoices", content='{"data":{}}', headers={**harness.bearer(), "Content-Type": "text/plain"}
    )
    assert r.status_code == 415
    assert not [c for c in harness.kros.calls if c.method == "POST"]


async def test_send_like_paths_never_forwarded(harness: Harness):
    for path in ("/api/proforma-invoices/5/send", "/api/invoices/5/send/x"):
        r = await harness.post_json(path, {})
        assert r.status_code in (403, 404), path
    assert not harness.kros.sends()


async def test_forwarded_body_is_reserialised(harness: Harness):
    raw = '{"data": {"documentNumber": "1", "x": 1.0}}'
    r = await harness.client.post(
        "/api/invoices", content=raw, headers={**harness.bearer(), "Content-Type": "application/json"}
    )
    assert r.status_code == 202
    sent = json.loads(harness.kros.calls[-1].content)
    assert sent == {"data": {"documentNumber": "1", "x": 1.0}}


# ---------------------------------------------------------------------------- approval flow


async def request_send(h: Harness, invoice_id: int = 159, body=None) -> httpx.Response:
    return await h.post_json(f"/api/invoices/{invoice_id}/send", body or {})


def split_link(link: str) -> tuple[str, str]:
    parts = urlsplit(link)
    return parts.path, parse_qs(parts.query)["token"][0]


async def test_send_is_held_for_approval(harness: Harness):
    r = await request_send(harness)
    assert r.status_code == 202
    assert r.json()["status"] == "pendingApproval"
    assert not harness.kros.sends()
    path, token = split_link(harness.last_link())
    assert harness.last_link().startswith(PUBLIC)

    # The link alone is not enough.
    page = await harness.client.get(f"{path}?token={token}")
    assert page.status_code == 401
    assert "WWW-Authenticate" in page.headers

    page = await harness.client.get(f"{path}?token={token}", headers=harness.basic())
    assert page.status_code == 200
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;" in page.text
    assert "SK3112000000198742637541" in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"]

    wrong = await harness.client.get(f"{path}?token=nope", headers=harness.basic())
    assert wrong.status_code == 404

    approve = await harness.client.post(
        f"{path}/approve",
        data={"token": token},
        headers={**harness.basic(), "Origin": PUBLIC},
    )
    assert approve.status_code == 200
    assert len(harness.kros.sends()) == 1

    # Double submit does not send twice.
    again = await harness.client.post(
        f"{path}/approve", data={"token": token}, headers={**harness.basic(), "Origin": PUBLIC}
    )
    assert again.status_code == 409
    assert len(harness.kros.sends()) == 1

    status = await harness.client.get(f"/results/approvals/{path.rsplit('/', 1)[1]}", headers=harness.bearer())
    assert status.json()["status"] == "submitted"


async def test_concurrent_approvals_send_once(harness: Harness):
    await request_send(harness)
    path, token = split_link(harness.last_link())
    headers = {**harness.basic(), "Origin": PUBLIC}
    results = await asyncio.gather(
        *[harness.client.post(f"{path}/approve", data={"token": token}, headers=headers) for _ in range(5)]
    )
    assert sorted(r.status_code for r in results).count(200) == 1
    assert len(harness.kros.sends()) == 1


async def test_approver_credentials_required_for_action(harness: Harness):
    await request_send(harness)
    path, token = split_link(harness.last_link())
    r = await harness.client.post(f"{path}/approve", data={"token": token}, headers={"Origin": PUBLIC})
    assert r.status_code == 401
    r = await harness.client.post(
        f"{path}/approve", data={"token": token}, headers={**harness.basic(password="x" * 40), "Origin": PUBLIC}
    )
    assert r.status_code == 401
    assert not harness.kros.sends()


async def test_cross_origin_post_rejected(harness: Harness):
    await request_send(harness)
    path, token = split_link(harness.last_link())
    r = await harness.client.post(
        f"{path}/approve", data={"token": token}, headers={**harness.basic(), "Origin": "https://evil.example"}
    )
    assert r.status_code == 403
    assert not harness.kros.sends()


async def test_changed_invoice_is_not_sent(harness: Harness):
    await request_send(harness)
    path, token = split_link(harness.last_link())
    harness.kros.invoices[159] = invoice(bankAccount={"iban": "SK0000000000000000000000"})
    r = await harness.client.post(
        f"{path}/approve", data={"token": token}, headers={**harness.basic(), "Origin": PUBLIC}
    )
    assert r.status_code == 409
    assert not harness.kros.sends()
    assert any(e == "approval_decided" and v["decision"] == "stale" for e, v, _ in harness.notifications)


async def test_reject(harness: Harness):
    await request_send(harness)
    path, token = split_link(harness.last_link())
    r = await harness.client.post(
        f"{path}/reject", data={"token": token}, headers={**harness.basic(), "Origin": PUBLIC}
    )
    assert r.status_code == 200
    r = await harness.client.post(
        f"{path}/approve", data={"token": token}, headers={**harness.basic(), "Origin": PUBLIC}
    )
    assert r.status_code == 409
    assert not harness.kros.sends()


async def test_one_open_approval_per_invoice_and_freeze(harness: Harness):
    assert (await request_send(harness)).status_code == 202
    second = await request_send(harness)
    assert second.status_code == 409 and second.json()["errorCode"] == "approvalAlreadyOpen"
    # Overwriting the invoice under approval is refused.
    for number in ("2026001", 2026001):
        r = await harness.post_json("/api/invoices", {"data": {"documentNumber": number, "numberingSequence": "FA"}})
        assert r.status_code == 409, number
    r = await harness.post_json("/api/invoices/batch", {"data": [{"documentNumber": "2026001"}]})
    assert r.status_code == 409


async def test_iban_allowlist(make_harness):
    async with make_harness(KM_ALLOWED_IBANS="SK99 0000 0000 0000 0000 0000") as h:
        r = await request_send(h)
        assert r.status_code == 422
    async with make_harness(KM_ALLOWED_IBANS="sk3112000000198742637541") as h:
        assert (await request_send(h)).status_code == 202


async def test_send_body_validated(harness: Harness):
    r = await request_send(harness, body={"to": ["not-an-email"]})
    assert r.status_code == 400
    r = await request_send(harness, body={"to": ["a@b.example"], "evil": 1})
    assert r.status_code == 400


async def test_send_modes(make_harness):
    async with make_harness(KM_SEND_MODE="off") as h:
        r = await request_send(h)
        assert r.status_code == 403
    async with make_harness(KM_SEND_MODE="auto", KM_SEND_AUTO_ACKNOWLEDGED="true") as h:
        r = await request_send(h)
        assert r.status_code == 202
        assert len(h.kros.sends()) == 1


async def test_unknown_outcome_on_transport_error(harness: Harness):
    await request_send(harness)
    path, token = split_link(harness.last_link())
    harness.kros.fail_send_with = httpx.ReadTimeout("boom")
    r = await harness.client.post(
        f"{path}/approve", data={"token": token}, headers={**harness.basic(), "Origin": PUBLIC}
    )
    assert r.status_code == 502
    status = await harness.client.get(f"/results/approvals/{path.rsplit('/', 1)[1]}", headers=harness.bearer())
    assert status.json()["status"] == "unknown"


async def test_results_scoped_to_owner(harness: Harness):
    await request_send(harness)
    approval_id = urlsplit(harness.last_link()).path.rsplit("/", 1)[1]
    r = await harness.client.get(f"/results/approvals/{approval_id}", headers=harness.bearer(READER_TOKEN))
    assert r.status_code == 404
    r = await harness.client.get("/results/approvals", headers=harness.bearer(READER_TOKEN))
    assert r.json()["approvals"] == []
    r = await harness.client.get("/results/approvals", headers=harness.bearer())
    assert len(r.json()["approvals"]) == 1
    assert "token" not in json.dumps(r.json()).lower()


# ---------------------------------------------------------------------------- webhooks


def signed(body: dict) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body, ensure_ascii=False)
    return raw.encode("utf-8"), {
        "X-Kros-Signature-256": compute_signature(raw, WEBHOOK_SECRET),
        "Content-Type": "application/json",
    }


async def test_webhook_signature_required(harness: Harness):
    r = await harness.client.post("/webhooks/kros", content=b'{"status":200}')
    assert r.status_code == 401
    raw, headers = signed({"status": 200})
    headers["X-Kros-Signature-256"] = compute_signature(raw.decode(), "x" * 40)
    assert (await harness.client.post("/webhooks/kros", content=raw, headers=headers)).status_code == 401
    assert any(e == "signature_invalid" for e, _, _ in harness.notifications)


async def test_webhook_results_flow(harness: Harness):
    r = await harness.post_json("/api/invoices", {"data": invoice(None)})
    request_id = r.json()["requestId"]
    event = {
        "companyId": 1,
        "entityType": 2,
        "results": {"entities": [{"index": 0, "source": 1, "operation": 1, "status": 400, "problems": ["x"]}]},
        "status": 207,
        "requestId": request_id,
    }
    raw, headers = signed(event)
    assert (await harness.client.post("/webhooks/kros", content=raw, headers=headers)).status_code == 200
    assert (await harness.client.post("/webhooks/kros", content=raw, headers=headers)).status_code == 200
    r = await harness.client.get(f"/results/requests/{request_id}", headers=harness.bearer())
    assert len(r.json()["events"]) == 1  # deduplicated
    assert [e for e, _, _ in harness.notifications].count("upload_result_problem") == 1
    # Another client cannot read it.
    r = await harness.client.get(f"/results/requests/{request_id}", headers=harness.bearer(READER_TOKEN))
    assert r.status_code == 404


async def test_send_status_webhook(harness: Harness):
    await request_send(harness)
    for event_id, status, when in (
        ("e1", "sent", "2026-11-30T08:00:00+00:00"),
        ("e2", "confirmed", "2026-11-30T08:05:00+00:00"),
        ("e1", "sent", "2026-11-30T08:00:00+00:00"),
    ):
        event = {
            "companyId": 1,
            "entityType": 4,
            "results": {
                "entities": [
                    {
                        "index": 0,
                        "source": 1,
                        "operation": 2,
                        "status": 200,
                        "data": {
                            "id": 159,
                            "numberingSequence": "FA",
                            "documentNumber": "2026001",
                            "eventId": event_id,
                            "channel": "peppol",
                            "sendStatus": status,
                            "occurredAt": when,
                            "errorCode": None,
                        },
                    }
                ]
            },
            "status": 200,
            "requestId": None,
        }
        raw, headers = signed(event)
        assert (await harness.client.post("/webhooks/kros", content=raw, headers=headers)).status_code == 200
    r = await harness.client.get("/results/invoices/159/send-status", headers=harness.bearer())
    body = r.json()
    assert len(body["events"]) == 2
    assert body["latestByChannel"]["peppol"]["sendStatus"] == "confirmed"


async def test_webhook_size_limit(harness: Harness):
    big = b"{" + b" " * (2 * 1024 * 1024) + b"}"
    r = await harness.client.post("/webhooks/kros", content=big)
    assert r.status_code == 413


async def test_retention_purges_old_rows(harness: Harness):
    from datetime import timedelta

    from kros_middleware.db import utcnow

    db = harness.app.state.core.db
    raw, headers = signed({"status": 200, "requestId": None})
    await harness.client.post("/webhooks/kros", content=raw, headers=headers)
    counts = await db.purge(90, now=utcnow() + timedelta(days=91))
    assert counts["webhook_events"] == 1
    counts = await db.purge(90)
    assert counts["webhook_events"] == 0


async def test_health(harness: Harness):
    r = await harness.client.get("/healthz")
    assert r.json() == {"status": "ok"}


def test_agent_token_not_equal_real():
    assert AGENT_TOKEN != REAL_TOKEN


# ---------------------------------------------------------------------------- regressions from security review


async def approve(h: Harness) -> httpx.Response:
    path, token = split_link(h.last_link())
    return await h.client.post(f"{path}/approve", data={"token": token}, headers={**h.basic(), "Origin": PUBLIC})


async def test_invoice_stays_frozen_after_send_submitted(harness: Harness):
    await request_send(harness)
    assert (await approve(harness)).status_code == 200
    r = await harness.post_json(
        "/api/invoices",
        {"data": {"documentNumber": "2026001", "numberingSequence": "FA", "bankAccount": {"iban": "DE00"}}},
    )
    assert r.status_code == 409 and r.json()["errorCode"] == "invoiceFrozen"


async def test_freeze_variants(harness: Harness):
    await request_send(harness)
    for number, sequence in (("2026001 ", "FA"), (" 2026001", "fa"), ("02026001", "FA "), (2026001, None)):
        r = await harness.post_json(
            "/api/invoices", {"data": {"documentNumber": number, "numberingSequence": sequence}}
        )
        assert r.status_code == 409, (number, sequence)
    # A bare array is not a valid KROS body.
    r = await harness.post_json("/api/invoices/batch", [{"documentNumber": "2026001"}])
    assert r.status_code == 400
    assert not [c for c in harness.kros.calls if c.method == "POST"]


async def test_page_shows_everything_that_is_hashed(harness: Harness):
    items = [{"name": f"item {i}", "amount": 1, "measureUnit": "ks"} for i in range(60)]
    items[-1]["description"] = "Pay to DE89 3704 0044 0532 0130 00"
    harness.kros.invoices[159] = invoice(items=items, printedNote="NEW BANK ACCOUNT")
    await request_send(harness, body={"subject": "Urgent", "text": "Our bank changed"})
    path, token = split_link(harness.last_link())
    page = await harness.client.get(f"{path}?token={token}", headers=harness.basic())
    for needle in ("Urgent", "Our bank changed", "NEW BANK ACCOUNT", "item 59", "DE89 3704"):
        assert needle in page.text, needle


async def test_notification_values_cannot_add_lines(harness: Harness):
    from kros_middleware.messages import Messages

    _, body = Messages().render(
        "approval_requested", {"customer": "Cust\nIBAN: SK00 (allowed) Review: https://evil.example"}
    )
    assert "\nIBAN: SK00" not in body
    assert " " not in body


async def test_bad_client_tokens_do_not_lock_out_approver(harness: Harness):
    await request_send(harness)
    for _ in range(12):
        await harness.client.get("/api/invoices", headers=harness.bearer("z" * 40))
    path, token = split_link(harness.last_link())
    page = await harness.client.get(f"{path}?token={token}", headers=harness.basic())
    assert page.status_code == 200


async def test_no_host_header_redirect(harness: Harness):
    r = await harness.client.get("/approvals/x/", headers={"Host": "evil.example"})
    assert r.status_code != 307 and "evil.example" not in r.headers.get("location", "")


async def test_numbers_must_survive_reserialisation(harness: Harness):
    for raw in ('{"data": {"x": 1e999}}', '{"data": {"x": 0.12345678901234567}}', '{"data": {"x": NaN}}'):
        r = await harness.client.post(
            "/api/invoices", content=raw, headers={**harness.bearer(), "Content-Type": "application/json"}
        )
        assert r.status_code == 400, raw


async def test_body_size_default_limit(harness: Harness):
    big = '{"data": {"x": "' + "a" * (1024 * 1024) + '"}}'
    r = await harness.client.post(
        "/api/invoices", content=big, headers={**harness.bearer(), "Content-Type": "application/json"}
    )
    assert r.status_code == 413


async def test_resend_warning(harness: Harness):
    await request_send(harness)
    await approve(harness)
    r = await request_send(harness)
    assert r.status_code == 202
    path, token = split_link(harness.last_link())
    page = await harness.client.get(f"{path}?token={token}", headers=harness.basic())
    assert "already submitted for sending" in page.text


async def test_freeze_key_case_and_unicode_variants(harness: Harness):
    await request_send(harness)
    bodies = [
        {"data": {"DocumentNumber": "2026001", "numberingSequence": "FA"}},
        {"data": {"DOCUMENTNUMBER": "2026001"}},
        {"data": {"documentNumber": "２０２６００１", "numberingSequence": "ＦＡ"}},
        {"data": {"documentNumber": "2026001​", "numberingSequence": "FA​"}},
    ]
    for body in bodies:
        r = await harness.post_json("/api/invoices", body)
        assert r.status_code == 409, body
    raw = '{"data": {"documentNumber": "1", "DocumentNumber": "2026001"}}'
    r = await harness.client.post(
        "/api/invoices", content=raw, headers={**harness.bearer(), "Content-Type": "application/json"}
    )
    assert r.status_code == 400
    assert not [c for c in harness.kros.calls if c.method == "POST"]


async def test_integer_range_and_trailing_zeros(harness: Harness):
    r = await harness.client.post(
        "/api/invoices",
        content='{"data": {"x": 123456789012345678901}}',
        headers={**harness.bearer(), "Content-Type": "application/json"},
    )
    assert r.status_code == 400
    r = await harness.client.post(
        "/api/invoices",
        content='{"data": {"x": 1.000000000000000000}}',
        headers={**harness.bearer(), "Content-Type": "application/json"},
    )
    assert r.status_code == 202


async def test_stale_links_do_not_lock_out_authenticated_approver(harness: Harness):
    await request_send(harness)
    path, token = split_link(harness.last_link())
    for _ in range(12):
        await harness.client.get(f"{path}?token=wrong-token", headers=harness.basic())
    page = await harness.client.get(f"{path}?token={token}", headers=harness.basic())
    assert page.status_code == 200


async def test_hash_fields_setting_adds_to_defaults(make_harness):
    async with make_harness(KM_APPROVAL_HASH_FIELDS="paymentDetails") as h:
        fields = h.settings.approval_hash_fields
        assert "paymentDetails" in fields and "printedNote" in fields and "openingText" in fields
        await request_send(h)
        path, token = split_link(h.last_link())
        h.kros.invoices[159] = invoice(printedNote="changed")
        r = await approve(h)
        assert r.status_code == 409 and not h.kros.sends()
