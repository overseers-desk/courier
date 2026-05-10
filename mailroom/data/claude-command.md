---
name: mailroom
description: Search, read, and look up information from the user's IMAP mailboxes via the mailroom CLI, and send mail when asked. Trigger when the user asks to find or look something up in their email, recall what someone has said in mail, summarise correspondence with a person, search the inbox for a topic, or check replies. Phrases like "tell me about X from emails", "what did X email me about", "find Y in my mail", "search my inbox for Z", or "show recent messages from W" all route here. Use this rather than guessing an email address from a name.
---

# Email workflow via mailroom

## Process

0. Note today's date, whether a reply, and if so the delay.
1. Show from / identity, to, cc, bcc, subject, body for review.
2. Wait for user approval.
3. Send via `mailroom`.
4. Report `message_id_sent` from the JSON output.

Do not save a draft if told to send.

## One-shot principle

Each mailroom invocation is the final call — no pre-flight `list`, `config-check`, or `folders` before a send the user already approved. If the command needs context (which identity, which folder), ask the user, not mailroom. mailroom errors name the exact corrective flag; a wrong invocation self-corrects without a probe phase.

Skip `mailroom config-check` unless the user reports config-related trouble. Skip `mailroom list` unless the user explicitly asks "what identities do I have".

## Sending: pick a route

`compose --send`, `reply --send`, and `send-draft` all require an explicit route. Two forms:

- **Mode A (preferred when the user names an identity)**: `--identity NAME` resolves From address, display name, IMAP block, SMTP block, and sent_folder from the configured `[identity.NAME]` block. One flag, no other identity-related flags valid.
- **Mode B (relay-style sends without a configured identity)**: `--smtp NAME --from EMAIL [--name "Display Name"] [--fcc IMAP_NAME:FOLDER]`. The named `[smtp.NAME]` block must carry its own username/password. Use this for SES-style relays authorised to carry many addresses.

If the user names an identity ("send as partnerships"), use `--identity partnerships` directly. If the user gives only an address, ask whether they have an `[identity.NAME]` for it before falling back to mode B.

## Cowardly refusal: --allow-no-copy

mailroom refuses to send when no copy will be retained. A copy is retained iff FCC will run, OR the BCC list includes the sender's own address. A BCC addressed only to a third party (e.g. an auditor) does not count. Pass `--allow-no-copy` only when the user explicitly opts into a no-record send (e.g. throwaway sends through a relay that archives independently).

## Identity-level bcc (send-only identities)

`[identity.NAME]` may declare a `bcc = "self@x"` (string or list). On every send from this identity, those addresses are appended to BCC automatically. When `bcc` is set, the identity may omit `imap` entirely — send-only, self-BCC substitutes for FCC. Such identities cannot fetch, save drafts, or reply to a parent; they only do `compose --send`.

## New emails

```bash
mailroom compose \
  --to recipient@example.com \
  --subject "..." \
  --body "..." \
  --send --identity NAME
```

HTML: add `--body-html "<p>...</p>"`; both `--body` and `--body-html` → `multipart/alternative`. `--attach <path>` and `--bcc <addr>` are repeatable.

JSON output includes `message_id_sent` (recipient-visible Message-ID; differs from `message_id_local` when the smarthost rewrites it, e.g. SES) and `accepted_recipients`.

## Replies

```bash
mailroom --imap <imap> reply -f <folder> -u <uid> --body "..." --send --identity NAME
```

Without `--identity`, mailroom matches the parent's recipients against the imap block's identities and uses the matching one. A miss errors rather than guessing. Threading headers set from parent automatically. `--reply-all`, `--cc`, `--body-html`, `--bcc`, `--attach` work the same as on `compose`. Drop `--send` to save a draft instead.

## Sending an existing draft

```bash
mailroom --imap <imap> send-draft -f Drafts -u <uid>
```

Reads draft, matches From to an imap identity, transmits, removes on success. `--keep-draft` retains it. `--dry-run` authenticates without sending. `--bcc` adds envelope-time recipients without rewriting the draft body. `--identity NAME` overrides the draft's From; `--smtp NAME --from EMAIL` is mode B.

## Top-level flags

- `--imap NAME`: select a configured `[imap.NAME]` block. Omit to use `default_imap`. Repeats across a `search` chain to query multiple blocks.
- `-A, --all-imap`: query every imap block (search only).
- `-c, --config PATH`: alternate config file.

Migration: `-a <account>` → `--imap <name>`; `[[identities]]` → `[identity.NAME]`.

## After a successful send

mailroom FCCs wire-form bytes to the Sent folder with `Bcc:` stripped and `Message-ID:` rewritten to the recipient-visible form (threading depends on this match). Configure Sent folder per identity via `sent_folder = "..."`; without it, mailroom auto-detects via SPECIAL-USE `\Sent` and falls back to `Sent`. When the SMTP block has `save_sent = false` (or `"auto"` resolving to false on Gmail, where the server auto-files), the FCC step is skipped.

## Looking up a person by name

Search by name, not a constructed address. AI-guessed addresses (e.g. `alicedoe@gmail.com`) commonly miss; real addresses have no surface relation to the name. Issue a name-based query first, read a hit to learn the real address, then narrow.

## Lookups

`search` accepts Gmail-style queries: operators stack with implicit AND between space-separated tokens, OR clusters with the `OR` keyword. Write the JSON to a temp file and slice with `jq`; `head`/`tail` on JSON cuts mid-structure:

```bash
RESULTS=$(mktemp /tmp/mailroom.XXXXXXXX)
mailroom -A --format json \
  search "from:@iberia.com OR 'Iberia Líneas Aéreas' OR 'Iberia Airlines'" > "$RESULTS"
jq '[.[] | .[] | .results[] | {subject, from, date}]' "$RESULTS"
```

The same airline shows up under a domain (`iberia.com`), a Spanish trading name (`Iberia Líneas Aéreas`), and an English variant (`Iberia Airlines`). One composed OR returns the union under one result key; three chained `search` verbs would split the same union across three labelled keys, which is overhead when the terms refer to one entity. The example illustrates that domains and company names diverge, and that company names carry language variants. The same shape covers other entities whose self-references vary: an airline known by IATA code and full name (`'CZ' OR 'China Southern' OR 'from:@csair.com'`); a person known by first name, full name, and address (`'Alice' OR 'Alice Schmidt' OR 'alice@example.com'`).

A complex example joining a recipient OR-cluster with a topic OR-cluster:

```bash
mailroom -A --format json \
  search "(from:bad@character.com OR to:bad@character.com OR cc:bad@character.com) (trespass OR threaten OR violent)" > "$RESULTS"
```

The query asks for messages where the address appears as sender or recipient AND the body mentions any of three topics. On Gmail accounts mailroom dispatches this through the `X-GM-RAW` extension (any token starting with `from:`/`to:`/`cc:`/`bcc:` triggers it, so the parser sees the un-prefixed `to:` and `cc:` tokens), and Gmail's web-UI grammar evaluates the parens. A non-Gmail backend without parens grouping needs the `imap:` raw escape for the same shape: `imap:OR OR FROM bad@character.com OR TO bad@character.com CC bad@character.com OR BODY trespass OR BODY threaten BODY violent`. The example illustrates that a question with two independent disjunctions (any of N recipient roles, any of M topic words) is one composed search; expanding the same intent into N×M chained searches multiplies server queries without changing the answer.

`from:`, `to:`, `subject:`, `after:`, `before:`, `is:unread`, `is:read`, and bare body words all combine. With `[local_cache]` configured the queries run against a local index orders of magnitude faster than IMAP; without it queries hit IMAP directly. Each per-block response carries a `provenance` field reporting `source` (`"local"` or `"remote"`) and any fall-back reason.

Output is JSON of shape `{op_key: {imap_name: {results: [...], provenance: {...}}}}`. An invocation is a chain of verbs; each `search` repetition is one outer key in the result. Composed-OR and chained-search answer different questions: alternatives within one entity (the Iberia case above) sit inside one `search` via OR and return a flat union under one key; genuinely separate questions sit in separate `search` repetitions, each under its own key:

```bash
mailroom -A --format json \
  search "from:alice@example.com subject:invoice" \
  search "from:billing@example.org after:2026-03-01" \
  > "$RESULTS"
```

`mailroom -A` queries every imap block; `--imap NAME` repeats across the chain to select specific blocks. Different verbs share one chain: `mailroom search "from:alice@example.com is:unread" read -u 42 -f INBOX` runs the search and the fetch over one connection per block.

`read` takes the same shape. Repeat the verb to add UIDs to the chain; one IMAP session covers them all. Trailing `-f FOLDER` peels off as a chain default, so each `read` reuses the folder. Login dominates the per-fetch cost (Gmail caps simultaneous IMAP connections per account):

```bash
RESULTS=$(mktemp /tmp/mailroom.XXXXXXXX)
mailroom --imap <imap> --format json read -u 100 read -u 200 read -u 300 -f INBOX > "$RESULTS"
jq '[.[] | .[] | {uid, subject, from, date}]' "$RESULTS"
```

List and extract attachments, or export the verbatim `.eml`:

```bash
mailroom --imap <imap> attachments -f <folder> -u <uid>
mailroom --imap <imap> save -f <folder> -u <uid> --attachment <name> -o <path>
mailroom --imap <imap> export -f <folder> -u <uid> --raw -o /tmp/msg.eml
```

`mailroom list` enumerates configured blocks/identities/SMTP. Run it only when the user explicitly asks.
