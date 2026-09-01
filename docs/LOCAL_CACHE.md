# Local Cache: offlineimap + mu

For AI agents searching across years of mail, IMAP is slow: every query round-trips to the server. Courier can answer `search`, `read`, `links`, and `attachments` from a local maildir instead, orders of magnitude faster, and falls back to IMAP transparently when the local copy can't serve the call.

This is opt-in. Without a `[local_cache]` block in the config, every `search` goes to IMAP exactly as before.

## How the pieces fit

Three components, each owned by a separate project:

- An IMAP-to-Maildir sync tool (e.g. [offlineimap](https://github.com/OfflineIMAP/offlineimap) or [mbsync/isync](https://isync.sourceforge.io/)) keeps a maildir on disk in sync with your IMAP server.
- [mu](https://www.djcbsoftware.nl/code/mu/) indexes the maildir into a Xapian database and answers queries.
- courier reads `mu`'s index for `search`, falling back to IMAP when `mu` is missing, the index is missing, the query is untranslatable, or `--no-cache` is given. It reads the maildir files directly for `read`, `links`, and `attachments`, independent of `mu` or the index; those fall back to IMAP when the file is not yet on disk or `--no-cache` is given.

Courier does not run any IMAP-to-Maildir syncer (e.g. `offlineimap`), nor `mu index`. The contract is "a maildir exists and `mu` indexes it"; how the maildir gets populated and how often `mu` re-indexes is your decision and runs outside courier.

## Prerequisites

Install an IMAP-to-Maildir syncer (e.g. offlineimap) and mu through your package manager, and set up syncing and indexing per their upstream documentation:

- offlineimap: https://github.com/OfflineIMAP/offlineimap (configuration in `~/.offlineimaprc`)
- mu: https://www.djcbsoftware.nl/code/mu/ (`mu init --maildir=<store root>`, then `mu index`)

One mu store can cover several accounts. Initialise it at a directory that contains the maildirs you want indexed. Give each `[imap.*]` block the subdirectory holding its own mail. mu reports every message's folder relative to the store root, and courier derives the block's scope from where its `maildir` sits under that root. With a single account, the store root and the block's `maildir` may be the same directory.

The maildir holds plain-text mail, and the Xapian index is repetitive, so both compress well. On btrfs, mounting the filesystem that holds them with `compress=zstd` cuts their disk cost. None of the commands here change.

A working setup ends with a maildir on disk and `mu find subject:hello` returning hits. Once that holds, courier can use it.

## Wiring courier

Add a `[local_cache]` block and a `maildir` field on the `[imap.*]` block whose mail you indexed:

```toml
[local_cache]
indexer = "mu"

[imap.gmail]
host = "imap.gmail.com"
username = "you@gmail.com"
password = "..."
maildir = "/path/to/maildir/you-gmail-com"

[identity.gmail]
imap = "gmail"
address = "you@gmail.com"
```

Courier serves from the index whenever one is present, whatever its age, and reports that age as `provenance.indexed_at` on every result. How old is too old depends on the question being asked, which courier does not know: "when did I last write to this person" tolerates a week, "did anything arrive this morning" tolerates nothing. A caller that needs live data passes `--no-cache`.

Courier locates the mu store by asking `mu info store`, which follows mu's own resolution: the `MUHOME` environment variable when set, mu's default location otherwise. Set `mu_index` in `[local_cache]` to the muhome path whenever the store sits anywhere else. Courier may be invoked by a cron job, an MCP client, or a desktop session, and none of those need share the environment your shell has.

## Contract and fallback

When `mu` is missing, the index is missing, the query is untranslatable, `--no-cache` is given, or any error occurs, `search` falls back to IMAP transparently. Folder-scoped searches are served from the cache like any other. Every `search` response carries a `provenance` field reporting `source` (`"local"` or `"remote"`), the index `indexed_at` timestamp, a `fell_back_reason` tag when applicable, a `date_source` naming which date a date bound was judged against (see [CONFIGURATION.md](CONFIGURATION.md)), and a `query` object recording the `dialect` the query was translated into along with any `approximations`, `fallbacks`, and terms `treated_as_text`. The caller can therefore detect when local served the query and when it did not.

`read`, `links`, and `attachments` serve from disk independent of the index: when the message file is present, it is read from the maildir with its synced flags, looked up by the IMAP UID that the syncer embeds in the maildir filename. offlineimap writes that segment as `,U=<uid>,` and mbsync as `,U=<uid>:`, and courier reads both. Reading a known UID's bytes consults no search index, so `mu` being missing or the index being stale does not push the call to IMAP; only `--no-cache` or the file not yet being on disk (e.g. a message arrived after the last sync) does, and IMAP also reflects current flags. Courier reports the index's age as `provenance.indexed_at` on every result, local or remote, so the caller can judge for itself whether the flags on a disk-served read are recent enough to trust. The UID is also surfaced on `search` results from the local cache, so search → read piping works the same way regardless of provenance. A maildir whose filenames carry no `U=<uid>` segment, written by a syncer that does not record the UID, still serves `search`; `read` for such a maildir always goes to IMAP because there is no UID-to-file index.

Use `--no-cache` on `search` or `read` to force live IMAP for a single call: to verify against the server, or when the index or the last sync is not trusted.

A `redact` policy on an `[imap.*]` block does not disable the cache. The policy is evaluated against the parsed on-disk message file at search and read time. Records whose policy matches are returned with sensitive fields blanked and `redacted_by` set, the same shape an IMAP-served call would return.
