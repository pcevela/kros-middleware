#!/usr/bin/env python3
"""kros-recurring: issue recurring monthly invoices through kros-middleware.

Standard library only, so it runs with the host's Python (3.11+) from a systemd timer.

For every enabled template in the TOML configuration, for one billing period (a month):

1. Look up the invoice by its externalId (``recurring:<key>:<YYYY-MM>``). If it exists,
   the run resumes with it instead of creating a second one.
2. Otherwise read the latest invoice number in the numbering sequence, create the invoice
   without a number (KROS assigns it), wait for the webhook result, and check that KROS
   assigned exactly the next number and stored exactly the expected content.
3. Only when every invoice of the period is created and verified, request sending. With the
   middleware in approval mode, a human approves each send.

A local journal records each step before it happens, so a crash or timeout never leads to a
second invoice: an unresolved step blocks the run until a human runs ``confirm`` or ``forget``.
Any surprise stops the run and is reported over ntfy, including what was already done.

Usage:
    kros_recurring.py --config recurring.toml plan [--period 2026-11]
    kros_recurring.py --config recurring.toml run  [--period 2026-11] [--allow-late]
    kros_recurring.py --config recurring.toml confirm recurring:<key>:2026-11 FV260038 --period 2026-11
    kros_recurring.py --config recurring.toml forget  recurring:<key>:2026-11 --period 2026-11
    kros_recurring.py --config recurring.toml status
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import fcntl
import http.client
import json
import logging
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from string import Template
from typing import Any

__version__ = "0.2.0"

log = logging.getLogger("kros-recurring")
CENT = Decimal("0.01")
OPEN_OR_DONE_APPROVAL = {"pending", "approved", "sending", "submitted", "unknown"}
PERIOD_RE = re.compile(r"^(?P<y>20[0-9]{2})-(?P<m>0[1-9]|1[0-2])$")


class JobError(Exception):
    """Stops the run; nothing further is created or sent."""


# ---------------------------------------------------------------------------- configuration
def _dec(value: Any, what: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise JobError(f"{what}: not a number: {value!r}") from exc
    if not result.is_finite():
        raise JobError(f"{what}: not a finite number")
    return result


@dataclass
class Item:
    name: Template
    amount: Decimal
    unit_price: Decimal  # net, per unit
    vat_rate: Decimal
    measure_unit: str = ""


@dataclass
class InvoiceTemplate:
    key: str
    enabled: bool
    fields: dict[str, Any]
    partner: dict[str, Any]
    items: list[Item]
    variable_symbol: str | None


@dataclass
class Config:
    base_url: str
    token: str
    timeout: float
    result_timeout: float
    poll_interval: float
    sequence: str
    number_prefix: Template
    number_digits: int
    my_company: dict[str, Any]
    templates: list[InvoiceTemplate]
    ntfy_url: str | None = None
    ntfy_topic: str | None = None
    ntfy_token: str | None = field(default=None, repr=False)
    title_prefix: str = "[kros-recurring]"
    lock_file: str = ""
    journal_file: str = ""


DEFAULT_KEYS = {
    "due_days",
    "vat_payer_type",
    "culture",
    "currency",
    "opening_text",
    "closing_text",
    "registration_court_text",
    "payment_type",
    "bank_iban",
    "bank_swift",
    "einvoice_from",
    "printed_note",
}
NAME_PLACEHOLDERS = {"MM": "01", "M": "1", "YYYY": "2000", "YY": "00"}
DEFAULT_VAT_RATES = ("23", "19", "5", "0")  # Slovak rates from 2025


def _abs_path(value: Any, what: str) -> str:
    path = str(value or "")
    if not path.startswith("/"):
        raise JobError(f"{what} must be an absolute path")
    return path


def _read_secret(path: str, what: str) -> str:
    try:
        value = Path(path).read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        raise JobError(f"{what}: cannot read {path}: {exc.strerror}") from exc
    if not value:
        raise JobError(f"{what}: {path} is empty")
    return value


def load_config(path: str) -> Config:
    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise JobError(f"cannot load config {path}: {exc}") from exc

    mw = raw.get("middleware") or {}
    base_url = str(mw.get("base_url") or "").rstrip("/")
    parts = urllib.parse.urlsplit(base_url)
    local = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if parts.scheme not in ("https", "http") or not parts.netloc or (parts.scheme == "http" and not local):
        raise JobError("middleware.base_url must be https://..., or http:// only for localhost")
    token = _read_secret(str(mw.get("token_file") or ""), "middleware.token_file")

    num = raw.get("numbering") or {}
    sequence = str(num.get("sequence") or "")
    if not re.fullmatch(r"[A-Za-z0-9]{1,5}", sequence):
        raise JobError("numbering.sequence must be a KROS sequence code (1-5 letters/digits)")
    prefix = Template(str(num.get("prefix") or ""))
    digits = int(num.get("counter_digits") or 0)
    if not 1 <= digits <= 10:
        raise JobError("numbering.counter_digits must be between 1 and 10")
    try:
        prefix.substitute(YYYY="2000", YY="00")
    except (KeyError, ValueError) as exc:
        raise JobError(f"numbering.prefix: only $YYYY and $YY are allowed ({exc})") from exc

    defaults = raw.get("defaults") or {}
    unknown = set(defaults) - DEFAULT_KEYS
    if unknown:
        raise JobError(f"defaults: unknown keys {sorted(unknown)}")

    my_company = raw.get("my_company") or {}
    if not (my_company.get("registrationId") and (my_company.get("address") or {}).get("businessName")):
        raise JobError("my_company needs registrationId and address.businessName")

    allowed_rates = {_dec(r, "allowed_vat_rates").normalize() for r in raw.get("allowed_vat_rates", DEFAULT_VAT_RATES)}
    templates: list[InvoiceTemplate] = []
    seen: set[str] = set()
    for entry in raw.get("invoice") or []:
        key = str(entry.get("key") or "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", key) or key in seen:
            raise JobError(f"invoice.key {key!r} must be unique, lowercase letters, digits and dashes")
        seen.add(key)
        fields = dict(defaults)
        for k in DEFAULT_KEYS & set(entry):
            fields[k] = entry[k]
        unknown = set(entry) - DEFAULT_KEYS - {"key", "enabled", "partner", "items", "variable_symbol"}
        if unknown:
            raise JobError(f"invoice {key}: unknown keys {sorted(unknown)}")
        partner = entry.get("partner") or {}
        if not partner.get("registrationId") or not (partner.get("address") or {}).get("businessName"):
            raise JobError(f"invoice {key}: partner needs registrationId and address.businessName")
        items = []
        for idx, it in enumerate(entry.get("items") or []):
            name = Template(str(it.get("name") or ""))
            try:
                name.substitute(NAME_PLACEHOLDERS)
            except (KeyError, ValueError) as exc:
                raise JobError(f"invoice {key} item {idx}: placeholders are $MM $M $YYYY $YY ({exc})") from exc
            items.append(
                Item(
                    name=name,
                    amount=_dec(it.get("amount", 1), f"{key} item {idx} amount"),
                    unit_price=_dec(it.get("unit_price"), f"{key} item {idx} unit_price"),
                    vat_rate=_dec(it.get("vat_rate"), f"{key} item {idx} vat_rate"),
                    measure_unit=str(it.get("measure_unit") or ""),
                )
            )
        if not items:
            raise JobError(f"invoice {key}: at least one item is required")
        for required in ("due_days", "vat_payer_type"):
            if required not in fields:
                raise JobError(f"invoice {key}: {required} missing (set it in [defaults])")
        try:
            due = int(fields["due_days"])
            payer = int(fields["vat_payer_type"])
            if fields.get("einvoice_from"):
                date.fromisoformat(str(fields["einvoice_from"]))
        except (TypeError, ValueError) as exc:
            raise JobError(f"invoice {key}: invalid due_days, vat_payer_type or einvoice_from ({exc})") from exc
        if not 0 <= due <= 120 or payer not in (0, 1, 2):
            raise JobError(f"invoice {key}: due_days must be 0-120 and vat_payer_type 0, 1 or 2")
        for idx, item in enumerate(items):
            if item.amount <= 0 or item.unit_price <= 0:
                raise JobError(f"invoice {key} item {idx}: amount and unit_price must be positive")
            if item.vat_rate.normalize() not in allowed_rates:
                raise JobError(
                    f"invoice {key} item {idx}: VAT rate {item.vat_rate} is not one of "
                    f"{sorted(str(r) for r in allowed_rates)} (allowed_vat_rates)"
                )
        vs = entry.get("variable_symbol")
        if vs is not None and not re.fullmatch(r"[0-9]{1,10}", str(vs)):
            raise JobError(f"invoice {key}: variable_symbol must be 1-10 digits")
        templates.append(
            InvoiceTemplate(
                key=key,
                enabled=bool(entry.get("enabled", True)),
                fields=fields,
                partner=partner,
                items=items,
                variable_symbol=None if vs is None else str(vs),
            )
        )
    if not templates:
        raise JobError("no [[invoice]] templates configured")

    notify = raw.get("notify") or {}
    if notify.get("ntfy_url"):
        nparts = urllib.parse.urlsplit(str(notify["ntfy_url"]))
        if nparts.scheme != "https" or not nparts.netloc:
            raise JobError("notify.ntfy_url must be an https:// URL")
    ntfy_token = (
        _read_secret(notify["ntfy_token_file"], "notify.ntfy_token_file") if notify.get("ntfy_token_file") else None
    )
    return Config(
        base_url=base_url,
        token=token,
        timeout=float(mw.get("timeout", 30)),
        result_timeout=float(mw.get("result_timeout", 300)),
        poll_interval=float(mw.get("poll_interval", 5)),
        sequence=sequence,
        number_prefix=prefix,
        number_digits=digits,
        my_company=my_company,
        templates=templates,
        ntfy_url=(notify.get("ntfy_url") or "").rstrip("/") or None,
        ntfy_topic=notify.get("ntfy_topic"),
        ntfy_token=ntfy_token,
        title_prefix=str(notify.get("title_prefix", "[kros-recurring]")),
        lock_file=_abs_path(raw.get("lock_file"), "lock_file"),
        journal_file=_abs_path(raw.get("journal_file"), "journal_file"),
    )


# ---------------------------------------------------------------------------- HTTP
class Client:
    """Minimal JSON client for the middleware. GETs are retried; POSTs only on 429 (not processed)."""

    def __init__(self, base_url: str, token: str, timeout: float) -> None:
        self.base_url = base_url
        self._token = token
        self.timeout = timeout

    def request(
        self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None
    ) -> tuple[int, Any]:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        for attempt in range(5):
            req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - scheme validated
            req.add_header("Authorization", f"Bearer {self._token}")
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", f"kros-recurring/{__version__}")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - URL validated
                    return resp.status, _json(resp.read())
            except urllib.error.HTTPError as exc:
                payload = _json(exc.read())
                retryable = exc.code == 429 or (method == "GET" and exc.code >= 500)
                if retryable and attempt < 4:
                    wait = _retry_after(exc.headers.get("retry-after"), attempt)
                    log.warning("%s %s -> %s, retrying in %ss", method, path, exc.code, wait)
                    time.sleep(wait)
                    continue
                return exc.code, payload
            except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
                if method == "GET" and attempt < 4:
                    time.sleep(2**attempt)
                    continue
                outcome = " - the request may or may not have been processed" if method == "POST" else ""
                detail = getattr(exc, "reason", exc)
                raise JobError(f"{method} {path}: {type(exc).__name__}: {detail}{outcome}") from exc
        raise JobError(f"{method} {path}: giving up after retries")


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, ValueError):
        return {"raw": raw[:300].decode("utf-8", "replace")}


def _retry_after(value: str | None, attempt: int) -> float:
    try:
        return min(max(float(value), 1.0), 60.0) if value else float(2**attempt)
    except ValueError:
        return float(2**attempt)


class Notifier:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def send(self, title: str, message: str, priority: int = 3) -> None:
        if not (self.cfg.ntfy_url and self.cfg.ntfy_topic):
            return
        body = json.dumps(
            {
                "topic": self.cfg.ntfy_topic,
                "title": f"{self.cfg.title_prefix} {title}".strip(),
                "message": message,
                "priority": priority,
                "tags": ["receipt"],
            }
        ).encode("utf-8")
        req = urllib.request.Request(self.cfg.ntfy_url, data=body, method="POST")  # noqa: S310 - https only
        req.add_header("Content-Type", "application/json")
        if self.cfg.ntfy_token:
            req.add_header("Authorization", f"Bearer {self.cfg.ntfy_token}")
        try:
            with urllib.request.urlopen(req, timeout=15):  # noqa: S310 - URL from trusted config
                pass
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            log.error("ntfy notification failed: %s", exc)


# ---------------------------------------------------------------------------- local journal
class Journal:
    """Durable record of what this job did, keyed by externalId.

    Phases: ``creating`` (POST about to be / was sent, outcome not yet confirmed),
    ``confirmed`` (invoice found and verified, number recorded), ``send_requested``.
    Anything left in ``creating`` blocks further runs for that invoice until a human
    resolves it with the ``confirm`` or ``forget`` command.
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        try:
            self.data: dict[str, dict[str, Any]] = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.data = {}
        except (OSError, ValueError) as exc:
            raise JobError(f"cannot read journal {path}: {exc}") from exc

    def get(self, key: str) -> dict[str, Any] | None:
        return self.data.get(key)

    def set(self, key: str, **values: Any) -> None:
        entry = self.data.setdefault(key, {})
        entry.update(values, at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        self._save()

    def remove(self, key: str) -> None:
        self.data.pop(key, None)
        self._save()

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        dir_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


# ---------------------------------------------------------------------------- the job
def last_day(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Planned:
    template: InvoiceTemplate
    external_id: str
    body: dict[str, Any]
    net: Decimal
    gross: Decimal


class Job:
    def __init__(
        self, cfg: Config, client: Client, notifier: Notifier, today: date, period: str | None, journal: Journal
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.notifier = notifier
        self.journal = journal
        self.today = today
        if period is None:
            period = f"{today.year:04d}-{today.month:02d}"
        match = PERIOD_RE.fullmatch(period)
        if not match:
            raise JobError(f"period must be YYYY-MM, got {period!r}")
        self.year, self.month = int(match["y"]), int(match["m"])
        self.period = period
        self.issue_date = last_day(self.year, self.month)
        self.report: list[str] = []
        self.accept_similar = False

    # ------------------------------------------------------------------ building
    def placeholders(self) -> dict[str, str]:
        return {"MM": f"{self.month:02d}", "M": str(self.month), "YYYY": str(self.year), "YY": f"{self.year % 100:02d}"}

    def number_prefix(self) -> str:
        return self.cfg.number_prefix.substitute(YYYY=str(self.year), YY=f"{self.year % 100:02d}")

    def build(self, tpl: InvoiceTemplate) -> Planned:
        f = tpl.fields
        ph = self.placeholders()
        items, net, gross = [], Decimal(0), Decimal(0)
        for it in tpl.items:
            line_net = money(it.unit_price * it.amount)
            line_gross = money(it.unit_price * it.amount * (Decimal(1) + it.vat_rate / Decimal(100)))
            net += line_net
            gross += line_gross
            item = {
                "name": it.name.substitute(ph),
                "amount": float(it.amount),
                "vatRate": float(it.vat_rate),
                "totalPriceInclVat": float(line_gross),
            }
            if it.measure_unit:
                item["measureUnit"] = it.measure_unit
            items.append(item)
        einvoice_from = f.get("einvoice_from")
        is_einvoice = bool(einvoice_from) and self.issue_date >= date.fromisoformat(str(einvoice_from))
        external_id = f"recurring:{tpl.key}:{self.period}"
        body: dict[str, Any] = {
            "externalId": external_id,
            "numberingSequence": self.cfg.sequence,
            "invoiceType": 0,
            "issueDate": self.issue_date.isoformat(),
            "deliveryDate": self.issue_date.isoformat(),
            "dueDate": (self.issue_date + timedelta(days=int(f["due_days"]))).isoformat(),
            "vatPayerType": int(f["vat_payer_type"]),
            "myCompany": self.cfg.my_company,
            "partner": tpl.partner,
            "items": items,
            "isEInvoice": is_einvoice,
        }
        for key, api in (
            ("culture", "culture"),
            ("currency", "currency"),
            ("opening_text", "openingText"),
            ("closing_text", "closingText"),
            ("registration_court_text", "registrationCourtText"),
            ("payment_type", "paymentType"),
            ("printed_note", "printedNote"),
        ):
            if f.get(key):
                body[api] = str(f[key])
        if f.get("bank_iban"):
            body["bankAccount"] = {
                "iban": str(f["bank_iban"]).replace(" ", ""),
                "swift": str(f.get("bank_swift") or ""),
            }
        if tpl.variable_symbol is not None:
            body["variableSymbol"] = tpl.variable_symbol
        return Planned(tpl, external_id, body, money(net), money(gross))

    def plans(self) -> list[Planned]:
        return [self.build(t) for t in self.cfg.templates if t.enabled]

    # ------------------------------------------------------------------ KROS lookups
    def list_invoices(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        rows, skip = [], 0
        while True:
            status, data = self.client.request("GET", "/api/invoices", {**params, "Top": 100, "Skip": skip})
            if status != 200 or not isinstance(data, dict):
                raise JobError(f"listing invoices failed: HTTP {status} {_short(data)}")
            page = data.get("data") or []
            rows.extend(page)
            if len(page) < 100:
                return rows
            skip += 100
            if skip > 10_000:
                raise JobError("too many invoices returned; refusing to continue")

    def find_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        rows = self.list_invoices({"ExternalId": external_id})
        if any(r.get("externalId") != external_id for r in rows):
            raise JobError("the ExternalId filter returned other invoices; refusing to guess (fail closed)")
        if len(rows) > 1:
            raise JobError(f"{len(rows)} invoices carry externalId {external_id}; fix this in KROS first")
        return rows[0] if rows else None

    def find_similar(self, planned: Planned) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Other invoices for the same customer around this period.

        Returns (blocking, notable). Blocking: an invoice carrying this job's externalId (a
        lookup that lagged), one with exactly this period's item texts, or one with the same
        amount issued inside the period. Notable: same customer and amount, issued after the
        period (e.g. last month's invoice issued late); reported only.
        """
        start = date(self.year, self.month, 1)
        rows = self.list_invoices(
            {
                "IssueDateFrom": start.isoformat(),
                "IssueDateTo": (self.issue_date + timedelta(days=15)).isoformat(),
            }
        )
        names = sorted(i["name"] for i in planned.body["items"])
        blocking, notable = [], []
        for r in rows:
            if r.get("externalId") == planned.external_id:
                blocking.append(r)
                continue
            if (r.get("partner") or {}).get("registrationId") != planned.template.partner["registrationId"]:
                continue
            detail = r if r.get("items") is not None else self.invoice_detail(r["id"])
            if sorted(str(i.get("name")) for i in detail.get("items") or []) == names:
                blocking.append(r)
                continue
            gross = ((r.get("prices") or {}).get("documentPrices") or {}).get("totalPriceInclVat")
            try:
                same_amount = money(Decimal(str(gross))) == planned.gross
            except InvalidOperation:
                same_amount = False
            if same_amount:
                in_period = str(r.get("issueDate") or "")[:10] <= self.issue_date.isoformat()
                (blocking if in_period else notable).append(r)
        return blocking, notable

    def invoice_detail(self, invoice_id: int) -> dict[str, Any]:
        status, data = self.client.request("GET", f"/api/invoices/{int(invoice_id)}")
        if status != 200 or not isinstance((data or {}).get("data"), dict):
            raise JobError(f"reading invoice {invoice_id} failed: HTTP {status} {_short(data)}")
        return data["data"]

    def counters(self) -> list[int]:
        prefix = self.number_prefix()
        rows = self.list_invoices(
            {
                "NumberingSequence": self.cfg.sequence,
                "DocumentNumberFrom": prefix + "0" * self.cfg.number_digits,
                "DocumentNumberTo": prefix + "9" * self.cfg.number_digits,
            }
        )
        result = []
        for inv in rows:
            number = str(inv.get("documentNumber") or "")
            rest = number[len(prefix) :]
            if number.startswith(prefix) and rest.isdigit() and len(rest) == self.cfg.number_digits:
                result.append(int(rest))
        return result

    def latest_counter(self) -> int:
        """The latest valid number of this year in the sequence."""
        return max(self.counters(), default=0)

    def format_number(self, counter: int) -> str:
        return f"{self.number_prefix()}{counter:0{self.cfg.number_digits}d}"

    def approvals_for(self, invoice_id: int) -> list[dict[str, Any]]:
        status, data = self.client.request("GET", "/results/approvals")
        if status != 200 or not isinstance(data, dict):
            raise JobError(f"reading approvals failed: HTTP {status} {_short(data)}")
        return [a for a in data.get("approvals") or [] if a.get("invoiceId") == invoice_id]

    # ------------------------------------------------------------------ verification
    def verify(self, planned: Planned, inv: dict[str, Any], expected_number: str) -> list[str]:
        body = planned.body
        problems: list[str] = []

        def check(label: str, actual: Any, expected: Any) -> None:
            if actual != expected:
                problems.append(f"{label}: expected {expected!r}, KROS has {actual!r}")

        number = str(inv.get("documentNumber") or "")
        check("document number", number, expected_number)
        check("externalId", inv.get("externalId"), planned.external_id)
        check("numbering sequence", inv.get("numberingSequence"), body["numberingSequence"])
        check("invoice type", inv.get("invoiceType"), 0)
        check("VAT payer type", inv.get("vatPayerType"), body["vatPayerType"])
        partner = inv.get("partner") or {}
        tp = planned.template.partner
        for key in ("registrationId", "taxId", "vatId", "email"):
            if key in tp:
                check(f"customer {key}", partner.get(key), tp[key])
        check("customer name", (partner.get("address") or {}).get("businessName"), tp["address"]["businessName"])
        check("supplier IČO", (inv.get("myCompany") or {}).get("registrationId"), self.cfg.my_company["registrationId"])
        for key in ("issueDate", "deliveryDate", "dueDate"):
            check(key, str(inv.get(key) or "")[:10], body[key])
        prices = inv.get("prices") or {}
        if "currency" in body:
            check("currency", prices.get("currency"), body["currency"])
        dp = prices.get("documentPrices") or {}
        try:
            check("net total", money(Decimal(str(dp.get("totalPrice")))), planned.net)
            check("gross total", money(Decimal(str(dp.get("totalPriceInclVat")))), planned.gross)
        except InvalidOperation:
            problems.append("totals missing in KROS response")
        got_items = inv.get("items") or []
        check("number of items", len(got_items), len(body["items"]))
        for idx, (got, want) in enumerate(zip(got_items, body["items"], strict=False)):
            check(f"item {idx} name", got.get("name"), want["name"])
            check(f"item {idx} amount", _num(got.get("amount")), _num(want["amount"]))
            check(f"item {idx} VAT rate", _num(got.get("vatRate")), _num(want["vatRate"]))
        expected_vs = body.get("variableSymbol") or re.sub(r"\D", "", number)
        check("variable symbol", str(inv.get("variableSymbol") or ""), expected_vs)
        check("e-invoice flag", bool((inv.get("eInvoice") or {}).get("isEInvoice")), body["isEInvoice"])
        if "bankAccount" in body:
            check(
                "IBAN",
                str((inv.get("bankAccount") or {}).get("iban") or "").replace(" ", ""),
                body["bankAccount"]["iban"],
            )
        if "paymentType" in body:
            check("payment type", inv.get("paymentType"), body["paymentType"])
        return problems

    def verify_or_fail(self, planned: Planned, invoice: dict[str, Any], expected_number: str, what: str) -> None:
        problems = self.verify(planned, invoice, expected_number)
        if problems:
            raise JobError(
                f"{what} {invoice.get('documentNumber')} ({planned.external_id}) does not match the "
                f"template, NOT sent - fix or delete it in KROS: " + "; ".join(problems)
            )
        if self.counters().count(int(expected_number[len(self.number_prefix()) :])) != 1:
            raise JobError(f"number {expected_number} exists more than once in {self.cfg.sequence}; NOT sent")

    # ------------------------------------------------------------------ steps
    def wait_for_result(self, request_id: str) -> dict[str, Any] | None:
        deadline = time.monotonic() + self.cfg.result_timeout
        while time.monotonic() < deadline:
            status, data = self.client.request("GET", f"/results/requests/{urllib.parse.quote(request_id, safe='')}")
            events = (data or {}).get("events") if status == 200 and isinstance(data, dict) else None
            if events:
                return events[-1].get("payload") or {}
            time.sleep(self.cfg.poll_interval)
        return None

    def create(self, planned: Planned) -> dict[str, Any]:
        ext = planned.external_id
        blocking, notable = self.find_similar(planned)
        own = [r for r in blocking if r.get("externalId") == ext]
        if own or (blocking and not self.accept_similar):
            numbers = ", ".join(f"{r.get('documentNumber')} ({str(r.get('issueDate') or '')[:10]})" for r in blocking)
            if own:
                raise JobError(
                    f"{ext}: KROS already has this job's invoice ({numbers}). Check it, then run "
                    f"'confirm {ext} <number>'. Nothing created"
                )
            raise JobError(
                f"{ext}: KROS already has a similar invoice for this customer in this period "
                f"({numbers}). If it is a different invoice, re-run with --accept-similar. Nothing created"
            )
        for r in (blocking if self.accept_similar else []) + notable:
            self.report.append(
                f"note: {r.get('documentNumber')} ({str(r.get('issueDate') or '')[:10]}) has the "
                f"same customer and amount as {ext}"
            )
        expected = self.format_number(self.latest_counter() + 1)
        # Journal first: if anything below fails or the process dies, the next run will not
        # blindly create a second invoice.
        self.journal.set(ext, phase="creating", expected_number=expected, request_id=None)
        status, data = self.client.request("POST", "/api/invoices", body={"data": planned.body})
        if status != 202 or not (data or {}).get("requestId"):
            definite = (
                status in (400, 401, 402, 403, 415, 422)
                and isinstance(data, dict)
                and ("errors" in data or "errorCode" in data)
            )
            if definite:
                self.journal.remove(ext)  # validation/permission rejection: nothing was processed
                raise JobError(f"KROS did not accept invoice {ext}: HTTP {status} {_short(data)}")
            raise JobError(
                f"{ext}: unclear outcome (HTTP {status} {_short(data)}); the invoice may exist. "
                f"Check KROS, then run 'confirm {ext} <number>' or 'forget {ext}'"
            )
        request_id = str(data["requestId"])
        self.journal.set(ext, request_id=request_id)
        log.info("%s: accepted, requestId %s", ext, request_id)
        result = self.wait_for_result(request_id)
        if result is not None:
            entities = ((result.get("results") or {}).get("entities")) or []
            statuses = [e.get("status") for e in entities]
            if entities and all(st == 400 for st in statuses):
                self.journal.remove(ext)
                raise JobError(f"KROS rejected {ext}: {_short(result)}")
            if statuses != [201]:
                raise JobError(
                    f"{ext}: unexpected KROS result {_short(result)}. Check KROS, then run "
                    f"'confirm {ext} <number>' or 'forget {ext}'"
                )
        found = self.find_by_external_id(ext)
        if found is None:
            raise JobError(
                f"{ext}: accepted by KROS (request {request_id}) but not found yet. Check KROS, then "
                f"run 'confirm {ext} <number>' or 'forget {ext}'"
            )
        invoice = self.invoice_detail(found["id"])
        self.verify_or_fail(planned, invoice, expected, "created invoice")
        self.journal.set(ext, phase="confirmed", invoice_id=int(invoice["id"]), number=invoice["documentNumber"])
        self.report.append(f"{invoice['documentNumber']}: created and verified ({ext})")
        return invoice

    def resume(self, planned: Planned) -> dict[str, Any] | None:
        """Return the confirmed invoice for this template, or None if it still has to be created."""
        ext = planned.external_id
        entry = self.journal.get(ext)
        found = self.find_by_external_id(ext)
        if entry is None:
            if found is not None:
                raise JobError(
                    f"{ext}: invoice {found.get('documentNumber')} exists in KROS but this job has no "
                    f"record of creating it. Check it, then run 'confirm {ext} <number>'"
                )
            return None
        if entry.get("phase") == "creating":
            raise JobError(
                f"{ext}: an earlier run did not finish creating it (request {entry.get('request_id')}). "
                f"Check KROS, then run 'confirm {ext} <number>' or 'forget {ext}'"
            )
        if found is None:
            raise JobError(
                f"{ext}: journal says {entry.get('number')}, but KROS no longer has it; "
                f"run 'forget {ext}' if it was deleted on purpose"
            )
        invoice = self.invoice_detail(found["id"])
        self.verify_or_fail(planned, invoice, str(entry.get("number")), "existing invoice")
        return invoice

    def request_send(self, planned: Planned, invoice: dict[str, Any]) -> None:
        ext = planned.external_id
        number = invoice.get("documentNumber")
        if (self.journal.get(ext) or {}).get("phase") == "send_requested":
            self.report.append(f"{number}: send already requested earlier ({ext})")
            return
        status_now = (invoice.get("eInvoice") or {}).get("status")
        previous = self.approvals_for(int(invoice["id"]))
        if status_now or previous:
            states = ", ".join(sorted({str(a.get("status")) for a in previous})) or f"e-invoice {status_now}"
            self.report.append(f"{number}: NOT requested again, earlier send activity: {states} ({ext})")
            return
        status, data = self.client.request("POST", f"/api/invoices/{int(invoice['id'])}/send", body={})
        if status == 202 or (status == 409 and (data or {}).get("errorCode") == "approvalAlreadyOpen"):
            self.journal.set(ext, phase="send_requested")
            self.report.append(f"{number}: send approval requested ({ext})")
            return
        raise JobError(f"send request for {number} failed: HTTP {status} {_short(data)}")

    # ------------------------------------------------------------------ entry points
    def check_date(self, allow_late: bool, max_late_days: int = 7) -> None:
        if self.issue_date == self.today:
            return
        if self.issue_date > self.today:
            hint = ""
            if self.today.day <= 7:
                prev = date(self.today.year, self.today.month, 1) - timedelta(days=1)
                hint = (
                    f" If this is a catch-up run, the previous period may not have been issued: "
                    f"run --period {prev.year:04d}-{prev.month:02d} --allow-late"
                )
            raise JobError(
                f"issue date {self.issue_date} is in the future; invoices are issued on the last day "
                f"of the month.{hint}"
            )
        late = (self.today - self.issue_date).days
        if not allow_late or late > max_late_days:
            raise JobError(
                f"period {self.period} has issue date {self.issue_date}, {late} day(s) ago. To issue it late, "
                f"run: run --period {self.period} --allow-late (at most {max_late_days} days)"
            )

    def run(self, allow_late: bool) -> list[str]:
        self.check_date(allow_late)
        plans = self.plans()
        # Phase 1: every invoice exists and is verified, before anything is sent.
        confirmed: list[tuple[Planned, dict[str, Any]]] = []
        for planned in plans:
            invoice = self.resume(planned)
            if invoice is None:
                invoice = self.create(planned)
            else:
                self.report.append(f"{invoice['documentNumber']}: exists and verified ({planned.external_id})")
            confirmed.append((planned, invoice))
        # Phase 2: request sending (a human approves each one in the middleware).
        for planned, invoice in confirmed:
            self.request_send(planned, invoice)
        return self.report

    def dry_run(self) -> list[str]:
        lines = []
        predicted = None
        for planned in self.plans():
            ext = planned.external_id
            entry = self.journal.get(ext)
            found = self.find_by_external_id(ext)
            if entry or found:
                lines.append(
                    f"{ext}: journal {entry.get('phase') if entry else 'none'}, "
                    f"KROS {found.get('documentNumber') if found else 'none'}"
                )
                continue
            blocking, notable = self.find_similar(planned)
            if blocking:
                lines.append(
                    f"{ext}: WOULD STOP, similar invoice in this period: "
                    + ", ".join(str(r.get("documentNumber")) for r in blocking)
                )
                continue
            for r in notable:
                lines.append(f"{ext}: note, {r.get('documentNumber')} has the same customer and amount")
            predicted = (self.latest_counter() if predicted is None else predicted) + 1
            lines.append(
                f"{ext}: would create {self.format_number(predicted)}, net {planned.net}, gross "
                f"{planned.gross}{', e-invoice' if planned.body['isEInvoice'] else ''}"
            )
        return lines

    def confirm(self, external_id: str, number: str, accept_gap: bool = False) -> str:
        planned = next((p for p in self.plans() if p.external_id == external_id), None)
        if planned is None:
            raise JobError(f"{external_id} does not belong to period {self.period} (use --period)")
        found = self.find_by_external_id(external_id)
        if found is None or found.get("documentNumber") != number:
            raise JobError(f"KROS has no invoice {number} with externalId {external_id}")
        prefix = self.number_prefix()
        rest = number[len(prefix) :]
        if not (number.startswith(prefix) and rest.isdigit() and len(rest) == self.cfg.number_digits):
            raise JobError(f"{number} is not a number of the form {self.format_number(1)}")
        counter = int(rest)
        if counter > 1 and (counter - 1) not in self.counters() and not accept_gap:
            raise JobError(
                f"{self.format_number(counter - 1)} does not exist, so {number} leaves a gap in the "
                "numbering. Fix the numbering in KROS, or re-run confirm with --accept-gap"
            )
        invoice = self.invoice_detail(found["id"])
        self.verify_or_fail(planned, invoice, number, "invoice")
        self.journal.set(external_id, phase="confirmed", invoice_id=int(invoice["id"]), number=number)
        return f"{number} confirmed for {external_id}"

    def forget(self, external_id: str) -> str:
        planned = next((p for p in self.plans() if p.external_id == external_id), None)
        if planned is None:
            raise JobError(f"{external_id} does not belong to period {self.period} (use --period)")
        own = [r for r in self.find_similar(planned)[0] if r.get("externalId") == external_id]
        if self.find_by_external_id(external_id) is not None or own:
            raise JobError(
                f"KROS still has an invoice with externalId {external_id}; delete it in KROS or use 'confirm'"
            )
        self.journal.remove(external_id)
        return f"{external_id} removed from the journal"


def _num(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)).normalize()
    except (InvalidOperation, ValueError):
        return None


def _short(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)[:400] if data is not None else ""


@contextlib.contextmanager
def single_instance(path: str):
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise JobError(f"cannot open lock file {path}: {exc.strerror}") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise JobError("another kros-recurring run is in progress") from exc
        yield
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Issue recurring monthly invoices through kros-middleware.")
    parser.add_argument("--config", required=True)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="show what a run would do (read-only, no lock)")
    p.add_argument("--period", help="YYYY-MM, default: current month")
    p.add_argument("--show-body", action="store_true", help="print the invoice bodies")
    p = sub.add_parser("run", help="create and verify the invoices, then request sending")
    p.add_argument("--period", help="YYYY-MM, default: current month")
    p.add_argument("--allow-late", action="store_true", help="issue a past period (at most 7 days late)")
    p.add_argument(
        "--accept-similar",
        action="store_true",
        help="create even though a similar invoice exists in the period (after checking it)",
    )
    p = sub.add_parser("confirm", help="record an invoice you checked in KROS as created by this job")
    p.add_argument("external_id")
    p.add_argument("number")
    p.add_argument("--period", required=True)
    p.add_argument("--accept-gap", action="store_true", help="accept a gap in the numbering before this number")
    p = sub.add_parser("forget", help="drop a journal entry after the invoice was removed from KROS")
    p.add_argument("external_id")
    p.add_argument("--period", required=True)
    sub.add_parser("status", help="print the journal")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    notifier: Notifier | None = None
    job: Job | None = None
    journal: Journal | None = None
    try:
        cfg = load_config(args.config)
        notifier = Notifier(cfg)
        if args.command == "status":
            print(json.dumps(Journal(cfg.journal_file).data, ensure_ascii=False, indent=1, sort_keys=True))
            return 0
        client = Client(cfg.base_url, cfg.token, cfg.timeout)
        period = getattr(args, "period", None)
        if args.command == "plan":
            job = Job(cfg, client, notifier, date.today(), period, Journal(cfg.journal_file))
            for planned in job.plans():
                print(
                    f"{planned.external_id}: issue {planned.body['issueDate']}, due {planned.body['dueDate']}, "
                    f"net {planned.net}, gross {planned.gross}, e-invoice {planned.body['isEInvoice']}"
                )
                for item in planned.body["items"]:
                    print(f"    - {item['name']} x{item['amount']} = {item['totalPriceInclVat']} incl. VAT")
                if args.show_body:
                    print(json.dumps(planned.body, ensure_ascii=False, indent=2))
            for line in job.dry_run():
                print("  " + line)
            return 0
        with single_instance(cfg.lock_file):
            journal = Journal(cfg.journal_file)  # read only while holding the lock
            job = Job(cfg, client, notifier, date.today(), period, journal)
            if args.command == "confirm":
                print(job.confirm(args.external_id, args.number, accept_gap=args.accept_gap))
                return 0
            if args.command == "forget":
                print(job.forget(args.external_id))
                return 0
            job.accept_similar = args.accept_similar
            report = job.run(allow_late=args.allow_late)
        count = len(job.plans())
        notifier.send(f"{job.period}: {count} invoice(s) prepared", "\n".join(report) or "nothing to do")
        return 0
    except Exception as exc:  # noqa: BLE001 - every failure must reach a human
        message = str(exc) if isinstance(exc, JobError) else f"unexpected {type(exc).__name__}: {exc}"
        log.error("%s", message)
        if not isinstance(exc, JobError):
            log.exception("details")
        if notifier is not None and getattr(args, "command", None) == "run":
            done = "\n".join(job.report) if job and job.report else "no invoice was created and verified"
            pending = ""
            if journal is not None:
                open_items = [
                    f"{k}: {v.get('phase')} {v.get('number') or v.get('expected_number') or ''}"
                    for k, v in journal.data.items()
                    if v.get("phase") != "send_requested"
                ]
                if open_items:
                    pending = "\n\nJournal entries not finished:\n" + "\n".join(open_items)
            notifier.send("run STOPPED", f"{message[:1000]}\n\nDone:\n{done[-500:]}{pending[-500:]}", priority=5)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
