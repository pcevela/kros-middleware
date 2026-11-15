# Notes for AI coding agents

- Never add AI attribution to commits or pull requests: no `Co-Authored-By: Claude …`,
  no `Claude-Session:` trailers, no "Generated with" lines.
- Commit as the repository owner's configured identity.
- Run `ruff check src tests` and `pytest -q` before committing; every security fix gets a regression test.
- Do not invent KROS API field or endpoint names; check them against the OpenAPI specification
  (https://api.krosdoplnky.sk/ai/kros-openapi.json).
