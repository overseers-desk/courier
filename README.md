# ![Courier](docs/logo.jpg)

[![CI](https://github.com/overseers-desk/courier/actions/workflows/code_checks.yml/badge.svg)](https://github.com/overseers-desk/courier/actions/workflows/code_checks.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Give your script or AI assistant access to your email.

## Contents

- [What your AI can do with it](#what-your-ai-can-do-with-it)
- [Installation](#installation)
- [Configuration](#configuration)
- [Quick test](#quick-test)
- [CLI usage](#cli-usage)
- [Claude Code (terminal-based Claude)](#claude-code-terminal-based-claude)
- [MCP server](#mcp-server)
- [Scripting and automation](#scripting-and-automation)
- [Library use](#library-use)
- [Faster searches](#faster-searches)
- [Connection handling](#connection-handling)
- [Security](#security)
- [License](#license)

Courier connects to your existing mailbox on Gmail, Outlook, Fastmail, or any IMAP provider. It does not create new email addresses or route mail through a third-party service.

Commandline users, script authors, and AI assistants can search, read, download, reply, send, and organize email. Two interfaces serve different environments: a CLI that outputs JSON (for terminal-based agents, scripts, and automation) and an MCP server (for web-based AI chats and MCP clients). Both expose the same operations.

## What your AI can do with it

- Courier runs on your machine, with no service of ours between you and your mailbox.
- Keep banking, OTPs, and sensitive senders out of the LLM's view, using per-mailbox Sieve rules.
- Reply from the right alias, using our identity feature.
- Gmail-style queries (`from:alice newer:3d is:unread`) work on any IMAP provider.
- An optional local `mu` index makes archive-grade searches near-instant.
- Search every mailbox in one call with `-A`.
- Drafts on your server by default; `--send` is opt-in.
- Handle a meeting invite (parse the `.ics`, draft an RSVP).
- Chain searches and reads in one call: `courier search foo search bar read -f INBOX -u 42`.
- Move, flag, or archive messages.
- Download attachments, export messages as HTML, extract links.

## Installation

The simplest cross-platform install is from PyPI:

```bash
pip install courier          # CLI only
pip install "courier[mcp]"   # CLI plus the MCP server
```

With uv, `uvx courier ...` runs it without installing and `uv tool install courier` installs it permanently.

See [INSTALLATION.md](docs/INSTALLATION.md) for Homebrew, Debian/Ubuntu (.deb), Fedora/RHEL (.rpm), and source installs.

## Configuration

Write the sample config and fill in your credentials:

```bash
mkdir -p ~/.config/courier
courier config-sample > ~/.config/courier/config.toml
```

A small config has three top-level named-entity tables: an `[imap.NAME]` mailbox, an `[smtp.NAME]` outgoing endpoint, and an `[identity.NAME]` describing one sendable address pointing at the IMAP block:

```toml
[smtp.gmail]
host = "smtp.gmail.com"
port = 587

[imap.personal]
host = "imap.gmail.com"
port = 993
username = "you@gmail.com"
# For Gmail, generate this at https://myaccount.google.com/apppasswords
password = "abcdefghijklmnop"
default_smtp = "gmail"

[identity.personal]
imap = "personal"
address = "you@gmail.com"
```

(Smaller is also valid: `[imap.*]` alone reads but cannot send; `[identity.*]` with `bcc` plus `[smtp.*]` sends but cannot read.)

For Gmail, the simpler path is the app-password example above. The alternative is OAuth2, which needs a Google Cloud project set up through Google's developer console (a much messier path); if you have already done that, the same `[imap.NAME]` block carries the OAuth2 keys instead of `password`:

```toml
[imap.personal]
host = "imap.gmail.com"
port = 993
username = "you@gmail.com"
client_id = "YOUR_CLIENT_ID"
client_secret = "YOUR_CLIENT_SECRET"
refresh_token = "YOUR_REFRESH_TOKEN"
default_smtp = "gmail"
```

`[smtp.NAME]` blocks declare named SMTP endpoints. When the block omits credentials, courier inherits them from the `[imap.NAME]` block in scope at send time, the right shape for Gmail and Fastmail where IMAP and SMTP share one credential. When the block carries its own `username` and `password`, courier uses those, the right shape for AWS SES and similar smarthosts where one IAM SMTP user serves many From addresses.

Sending requires at least one `[identity.NAME]` block pointing at the `[imap.NAME]`. A block with no identities is read-only for sending; drafting and reading still work. This is a valid state, not an error. Declaring identities explicitly avoids the registration-handle hazard, where an IMAP login (e.g. a Gmail handle that is not an intended sender) could otherwise become a sendable identity by accident.

`courier config-check` validates the config (cross-references, identity addresses, send-route resolution) without performing any IMAP or SMTP traffic. The same warnings surface on `courier`, `courier --help`, `courier status`, and `courier list`.

Gmail OAuth2 setup requires a Google Cloud project with the Gmail API enabled. See [GMAIL_SETUP.md](docs/GMAIL_SETUP.md) for the full walkthrough.

For multi-account configs (multiple `[imap.*]` blocks, send-as through several identities, SES smarthost routing), see [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Quick test

With uv (any platform):

```bash
uvx courier search "subject:invoice"
```

No installation step; `uvx` runs it directly. To install permanently:

```bash
uv tool install courier
```

On Debian and Ubuntu, the default install path is the `.deb` package from the GitHub release (see [INSTALLATION.md](docs/INSTALLATION.md)). As an alternative for running from a clone without installing, on Ubuntu 25.04 or later the CLI dependencies are all in the standard repositories:

```bash
sudo apt-get install python3-typer python3-dotenv python3-imapclient python3-requests
python3 -m courier search "subject:invoice"
```

Courier looks for a config file at `~/.config/courier/config.toml`. Use `--config /path/to/config.toml` to point to a different location.

The MCP server (`courier mcp`) requires the `mcp` Python package, which is not in apt. Use `uv` or `pip` for that. Many users prefer the CLI to MCP because the latter loads 80+ tools into every conversation; the CLI is the lighter footprint.

## CLI usage

Every command outputs JSON to stdout. Errors go to stderr. This makes Courier composable with `jq`, shell scripts, and AI agent skill definitions.

```bash
# Look up several keywords in one invocation; each gets its own outer key
# in the result, so hits stay attributed to the keyword that matched them
courier search "from:alice" search 'subject:"hotel booking"' search "is:unread"

# What's unread in INBOX?
courier search "is:unread" --folder INBOX --limit 10

# Read an email
courier read -f INBOX -u 4523

# List and download attachments
courier attachments -f INBOX -u 4523
courier save -f INBOX -u 4523 --attachment itinerary.pdf -o /tmp/itinerary.pdf

# Export an HTML email as a standalone file (images embedded)
courier export -f INBOX -u 4523 -o /tmp/email.html
courier export -f INBOX -u 4523 -o /tmp/email.eml --raw

# Extract all links from several emails
courier links -f INBOX -u 4523 -u 4524 -u 4525

# Reply, saved to drafts by default; --send transmits via SMTP
courier reply -f INBOX -u 4523 -b "Thanks, confirmed."
courier reply -f INBOX -u 4523 -b "Invoice attached." --attach /tmp/invoice.pdf
courier reply -f INBOX -u 4523 -b "Thanks, confirmed." --send

# Compose a new message (--send requires --identity NAME, or
# --smtp NAME --from EMAIL; see docs/CONFIGURATION.md)
courier compose --to alice@example.com --subject "Meeting" \
  -b "See attached." --send --identity work

# Send a draft
courier send-draft -f Drafts -u 4530

# Organize
courier move -f INBOX -u 4523 -t Archive
courier mark-read -f INBOX -u 4524
courier flag -f INBOX -u 4525
```

Run `courier --help` for the full command list.

## Claude Code (terminal-based Claude)

Run `courier install-claude-command` once after installation. This writes `~/.claude/commands/courier.md`, which tells Claude Code how to use the courier CLI for email tasks. After that, prompts like "find the booking confirmation from last week" or "reply to Alice's message" route through courier automatically.

## MCP server

For AI environments that cannot run shell commands (Claude web, Cursor, or any MCP client):

```bash
courier mcp
```

This starts an MCP server exposing the same operations as tools. The MCP package is only imported when this subcommand runs, so the CLI stays lightweight.

## Scripting and automation

Because every command returns JSON and uses non-zero exit codes on failure, Courier works as a building block in pipelines and cron jobs.

```bash
# Forward all emails from a sender to another folder
courier search "from:sender@example.com" --folder INBOX \
  | jq -r '.[].uid' \
  | xargs -I{} courier move -f INBOX -u {} -t Forwarded

# Daily digest: save today's unread subjects to a file
courier search "is:unread" --folder INBOX \
  | jq -r '.[].subject' > ~/daily-digest.txt

# Auto-acknowledge incoming invoices
courier search "is:unread subject:invoice" --folder INBOX \
  | jq -r '.[].uid' \
  | xargs -I{} courier reply -f INBOX -u {} -b "Received, processing." \
      --send --identity work
```

AI agents with skill/hook systems call Courier the same way: define a skill that runs a shell command and parses the JSON output.

## Library use

Courier is also a Python library. Import from the top-level `courier` package. The names in `courier.__all__` are the supported import surface: config loading, the error taxonomy, identity and SMTP resolution, the `ImapClient`, message models, the query parser, MIME building, and `send_with_fcc`. Anything not listed in `__all__` is internal and may reshape without announcement.

```python
from courier import (
    load_config,
    ImapClient,
    EmailAddress,
    create_mime,
    resolve_smtp_for_identity,
    send_with_fcc,
)

cfg = load_config()
identity = cfg.identities["work"]
imap_block = cfg.imap_blocks[identity.imap]
smtp = resolve_smtp_for_identity(identity, imap_block, identity.imap, cfg.smtp_blocks)

client = ImapClient(imap_block)
client.connect()

mime = create_mime(
    from_addr=EmailAddress(name=identity.name, address=identity.address),
    to=[EmailAddress.parse("alice@example.com")],
    subject="Hello",
    body="Sent from courier used as a library.",
)

# fcc_client files the sent copy in the server's Sent folder; drop it to skip.
result = send_with_fcc(mime, smtp, fcc_client=client)
print(result["fcc_uid"], result["fcc_error"])
```

## Faster searches

Courier can answer `search` from a local Xapian index instead of IMAP, orders of magnitude faster, with transparent fallback to IMAP when the index can't serve the query. See [docs/LOCAL_CACHE.md](docs/LOCAL_CACHE.md) for the offlineimap+mu setup.

## Connection handling

IMAP servers drop idle connections after 10-30 minutes. AI assistants work in bursts: a flurry of operations, then thinking time. Courier tracks connection age and reconnects transparently before operations fail. The default idle timeout is 300 seconds; set `idle_timeout` in the config to adjust.

## Security

Courier accesses your email account. Store credentials outside your repository (environment variables, a secrets manager, or a config file in `.gitignore`). Use app-specific passwords or OAuth2 rather than your main account password. Restrict `allowed_folders` in the config to limit what the tool can see.

For per-message control over what reaches the LLM, point an `[imap.NAME]` block's `redact` field at a Sieve script. Matching messages have subject, body, and party addresses blanked before courier returns them, so banking notices, OTPs, and other sensitive content stay out of the model's context window. See [examples/work-only.sieve](examples/work-only.sieve) for a starting policy.

## License

MIT. See [LICENSE](LICENSE).
