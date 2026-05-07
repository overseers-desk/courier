# Competitive Landscape: MCP Email Servers

Snapshot taken May 2026 by an automated research run. Survey covered the top MCP email servers visible via GitHub topic search (`topic:mcp-server email/imap/gmail`), npm/PyPI, Smithery, glama.ai, and the Anthropic-maintained `modelcontextprotocol/servers` directory. Star counts and last-active dates approximate as of May 2026.

Re-snapshot when the field changes meaningfully (new top-10 server, an existing top-10 server abandoned, a major capability shifts from rare to common).

## Feature matrix

| Server | Lang | Stars | Last active | Tools | Auth | Multi-acct | Search DSL | Threading hdr | Draft save | Attachment DL | Sieve/Redact | Local cache | Gmail X-GM-RAW | Multi-acct simultaneous | Verb-chain |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **mailroom** (SmartLayer) | Python | 43 | May 2026 | ~15 CLI plus MCP | App pwd, OAuth2 (Gmail) | Yes (-A, -i) | Gmail-style DSL plus `imap:` escape | Yes plus draft | Yes | Yes | Yes (`sieve_filter.py`) | Yes (mu / offlineimap) | Yes (via `imap:` escape) | Yes (parallel per block) | Yes (verb-chain CLI) |
| GongRzhe/Gmail-MCP-Server | JS/TS | 1100 | Mar 2026 (archived) | 18 | OAuth2 Gmail | No | Gmail operators | No | Yes (drafts) | Yes | No | No | No (Gmail API only) | No | No |
| codefuturist/email-mcp | TypeScript | 41 | Feb 2026 | 47 | App pwd, OAuth2 (experimental) | Yes | Keyword | Yes | Yes | Yes (5 MB cap) | No | No | No | Undocumented | No |
| ai-zerolab/mcp-email-server | Python | 230 | May 2026 | ~10 | App pwd | Yes (multi-account TOML) | Not documented | Yes | No | Yes (disabled by default) | No | No | No | No | No |
| jgalea/mailbox-mcp | TypeScript | 1 | Apr 2026 | ~15 | App pwd, OAuth2 Gmail, JMAP | Yes | Basic keyword | Yes (Gmail/JMAP native threads) | Yes | Yes (25 MB) | "Prompt injection fencing" (no policy) | No | No | Yes (`multi_account_search`) | No |
| nikolausm/imap-mcp-server | TypeScript | 18 | May 2026 | ~20 | App pwd (AES-256-CBC) | Yes (account switching) | Sender / subject / body / date / flags | Yes (RFC 3501 header search) | No | Yes | No | No | No | No | No |
| thegreystone/mcp-email | Java | 8 | Apr 2026 | 30+ | App pwd | Yes (named accounts) | Subject / sender / body | Yes | Yes | Yes (PDF-to-text) | Opt-in send/delete flags | No | No | No | No |
| marlinjai/email-mcp | TypeScript | 9 | Mar 2026 | 24 | App pwd, OAuth2 (Gmail / Outlook PKCE) | Yes (Gmail + Outlook + iCloud + IMAP) | Lightweight search | Yes (thread retrieval) | Yes | Yes | No | No | No | No | No |
| n24q02m/better-email-mcp | TypeScript | 12 | May 2026 | 6 composite | App pwd, OAuth2 (Outlook device code) | Yes (6+ accounts) | UNREAD/FLAGGED/FROM/SINCE | Yes (In-Reply-To/References) | Yes | Yes | No | In-memory only | No | No | No |
| david-strejc/gmail-mcp-server | Python | 9 | ~2025 | 15 | App pwd (Gmail) | No | Gmail query plus X-GM-RAW | No | No | Forward-only | No | No | Yes | No | No |
| navbuildz/gmail-mcp-server | TypeScript | 8 | Apr 2026 | 7 | OAuth2 Gmail | Yes ("all" accounts) | Gmail query syntax | No | No | No | No | No | No | Yes (`"account":"all"`) | No |
| florianbuetow/imap-mini-mcp | TypeScript | 6 | ~2025 | 18 | App pwd | Single | Date / sender / subject | No | Yes (draft, no send) | Yes | No | No | No | No | No |

## Per-USP differentiation verdict

USP numbers cross-reference `usp-ranking.md`.

**USP 1 (identity resolution on reply).** Unique. No competitor implements automated alias-matching where the correct From is derived by comparing the parent message's To/CC against a configured set of aliases. `codefuturist/email-mcp` and `marlinjai/email-mcp` support multiple accounts but require the caller to select one explicitly. Mailroom errors rather than guessing, a deliberate privacy/correctness posture no competitor articulates.

**USP 2 (markdown body to HTML auto-upgrade).** Unique. No competitor documents automatic markdown-to-HTML promotion on send. `thegreystone/mcp-email` converts PDF attachments to text on read; none converts plain-text bodies on send.

**USP 3 (Sieve-based redaction before model sees body).** Unique. `jgalea/mailbox-mcp` mentions "prompt injection fencing" but provides no user-configurable policy mechanism. Mailroom is the only implementation using a standards-based declarative rule language (Sieve) with a failure-closed config-load contract.

**USP 4 (folder name realism, SPECIAL-USE plus server-side sent copy).** Unique. No competitor documents SPECIAL-USE flag-based Sent folder auto-detection or `save_sent = "auto"` logic that skips FCC on Gmail (which auto-files server-side). Others either hard-code "Sent" or do no FCC at all.

**USP 5 (threading headers plus draft saving).** Common. `codefuturist/email-mcp`, `marlinjai/email-mcp`, `n24q02m/better-email-mcp`, `jgalea/mailbox-mcp`, and `thegreystone/mcp-email` all have both. Threading on reply is table stakes for any server that does replies. Draft saving is present in roughly half the field. The differentiation, if any, is in positioning the draft-default flow as the safety primitive.

**USP 6 (IMAP connection lifecycle).** Rare. `nikolausm/imap-mcp-server` documents "connection pooling." No other competitor articulates reconnect-on-stale logic.

**USP 7 (Gmail X-GM-RAW search passthrough).** Rare. `david-strejc/gmail-mcp-server` also uses X-GM-RAW directly; mailroom exposes it via the `imap:` DSL escape. Most Gmail-focused servers use the Gmail REST API and never touch X-GM-RAW.

**USP 8 (local cache fallback via mu / offlineimap).** Unique. No competitor documents integration with a local mail index. `n24q02m/better-email-mcp` has in-memory caching for one session, categorically different.

**USP 9 (Gmail-style query DSL).** Common. `navbuildz/gmail-mcp-server`, `GongRzhe/Gmail-MCP-Server`, `david-strejc/gmail-mcp-server` support full Gmail syntax. `codefuturist/email-mcp`, `marlinjai/email-mcp` support a subset. Mailroom's DSL is the most complete IMAP-portable implementation, but the concept itself is not novel.

**USP 10 (.ics meeting invite reply).** Unique. No competitor documents calendar invite parsing or structured RSVP. Mailroom has `workflows/meeting_reply.py`, `workflows/invite_parser.py`, `workflows/calendar_mock.py`.

**USP 11 (multi-account simultaneous search).** Rare. `jgalea/mailbox-mcp` documents `multi_account_search`. `navbuildz/gmail-mcp-server` supports `"account":"all"` but only for Gmail API accounts. Mailroom's `-A` flag fans out parallel IMAP connections across generic IMAP blocks with per-block attributed results.

**USP 12 (verb-chain in one CLI invocation).** Unique. No competitor exposes a CLI that accepts chained verb invocations over a single connection per block. Competitors expose individual MCP tools and the LLM must fan out one tool call per query.

## Top 3 moats

1. **USP 3 (Sieve-based redaction).** Configurable, standards-based, failure-closed privacy policy. No competitor is even attempting it. Targets enterprise and privacy-conscious user segments that no one else serves.
2. **USP 8 (local cache fallback via mu).** Integration with a pre-existing local index is unique and orders-of-magnitude faster for power users. Works offline. The mu ecosystem (Emacs, CLI-heavy users) is a natural mailroom audience.
3. **USP 10 (.ics meeting invite reply).** The only server with a workflow layer above raw IMAP. High-value, narrow feature that no competitor has bothered with.

## Top 3 table stakes

1. **USP 5 (threading plus draft saving).** Half the field has it.
2. **USP 9 (Gmail-style query DSL).** Multiple competitors match.
3. **USP 11 base capability (multiple accounts).** Single-switch multi-account is common; simultaneous parallel fan-out is the rare part.

## Features competitors have that mailroom lacks

- IMAP IDLE real-time push / watcher with AI triage presets. `codefuturist/email-mcp` ships this with multi-channel alerts (desktop, webhook).
- Gmail label CRUD. `GongRzhe/Gmail-MCP-Server`, `david-strejc/gmail-mcp-server`, `navbuildz/gmail-mcp-server` expose full label management.
- Auto-unsubscribe. `navbuildz/gmail-mcp-server`.
- OAuth2 for Microsoft 365 / Outlook. `marlinjai/email-mcp`, `n24q02m/better-email-mcp`, `codefuturist/email-mcp` (experimental).
- 47-tool fine-grained surface plus calendar event extraction from email bodies plus per-account analytics. `codefuturist/email-mcp`.
