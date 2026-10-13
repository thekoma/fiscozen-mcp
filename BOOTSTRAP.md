# fiscozen-mcp BOOTSTRAP

This file is for an AI agent (Claude Code, Codex, Gemini CLI, ...). The user pointed you at
this repo and asked you to set up the Fiscozen MCP server for them. Follow these steps in
order. Talk to the user in their language; keep commands and config in English.

## Ground rules (read first)

- **Never ask for, read, echo or store the user's Fiscozen password in the chat, in a
  file, or in a command you run.** The user stores it themselves (Step 3).
- **This server never sends invoices to SdI**, and you must not try to make it do so.
- Do not create, delete or modify any invoice or client during setup. Setup is read-only
  on the Fiscozen account.
- Stop and tell the user if a step fails; do not improvise workarounds that touch
  credentials.

## Step 0: Prerequisites

Check, and report what is missing:

```sh
uv --version          # required: https://docs.astral.sh/uv/
uname -s              # Darwin = macOS (Keychain available); otherwise see Step 3b
```

If `uv` is missing, give the user the official install command from the uv docs and stop
until they confirm it is installed.

## Step 1: Fiscozen login email

Ask the user for the email they use to log in at https://app.fiscozen.it (email + password
login; accounts that only use SPID or Google login are not supported).

## Step 2: Config file

Create `~/.config/fiscozen/config.toml` (do not overwrite an existing one without asking):

```sh
mkdir -p ~/.config/fiscozen && chmod 700 ~/.config/fiscozen
printf 'email = "%s"\n' "<EMAIL>" > ~/.config/fiscozen/config.toml
```

## Step 3a: Password on macOS (Keychain)

Ask the user to run this **themselves**. In Claude Code they can prefix it with `!` so it
runs in their session; the prompt for the password is interactive and never reaches you:

```sh
security add-generic-password -s fiscozen-mcp -a "<EMAIL>" -w
```

Verify only that the item exists (this does not print the password):

```sh
security find-generic-password -s fiscozen-mcp -a "<EMAIL>" >/dev/null && echo present
```

## Step 3b: Password elsewhere (Linux, Windows)

There is no Keychain integration outside macOS. The server reads `FISCOZEN_PASSWORD` from
its environment. Explain the trade-off to the user (the value ends up in the MCP client's
config file in plain text) and let them add it themselves in Step 4 with
`-e FISCOZEN_PASSWORD=...`, or export it in the environment that starts their MCP client.

## Step 4: Register the MCP server

Claude Code (user scope, available in every project):

```sh
claude mcp add fiscozen -s user -- uvx --from git+https://github.com/thekoma/fiscozen-mcp fiscozen-mcp
claude mcp get fiscozen     # expect: Status: Connected
```

Other MCP clients: the server is a standard stdio server. Command
`uvx`, args `["--from", "git+https://github.com/thekoma/fiscozen-mcp", "fiscozen-mcp"]`.
For example in a JSON-configured client:

```json
{
  "mcpServers": {
    "fiscozen": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/thekoma/fiscozen-mcp", "fiscozen-mcp"]
    }
  }
}
```

## Step 5: Verify (read-only)

Ask the user to restart or reload their MCP client so the tools appear. Then call:

1. `list_clients` and show the user their customers.
2. `list_invoices` for the current year.

If login fails, the error says why (wrong password, missing Keychain item). Fix the cause
with the user before trying again; don't loop on failed logins.

## Step 6: Optional default client

If the user mostly invoices one customer, take its `client_id` from `list_clients` and add:

```sh
printf 'default_client_id = %s\n' "<CLIENT_ID>" >> ~/.config/fiscozen/config.toml
```

## Step 7: Tell the user how it works

Summarize in their language:

- Ask in plain words, e.g. "invoice ACME 1500 EUR for September consulting". The agent
  previews it (number, rows, totals, legal notes, bank account); nothing is saved.
- After they approve, the agent creates it with the preview's `confirmation_token`. The
  invoice is **saved but not sent**: they review and send it to SdI from the Fiscozen web
  app, then email the courtesy PDF to the customer.
- **Do not "always allow" `create_invoice`, `create_client`, `delete_invoice` or
  `delete_client`** in the MCP client's permission prompt: that prompt is their approval.
- Legal wording (reverse charge, forfettario, stamp duty) is added by Fiscozen; they
  should not put it in notes.

Setup is done. Do not commit or change anything in the user's current repository.
