---
name: mailroom
description: Search, read, and look up information from the user's IMAP mailboxes via the mailroom CLI, and send mail when asked. Trigger when the user asks to find or look something up in their email, recall what someone has said in mail, summarise correspondence with a person, search the inbox for a topic, or check replies. Phrases like "tell me about X from emails", "what did X email me about", "find Y in my mail", "search my inbox for Z", or "show recent messages from W" all route here. Use this rather than guessing an email address from a name.
---

# mailroom

mailroom searches, reads, and pulls attachments from the user's mailboxes via the mailroom CLI, and sends mail when asked, although most of the time it is not for sending. Each request is one invocation; chained verbs share one connection per IMAP block.

## Searches

```bash
RESULTS=$(mktemp /tmp/mailroom.XXXXXXXX)
mailroom -A --format json \
  search "from:alice hotel booking" \
  search "from:bob contract" \
  -n 5 > "$RESULTS"
jq '[.[] | .[] | .results[] | {uid, folder, subject, from, date}]' "$RESULTS"
```

Pack questions into one invocation: each `search` becomes one outer key in the result, sharing one connection per IMAP block. `search` accepts Gmail-style queries; tokens combine with implicit AND, `OR` clusters alternatives. Results sort newest-first; trailing `-n` peels off as a chain default (default 10) and applies to every `search` that doesn't set its own. Use the words from the user's request (a name they mentioned, a domain, a subject phrase); an AI-constructed address often does not match the real one, which sits in each hit's `from`.

`OR` inside one `search` returns a flat union under that one outer key, so synonyms or trading-name variants of the same entity stay together rather than scatter across separate keys:

```bash
mailroom -A --format json \
  search "from:@example.com OR 'Example Trading' OR 'Example Inc'" \
  search "subject:invoice after:2026-01-01" \
  > "$RESULTS"
```

Output shape: `{op_key: {imap_name: {results: [...], provenance: {...}}}}`. `mailroom -A` queries every IMAP block; `--imap NAME` (repeats across the chain) selects specific blocks. Slice the JSON with `jq` against a tempfile; `head`/`tail` cut mid-structure.

## Reads

```bash
mailroom --imap <imap> --format json \
  read -u 100 \
  read -u 200 \
  read -u 300 \
  -f INBOX > "$RESULTS"
jq '[.[] | .[] | {uid, subject, from, date, has_attachments}]' "$RESULTS"
```

UIDs from a prior search go into one chain over a single IMAP session; trailing `-f FOLDER` peels off as the chain default. Login dominates the per-fetch cost; Gmail caps simultaneous IMAP connections per account, so N parallel `read` processes hit that cap.

## Attachments

```bash
mailroom --imap <imap> attachments -f <folder> -u <uid>
mailroom --imap <imap> save -f <folder> -u <uid> --attachment <name> -o <path>
mailroom --imap <imap> export -f <folder> -u <uid> --raw -o /tmp/msg.eml
```

`--attachment` accepts a filename or the numeric index reported by `attachments`.

## Sending

```bash
mailroom compose --to recipient@example.com --subject "..." --body "..." --send -i NAME
mailroom --imap <imap> reply -f <folder> -u <uid> --body "..." --send -i NAME
mailroom --imap <imap> send-draft -f Drafts -u <uid>
```

`-i NAME` (= `--identity NAME`) picks a configured `[identity.NAME]` block; reply inherits the parent's threading headers. Drop `--send` to save as a draft. `mailroom list` returns the configured identity names under its `identity` key; `mailroom <verb> --help` carries flags for relay-style sends and other less-common paths.
