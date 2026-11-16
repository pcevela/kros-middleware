# Changelog

## Unreleased

* Security: an upload without a KROS result keeps blocking send approvals of that invoice until the result
  webhook arrives or an operator resolves it (`python -m kros_middleware uploads list|resolve`). Previously
  it stopped blocking after `KM_UPLOAD_SETTLE_SECONDS` (now removed and ignored), so an overwrite processed
  late by KROS could change an invoice after it was reviewed. Retention no longer deletes open uploads.
* Security: a result webhook that arrives before the middleware records the upload's `requestId` now closes
  the upload. Storing the event and closing uploads, and recording the `requestId` and checking for an
  already stored result, each happen in one transaction.
* Skill: looks for an existing URL and token before asking (project instructions, `KROS_MW_URL` /
  `KROS_MW_TOKEN`, a token attached by an outbound proxy such as a Claude network secret), and never repeats
  a pasted token in commands.
* Docs: `skills/kros-invoicing/README.md` covers installing the skill and providing the URL and token in
  Claude Code, cloud sessions and scheduled tasks, and the Claude apps; the README covers issuing and
  revoking hashed client tokens.

## 0.2.0

* New: `tools/kros_recurring.py`, a recurring monthly invoicing job with a crash-safe journal, number and
  content verification, and two-phase sending; systemd units and an example config in `deploy/recurring/`.
* Security: uploads naming a document number are tracked until KROS reports the result; approvals for that
  invoice are refused or blocked meanwhile (fixes a race where an in-flight overwrite could change an
  invoice between review and send).
* Security: send mode and IBAN allowlist are re-checked when an approval is executed (new status `blocked`).
* Security: the pending-approval cap is enforced in the same transaction as the insert.
* Security: `/results/events` rejects `limit` outside 1–500 and negative `afterId`.

## 0.1.0 — unreleased

First version:
* restricting proxy to the KROS OpenAPI with scoped client tokens and route allowlists
* send approval with content-bound snapshots, approver credentials and one-time links
* webhook receiver with UTF-16LE HMAC verification, deduplication and a results API
* ntfy and SMTP notifications with overridable texts
* SQLite storage with configurable retention (minimum 90 days by default)
* rootless Podman Quadlet, nginx example and a hardened container image
