"""Rendering of the approval page. All dynamic values are HTML-escaped."""

from __future__ import annotations

import json
from html import escape
from importlib import resources
from pathlib import Path
from string import Template
from typing import Any

DEFAULT_LABELS = {
    "lang": "en",
    "page_title": "Send invoice $number?",
    "number": "Invoice",
    "customer": "Customer",
    "customer_address": "Customer address",
    "partner_email": "Customer e-mail on invoice",
    "tax_id": "DIČ",
    "vat_id": "IČ DPH",
    "registration_id": "Company ID (IČO)",
    "total": "Total incl. VAT",
    "issue_date": "Issue date",
    "due_date": "Due date",
    "variable_symbol": "Variable symbol",
    "iban": "IBAN on invoice",
    "recipients": "E-mail recipients",
    "cc": "Cc",
    "bcc": "Bcc",
    "einvoice": "E-invoice via Peppol",
    "client": "Requested by",
    "subject": "E-mail subject",
    "text": "E-mail text",
    "opening_text": "Opening text",
    "closing_text": "Closing text",
    "printed_note": "Note on invoice",
    "previously_sent": "This invoice was already submitted for sending on $when ($status). Approving sends it again.",
    "check_pdf": "Also check the invoice PDF in KROS before approving.",
    "bound_fields": "All invoice data this approval is bound to",
    "expires_at": "Request expires",
    "items": "Items",
    "yes": "yes",
    "no": "no",
    "approve": "Approve and send",
    "reject": "Reject",
    "iban_not_allowed": "The IBAN on this invoice is not in the list of allowed accounts.",
    "already_sent": "KROS reports this e-invoice as already sent ($status). Approving sends the e-mail link again.",
    "footer": "Approving sends the invoice to the customer. A sent e-invoice cannot be changed, only credited.",
    "state": "Status: $status",
}


def load_template(template_dir: str | None) -> Template:
    if template_dir:
        custom = Path(template_dir) / "approval.html"
        if custom.is_file():
            return Template(custom.read_text(encoding="utf-8"))
    text = resources.files("kros_middleware").joinpath("templates/approval.html").read_text(encoding="utf-8")
    return Template(text)


def load_labels(template_dir: str | None) -> dict[str, str]:
    labels = dict(DEFAULT_LABELS)
    if template_dir:
        custom = Path(template_dir) / "labels.json"
        if custom.is_file():
            data = json.loads(custom.read_text(encoding="utf-8"))
            labels.update({k: str(v) for k, v in data.items() if k in DEFAULT_LABELS})
    return labels


def _e(value: Any) -> str:
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    return escape("" if value is None else str(value), quote=True)


def _pre(value: Any) -> str:
    text = _e(value)
    return f'<div style="white-space:pre-wrap">{text}</div>' if text else ""


def render_page(
    template: Template,
    labels: dict[str, str],
    summary: dict[str, Any],
    approval: dict[str, Any],
    action_base: str | None,
    token: str | None,
    message: str | None = None,
) -> str:
    lab = {k: _e(v) for k, v in labels.items()}
    yes_no = lambda v: lab["yes"] if v else lab["no"]  # noqa: E731
    rows = [
        ("number", f'<span class="key">{_e(summary.get("number"))}</span>'),
        ("customer", f'<span class="key">{_e(summary.get("customer"))}</span>'),
        ("registration_id", _e(summary.get("registration_id"))),
        ("total", f'<span class="key">{_e(summary.get("total"))} {_e(summary.get("currency"))}</span>'),
        ("issue_date", _e(summary.get("issue_date"))),
        ("due_date", _e(summary.get("due_date"))),
        ("variable_symbol", _e(summary.get("variable_symbol"))),
        ("iban", f'<span class="key">{_e(summary.get("iban"))}</span>'),
        ("recipients", f'<span class="key">{_e(summary.get("recipients"))}</span>'),
        ("cc", _e(summary.get("cc"))),
        ("bcc", _e(summary.get("bcc"))),
        ("einvoice", yes_no(summary.get("einvoice"))),
        ("customer_address", _e(summary.get("customer_address"))),
        ("partner_email", _e(summary.get("partner_email"))),
        ("tax_id", _e(summary.get("tax_id"))),
        ("vat_id", _e(summary.get("vat_id"))),
        ("subject", _e(summary.get("subject"))),
        ("text", _pre(summary.get("text"))),
        ("opening_text", _pre(summary.get("opening_text"))),
        ("closing_text", _pre(summary.get("closing_text"))),
        ("printed_note", _pre(summary.get("printed_note"))),
        ("client", _e(approval.get("client"))),
        ("expires_at", _e(approval.get("expires_at"))),
    ]
    rows_html = "\n".join(f"<tr><th>{lab[key]}</th><td>{value}</td></tr>" for key, value in rows)

    warnings = []
    if summary.get("iban_allowed") is False:
        warnings.append(f'<div class="warn">{lab["iban_not_allowed"]}</div>')
    if summary.get("einvoice_status") in ("pending", "sent", "confirmed"):
        text = Template(labels["already_sent"]).safe_substitute(status=summary.get("einvoice_status"))
        warnings.append(f'<div class="warn">{_e(text)}</div>')

    previous = summary.get("previously_sent")
    if previous:
        text = Template(labels["previously_sent"]).safe_substitute(
            when=previous.get("at", ""), status=previous.get("status", "")
        )
        warnings.append(f'<div class="warn">{_e(text)}</div>')
    warnings.append(f'<div class="state">{lab["check_pdf"]}</div>')

    items = summary.get("items") or []
    items_html = ""
    if items:
        lis = "".join(
            f"<li><b>{_e(i.get('name'))}</b> — {_e(i.get('amount'))} {_e(i.get('unit'))}"
            f" = {_e(i.get('total'))}{_pre(i.get('description'))}</li>"
            for i in items
        )
        items_html = f"<h2>{lab['items']}</h2><ul>{lis}</ul>"

    state_text = message or Template(labels["state"]).safe_substitute(status=approval.get("status"))
    state_block = f'<div class="state">{_e(state_text)}</div>'

    actions = ""
    if action_base and token:
        hidden = f'<input type="hidden" name="token" value="{_e(token)}">'
        actions = (
            f'<form method="post" action="{_e(action_base)}/approve">{hidden}'
            f'<button class="approve" type="submit">{lab["approve"]}</button></form>'
            f'<form method="post" action="{_e(action_base)}/reject">{hidden}'
            f'<button class="reject" type="submit">{lab["reject"]}</button></form>'
        )

    snapshot = approval.get("snapshot")
    bound_html = ""
    if snapshot:
        try:
            snap = json.loads(snapshot)
        except ValueError:
            snap = {}
        bound_rows = "".join(
            f'<tr><th>{_e(k)}</th><td><code style="white-space:pre-wrap">'
            f"{_e(json.dumps(v, ensure_ascii=False, indent=1))}</code></td></tr>"
            for k, v in sorted(snap.items())
            if v not in (None, "", [], {})
        )
        bound_html = f"<details><summary>{lab['bound_fields']}</summary><table>{bound_rows}</table></details>"

    title = Template(labels["page_title"]).safe_substitute(number=summary.get("number") or "")
    return template.safe_substitute(
        lang=lab["lang"],
        page_title=_e(title),
        state_block=state_block,
        warnings="\n".join(warnings),
        rows=rows_html,
        items_block=items_html + bound_html,
        actions=actions,
        footer=lab["footer"],
    )
