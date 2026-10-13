# fiscozen-mcp

Let your AI assistant prepare your [Fiscozen](https://www.fiscozen.it) invoices.

An MCP server (and Python client) for Fiscozen, the Italian service for freelancers with a
Partita IVA. You say *"invoice ACME 1500 EUR for September consulting"*; the agent shows you
exactly what Fiscozen will issue (number, rows, totals, legal notes, bank account), and only
after your OK it saves the invoice. **It never sends anything to SdI**: you review and send
from the Fiscozen web app, like always.

> **Unofficial.** Not affiliated with or endorsed by Fiscozen. Fiscozen has no public API:
> this uses the private REST API of its web app, mapped from recorded browser traffic. It
> can break whenever Fiscozen changes their app. You are responsible for the invoices you
> issue; use it at your own risk.

---

## Install with your AI

Open Claude Code (or Codex, Gemini CLI, any agent with a shell) and paste:

```
Fetch the fiscozen-mcp bootstrap procedure with
`curl -fsSL https://raw.githubusercontent.com/thekoma/fiscozen-mcp/main/BOOTSTRAP.md`,
then follow it to set up the Fiscozen MCP server for me.
```

The agent checks prerequisites, asks for your login email, has **you** store the password
in the macOS Keychain (it never sees it), registers the server and verifies it read-only.

## Install by hand

Requires [uv](https://docs.astral.sh/uv/) and a Fiscozen account with email + password
login.

```sh
# 1. Login email
mkdir -p ~/.config/fiscozen
printf 'email = "you@example.com"\n' > ~/.config/fiscozen/config.toml

# 2. Password in the macOS Keychain (elsewhere: FISCOZEN_PASSWORD in the server's env)
security add-generic-password -s fiscozen-mcp -a you@example.com -w

# 3. Register with Claude Code
claude mcp add fiscozen -s user -- uvx --from git+https://github.com/thekoma/fiscozen-mcp fiscozen-mcp
```

Optional settings in `~/.config/fiscozen/config.toml` (or the `FISCOZEN_*` env variables):

```toml
default_client_id = 1001          # client used when none is given (see list_clients)
default_payment_method_id = 4001  # else the only "Bonifico" method on the account
ateco = "70.20.09"                # else the only ATECO code on the account
```

## Tools

| Tool | What it does |
|---|---|
| `preview_invoice` | Fiscozen's own computation of the invoice: client, number, rows, automatic rows (stamp duty), totals, legal notes, bank account, possible duplicates. Saves nothing. Returns a `confirmation_token`. |
| `create_invoice` | Without a token: same as preview. With the token of that preview: saves the invoice, **unsent**. |
| `list_invoices`, `get_invoice` | Invoices with their SdI status (`TO_SEND`, `DELIVERED`, `REJECTED`, ...). |
| `delete_invoice` | Deletes an unsent invoice that is the latest of its year (keeps numbering gapless). Sent ones need a credit note. Token-confirmed. |
| `list_clients`, `check_vat` | Search customers; company name and address from a VAT number. |
| `create_client`, `delete_client` | Manage customers. Token-confirmed; refuses a client that already exists (same VAT number, tax code or name). |

## Safety model

Invoices are fiscal documents and every creation burns the next number, so writes are
deliberately hard to get wrong:

- **Human approval** is your MCP client's permission prompt. Don't "always allow"
  `create_invoice`, `create_client`, `delete_invoice` or `delete_client`.
- **What you approve is what gets written.** The `confirmation_token` fingerprints the
  whole preview, including the bank account and the duplicates shown. If anything changed
  (another invoice took the number, the IBAN was edited, arguments differ), the token is
  rejected and you get a fresh preview. A token works once.
- **No double invoices.** Creation is never retried automatically. An invoice for the same
  client and total within 7 days is flagged as a possible duplicate and needs an explicit
  override, previewed and confirmed on its own. If a create times out or the server answers
  oddly, further creates for that client and total are blocked until the invoice shows up
  or you override.
- **No SdI.** There is no code path that transmits to SdI.
- **Credentials**: the password comes from the Keychain or the environment and is never
  written to disk. The session cookie is cached in `~/.config/fiscozen/session.json`
  (0600), bound to your email, renewed automatically (~14 days).

Legal wording (reverse charge art. 7-ter, forfettario, stamp duty) is added by Fiscozen
itself; don't put it in notes. `charge_stamp_duty=true` (default) bills the 2 EUR stamp duty
to the customer when it applies.

## How it works

`src/fiscozen/client.py` replays the web app's calls: login (Django session + CSRF),
`compute_data` for the preview (Fiscozen picks the VAT code for the client, e.g. RC02 for EU
B2B reverse charge), then the same create payload the web app sends. The endpoint map is in
[docs/api.md](docs/api.md).

## Development

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest                          # offline, against fixtures from real traffic
FISCOZEN_LIVE=1 uv run pytest -m live  # read-only checks on your real account
```

Mapping a new endpoint:

1. Record the flow in a real browser: `uv run tools/capture.py` (HAR files land in
   `captures/`, git-ignored: they contain session cookies and your login).
2. Turn them into sanitized fixtures:
   `uv run tools/make_fixtures.py har captures/<file>.har`. Personal data is masked; to
   also anonymize business data (client names, amounts, ids), put a real-to-fake map in
   `captures/anonymize.json` (see the script's docstring).
3. Write the code and tests against those fixtures. Never guess payload shapes.

Contributor and agent rules are in [CLAUDE.md](CLAUDE.md).

## License

[MIT](LICENSE)
