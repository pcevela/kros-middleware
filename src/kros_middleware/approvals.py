"""Helpers for the send-approval workflow: invoice snapshots and content hashing."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

_EMAIL_RE = re.compile(r"^[^@\s<>\"',;]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}$")
SEND_BODY_KEYS = {"to", "cc", "bcc", "subject", "text"}
SENT_EINVOICE_STATES = {"pending", "sent", "confirmed"}


class SendBodyError(ValueError):
    pass


def validate_send_body(body: Any) -> dict[str, Any]:
    """Validate the optional body of POST /api/invoices/{id}/send (SendInvoiceRequest)."""
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise SendBodyError("body must be a JSON object")
    unknown = set(body) - SEND_BODY_KEYS
    if unknown:
        raise SendBodyError(f"unsupported fields: {', '.join(sorted(unknown))}")
    for key in ("to", "cc", "bcc"):
        if key in body and body[key] is not None:
            value = body[key]
            if not isinstance(value, list) or len(value) > 20:
                raise SendBodyError(f"{key} must be a list of at most 20 e-mail addresses")
            for address in value:
                if not isinstance(address, str) or not _EMAIL_RE.fullmatch(address):
                    raise SendBodyError(f"{key} contains an invalid e-mail address")
    for key, limit in (("subject", 300), ("text", 10_000)):
        if key in body and body[key] is not None and (not isinstance(body[key], str) or len(body[key]) > limit):
            raise SendBodyError(f"{key} must be a string of at most {limit} characters")
    return body


def _get(data: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def snapshot(invoice: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """The content of the invoice that the approver signs off on."""
    return {field: _get(invoice, field) for field in fields}


def content_hash(snap: dict[str, Any], send_body: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"invoice": snap, "send": send_body}, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalise_iban(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def document_key(sequence: Any, number: Any) -> tuple[str, str] | None:
    """Normalised (sequence, number) used to detect writes to an invoice under approval.

    Comparison ignores case, surrounding whitespace and leading zeros, because the
    upstream database may treat such variants as the same document.
    """
    if number is None or isinstance(number, bool) or not isinstance(number, (str, int)):
        return None
    num = _fold(number).lstrip("0") or "0"
    seq = _fold(sequence) if isinstance(sequence, (str, int)) and not isinstance(sequence, bool) else ""
    return seq, num


def _fold(value: Any) -> str:
    """NFKC, drop invisible format characters and whitespace, casefold."""
    text = unicodedata.normalize("NFKC", str(value))
    text = "".join(c for c in text if unicodedata.category(c) not in ("Cf", "Zs", "Cc") and not c.isspace())
    return text.casefold()


def summary(invoice: dict[str, Any], send_body: dict[str, Any], allowed_ibans: list[str]) -> dict[str, Any]:
    partner = _d(invoice.get("partner"))
    address = _d(partner.get("address"))
    prices = _d(invoice.get("prices"))
    document_prices = _d(prices.get("documentPrices"))
    bank = _d(invoice.get("bankAccount"))
    einvoice = _d(invoice.get("eInvoice"))
    iban = normalise_iban(bank.get("iban"))
    recipients = send_body.get("to") or ([partner.get("email")] if partner.get("email") else [])
    number = f"{invoice.get('numberingSequence') or ''} {invoice.get('documentNumber') or ''}".strip()
    return {
        "invoice_id": invoice.get("id"),
        "number": number or str(invoice.get("id")),
        "document_number": invoice.get("documentNumber"),
        "numbering_sequence": invoice.get("numberingSequence"),
        "customer": address.get("businessName") or address.get("contactName") or "",
        "customer_address": ", ".join(
            str(address.get(k)) for k in ("street", "postCode", "city", "country") if address.get(k)
        ),
        "partner_email": partner.get("email") or "",
        "tax_id": partner.get("taxId") or "",
        "vat_id": partner.get("vatId") or "",
        "registration_id": partner.get("registrationId") or "",
        "total": document_prices.get("totalPriceInclVat"),
        "currency": prices.get("currency") or "",
        "issue_date": invoice.get("issueDate") or "",
        "due_date": invoice.get("dueDate") or "",
        "variable_symbol": invoice.get("variableSymbol") or "",
        "iban": iban,
        "iban_allowed": (iban in allowed_ibans) if allowed_ibans else None,
        "recipients": recipients,
        "cc": send_body.get("cc") or [],
        "bcc": send_body.get("bcc") or [],
        "einvoice": bool(einvoice.get("isEInvoice")),
        "einvoice_status": einvoice.get("status") or "",
        "subject": send_body.get("subject") or "",
        "text": send_body.get("text") or "",
        "opening_text": invoice.get("openingText") or "",
        "closing_text": invoice.get("closingText") or "",
        "printed_note": invoice.get("printedNote") or "",
        "items": [
            {
                "name": _d(item).get("name"),
                "description": _d(item).get("description"),
                "amount": _d(item).get("amount"),
                "unit": _d(item).get("measureUnit"),
                "total": _d(_d(_d(item).get("prices")).get("documentPrices")).get("totalPriceInclVat"),
            }
            for item in (invoice.get("items") if isinstance(invoice.get("items"), list) else [])
        ],
    }
