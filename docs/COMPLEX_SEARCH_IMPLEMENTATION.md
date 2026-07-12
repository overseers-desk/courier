# Gmail-Style Search Query Syntax

Courier uses a Gmail-inspired query language for email search. Queries work identically across the CLI, MCP tool, and MCP resource interfaces.

## Quick reference

| Syntax | Meaning | IMAP equivalent |
|--------|---------|-----------------|
| `from:alice` | Sender contains "alice" | `FROM alice` |
| `to:bob` | Recipient contains "bob" | `TO bob` |
| `cc:team` | CC contains "team" | `CC team` |
| `subject:invoice` | Subject contains "invoice" | `SUBJECT invoice` |
| `body:hello` | Body contains "hello" | `BODY hello` |
| `is:unread` | Unread messages | `UNSEEN` |
| `is:read` | Read messages | `SEEN` |
| `is:flagged` / `is:starred` | Flagged messages | `FLAGGED` |
| `is:answered` | Replied-to messages | `ANSWERED` |
| `after:2025-03-01` | Sent on or after date | `SINCE 01-Mar-2025` |
| `before:2025-04-01` | Sent before date | `BEFORE 01-Apr-2025` |
| `on:2025-03-15` | Sent on exact date | `ON 15-Mar-2025` |
| `msgid:<id@host>` | Match by RFC 5322 Message-ID | `HEADER Message-ID id@host` |
| `newer:3d` | Within the last 3 days | Calculated `SINCE` |
| `older:7d` | Older than 7 days | Calculated `BEFORE` |
| bare words | Text search | `TEXT ...` |
| `imap:EXPR` | Raw IMAP passthrough | Verbatim |

## Bare words

Tokens without a prefix search the full message text:

```bash
courier search "meeting notes"
# → IMAP: TEXT "meeting notes"
```

## Combining terms

Space-separated terms are implicitly AND-ed:

```bash
courier search "from:alice subject:invoice is:unread"
# → IMAP: FROM alice SUBJECT invoice UNSEEN
```

## Boolean operators

**OR** between terms:

```bash
courier search "from:alice or from:bob"
# → IMAP: OR FROM alice FROM bob
```

Chained OR right-associates:

```bash
courier search "from:a or from:b or from:c"
# → IMAP: OR FROM a OR FROM b FROM c
```

**NOT** with `-` prefix or `not` keyword:

```bash
courier search "-from:alice"
courier search "not is:read"
```

## Dates

ISO format (`YYYY-MM-DD`) and slash format (`YYYY/MM/DD`) both work:

```bash
courier search "after:2025-03-01 before:2025-04-01"
```

Relative dates use `d` (days), `w` (weeks), `m` (months):

```bash
courier search "newer:3d"     # last 3 days
courier search "older:2w"     # older than 2 weeks
```

`newer_than:` and `older_than:` are accepted as synonyms.

## Standalone keywords

When the entire query is one of these words, it maps to a predefined search:

- `all` — every message
- `today` — messages from today
- `yesterday` — messages from yesterday
- `week` — messages from the last 7 days
- `month` — messages from the last 30 days

## Quoted values

Use double or single quotes for multi-word values:

```bash
courier search 'subject:"hotel booking"'
courier search "from:'Alice Smith'"
```

## Raw IMAP passthrough

Prefix with `imap:` to send a raw IMAP SEARCH expression:

```bash
courier search 'imap:OR TEXT "Edinburgh" OR TEXT "Berlin" TEXT "Munich"'
```

This bypasses the query parser entirely and passes the expression to the IMAP server in Polish (prefix) notation.

## Implementation

The parser lives in `courier/query_parser.py`. It tokenizes the query with `shlex.split()`, classifies each token (prefix:value, keyword, bare word, operator), and assembles a flat list compatible with `imapclient.IMAPClient.search()`.
