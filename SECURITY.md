# Security policy

Please report vulnerabilities privately through GitHub's
"Report a vulnerability" (Security → Advisories) on this repository, not in public issues.

Include a description, the affected version, and steps to reproduce. You should get an answer within
a week.

## Scope

In scope: authentication and authorisation bypasses, bypassing the send-approval flow, leaking the upstream
KROS token or other secrets, webhook signature bypasses, injection into the approval page or notifications.

Out of scope: issues that require an already compromised host, or a deliberately insecure configuration
(`KM_SEND_MODE=auto`, `KM_APPROVAL_AUTH=none`, `KM_ALLOW_INSECURE_URLS=true`,
`KM_WEBHOOK_ALLOW_UNSIGNED=true`, `KM_FORWARDED_ALLOW_IPS=*`, wildcard allow rules).
