# USP Ranking

Re-ranked USP list synthesised from `voices.md`, `competitive-landscape.md`, and `use-case-survey.md`. Snapshot May 2026.

The ranking sorts by demand (how often users voice the underlying pain) multiplied by differentiation (how rare the supply is among MCP email servers and commercial agentic-email products). Defensive USPs (real correctness, no voiced demand) rank lower deliberately, even where they are technically unique.

## The 12 USPs

1. **Identity resolution on reply.** Multiple aliases per IMAP block. From-of-reply derived by matching the parent message's To/CC against the configured identities. Errors rather than guessing on miss. (`mailroom/identity.py`)
2. **Markdown body to HTML auto-upgrade.** Plain-text body with tables or ATX headings becomes `multipart/alternative` so the table renders. Conservative trigger; no styling rewrites. (`mailroom/markdown_render.py`)
3. **Sieve-based redaction before the model sees the body.** Per-mailbox policy in standard Sieve syntax blanks subject, body, and parties on matching messages. Fails closed at config-load. (`mailroom/sieve_filter.py`)
4. **Folder name realism.** RFC 6154 SPECIAL-USE flag detection for Sent / Drafts; multi-locale; server-side sent-copy save (FCC) for providers that do not auto-file. (`mailroom/imap_client.py`)
5. **Threading headers plus draft saving on the IMAP server.** `In-Reply-To` and `References` correctly populated on reply; draft saved to the server's Drafts folder visible across clients.
6. **IMAP connection lifecycle.** Stale-connection detection, reconnect, IDLE handling. (`mailroom/imap_client.py`)
7. **Gmail X-GM-RAW search passthrough.** Gmail's actual search engine, not the weak IMAP `SEARCH` command. Exposed via the `imap:` escape in the DSL.
8. **Local cache fallback.** Optional `mu` or offlineimap-indexed local search; orders-of-magnitude faster on large archives. (`mailroom/local_cache.py`)
9. **Gmail-style query DSL.** `from:x is:unread newer:3d` translates portably to IMAP search. (`mailroom/query_parser.py`)
10. **`.ics` meeting invite reply.** Calendar invite parsing and structured RSVP generation. (`mailroom/workflows/`)
11. **Multi-account simultaneous search.** `-A` queries every configured `[imap.*]` block in parallel and returns per-block-attributed results.
12. **Verb-chain in one CLI invocation.** `mailroom -A search foo search bar read -f INBOX -u 42` collapses the LLM's natural fan-out into one process call. (`mailroom/__main__.py`)

## Re-ranked by demand-times-differentiation

| Rank | USP | Demand | Differentiation | Tier |
|---|---|---|---|---|
| 1 | #3 Sieve redaction | OWASP #1 LLM risk; EchoLeak CVE; ShadowLeak; postmark-mcp BCC'd 300 orgs (September 2025); simonw's first question on every show-HN | Unique among open-source IMAP MCPs | Headline moat |
| 2 | #1 Identity resolution on reply | LobsterMail dedicated post; AgentMail seed-pitched it; Highflame "agent identity crisis"; HN multi-account complaints | Unique. No competitor handles alias / role-address / plus-tag matching | Headline moat |
| 3 | #7 plus #8 plus #9 search-quality stack | Most-cited technical complaint about IMAP-based AI tools; Gemini's most-marketed feature is NL inbox search; Haakam21 on HN: "crappy keyword search" is the deal-breaker | #7 rare (2 servers), #8 unique, #9 common. Composite is unique | Headline moat |
| 4 | #11 Multi-account simultaneous (`-A`) | Use case #9 in the survey; Shortwave's Gmail-only is recurring criticism; figassis on HN: "Gmail only is a bait-and-switch" | Rare. Only mailroom does parallel fan-out across generic IMAP | Strong differentiator |
| 5 | #5 Draft saving, re-framed as safety primitive | OpenClaw incident on TechCrunch (February 2026); Harper Reed pivoted his workflow after unauthorised send; jimmcslim on HN: "I would very much like read-only mode!" | Common as a feature, **unique as a positioning angle**. No competitor frames it as the agentic-safety answer | Free win, copy change only |
| 6 | #12 Verb-chain (CLI fan-out collapse) | Reduces cost of triage (use case #1); not user-voiced but maps to triage tutorials | Unique | Developer-credibility USP. Technical readers appreciate it; non-technical readers will not |
| 7 | #10 .ics RSVP | Use case #6; HN wrongsahil: "will it work for hidden calendars?" The ask is broader than RSVP. Users want availability awareness | Unique | Narrow but real |
| 8 | #6 Connection lifecycle | Mailbird's "2025-2026 IMAP crisis"; users do not say "stale connection", they say "search hangs" | Rare | Defensive, silent correctness |
| 9 | #4 Folder realism (SPECIAL-USE plus sent-copy) | Outlook sent-copy issue acknowledged in Fastmail docs but not user-voiced as a frustration | Unique | Defensive, discovered in production |
| 10 | #2 Markdown to HTML auto-upgrade | Zero community evidence as voiced demand; output-quality polish | Unique | Defensive, pure correctness |

## Confirmed gaps

In rough priority for positioning impact and feasibility.

1. **Explicit send-gate / confirmation workflow as documented safety architecture.** Highest-evidence gap. The OpenClaw incident, Harper Reed's pivot, and the autonomous-reply incident category are the loudest current narratives. Action: write `docs/SAFETY.md` covering the draft-default flow; position as "the answer to the OpenClaw problem." Probably no code change required, only documentation and a README section.
2. **Label / move / archive write operations.** Use case #7 in the survey; dominates triage tutorials (n8n, LangGraph, Lindy). Mailroom has `move` and `flag` tools that may not be visible enough in the README and skill prompt. Audit and surface, do not rebuild.
3. **OAuth2 for Outlook / Microsoft 365.** Pain point #3 (survey) and pain #6 (voices). Multiple competitors already ship it (`marlinjai`, `n24q02m`, `codefuturist`). Genuine adoption blocker for a large user segment.
4. **IMAP IDLE / push notifications.** `codefuturist/email-mcp` ships this with multi-channel alerts. Enables event-driven agents (the "watch for X then trigger Y" pattern). Real feature gap.
5. **Free / busy availability check beyond .ics RSVP.** Both voices and survey cite this. Probably out of scope for an IMAP / MCP layer (needs CalDAV or Google Calendar API). The demand is clear; the architectural fit is not.
6. **Voice / tone matching.** Top-three commercial pitch (Superhuman Auto Draft, Shortwave Ghostwriter, Gemini Help Me Write). Out of scope for the IMAP layer; solved at the prompt-engineering layer above mailroom. Worth a one-line "mailroom gives the LLM what it needs to do this; prompt strategy is up to you" disclaimer to pre-empt the question.

## What changed from the prior USP list

- Promoted: USP #3 (redaction) is now rank 1. Was framed as "the security USP"; the evidence shows it is the strongest USP, not an honourable mention.
- Promoted: USP #5 (draft saving) re-framed as safety primitive. Single highest-leverage copy change in the project.
- Demoted: USP #2 (markdown HTML) and USP #4 (folder realism). Real, but solving a problem nobody is voicing. Keep them in the feature list; do not headline.
- New gap surfaced: OAuth for Outlook / Microsoft 365. Real adoption-blocker territory.
- Confirmed: USP #11 multi-account and USP #12 verb-chain are correctly placed. #11 is a strong differentiator with cross-source demand; #12 is unique but developer-flavoured and will not pull non-technical users.

## Suggested landing-page hero

Strongest-first ordering, drawn from words users use in `voices.md`:

> Give your AI agent access to email without giving up the inbox.
>
> - **Redaction policy in your hands.** Per-mailbox Sieve rules blank sensitive bodies before they ever reach the model. Newsletters, banking, OTPs, anything you do not want in an LLM's context window: gone before the agent reads it.
> - **Drafts by default.** Every reply lands in your IMAP Drafts folder, viewable from your phone, syncing to your laptop. The agent writes; you press send.
> - **Replies from the right address.** If a message came to `you+work@example.com`, the reply goes from `you+work@example.com`. Plus-tags, role addresses, multiple aliases per mailbox: handled at the protocol layer, not guessed.
> - **Search that actually works.** Gmail-style query DSL, X-GM-RAW passthrough on Gmail, optional local-cache fallback via `mu` for archive-grade speed across 50k-plus messages.
> - **Every mailbox in one call.** Search across personal, work, and role inboxes simultaneously with `-A`, no per-account fan-out.

Three of the five lines above are moats; the remaining two are demand-pull features matching what people are explicitly asking for online.
