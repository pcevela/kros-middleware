# kros-invoicing skill

A Claude skill that works with **KROS Fakturácia** invoices through a
[kros-middleware](../../README.md) instance. It can:

* create an invoice from details you give it,
* read invoices (list, search, detail, payment and sending state),
* correct invoices that were not sent yet,
* request sending, which the middleware turns into a human approval.

The skill contains no URLs, tokens or company data. It needs two values at run time:

| Value | Example | Secret? |
| --- | --- | --- |
| `KROS_MW_URL` | `https://kros-mw.example.com` | no |
| `KROS_MW_TOKEN` | a client token issued by the middleware | **yes** |

Skills have no settings or secret fields, so these values come from where the skill runs. Before it asks
you, the skill looks in this order:

* **URL:** what you said in the conversation or in project instructions, then the `KROS_MW_URL` env var.
* **Token:**
  1. the `KROS_MW_TOKEN` env var;
  2. a token an outbound proxy attaches (checked with one unauthenticated `GET /api/auth/check`);
  3. a token you gave in the conversation;
  4. a location you pointed to.

Pick one option per surface below.

## 1. Issue a client token

Create a dedicated token for each place you use the skill. You can then revoke one without touching the
others and see in the audit log who did what. The steps are in the main README under
[Issuing and revoking client tokens](../../README.md#issuing-and-revoking-client-tokens).

Which scopes to give it:

| Use | Scopes | Why |
| --- | --- | --- |
| Looking up invoices only | `read+results` | Can't change anything. Safe to paste into a chat |
| Interactive invoicing (you watch the session) | `read+issue+send+results` | Sending still needs your approval in the middleware |
| Token stored in a shared environment (network secret, env var) | `read+issue+results` | Every session in that environment can use it; leaving out `send` means it can't even request a send |
| Unattended scheduled runs that request sending | `read+issue+send+results` | Use an environment reserved for this job |

The middleware only issues invoices when `KM_ISSUE_ENABLED=true`. Every send goes through approval while
`KM_SEND_MODE=approval`.

## 2. Install the skill and provide the values

### Claude Code on your computer (terminal, IDE, desktop app with *Local*)

Install:

```sh
cp -r skills/kros-invoicing ~/.claude/skills/        # for all your projects
# or: cp -r skills/kros-invoicing .claude/skills/    # only for one repository
```

URL: put it in your shell profile (`export KROS_MW_URL=https://kros-mw.example.com`), or in the `env` block
of `~/.claude/settings.json`:

```json
{ "env": { "KROS_MW_URL": "https://kros-mw.example.com" } }
```

Token: load it from your password manager when you start Claude Code, so it is never stored in plain text.
Examples:

```sh
KROS_MW_TOKEN="$(security find-generic-password -s kros-mw -w)" claude    # macOS Keychain
KROS_MW_TOKEN="$(rbw get kros-mw)" claude                                 # Bitwarden / Vaultwarden (rbw)
KROS_MW_TOKEN="$(pass show kros-mw)" claude                               # pass
```

Avoid putting the token in `settings.json` or in a committed `.env`. Both are plain files, and the
project `.claude/settings.json` is usually committed.

### Claude Code cloud sessions and cloud scheduled tasks

These run in a **cloud environment** (claude.ai/code → environment settings). The same environment applies
to cloud sessions started from the browser, the apps, `claude --cloud`, and scheduled tasks.

Install: commit the skill to the repository the session clones (`.claude/skills/kros-invoicing/`), or add it
to your Claude account (see the next section). Account skills also load in Claude Code.

URL: add it to the environment's **environment variables**:

```text
KROS_MW_URL=https://kros-mw.example.com
```

Token: add a **network secret**. The agent proxy adds it to requests after they leave the session's VM, so
the token never appears in the session, its env vars, or the transcript:

* Secret type: **Bearer**
* Allowed websites: your middleware host, e.g. `kros-mw.example.com`
* Custom header: `Authorization`, prefix `Bearer`, value: the client token

The skill detects this with the unauthenticated probe and then sends no `Authorization` header of its own.
Notes:

* The secret is attached in **every** session in that environment, whoever starts it. Create a separate
  environment for KROS work, and give its token the narrowest scopes that work.
* A network secret can't be edited. To rotate it, delete it and add a new one.
* Network secrets exist only in Anthropic-hosted environments. In a self-hosted environment, use your own
  secret injection.
* Don't put the token in the environment's variables or setup script: anyone who uses the environment can
  read those.

### Claude apps: chat, Projects and Cowork (web, desktop, mobile)

Install: package the folder as a ZIP (`cd skills && zip -r kros-invoicing.zip kros-invoicing`) and upload
it under **Customize → Skills → Upload a skill**. Code execution must be enabled in Settings →
Capabilities. Skills you add to your account also load in Claude Code when you sign in with the same
account.

Network: the code sandbox only reaches allowed domains. In Settings → Capabilities → code execution, turn on
network egress and add your middleware host under the domain allowlist (on Team and Enterprise an Owner
sets this). If a session's sandbox still can't reach the host, use Claude Code instead.

URL: put a line in the **project instructions** (or say it in the chat):

```text
KROS_MW_URL=https://kros-mw.example.com
```

Token: these sessions have no secret store you can configure, so the token reaches the model one way or
another. Options, from best to worst:

1. **Read it from your computer.** If the session is linked to your computer through the desktop app,
   point the skill to where the token is (a file in a connected folder, or a password-manager command).
2. **Give it per conversation** when the skill asks. Prefer a `read+results` token here, or a short-lived
   one you revoke afterwards.
3. **Project instructions.** This works, but the token is then in the model's context in every chat of the
   project, and anyone the project is shared with can see it. A prompt injection in something Claude reads
   (an invoice text, a web page) could try to send it elsewhere. Use only a `read+results` token here.

Scheduled tasks created from the Claude apps run as cloud sessions, so use the cloud-environment setup
above for them, not project instructions.

## 3. Check it

Ask Claude something like *"List my KROS invoices from this month."* The skill first calls
`GET /api/auth/check`:

| Result | Meaning |
| --- | --- |
| `200` | Ready |
| `401 {"errorCode": "unauthorized"}` | Wrong or revoked token, or no token found |
| `403 {"errorCode": "scopeRequired"}` | The token lacks a scope for what you asked |
| plain `Unauthorized` | The middleware's own KROS token is wrong. Fix it on the server |
| connection refused / blocked | The host isn't on the sandbox's allowlist, or the middleware is down |
