# Changelog

## 0.1.0 — unreleased

First version:
* restricting proxy to the KROS OpenAPI with scoped client tokens and route allowlists
* send approval with content-bound snapshots, approver credentials and one-time links
* webhook receiver with UTF-16LE HMAC verification, deduplication and a results API
* ntfy and SMTP notifications with overridable texts
* SQLite storage with configurable retention (minimum 90 days by default)
* rootless Podman Quadlet, nginx example and a hardened container image
