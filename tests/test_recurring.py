from __future__ import annotations

import importlib.util
import itertools
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent.parent / "tools" / "kros_recurring.py"
spec = importlib.util.spec_from_file_location("kros_recurring", TOOL)
kr = importlib.util.module_from_spec(spec)
sys.modules["kros_recurring"] = kr
spec.loader.exec_module(kr)

CONFIG = """
lock_file = "{lock}"
journal_file = "{journal}"

[middleware]
base_url = "http://127.0.0.1:8095"
token_file = "{token}"
result_timeout = 1
poll_interval = 0

[numbering]
sequence = "FV"
prefix = "FV$YY"
counter_digits = 4

[defaults]
due_days = 14
vat_payer_type = 1
culture = "sk-SK"
currency = "EUR"
payment_type = "Bankový prevod"
bank_iban = "SK85 1100 0000 0029 2584 2663"
bank_swift = "TATRSKBX"
einvoice_from = "2027-01-01"

[my_company]
registrationId = "11111111"
address = {{ businessName = "Supplier s.r.o." }}

[[invoice]]
key = "acme"
partner = {{ registrationId = "45299951", address = {{ businessName = "Acme s.r.o." }} }}
items = [{{ name = "Monthly fee for $MM/$YYYY", amount = 1, unit_price = "150.00", vat_rate = "23" }}]

[[invoice]]
key = "fixed-vs"
variable_symbol = "5526"
partner = {{ registrationId = "53826230", address = {{ businessName = "Fixed VS s.r.o." }} }}
items = [{{ name = "Fee $MM/$YYYY", amount = 1, unit_price = "370", vat_rate = "23" }}]
"""


class FakeMiddleware:
    """Simulates the KROS API as seen through kros-middleware."""

    def __init__(self, existing_numbers=("FV260036", "FV260037")):
        self.invoices = []
        self.ids = itertools.count(1000)
        for number in existing_numbers:
            self.invoices.append(
                {
                    "id": next(self.ids),
                    "documentNumber": number,
                    "numberingSequence": "FV",
                    "externalId": "",
                    "issueDate": "2026-10-31",
                }
            )
        self.results = {}
        self.approvals = []
        self.calls = []
        self.skip_numbers = 0  # simulate KROS assigning an unexpected number
        self.tamper = None  # function(invoice) simulating KROS storing something else
        self.send_webhooks = True
        self.reject = False
        self.ignore_external_filter = False
        self.lookup_lag = 0
        self.fail_on_post = None
        self.lag_after_post = 0  # number of ExternalId lookups that miss right after a create

    def next_number(self):
        nums = [int(i["documentNumber"][4:]) for i in self.invoices if i["documentNumber"].startswith("FV26")]
        return f"FV26{max(nums, default=0) + 1 + self.skip_numbers:04d}"

    def list_invoices(self, params):
        rows = self.invoices
        if params.get("ExternalId"):
            if self.lookup_lag:
                self.lookup_lag -= 1
                return []
            if not self.ignore_external_filter:
                rows = [i for i in rows if i["externalId"] == params["ExternalId"]]
        if params.get("NumberingSequence"):
            rows = [i for i in rows if i.get("numberingSequence") == params["NumberingSequence"]]
        if params.get("DocumentNumberFrom"):
            lo, hi = params["DocumentNumberFrom"], params["DocumentNumberTo"]
            rows = [i for i in rows if lo <= i["documentNumber"] <= hi]
        if params.get("IssueDateFrom"):
            lo, hi = params["IssueDateFrom"], params["IssueDateTo"]
            rows = [i for i in rows if lo <= i["issueDate"][:10] <= hi]
        return rows

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, params, body))
        if method == "GET" and path == "/api/invoices":
            rows = self.list_invoices(params)
            skip = int(params.get("Skip") or 0)
            return 200, {"data": rows[skip : skip + int(params.get("Top") or 100)]}
        if method == "GET" and path.startswith("/api/invoices/"):
            inv = next(i for i in self.invoices if i["id"] == int(path.rsplit("/", 1)[1]))
            return 200, {"data": inv}
        if method == "POST" and path == "/api/invoices":
            data = body["data"]
            if self.fail_on_post and self.fail_on_post(data):
                return 400, {"isValid": False, "errors": [{"errorMessage": "boom"}]}
            request_id = f"00000000-0000-4000-8000-{len(self.results):012d}"
            if self.reject:
                self.results[request_id] = {
                    "status": 207,
                    "results": {"entities": [{"index": 0, "status": 400, "problems": [{"type": "x"}]}]},
                }
                return 202, {"requestId": request_id}
            number = self.next_number()
            net = sum(
                Decimal(str(i["totalPriceInclVat"])) / (1 + Decimal(str(i["vatRate"])) / 100) for i in data["items"]
            )
            gross = sum(Decimal(str(i["totalPriceInclVat"])) for i in data["items"])
            inv = {
                "id": next(self.ids),
                "documentNumber": number,
                "numberingSequence": data["numberingSequence"],
                "invoiceType": data["invoiceType"],
                "vatPayerType": data["vatPayerType"],
                "externalId": data["externalId"],
                "partner": data["partner"],
                "myCompany": data["myCompany"],
                "issueDate": data["issueDate"] + "T00:00:00",
                "deliveryDate": data["deliveryDate"] + "T00:00:00",
                "dueDate": data["dueDate"] + "T00:00:00",
                "prices": {
                    "currency": data.get("currency"),
                    "documentPrices": {"totalPrice": float(round(net, 2)), "totalPriceInclVat": float(gross)},
                },
                "items": [{"name": i["name"], "amount": i["amount"], "vatRate": i["vatRate"]} for i in data["items"]],
                "variableSymbol": data.get("variableSymbol") or number[2:],
                "eInvoice": {"isEInvoice": data["isEInvoice"], "status": None},
                "bankAccount": data.get("bankAccount"),
                "paymentType": data.get("paymentType"),
            }
            if self.tamper:
                self.tamper(inv)
            self.invoices.append(inv)
            self.lookup_lag = self.lag_after_post
            if self.send_webhooks:
                self.results[request_id] = {"status": 200, "results": {"entities": [{"index": 0, "status": 201}]}}
            return 202, {"requestId": request_id}
        if method == "GET" and path.startswith("/results/requests/"):
            payload = self.results.get(path.rsplit("/", 1)[1])
            return 200, {"events": [{"payload": payload}] if payload else []}
        if method == "GET" and path == "/results/approvals":
            return 200, {"approvals": self.approvals}
        if method == "POST" and path.endswith("/send"):
            invoice_id = int(path.split("/")[3])
            approval = {"approvalId": f"a{invoice_id}", "invoiceId": invoice_id, "status": "pending"}
            self.approvals.append(approval)
            return 202, {"approvalId": approval["approvalId"], "status": "pendingApproval"}
        raise AssertionError(f"unexpected call {method} {path}")

    def created(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == "/api/invoices"]

    def sends(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/send")]


class Silent:
    def __init__(self):
        self.messages = []

    def send(self, title, message, priority=3):
        self.messages.append((title, message, priority))


@pytest.fixture
def cfg(tmp_path):
    token = tmp_path / "token"
    token.write_text("km_test\n")
    path = tmp_path / "recurring.toml"
    path.write_text(CONFIG.format(token=token, lock=tmp_path / "lock", journal=tmp_path / "journal.json"))
    return kr.load_config(str(path))


def make_job(cfg, fake, today=date(2026, 11, 30), period=None):
    return kr.Job(cfg, fake, Silent(), today, period, kr.Journal(cfg.journal_file))


def test_build_body(cfg):
    job = make_job(cfg, FakeMiddleware())
    planned = job.build(cfg.templates[0])
    body = planned.body
    assert body["externalId"] == "recurring:acme:2026-11"
    assert body["issueDate"] == body["deliveryDate"] == "2026-11-30"
    assert body["dueDate"] == "2026-12-14"
    assert body["items"][0]["name"] == "Monthly fee for 11/2026"
    assert body["items"][0]["totalPriceInclVat"] == 184.5
    assert body["bankAccount"]["iban"] == "SK8511000000002925842663"
    assert "documentNumber" not in body and "variableSymbol" not in body
    assert body["isEInvoice"] is False
    assert planned.net == Decimal("150.00") and planned.gross == Decimal("184.50")
    fixed = job.build(cfg.templates[1]).body
    assert fixed["variableSymbol"] == "5526"


def test_einvoice_from_january_2027(cfg):
    job = make_job(cfg, FakeMiddleware(), today=date(2027, 1, 31))
    assert job.build(cfg.templates[0]).body["isEInvoice"] is True
    december = make_job(cfg, FakeMiddleware(), today=date(2026, 12, 31))
    assert december.build(cfg.templates[0]).body["isEInvoice"] is False


def test_february_and_leap_year(cfg):
    assert make_job(cfg, FakeMiddleware(), period="2028-02").issue_date == date(2028, 2, 29)
    assert make_job(cfg, FakeMiddleware(), period="2027-02").issue_date == date(2027, 2, 28)
    with pytest.raises(kr.JobError):
        make_job(cfg, FakeMiddleware(), period="2026-13")


def test_full_run_creates_numbers_in_order_and_requests_approval(cfg):
    fake = FakeMiddleware()
    report = make_job(cfg, fake).run(allow_late=False)
    assert [i["documentNumber"] for i in fake.invoices[-2:]] == ["FV260038", "FV260039"]
    assert len(fake.sends()) == 2
    assert report[0].startswith("FV260038: created and verified")
    assert report[2].startswith("FV260038: send approval requested")


def test_rerun_is_idempotent(cfg):
    fake = FakeMiddleware()
    make_job(cfg, fake).run(allow_late=False)
    make_job(cfg, fake).run(allow_late=False)
    assert len(fake.created()) == 2
    assert len(fake.sends()) == 2


def test_dry_run_creates_nothing_and_predicts_sequential_numbers(cfg):
    fake = FakeMiddleware()
    report = make_job(cfg, fake, today=date(2026, 11, 15)).dry_run()
    assert not fake.created() and not fake.sends()
    assert "FV260038" in report[0] and "FV260039" in report[1]


def test_refuses_other_day(cfg):
    with pytest.raises(kr.JobError, match="--allow-late"):
        make_job(cfg, FakeMiddleware(), today=date(2026, 12, 1), period="2026-11").run(allow_late=False)
    with pytest.raises(kr.JobError, match="future"):
        make_job(cfg, FakeMiddleware(), today=date(2026, 11, 15)).run(allow_late=True)
    with pytest.raises(kr.JobError, match="at most 7"):
        make_job(cfg, FakeMiddleware(), today=date(2026, 12, 9), period="2026-11").run(allow_late=True)
    fake = FakeMiddleware()
    make_job(cfg, fake, today=date(2026, 12, 2), period="2026-11").run(allow_late=True)
    assert len(fake.sends()) == 2


def test_unexpected_number_stops_before_sending(cfg):
    fake = FakeMiddleware()
    fake.skip_numbers = 1
    with pytest.raises(kr.JobError, match="document number"):
        make_job(cfg, fake).run(allow_late=False)
    assert len(fake.created()) == 1  # stopped after the first invoice
    assert not fake.sends()


def test_content_mismatch_stops_before_sending(cfg):
    fake = FakeMiddleware()
    fake.tamper = lambda inv: inv["prices"]["documentPrices"].update(totalPrice=149.99)
    with pytest.raises(kr.JobError, match="net total"):
        make_job(cfg, fake).run(allow_late=False)
    assert not fake.sends()


def test_rejected_upload_stops(cfg):
    fake = FakeMiddleware()
    fake.reject = True
    with pytest.raises(kr.JobError, match="rejected"):
        make_job(cfg, fake).run(allow_late=False)
    assert not fake.sends()


def test_missing_webhook_falls_back_to_lookup(cfg):
    fake = FakeMiddleware()
    fake.send_webhooks = False
    make_job(cfg, fake).run(allow_late=False)
    assert len(fake.sends()) == 2


def test_existing_send_not_requested_again(cfg):
    fake = FakeMiddleware()
    make_job(cfg, fake).run(allow_late=False)
    report = make_job(cfg, fake).run(allow_late=False)
    assert len(fake.sends()) == 2
    assert sum("already requested" in line for line in report) == 2


def test_duplicate_external_id_stops(cfg):
    fake = FakeMiddleware()
    for _ in range(2):
        fake.invoices.append({"id": next(fake.ids), "documentNumber": "X", "externalId": "recurring:acme:2026-11"})
    with pytest.raises(kr.JobError, match="2 invoices carry|ExternalId filter"):
        make_job(cfg, fake).run(allow_late=False)


def test_new_year_counter_starts_at_one(cfg):
    fake = FakeMiddleware(existing_numbers=())
    make_job(cfg, fake, today=date(2026, 1, 31)).run(allow_late=False)
    assert fake.invoices[0]["documentNumber"] == "FV260001"


@pytest.mark.parametrize(
    "broken, message",
    [
        ('base_url = "http://kros.example.com"', "localhost"),
        ("counter_digits = 0", "counter_digits"),
        ('name = "Fee $MONTH"', "placeholders"),
        ('variable_symbol = "55-26"', "variable_symbol"),
    ],
)
def test_config_validation(tmp_path, broken, message):
    token = tmp_path / "token"
    token.write_text("x")
    text = CONFIG.format(token=token, lock=tmp_path / "lock", journal=tmp_path / "j.json")
    replacements = {
        "base_url": ('base_url = "http://127.0.0.1:8095"', broken),
        "counter_digits": ("counter_digits = 4", broken),
        "placeholders": ('name = "Fee $MM/$YYYY"', broken),
        "variable_symbol": ('variable_symbol = "5526"', broken),
    }
    old, new = next(v for k, v in replacements.items() if k in message or k in broken)
    path = tmp_path / "c.toml"
    path.write_text(text.replace(old, new))
    with pytest.raises(kr.JobError, match=message):
        kr.load_config(str(path))


# ---------------------------------------------------------------------------- regressions from the review


def test_rerun_after_number_mismatch_stays_blocked(cfg):
    fake = FakeMiddleware()
    fake.skip_numbers = 1
    with pytest.raises(kr.JobError, match="document number"):
        make_job(cfg, fake).run(allow_late=False)
    fake.skip_numbers = 0
    with pytest.raises(kr.JobError, match="did not finish creating"):
        make_job(cfg, fake).run(allow_late=False)
    assert not fake.sends()


def test_lookup_lag_never_creates_a_duplicate(cfg):
    fake = FakeMiddleware()
    fake.send_webhooks = False
    fake.lag_after_post = 1  # the ExternalId lookup right after creation misses
    with pytest.raises(kr.JobError):
        make_job(cfg, fake).run(allow_late=False)
    for _ in range(2):
        with pytest.raises(kr.JobError, match="did not finish creating"):
            make_job(cfg, fake).run(allow_late=False)
    assert len(fake.created()) == 1
    assert len([i for i in fake.invoices if i.get("externalId") == "recurring:acme:2026-11"]) == 1
    assert not fake.sends()


def test_external_id_filter_ignored_fails_closed(cfg):
    fake = FakeMiddleware()
    fake.ignore_external_filter = True
    with pytest.raises(kr.JobError, match="fail closed"):
        make_job(cfg, fake).run(allow_late=False)
    assert not fake.created()


def test_confirm_and_forget(cfg):
    fake = FakeMiddleware()
    fake.skip_numbers = 1  # KROS assigns FV260039 although FV260038 was expected
    with pytest.raises(kr.JobError):
        make_job(cfg, fake).run(allow_late=False)
    job = make_job(cfg, fake)
    with pytest.raises(kr.JobError):
        job.confirm("recurring:acme:2026-11", "FV260038")  # wrong number is refused
    with pytest.raises(kr.JobError, match="gap"):
        job.confirm("recurring:acme:2026-11", "FV260039")
    assert "confirmed" in job.confirm("recurring:acme:2026-11", "FV260039", accept_gap=True)
    with pytest.raises(kr.JobError, match="still has"):
        job.forget("recurring:acme:2026-11")
    fake.skip_numbers = 0
    make_job(cfg, fake).run(allow_late=False)
    assert len(fake.sends()) == 2


def test_partial_failure_sends_nothing(cfg):
    fake = FakeMiddleware()
    fake.fail_on_post = lambda data: data["externalId"].startswith("recurring:fixed-vs")
    job = make_job(cfg, fake)
    with pytest.raises(kr.JobError, match="did not accept"):
        job.run(allow_late=False)
    assert not fake.sends()
    assert any("FV260038" in line for line in job.report)
    # the rejected one left no journal entry; the next run can continue
    fake.fail_on_post = None
    make_job(cfg, fake).run(allow_late=False)
    assert len(fake.sends()) == 2


def test_manual_invoice_for_period_stops_creation(cfg):
    fake = FakeMiddleware()
    fake.invoices.append(
        {
            "id": 1,
            "documentNumber": "FV260038",
            "numberingSequence": "FV",
            "externalId": "",
            "issueDate": "2026-11-30",
            "partner": {"registrationId": "45299951"},
            "items": [{"name": "Monthly fee for 11/2026"}],
            "prices": {"documentPrices": {"totalPriceInclVat": 184.5}},
        }
    )
    with pytest.raises(kr.JobError, match="similar invoice"):
        make_job(cfg, fake).run(allow_late=False)
    assert not fake.created()


def test_same_amount_in_period_needs_explicit_override(cfg):
    fake = FakeMiddleware()
    fake.invoices.append(
        {
            "id": 1,
            "documentNumber": "FV260038",
            "numberingSequence": "FV",
            "externalId": "",
            "issueDate": "2026-12-03",
            "partner": {"registrationId": "45299951"},
            "items": [{"name": "Monthly fee for 11/2026"}],
            "prices": {"documentPrices": {"totalPriceInclVat": 184.5}},
        }
    )
    # Same customer and amount inside the period: blocks until a human checks it ...
    with pytest.raises(kr.JobError, match="--accept-similar"):
        make_job(cfg, fake, today=date(2026, 12, 31), period="2026-12").run(allow_late=False)
    # ... then goes ahead with an explicit override, and reports the other invoice.
    job = make_job(cfg, fake, today=date(2026, 12, 31), period="2026-12")
    job.accept_similar = True
    job.run(allow_late=False)
    assert len(fake.sends()) == 2
    assert any(line.startswith("note: FV260038") for line in job.report)


def test_missing_supplier_in_kros_is_detected(cfg):
    fake = FakeMiddleware()
    fake.tamper = lambda inv: inv.pop("myCompany")
    with pytest.raises(kr.JobError, match="supplier"):
        make_job(cfg, fake).run(allow_late=False)
    assert not fake.sends()


def test_connection_errors_become_job_errors(monkeypatch):
    import http.client

    def boom(*a, **k):
        raise http.client.RemoteDisconnected("closed")

    monkeypatch.setattr(kr.urllib.request, "urlopen", boom)
    monkeypatch.setattr(kr.time, "sleep", lambda s: None)
    client = kr.Client("http://127.0.0.1:1", "t", 1)
    with pytest.raises(kr.JobError, match="may or may not"):
        client.request("POST", "/api/invoices", body={})
    with pytest.raises(kr.JobError, match="RemoteDisconnected"):
        client.request("GET", "/api/invoices")


@pytest.mark.parametrize(
    "old, new, message",
    [
        ('vat_rate = "23"', 'vat_rate = "0.23"', "VAT rate"),
        ('unit_price = "150.00"', 'unit_price = "-150"', "positive"),
        ("due_days = 14", "due_days = 400", "due_days"),
        ('einvoice_from = "2027-01-01"', 'einvoice_from = "2027-13-01"', "einvoice_from"),
        ('lock_file = "{lock}"', 'lock_file = "relative.lock"', "absolute"),
    ],
)
def test_strict_validation(tmp_path, old, new, message):
    token = tmp_path / "token"
    token.write_text("x")
    text = CONFIG.replace(old, new).format(token=token, lock=tmp_path / "lock", journal=tmp_path / "j.json")
    path = tmp_path / "c.toml"
    path.write_text(text)
    with pytest.raises(kr.JobError, match=message):
        kr.load_config(str(path))


def test_lock_excludes_second_run(cfg):
    with kr.single_instance(cfg.lock_file):
        with pytest.raises(kr.JobError, match="in progress"):
            with kr.single_instance(cfg.lock_file):
                pass


def test_catch_up_run_on_the_first_names_previous_period(cfg):
    with pytest.raises(kr.JobError, match="--period 2026-11 --allow-late"):
        make_job(cfg, FakeMiddleware(), today=date(2026, 12, 1)).run(allow_late=False)


def test_lagging_lookup_then_forget_cannot_duplicate(cfg):
    fake = FakeMiddleware()
    fake.send_webhooks = False
    fake.lag_after_post = 3
    with pytest.raises(kr.JobError):
        make_job(cfg, fake).run(allow_late=False)
    fake.lookup_lag = 3
    with pytest.raises(kr.JobError, match="still has"):
        make_job(cfg, fake).forget("recurring:acme:2026-11")
    fake.lookup_lag = 3
    with pytest.raises(kr.JobError):
        make_job(cfg, fake).run(allow_late=False)
    assert len(fake.created()) == 1


def test_ambiguous_http_status_keeps_journal_entry(cfg):
    fake = FakeMiddleware()
    original = fake.request

    def flaky(method, path, params=None, body=None):
        if method == "POST" and path == "/api/invoices":
            original(method, path, params, body)  # KROS processed it ...
            return 408, None  # ... but the client only saw a timeout
        return original(method, path, params, body)

    fake.request = flaky
    with pytest.raises(kr.JobError, match="unclear outcome"):
        make_job(cfg, fake).run(allow_late=False)
    fake.request = original
    with pytest.raises(kr.JobError, match="did not finish creating"):
        make_job(cfg, fake).run(allow_late=False)
    assert len([i for i in fake.invoices if i.get("externalId") == "recurring:acme:2026-11"]) == 1
