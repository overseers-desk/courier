# Local Cache: offlineimap + mu

For AI agents searching across years of mail, IMAP is slow: every query round-trips to the server. Mailroom can answer `search`, `read`, `links`, and `attachments` from a local maildir instead, orders of magnitude faster, and falls back to IMAP transparently when the local copy can't serve the call.

This is opt-in. Without a `[local_cache]` block in the config, every `search` goes to IMAP exactly as before.

## How the pieces fit

Three components, each owned by a separate project:

- An IMAP-to-Maildir sync tool (e.g. [offlineimap](https://github.com/OfflineIMAP/offlineimap) or [mbsync/isync](https://isync.sourceforge.io/)) keeps a maildir on disk in sync with your IMAP server.
- [mu](https://www.djcbsoftware.nl/code/mu/) indexes the maildir into a Xapian database and answers queries.
- mailroom reads `mu`'s index for `search` and reads the maildir files directly for `read`, `links`, and `attachments`, all under one eligibility rule. When `mu` is missing, the index is missing or stale, the query is untranslatable, the file is not yet on disk, or `--no-cache` is given, it falls back to IMAP.

Mailroom does not run any IMAP-to-Maildir syncer (e.g. `offlineimap`), nor `mu index`. The contract is "a maildir exists and `mu` indexes it"; how the maildir gets populated and how often `mu` re-indexes is your decision and runs outside mailroom.

## Prerequisites

Install an IMAP-to-Maildir syncer (e.g. offlineimap) and mu through your package manager, and set up syncing and indexing per their upstream documentation:

- offlineimap: https://github.com/OfflineIMAP/offlineimap (configuration in `~/.offlineimaprc`)
- mu: https://www.djcbsoftware.nl/code/mu/ (`mu init --maildir=/path/to/maildir`, then `mu index`)

A working setup ends with a maildir on disk and `mu find subject:hello` returning hits. Once that holds, mailroom can use it.

## Wiring mailroom

Add a `[local_cache]` block and a `maildir` field on the `[imap.*]` block whose mail you indexed:

```toml
[local_cache]
indexer = "mu"
max_staleness_seconds = 4000

[imap.gmail]
host = "imap.gmail.com"
username = "you@gmail.com"
password = "..."
maildir = "/var/local/mail/you-gmail-com"

[identity.gmail]
imap = "gmail"
address = "you@gmail.com"
```

`max_staleness_seconds` is the threshold past which mailroom considers the index stale and falls back to IMAP for that query. Pick a value matching how often your sync job runs (e.g. if you run offlineimap every hour, `max_staleness_seconds = 4000` lets a slightly delayed sync still serve the query).

## Contract and fallback

When `mu` is missing, the index is missing or stale, the query is untranslatable, `--no-cache` is given, or any error occurs, the call falls back to IMAP transparently. Folder-scoped searches are served from the cache like any other, under the same eligibility rule. Every `search` response carries a `provenance` field reporting `source` (`"local"` or `"remote"`), the index `indexed_at` timestamp, and a `fell_back_reason` tag when applicable. The caller can therefore detect when local served the query and when it did not.

`read`, `links`, and `attachments` serve from disk under the same eligibility rule as `search`: when the index is fresh and the message file is present, the message is read from the maildir with its synced flags, looked up by the IMAP UID embedded in the mbsync-style filename (`,U=<uid>,`). The staleness window bounds how old those flags can be. When the index is stale, `--no-cache` is given, or the file is not yet on disk (e.g. a message arrived after the last sync), the call goes to live IMAP, which also reflects current flags. The UID is also surfaced on `search` results from the local cache, so search → read piping works the same way regardless of provenance. A maildir whose filenames do not embed `U=<uid>` (a non-mbsync layout) still serves `search`; `read` for such a maildir always goes to IMAP because there is no UID-to-file index.

Use `--no-cache` on `search` or `read` to force live IMAP for a single call: for freshness below the staleness window, to verify against the server, or when the index is not trusted.

A `redact` policy on an `[imap.*]` block does not disable the cache. The policy is evaluated against the parsed on-disk message file at search and read time. Records whose policy matches are returned with sensitive fields blanked and `redacted_by` set, the same shape an IMAP-served call would return.
