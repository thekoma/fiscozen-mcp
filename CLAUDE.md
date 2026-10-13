# fiscozen-mcp

Python client + MCP server for invoicing on Fiscozen (Italian forfettario P.IVA), built on
the web app's private REST API. API map: [docs/api.md](docs/api.md).

## Layout

- `src/fiscozen/invoice.py`: pure payload builders (no I/O)
- `src/fiscozen/client.py`: HTTP client (auth, session cache, endpoints)
- `src/fiscozen/mcp_server.py`: MCP tools (mcp 2.x `MCPServer`)
- `src/fiscozen/config.py`: env / `~/.config/fiscozen/config.toml` / Keychain password
- `tools/capture.py`: record a real browser session to `captures/*.har` (git-ignored)
- `tools/make_fixtures.py`: sanitized fixtures for `tests/fixtures/`
  (business data is anonymized via the git-ignored `captures/anonymize.json` real->fake map;
  fixtures must never contain the real client, amounts or Fiscozen ids)

## Commands

```sh
uv run ruff check . && uv run ruff format --check .
uv run pytest                          # offline
FISCOZEN_LIVE=1 uv run pytest -m live  # read-only on the real account
```

## Hard rules

- Never implement or call SdI sending. Invoices are created unsent; the user sends them.
- `POST /api/v1/invoices/` is not idempotent and consumes a fiscal invoice number: no
  retries on errors other than 401/403; timeouts/5xx are reported as "outcome unknown".
- Every write tool is two-step with a `confirmation_token` that fingerprints exactly what
  was previewed (content, number, duplicates shown, override flag). Keep it that way; never
  reintroduce a plain boolean confirm. The human gate is Claude Code's per-call permission
  prompt, so write tools must not be allow-listed.
- Money is `Decimal`, serialized as strings with 2 decimals. Never floats.
- Never let passwords, `sessionid` or `csrftoken` reach logs, exceptions, fixtures or
  commits. `captures/` stays git-ignored; fixtures only via `tools/make_fixtures.py`.
- Payload shapes must match real captured traffic; when unsure, record a new capture
  instead of guessing, and test against it offline.
- Don't put legal wording in invoice notes: Fiscozen adds it (reverse charge, bollo).
- Code, comments and commits in English.
