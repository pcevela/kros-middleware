# kros-middleware

A small, security-focused service that sits between your automation (scripts, AI agents, cron jobs)
and the [KROS OpenAPI](https://api.krosdoplnky.sk) for KROS Fakturácia / KROS Sklad.

* **Holds the real KROS token.** Clients get their own revocable tokens with narrow scopes.
* **Restricting proxy.** Only explicitly listed routes are forwarded. Creating invoices sits behind a flag.
  Sending invoices to customers needs a **human approval**.
* **Receives KROS webhooks**, verifies their signatures and stores the results, so clients can read
  what actually happened to a request (`202 Accepted` only means "received").
* **Notifies** you over [ntfy](https://ntfy.sh) and/or e-mail.
* Keeps an **audit log**. Retention is configurable, 90 days by default and at minimum.

> Not affiliated with or endorsed by KROS a.s. "KROS" is a trademark of its owner.

## Why

The KROS API has one static token per company connection, with no scopes and no expiry. Anyone holding it can
create invoices, send them to your customers by e-mail and through the Peppol e-invoicing network, and post
payments. A sent e-invoice is locked and can only be fixed with a credit note. You probably do not want a
script, let alone an AI agent, to hold that token directly.

```
agent / script ──(client token)──▶ nginx (TLS) ──▶ kros-middleware ──(KROS token)──▶ api-economy.kros.sk
                                                        ▲   │
                    KROS webhooks (HMAC signed) ────────┘   └──▶ ntfy / e-mail ──▶ you ──▶ approval page
```

## How sending works

1. The client calls `POST /api/invoices/{id}/send` exactly as it would against KROS.
2. In `approval` mode (the default) the middleware does not forward the call. It fetches the invoice,
   stores a snapshot and a hash of its content (customer, items, totals, IBAN, due date, recipients…)
   and returns `202 {"approvalId": …, "status": "pendingApproval"}`.
3. You get a notification with a link to a review page. The link carries a random token that allows a single
   decision (it can still be used to view the outcome later). The page also requires **approver credentials
   (HTTP Basic)** that the client never has, so a client that can read your notifications still cannot approve
   its own request. The page shows every invoice field the approval is bound to.
4. On **Approve**, the middleware re-fetches the invoice and checks that it is unchanged. Only then does it
   call KROS send, once. If the invoice changed, the approval becomes `stale` and nothing is sent.
5. The client polls `GET /results/approvals/{approvalId}` and `GET /results/invoices/{id}/send-status`.

Other safety rails:
* While an approval is open, and after the send was submitted, the invoice cannot be overwritten through the
  proxy (`409 invoiceFrozen`). Document numbers are compared ignoring case, Unicode width, invisible characters,
  whitespace and leading zeros, and JSON keys that differ only in case are rejected. After the retention period
  the approval record is purged, and with it the freeze.
* There is at most one open approval per invoice, plus a cap on pending approvals.
* `options.send` in upload bodies is rejected unless sending is in `auto` mode.
* Send-like endpoints other than invoice send are never forwarded.
* Uploads that name a document number are tracked until KROS reports their result. While one is in flight,
  no approval can be created or executed for that invoice (`409 uploadInFlight`).
* Policy is re-checked when you approve: if sending was switched off or the IBAN allowlist changed since the
  request, the approval becomes `blocked` and nothing is sent.
* Sends are never retried automatically. A send interrupted by a timeout or a restart becomes `unknown`.
* An optional IBAN allowlist rejects invoices pointing to an unexpected bank account.

## Endpoints

| Path | Who | Purpose |
| --- | --- | --- |
| `GET/POST /api/...` | clients (Bearer client token) | Proxy to KROS with the same paths, subject to scopes and rules |
| `GET /results/requests/{requestId}` | `results` | Webhook results for your own upload (the `requestId` from `202`) |
| `GET /results/invoices/{id}/send-status` | `results` | Send-status events (Peppol / e-mail) for invoices you requested to send |
| `GET /results/approvals[/{approvalId}]` | `results` | Your approval requests and their outcome |
| `GET /results/events?since=&entityType=&afterId=&limit=` | `events` | All stored webhook events |
| `POST /webhooks/kros` | KROS | Webhook receiver (`X-Kros-Signature-256` verified) |
| `GET /approvals/{id}?token=…` | humans (approver credentials) | Review page with Approve / Reject |
| `GET /healthz` | anyone | Liveness |

Client scopes: `read` (GET routes), `issue` (create invoices, and any POST route added with `KM_ALLOW_RULES`),
`send` (request a send), `results` (own results), `events` (all webhook events, all clients' request results,
send statuses and approvals).

The default GET allowlist covers invoices, proforma invoices, received orders, delivery notes, expenses, payments,
numbering sequences, tags, warehouses, catalog items and attachments. Override it with `KM_ALLOW_RULES`.

## Configuration

Everything is configured with environment variables. Every secret can also be read from a file through the
`_FILE` suffix, for example `KM_UPSTREAM_TOKEN_FILE=/run/secrets/upstream_token`.

| Variable | Default | Description |
| --- | --- | --- |
| `KM_UPSTREAM_TOKEN` | — (required) | KROS API token |
| `KM_UPSTREAM_BASE_URL` | `https://api-economy.kros.sk` | KROS API base URL |
| `KM_PUBLIC_BASE_URL` | — (required) | Public HTTPS URL of this service, used in approval links |
| `KM_CLIENT_TOKENS` | — (required) | Lines `name:secret[:scope+scope]`. The secret is ≥ 32 chars, or `sha256$<hex>` of it. The default scopes are `read+results` |
| `KM_WEBHOOK_SECRET` | — (required) | ≥ 32 chars; the same value as KROS "Autorizačný kľúč" |
| `KM_WEBHOOK_SECRET_PREVIOUS` | — | Previous secret, accepted during rotation |
| `KM_WEBHOOK_PATH` | `/webhooks/kros` | Webhook path |
| `KM_WEBHOOK_ALLOW_UNSIGNED` | `false` | Accept webhooks without a secret configured (local testing only) |
| `KM_SEND_MODE` | `approval` | `off`, `approval` or `auto` (`auto` also needs `KM_SEND_AUTO_ACKNOWLEDGED=true`) |
| `KM_ISSUE_ENABLED` | `false` | Allow creating invoices (`KM_ISSUE_RULES`) |
| `KM_APPROVER_CREDENTIALS` | — (required in approval mode) | Lines `user:password` (≥ 32 chars, or `sha256$<hex>`) |
| `KM_APPROVAL_AUTH` | `basic` | `none` means the link alone approves; needs `KM_APPROVAL_AUTH_NONE_ACKNOWLEDGED=true` |
| `KM_APPROVAL_TTL_HOURS` | `24` | Lifetime of an approval request |
| `KM_APPROVAL_MAX_PENDING` | `20` | Maximum open approval requests (enforced atomically) |
| `KM_APPROVAL_HASH_FIELDS` | — | Additional invoice fields (dotted paths) to bind to the approval. The default set, which covers everything the page shows, always stays bound |
| `KM_ALLOWED_IBANS` | — | Optional allowlist of IBANs for invoices to be sent |
| `KM_ALLOW_RULES` | GET allowlist | `METHOD /path` rules; segments `{id}`, `{word}`, `*`, `**` |
| `KM_ISSUE_RULES` | invoice/proforma uploads | Routes enabled by `KM_ISSUE_ENABLED` |
| `KM_NOTIFY_CHANNELS` | — | `ntfy`, `email` or `ntfy,email` |
| `KM_NOTIFY_EVENTS` | sensible set | Any of `approval_requested`, `approval_decided`, `upload_result_ok`, `upload_result_problem`, `send_status_ok`, `send_status_problem`, `signature_invalid`, `retention_run` |
| `KM_NTFY_URL`, `KM_NTFY_TOPIC`, `KM_NTFY_TOKEN` | — | ntfy server, topic and access token |
| `KM_NTFY_PRIORITY`, `KM_NTFY_PRIORITY_PROBLEM`, `KM_NTFY_TAGS` | `3`, `4`, — | ntfy message options |
| `KM_SMTP_HOST`, `KM_SMTP_PORT`, `KM_SMTP_SECURITY` | —, 587/465, `starttls` | SMTP server (`starttls`, `ssl`, or `none` for testing only) |
| `KM_SMTP_USERNAME`, `KM_SMTP_PASSWORD` | — | SMTP login |
| `KM_SMTP_TIMEOUT` | `20` | Seconds |
| `KM_EMAIL_FROM`, `KM_EMAIL_TO`, `KM_EMAIL_SUBJECT_PREFIX` | — | Sender, recipients (comma-separated) and subject prefix |
| `KM_MESSAGES_FILE` | — | JSON file overriding notification texts (`{"event": {"title": "…", "body": "…"}}`, `$placeholders`) |
| `KM_TEMPLATE_DIR` | — | Directory with `approval.html` and/or `labels.json` to restyle or translate the approval page |
| `KM_RETENTION_DAYS` | `90` | Retention of webhook events, approvals and the audit log; below 90 needs `KM_RETENTION_ALLOW_BELOW_FLOOR=true` |
| `KM_RETENTION_INTERVAL_HOURS` | `24` | Purge interval |
| `KM_AUDIT_STORE_BODIES` | `false` | Store full request bodies in the audit log (personal data!). By default only a hash is stored |
| `KM_DB_PATH` | `/data/kros-middleware.sqlite3` | SQLite database |
| `KM_UPSTREAM_RATE_PER_SECOND`, `KM_UPSTREAM_BURST`, `KM_UPSTREAM_RATE_PER_MINUTE` | `5`, `10`, `240` | Client-side throttle (KROS allows 10/s and 300/min) |
| `KM_UPSTREAM_TIMEOUT` | `30` | Seconds |
| `KM_MAX_BODY_BYTES`, `KM_WEBHOOK_MAX_BODY_BYTES` | 1 MiB (max 12 MiB), 1 MiB | Request size limits |
| `KM_AUTH_FAILURES_PER_MINUTE` | `10` | Failed logins per client address before `429` |
| `KM_PROXY_PREFIX`, `KM_RESULTS_PREFIX`, `KM_APPROVALS_PREFIX` | `/api`, `/results`, `/approvals` | Path prefixes |
| `KM_LISTEN_HOST`, `KM_LISTEN_PORT` | `0.0.0.0`, `8080` | Bind address inside the container |
| `KM_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Address of nginx as seen from the container, trusted for `X-Forwarded-For` (see the env example) |
| `KM_LOG_LEVEL`, `KM_INSTANCE_NAME` | `info`, `kros-middleware` | |
| `KM_ALLOW_INSECURE_URLS` | `false` | Allow `http://` URLs and plain SMTP (local testing only) |

## Deployment (rootless Podman + nginx)

1. **Get the image.** Release images are published to the GitHub Container Registry for every version tag:
   ```sh
   podman pull ghcr.io/pcevela/kros-middleware:v0.2.2    # or :latest for the newest release
   ```
   Pin a version in production. Alternatively, build it yourself from a checkout and set
   `Image=localhost/kros-middleware:latest` in the Quadlet:
   ```sh
   podman build --format docker -t localhost/kros-middleware:latest -f Containerfile .
   ```
2. **Create the secrets.** Generate random values; never put them in the env file.
   ```sh
   openssl rand -base64 32 | tr -d '\n' | podman secret create kros-webhook-secret -
   printf 'agent:%s:read+issue+send+results\n' "km_$(openssl rand -hex 32)" | podman secret create kros-client-tokens -
   printf 'you:%s\n' "$(openssl rand -base64 33)" | podman secret create kros-approvers -
   ```
   This creates the first client. The secret holds one `name:token:scopes` line per client; add more later as
   described in [Issuing and revoking client tokens](#issuing-and-revoking-client-tokens). Keep a copy of the
   client token and the approver password in your password manager.
3. **Create the KROS connection.** In KROS Fakturácia, go to *Nastavenia firmy → API prepojenia*:
   * *Názov prepojenia*: anything recognisable
   * *URL pre príjem notifikácií*: `https://<your host>/webhooks/kros`
   * *Autorizačný kľúč*: the webhook secret from step 2 (`podman secret inspect --showsecret kros-webhook-secret`)
   * Save it and copy the token. **It is shown only once.**
   ```sh
   printf '%s' '<token>' | podman secret create kros-upstream-token -
   ```
4. **Install the Quadlet and the env file:**
   ```sh
   mkdir -p ~/.config/containers/systemd ~/.config/kros-middleware
   cp deploy/quadlet/* ~/.config/containers/systemd/
   cp deploy/kros-middleware.env.example ~/.config/kros-middleware/kros-middleware.env   # then edit
   systemctl --user daemon-reload && systemctl --user start kros-middleware
   loginctl enable-linger "$USER"   # keep running without a login session
   ```
   For automatic updates within a release line, point `Image=` at a moving tag, uncomment
   `AutoUpdate=registry` and enable `podman-auto-update.timer` (`systemctl --user enable --now podman-auto-update.timer`).
   Sending invoices is business-critical, so pinned versions with deliberate upgrades are the safer default.
5. **nginx:** adapt `deploy/nginx/kros-middleware.conf` (server name, certificates) and reload.
6. **Check:** `curl https://<your host>/healthz`, then `curl -H "Authorization: Bearer <client token>"
   https://<your host>/api/auth/check`.

### Rotating secrets

* **Client token:** issue a new one under another name (below), switch the client, revoke the old one.
* **Webhook secret:** set the old one as `KM_WEBHOOK_SECRET_PREVIOUS`, put the new one in KROS and in
  `KM_WEBHOOK_SECRET`, restart, and later remove the previous one.
* **KROS token:** reset the connection in KROS and replace the `kros-upstream-token` secret.

### Issuing and revoking client tokens

Client tokens live in the `kros-client-tokens` Podman secret, one `name:secret:scopes` line per client.
Scopes are `read`, `issue`, `send` and `results`, joined with `+`. Give every client its own token, with only
the scopes it needs. Store the token as `sha256$<hex>`: the server then holds only the hash, and the token
itself exists only in your password manager and in the client.

Run this as the user that runs the container (with systemd user sessions, e.g. `sudo machinectl shell <user>@`).
`--replace` needs Podman 4.7 or later.

```sh
NAME=claude-skill              # letters, digits, . _ - (unique)
SCOPES=read+issue+results      # see the skill README for suggestions

TOKEN="km_$(openssl rand -hex 32)"
{ podman secret inspect --showsecret --format '{{.SecretData}}' kros-client-tokens
  printf '%s:sha256$%s:%s\n' "$NAME" "$(printf '%s' "$TOKEN" | sha256sum | cut -d' ' -f1)" "$SCOPES"
} | podman secret create --replace kros-client-tokens -
systemctl --user restart kros-middleware

# names and scopes only (no secrets on screen)
podman secret inspect --showsecret --format '{{.SecretData}}' kros-client-tokens | grep -v '^#' | cut -d: -f1,3

curl -fsS -A kros-agent/1.0 -H "Authorization: Bearer $TOKEN" https://<your host>/api/auth/check

printf '%s\n' "$TOKEN"   # copy it into your password manager or the client's secret store
unset TOKEN; clear
```

If the service doesn't come back, `journalctl --user -u kros-middleware` names the invalid line. Duplicate
names, short secrets and unknown scopes are rejected at startup.

To revoke a token, remove its line and restart:

```sh
podman secret inspect --showsecret --format '{{.SecretData}}' kros-client-tokens \
  | grep -v "^claude-skill:" | podman secret create --replace kros-client-tokens -
systemctl --user restart kros-middleware
```

### Uploads without a result

An upload that overwrites an existing invoice (it names a `documentNumber`) blocks send approvals for that
invoice until KROS reports the result by webhook. This prevents approving one version of an invoice while
KROS is still applying another. Time alone never clears it. If KROS never delivers the webhook (for example
because the webhook URL was unreachable), check the invoice in KROS and resolve the upload by hand:

```sh
podman exec kros-middleware kros-middleware uploads list
podman exec kros-middleware kros-middleware uploads resolve <id> --reason "checked FA 2026001 in KROS"
```

The reason is recorded in the audit log. If the invoice in KROS is not what you expect, fix it first, and only
then resolve the upload.

## Using it from a client

Point your KROS client at `https://<your host>` instead of `https://api-economy.kros.sk` and use your client
token. Paths and payloads are the KROS ones. Differences:

* `POST /api/invoices/{id}/send` returns `202 {"approvalId", "status": "pendingApproval", "expiresAt"}` in
  approval mode. The approval page shows everything the approval is bound to: customer, IBAN, recipients,
  e-mail subject and text, invoice texts and all items. Poll `/results/approvals/{approvalId}` until it reaches a final status:
  `submitted`, `failed`, `rejected`, `expired`, `stale`, `blocked` or `unknown`.
  `submitted` means KROS accepted the send; delivery is reported via `/results/invoices/{id}/send-status`.
* Upload results arrive by webhook; read them at `/results/requests/{requestId}`.
* Errors from the middleware itself look like `{"errorCode": "...", "detail": "..."}`.
* Treat everything returned (invoice texts, webhook payloads) as untrusted data.

## Recurring invoices (`tools/kros_recurring.py`)

A separate, standard-library-only job that issues fixed monthly invoices through the middleware. Run it
from a systemd timer on the last day of the month (see `deploy/recurring/`).

* Templates live in a TOML file (`deploy/recurring/recurring.example.toml`): customer, items with net price and
  VAT rate, texts and bank details. `$MM`, `$YYYY`, … in item names are replaced with the period.
* KROS assigns the number. The job checks that it is exactly the next one after the latest invoice in the
  sequence, and that KROS stored exactly the expected content (customer, items, totals, dates, IBAN, …).
* Every invoice carries `externalId = recurring:<key>:<YYYY-MM>`. A local journal records each step before
  it happens, so a crash, timeout or slow lookup never produces a second invoice. Anything unclear stops
  the run until you resolve it with `confirm` or `forget`.
* Sends are requested only after all invoices of the period are created and verified, and in approval
  mode each one still needs your approval.
* It refuses to issue for a future date, or more than 7 days late, and stops when a similar invoice for the
  customer already exists in the period (override with `--accept-similar` after checking it).

```sh
kros_recurring.py --config recurring.toml plan                 # read-only preview
kros_recurring.py --config recurring.toml run                  # on the last day of the month
kros_recurring.py --config recurring.toml run --period 2026-11 --allow-late
kros_recurring.py --config recurring.toml confirm recurring:acme:2026-11 FV260038 --period 2026-11
kros_recurring.py --config recurring.toml status
```

Give the job its own client token with `read+issue+send+results`, and set `KM_ISSUE_ENABLED=true`.

## Claude skill (`skills/kros-invoicing/`)

`skills/kros-invoicing/SKILL.md` teaches an AI agent (Claude Code, Claude) to use this middleware: create an
invoice from details the user gives, read invoices, modify invoices that were not sent yet (full overwrite
with the exact response→upload field mapping), and request sending through the approval flow. Copy the folder
into `.claude/skills/` (or `~/.claude/skills/`), or upload it to your Claude account. The skill needs `KROS_MW_URL` and
a client token. [skills/kros-invoicing/README.md](skills/kros-invoicing/README.md) shows how to provide them
in Claude Code (local), Claude Code cloud sessions and scheduled tasks (environment variables and network
secrets), and the Claude apps (chat, Projects, Cowork), and which token scopes to use for each.

## Security notes

* Run it behind TLS only, and bind the container port to `127.0.0.1` (as the Quadlet does). Deploy it on a host
  you control: local users can reach `127.0.0.1:8080` directly, bypassing nginx's rate limits.
* Keep `KM_SEND_MODE=approval`. Use `auto` only if you accept that a compromised client can e-mail
  invoices to your customers.
* Protect the approval path even more if you can (VPN or IP allowlist in nginx).
* Use a private or authenticated ntfy topic. Approval links are one-time and useless without the approver
  credentials, but they still identify your invoices.
* Back up the `/data` volume if you need the audit trail. It contains customer data, so encrypt the backups.

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## Development

```sh
pip install -e '.[test]'
pytest
ruff check src tests
```

## License

Apache License 2.0, see [LICENSE](LICENSE).
