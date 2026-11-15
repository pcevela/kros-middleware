from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from kros_middleware.app import create_app
from kros_middleware.config import load_settings

AGENT_TOKEN = "agent-token-0123456789abcdefghijklmnopqrstuv"
READER_TOKEN = "reader-token-0123456789abcdefghijklmnopqrstu"
REAL_TOKEN = "REAL-KROS-TOKEN-must-never-leak-0123456789"
WEBHOOK_SECRET = "webhook-secret-0123456789abcdefghijklmnop"
APPROVER = ("boss", "approver-password-0123456789abcdefghij")
PUBLIC = "https://kros.example.test"


def base_env(**overrides: str) -> dict[str, str]:
    env = {
        "KM_UPSTREAM_TOKEN": REAL_TOKEN,
        "KM_PUBLIC_BASE_URL": PUBLIC,
        "KM_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "KM_CLIENT_TOKENS": f"agent:{AGENT_TOKEN}:read+issue+send+results\nreader:{READER_TOKEN}:read+results",
        "KM_APPROVER_CREDENTIALS": f"{APPROVER[0]}:{APPROVER[1]}",
        "KM_DB_PATH": ":memory:",
        "KM_ISSUE_ENABLED": "true",
    }
    env.update(overrides)
    return env


def invoice(invoice_id: int = 159, **changes: Any) -> dict[str, Any]:
    data = {
        "id": invoice_id,
        "documentNumber": "2026001",
        "numberingSequence": "FA",
        "invoiceType": 0,
        "partner": {
            "address": {"businessName": "Acme <script>alert(1)</script> s.r.o.", "city": "Bratislava"},
            "registrationId": "12345678",
            "taxId": "2020202020",
            "email": "uctaren@acme.example",
        },
        "items": [{"name": "Consulting", "amount": 10, "measureUnit": "h"}],
        "prices": {"documentPrices": {"totalPriceInclVat": 1230.0}, "currency": "EUR"},
        "issueDate": "2026-11-30T00:00:00Z",
        "dueDate": "2026-12-14T00:00:00Z",
        "variableSymbol": "2026001",
        "bankAccount": {"iban": "SK31 1200 0000 1987 4263 7541"},
        "eInvoice": {"isEInvoice": True, "status": ""},
    }
    data.update(changes)
    return data


class FakeKros:
    """Minimal stand-in for the KROS API."""

    def __init__(self) -> None:
        self.invoices: dict[int, dict[str, Any]] = {159: invoice()}
        self.calls: list[httpx.Request] = []
        self.fail_send_with: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.headers["authorization"] == f"Bearer {REAL_TOKEN}"
        path = request.url.path
        if request.method == "GET" and path.startswith("/api/invoices/"):
            invoice_id = int(path.rsplit("/", 1)[1])
            if invoice_id not in self.invoices:
                return httpx.Response(404, json={"title": "not found"})
            return httpx.Response(200, json={"data": self.invoices[invoice_id]})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"data": [], "path": path},
                headers={"x-rate-limit-remaining": "299", "set-cookie": "secret=1", "server": "kros"},
            )
        if request.method == "POST" and path.endswith("/send"):
            if self.fail_send_with:
                raise self.fail_send_with
            return httpx.Response(202, json={"id": int(path.split("/")[3]), "outcome": "accepted"})
        if request.method == "POST":
            return httpx.Response(202, json={"requestId": "3fa85f64-5717-4562-b3fc-2c963f66afa6"})
        return httpx.Response(405)

    def sends(self) -> list[httpx.Request]:
        return [c for c in self.calls if c.method == "POST" and c.url.path.endswith("/send")]


class Harness:
    def __init__(self, env: dict[str, str]) -> None:
        self.kros = FakeKros()
        self.settings = load_settings(env)
        self.app = create_app(
            self.settings, upstream_transport=httpx.MockTransport(self.kros.handler), start_background=False
        )
        self.notifications: list[tuple[str, dict[str, Any], str | None]] = []
        self.app.state.core.notifier.fire = lambda event, values, link=None: self.notifications.append(
            (event, values, link)
        )
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=PUBLIC)

    async def __aenter__(self) -> Harness:
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.client.aclose()
        await self._lifespan.__aexit__(*exc)

    @staticmethod
    def bearer(token: str = AGENT_TOKEN) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def basic(user: str = APPROVER[0], password: str = APPROVER[1]) -> dict[str, str]:
        raw = base64.b64encode(f"{user}:{password}".encode()).decode()
        return {"Authorization": f"Basic {raw}"}

    async def post_json(self, path: str, body: Any, token: str = AGENT_TOKEN) -> httpx.Response:
        return await self.client.post(
            path, content=json.dumps(body), headers={**self.bearer(token), "Content-Type": "application/json"}
        )

    def last_link(self) -> str:
        links = [link for event, _, link in self.notifications if event == "approval_requested"]
        assert links, "no approval link was produced"
        return links[-1]


@pytest.fixture
async def harness():
    async with Harness(base_env()) as h:
        yield h


@pytest.fixture
def make_harness():
    def factory(**overrides: str) -> Harness:
        return Harness(base_env(**overrides))

    return factory
