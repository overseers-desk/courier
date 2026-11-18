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

When `mu` is missing, the index is missing, the query is untranslatable, `--no-cache` is given, or any error occurs, `search` falls back to IMAP transparently. A search the index answers with nothing is served as that empty result, not confirmed against the server: provenance carries a hint naming that the server was not consulted and that `--no-cache` re-runs the search against IMAP. The caller knows whether the interest is current or historical and decides whether the live search is worth its round trip. Folder-scoped searches are served from the cache like any other. `in:sent` is served from the Sent folder of the maildir: the server's `\Sent` folder when this run already listed folders, otherwise the first of the usual names (`Sent`, `INBOX.Sent`, `Sent Items`, `[Gmail]/Sent Mail` and so on) present on disk; a maildir with no Sent folder falls back to IMAP with `fell_back_reason` `folder_not_synced`. A call the cache answers makes no network connection: courier logs in only when a call needs the server (a fallback, `--no-cache`, `export --raw`, any write), so `search`, `read -f FOLDER -u UID` and `read --message-id` work with the network down. `read --message-id` finds the message through the index and reads its file; it searches the server only when the index cannot answer. Every `search` response carries a `provenance` field reporting `source` (`"local"` or `"remote"`), the index `indexed_at` timestamp, a `fell_back_reason` tag when applicable, a `date_source` naming which date a date bound was judged against (see [CONFIGURATION.md](CONFIGURATION.md)), and a `query` object recording the `dialect` the query was translated into along with any `approximations`, `fallbacks`, and terms `treated_as_text`. The caller can therefore detect when local served the query and when it did not.

`read`, `links`, and `attachments` serve from disk independent of the index: when the message file is present, it is read from the maildir with its synced flags, looked up by the IMAP UID that the syncer embeds in the maildir filename. offlineimap writes that segment as `,U=<uid>,` and mbsync as `,U=<uid>:`, and courier reads both. Reading a known UID's bytes consults no search index, so `mu` being missing or the index being stale does not push the call to IMAP; only `--no-cache` or the file not yet being on disk (e.g. a message arrived after the last sync) does, and IMAP also reflects current flags. Courier reports the index's age as `provenance.indexed_at` on every result, local or remote, so the caller can judge for itself whether the flags on a disk-served read are recent enough to trust. The UID is also surfaced on `search` results from the local cache, so search → read piping works the same way regardless of provenance. A maildir whose filenames carry no `U=<uid>` segment, written by a syncer that does not record the UID, still serves `search`; `read` for such a maildir always goes to IMAP because there is no UID-to-file index.

Use `--no-cache` on `search` or `read` to force live IMAP for a single call: to verify against the server, or when the index or the last sync is not trusted.

## How a cached answer compares with the server's

A search the index answers gives the answer the server would give to the same query, or says how its answer differs; [INVARIANTS.md](../INVARIANTS.md) states the rule. Each row is built from the message's file the way a live search builds its rows, so names, subject, date, attachments, the system flags (`\Seen`, `\Answered`, `\Flagged`, `\Deleted` and `\Draft`, read from the maildir file name) and the `<...>` Message-ID read the same whichever side answered. A cached row also carries the file's `path`, and carries no `uid` when the file name records none. A page cut at the limit reports the index's full match count as `total_count`. A whole-account search of a Gmail maildir (one holding a `[Gmail]` folder) shows each message once, as the server's All Mail does. `msgid:` matches the whole Message-ID on both sides. Under `WORLD_AS_OF`, mail dated after the bound leaves before the limit cut, as it does on the server.

Where matching the server would cost the index its speed or its offline answer, the cached answer can differ, and `provenance.query.approximations` says how: one sentence per difference, each ending with the `--no-cache` retry. `--format text` prints them as `# note:` lines, `--format oneline` writes them to stderr and leaves stdout to the rows, and JSON and MCP carry them in the envelope. The differences disclosed:

- Text operators (`from:`, `to:`, `cc:`, `bcc:`, `subject:`, `list:`, `filename:`, `body:`) and bare words and phrases: the index matches whole words and whole addresses, ignores accents and HTML markup, and reads only the fields it indexes, while the server matches by its own rules, often on parts of words and addresses (Gmail's `to:` also searches Cc), so either side can find messages the other misses.
- `larger:` and `smaller:`: the index measures each message as stored on disk, often with shorter line endings than the server counts, so messages near the bound can differ.
- `newer:` and `older:`: the index counts whole days from the start of the local day, by each message's Date header, while the server counts from the date it received the message, to the second where it can.
- `after:`, `before:`, `on:`, `today`, `yesterday`, `week` and `month`: the index judges each message's Date header in the local time zone, and a message without one matches no date, while the server judges the date it received the message, in its own time zone.
- A double quote inside a value: the index treats quote characters as token boundaries, so the quote matches as a space, while the server matches the quote itself.
- Flags: a row lists only the flags a maildir file name records (`\Seen`, `\Answered`, `\Flagged`, `\Deleted`, `\Draft`), not `\Recent` or the keywords the server keeps.
- A page cut at the limit: the index keeps the newest messages by Date header, the server the newest by the date it received them, so the two pages can hold different messages.
- A whole-account search of a Gmail maildir: the maildir holds the label folders but not All Mail, so archived mail that carries no label is missing, and rows name a label folder and its UID where the server's name All Mail.
- A message whose file was moved or removed after indexing: its row comes from the index's own fields, which can be worded differently from the server's; on a block with a `redact` policy, which cannot be judged without the file, the message is left out. The note gives the count.

A `redact` policy on an `[imap.*]` block does not disable the cache. The policy is evaluated against the parsed on-disk message file at search and read time. Records whose policy matches are returned with sensitive fields blanked and `redacted_by` set, the same shape an IMAP-served call would return.
