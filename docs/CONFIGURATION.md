# Multi-Account and Send-As Routing

This document covers the parts of the mailroom config that deal with more than one mailbox or more than one From address: multiple `[imap.*]` blocks, multiple `[smtp.*]` endpoints, and how identities select between them on send.

For the small single-account config, see the [Configuration section in the README](../README.md#configuration).

## Multiple `[imap.*]`, `[smtp.*]`, `[identity.*]` blocks

A single config holds multiple `[imap.*]` blocks, `[smtp.*]` endpoints, and `[identity.*]` blocks. Each identity declares which `[imap.NAME]` block it routes through:

```toml
default_imap = "personal"

[smtp.gmail]
host = "smtp.gmail.com"
port = 587

[smtp.ses-syd]
host = "email-smtp.ap-southeast-2.amazonaws.com"
port = 587
username = "AKIA..."
password = "BPa+..."

[imap.personal]
host = "imap.gmail.com"
username = "you@gmail.com"
password = "personal-app-password"
default_smtp = "gmail"

[identity.personal]
imap = "personal"
address = "you@gmail.com"

[imap.work]
host = "outlook.office365.com"
username = "you@company.com"
password = "work-app-password"

[identity.work]
imap = "work"
address = "you@company.com"
```

## Selecting an `[imap.*]` block with `--imap`

```bash
mailroom --imap work search "is:unread"
```

## One mailbox, several identities

A single `[imap.NAME]` block can have several identities, useful when one Gmail mailbox handles personal mail and an organisational alias routed through SES:

```toml
[imap.director]
host = "imap.gmail.com"
username = "alias-host@gmail.com"
password = "gmail-app-password"
default_smtp = "gmail"

[identity.director]
imap = "director"
address = "director@example.org"
name = "Director Name"
smtp = "ses-syd"
fcc = "[Gmail]/Sent Mail"

[identity.director-alias]
imap = "director"
address = "alias-host@gmail.com"
```

## Keeping a copy of sent mail: `fcc` and `bcc`

Two independent settings decide how an identity keeps a record of what it sends.

`fcc` controls the Sent copy filed by IMAP APPEND, mirroring `save_sent`'s tri-state:

- omitted: file into the `[imap.*]` block's Sent folder, following the host convention (Gmail auto-files, so mailroom skips its own copy);
- `fcc = "Folder Name"`: file into that folder explicitly;
- `fcc = false`: do not file a Sent copy.

`bcc` adds recipients to every send, as a string `bcc = "x@y.com"` or a list `bcc = ["x@y.com", "audit@y.com"]`. It is independent of `fcc`: an identity may keep a Sent copy and also BCC an address. Setting `bcc` no longer suppresses the Sent copy.

Every identity must retain a copy of its sent mail. That is satisfied by `fcc` (an `imap` block with `fcc` not set to `false`) or by a `bcc` that includes the identity's own address. When `fcc = false`, a self-inclusive `bcc` is required, and the config is rejected otherwise.

This covers a shared sending address that is itself a distribution list, for example `marketing@company.com`, the From address for the whole team. BCC the list so every member (including the sender) receives the record, and turn off the personal Sent copy so the sender does not also get a duplicate:

```toml
[identity.marketing]
imap    = "company"
address = "marketing@company.com"
bcc     = "marketing@company.com"
fcc     = false
```

## Picking a send identity (`--send` mode)

`compose --send`, `reply --send`, and `send-draft` require the route to be named explicitly. There are two forms.

**Mode A: `--identity NAME`.** Names a configured `[identity.NAME]` block; resolves From, display name, the `[imap.*]` block, the SMTP route, and the Sent folder.

```bash
mailroom compose --send --identity director \
  --to client@example.com -b "..."
```

**Mode B: `--smtp NAME --from EMAIL [--name N] [--fcc IMAP:FOLDER]`.** Sends a free-form `--from` through a named SMTP block, without consulting any `[identity.*]`. The SMTP block must carry its own username and password (no inheritance from an `[imap.*]` block, since none is in scope). Useful for relays like SES that are authorised to carry many addresses. With no `--fcc`, no copy is saved; with `--fcc work:Sent`, mailroom appends the message to the named folder on `[imap.work]` after a successful send.

```bash
mailroom compose --send --smtp ses-syd \
  --from "noreply@example.org" --name "Example Org" \
  --fcc director:Sent \
  --to client@example.com -b "..."
```

**Reply** has one extra path: when neither flag is given, mailroom matches the parent's recipients against identities on the selected `[imap.*]` block and uses the match. If no recipient matches, `reply --send` errors rather than silently picking an arbitrary identity. The drafting path (no `--send`) keeps the older fallback behaviour.

**`send-draft`** by default uses the draft's own From header and refuses to send if it does not match a configured identity. `--identity` or `--smtp/--from` override the draft's From for that send.

Drafting (no `--send`) keeps the previous convenience defaults: the first identity on the selected `[imap.*]` block is the From, and `--from EMAIL` selects a different identity by address.

## Claude Code integration

Mailroom ships a Claude Code command definition that tells Claude how to invoke the CLI for email tasks. Once registered, Claude routes requests like "find the invoice from last week" or "reply to Alice's message" through mailroom automatically.

To register it, run:

```bash
mailroom install-claude-command
```

This writes `~/.claude/commands/mailroom.md`. The file is bundled inside the mailroom package, so the same command works regardless of how mailroom was installed (Homebrew, `.deb`, `.rpm`, `pip`, or `uv`).

`mailroom status` will note if `~/.claude` is present but mailroom is not yet registered.
