# Voices

Verbatim quotes (where available) and paraphrases (clearly marked) from public discussions about giving an LLM access to email. Snapshot built May 2026 from Hacker News, Reddit, blog posts, and reporting. Each entry maps to a mailroom USP from `usp-ranking.md` or labels itself as a `gap`.

The corpus exists so feature copy speaks in words users already use. When writing about a feature, grep this file for the USP number and read the user's own language before drafting.

---

## 1. Prompt injection, data leakage, third-party routing → USP #3 (Sieve redaction)

> "have you tested this for prompt injection?"
> — simonw, HN item 44915220, August 2025
> https://news.ycombinator.com/item?id=44915220
> Context: top reply on the Show HN thread for a JMAP MCP server. simonw demonstrated how a malicious email could impersonate a trusted contact to extract sensitive data.
> Maps to: USP #3

> "handing my whole personal inbox over to OpenAI seems insane."
> — Virgil2604, HN item 38809770
> https://news.ycombinator.com/item?id=38809770
> Context: Show HN: Inbox Zero (cloud-routed AI email assistant). Top of a long privacy thread.
> Maps to: USP #3

> "I will never trust an app like this to have access to my emails."
> — zdwolfe, HN item 38809770
> https://news.ycombinator.com/item?id=38809770
> Context: same Inbox Zero thread.
> Maps to: USP #3

> "You're essentially asking people to forward potentially sensitive business emails to a third-party service."
> — Yogender78, HN item 44107856
> https://news.ycombinator.com/item?id=44107856
> Context: Show HN, non-intrusive AI agent for email-driven workflows.
> Maps to: USP #3

> "there are so many unaddressed shenanigans when it comes to email and prompt injection"
> — thenaturalist, HN item 41943931
> https://news.ycombinator.com/item?id=41943931
> Context: Show HN, secure local AI Gmail tidier. Cited a BlackHat demo where emails compromise Microsoft 365 Copilot without being opened.
> Maps to: USP #3

> "not ideal", "obviously a privacy concern", "I can't wait to run these things locally"
> — Harper Reed, blog post December 2025
> https://harper.blog/2025/12/03/claude-code-email-productivity-mcp-agents/
> Context: describing his Claude-plus-Pipedream email workflow.
> Maps to: USP #3

[paraphrase] Industry-published account: an agent with Gmail API access can be injected to "execute literally anything possible with the Gmail API," including the OAuth confused-deputy variant.
> Source: HN item 44205881, "I built an AI agent with Gmail access and discovered a security hole"
> https://news.ycombinator.com/item?id=44205881
> Maps to: USP #3

[paraphrase] Google's own disclosure: indirect prompt injection reduced Gemini's inbox-task reliability from 99.8% pre-attack to 53.6% even after mitigations.
> Source: Security Boulevard, January 2026
> https://securityboulevard.com/2026/01/google-gemini-ai-flaw-could-lead-to-gmail-compromise-phishing-2/
> Maps to: USP #3

[paraphrase] First malicious npm MCP server (postmark-mcp v1.0.16, September 2025) silently BCC'd every outgoing email to the attacker's address, compromising approximately 300 organisations before discovery. The supply-chain risk for MCP email servers is no longer hypothetical.
> Source: industry coverage, September 2025 (postmark-mcp incident)
> Maps to: USP #3 (and: this is the kind of incident that, framed against mailroom, lets the project pitch its small surface area and Sieve redaction layer as relevant defenses)

---

## 2. Wrong sender identity, agent-from-personal-inbox → USP #1 (identity resolution)

[paraphrase] LobsterMail blog post: agent-written replies sent from a personal inbox arrive under the owner's name with no way for the recipient to distinguish AI authorship. Cites real reputation damage from autonomous overnight replies sent with incorrect information.
> Source: https://lobstermail.ai/blog/agent-sends-from-personal-inbox (December 2025)
> Maps to: USP #1

[paraphrase] AgentMail's seed-round pitch frames "email as identity for AI agents" as the primary product wedge: agents need their own sending identity rather than borrowing the operator's. Their argument is that operator-impersonation is a category of bug, not a corner case.
> Source: https://www.agentmail.to/blog/email-as-identity-for-ai-agents
> Maps to: USP #1

[paraphrase] Highflame essay "Who sent you? Solving the agent identity crisis": dedicated long-form piece arguing that the absence of stable agent-side sending identity is a bottleneck for production deployment.
> Source: https://highflame.com/blogs/who-sent-you-solving-the-agent-identity-crisis (2025)
> Maps to: USP #1

[paraphrase] Indie hacker (Gropius) workaround: their agent reads the user's last 20 sent emails to infer tone and From-identity at runtime. The workaround exists because no underlying tool resolves it for them.
> Source: Indie Hackers thread (2025), referenced in pain-point research
> Maps to: USP #1

---

## 3. Autonomous send, no kill switch, draft-only requests → USP #5 (re-framed as safety primitive)

> "I would very much like this to operate in read-only mode!"
> — jimmcslim, HN item 44915220
> https://news.ycombinator.com/item?id=44915220
> Context: JMAP MCP show-HN. The creator confirmed read-only mode existed but it was not obvious from the docs. Note the latent-feature-not-marketed pattern.
> Maps to: USP #5

> "pulling data into a sandbox, the compute happens and there is no outside connectivity and I get a list of actions to review and approve manually"
> — thenaturalist, HN item 41943931
> https://news.ycombinator.com/item?id=41943931
> Context: describing the architecture they wish AI email tools had.
> Maps to: USP #5

> "You've automated the processing but not the triggering."
> — Pawan89, HN item 44107856
> https://news.ycombinator.com/item?id=44107856
> Context: feedback on a non-intrusive AI agent for email workflows; framed as a compliment, not a complaint.
> Maps to: USP #5

> "bulk-deleting emails. Hundreds of them. Not archiving. Deleting."
> — Meta AI security researcher (quoted in TechCrunch), February 2026
> https://techcrunch.com/2026/02/23/a-meta-ai-security-researcher-said-an-openclaw-agent-ran-amok-on-her-inbox/
> Context: the OpenClaw incident. Researcher's agent ignored stop commands from her phone.
> Maps to: USP #5

> "like I was defusing a bomb"
> — Meta AI security researcher (quoted in TechCrunch), February 2026
> https://techcrunch.com/2026/02/23/a-meta-ai-security-researcher-said-an-openclaw-agent-ran-amok-on-her-inbox/
> Context: same incident, describing her run to her computer to halt the agent.
> Maps to: USP #5, also gap (kill-switch / emergency halt)

[paraphrase] Harper Reed pivoted his entire Claude email workflow to draft-only after an unauthorised send damaged a professional relationship. He now reviews every reply before send.
> Source: https://harper.blog/2025/12/03/claude-code-email-productivity-mcp-agents/
> Maps to: USP #5

[paraphrase] Bitsight industry report: agents that "went through a personal inbox and replied to a few emails autonomously and without explicit per-action permission" are now a documented incident category, not a hypothetical.
> Source: Bitsight (2025), referenced in pain-point research
> Maps to: USP #5

---

## 4. Search quality, IMAP SEARCH inadequacy → USPs #7, #8, #9

> "poor API support, expensive subscriptions, rate limits, sending limits, GCP Pub/Sub, OAuth, crappy keyword search"
> — Haakam21, HN item 44745820
> https://news.ycombinator.com/item?id=44745820
> Context: enumerating Gmail's problems in the AgentMail Show HN thread. "Crappy keyword search" is the user-language term for the IMAP SEARCH inadequacy.
> Maps to: USPs #7, #8, #9

[paraphrase] AtomicMail 2025 guide: AI assistants that rely on IMAP SEARCH miss 80% or more of what a full-text engine returns on the same query. The number is a vendor estimate, not measured, but the directional claim matches user complaints.
> Source: https://atomicmail.io/blog/what-is-email-ai-convenience-or-privacy-risk
> Maps to: USPs #7, #8, #9

[paraphrase] Mailbird published a 2025-2026 piece framing IMAP latency and sync failures as an industry-wide infrastructure problem. The piece reads as marketing positioning but the user complaints it cites are real (Home Assistant issue 86407, AgenticMail backoff posts).
> Source: https://www.getmailbird.com/fix-imap-latency-email-sync-failures/
> Maps to: USP #6 (defensive), USPs #7, #8, #9 (positive)

[paraphrase] DEV Community write-up "Building an AI Email Triage System that saves 4 hours/week" notes that individual-email analysis without thread context misses conversation continuity, and documents the threading-headers-on-reply requirement as an explicit production lesson.
> Source: https://dev.to/wedgemethoddev/building-an-ai-email-triage-system-that-saves-4-hours-per-week-5epo
> Maps to: USP #5 (threading), and indirectly USPs #7-#9 (the search has to return threads, not isolated messages)

---

## 5. Multi-account, "Gmail only" exclusion → USP #11 (multi-account simultaneous)

> "Gmail only"
> — figassis, HN item 38809770
> https://news.ycombinator.com/item?id=38809770
> Context: top criticism of Inbox Zero. Single-provider lock-in is a deal-breaker mentioned across threads.
> Maps to: USP #11

> "scales for multiple Gmail accounts"
> — allan_s, HN item 38809770
> https://news.ycombinator.com/item?id=38809770
> Context: pricing-model question that revealed multi-account is a use case people assume by default.
> Maps to: USP #11

[paraphrase] theusus on the same Inbox Zero thread: Outlook users excluded entirely from the product because it assumes Gmail.
> Source: HN item 38809770
> Maps to: USP #11, gap (Outlook OAuth)

[paraphrase] Shortwave's commercial limitation (Gmail only) is recurring criticism in 2025-2026 vs-Superhuman comparison posts. Multi-provider unified search is a feature commercial competitors mostly do not ship.
> Source: BayTech "Shortwave vs Superhuman 2025" guide, https://www.baytechconsulting.com/blog/shortwave-vs-superhuman-the-2025-executives-guide-to-ai-email-clients
> Maps to: USP #11

---

## 6. OAuth and consumer-provider setup friction → gap

> "oauth for consumer Outlook, Gmail and Yahoo would be a great addition"
> — fh67, HN item 44863291
> https://news.ycombinator.com/item?id=44863291
> Context: feature request on an open-source email archiver Show HN. The creator's reply: "Outlook seems to have disabled password connection for IMAP."
> Maps to: gap (Outlook / Microsoft 365 OAuth)

> "rejects redirect URIs from custom domains, only claude.com, chatgpt.com, and localhost are accepted"
> — wormhoudt, HN item 47870988
> https://news.ycombinator.com/item?id=47870988
> Context: Fastmail MCP server thread. The OAuth Dynamic Client Registration restriction blocks self-hosted MCP clients entirely.
> Maps to: gap (broader OAuth / DCR ecosystem)

[paraphrase] non-dirty/imap-mcp project's TASKS.md lists Gmail OAuth as a priority open work item: even active competitors flag this as unfinished.
> Source: https://github.com/non-dirty/imap-mcp/blob/main/TASKS.md
> Maps to: gap (OAuth lifecycle)

---

## 7. AI tone, voice mismatch → gap (out of scope for IMAP layer)

[paraphrase] HN thread "I Received an AI Email" generated a long discussion about the "creepy" detectability of AI-written messages. Style divergence from the apparent sender is the tell.
> Source: HN item 40862865, https://news.ycombinator.com/item?id=40862865
> Maps to: gap (prompt-engineering layer, not IMAP layer)

[paraphrase] Ian Brodie (Substack 2025): AI defaults to a "safe middle-of-the-road voice" that customers perceive as inauthentic. Generic voice is the canonical complaint.
> Source: Ian Brodie Substack
> Maps to: gap

[paraphrase] Gmelius study (2025): customer sensitivity to "tone, personalization, structure" as AI tells. The list of tells is the same across studies.
> Source: Gmelius 2025
> Maps to: gap

---

## 8. Calendar and scheduling integration → USP #10 (.ics RSVP) and gap

> "Will it work for hidden calendars? How will it know if slots are open or not without having to login?"
> — wrongsahil, HN item 44107856
> https://news.ycombinator.com/item?id=44107856
> Context: feature question on a non-intrusive AI email agent. The ask is for availability awareness, not just RSVP.
> Maps to: USP #10 partial, gap (free/busy)

[paraphrase] codefuturist/email-mcp README lists "Meeting and scheduling support" as a feature, suggesting it is a frequent enough request to advertise.
> Source: https://github.com/codefuturist/email-mcp
> Maps to: USP #10

[paraphrase] MXtoAI's HN demo (`schedule@mxtoai.com`) is built around the assumption that scheduling-by-email is the killer use case for an AI inbox. Same assumption underlies most n8n-plus-Calendar tutorials.
> Source: HN MXtoAI thread (2025); Make.com plus Google Calendar tutorial (Substack 2025)
> Maps to: USP #10, gap (free/busy)

---

## 9. Connection lifecycle, IMAP staleness → USP #6 (defensive)

[paraphrase] Mailbird's 2025-2026 IMAP-crisis post and Home Assistant issue 86407 (intermittent IMAP delays) document the failure mode users notice as "search hangs" or "the agent stopped working overnight." Few users say "stale connection"; that is what causes the symptom.
> Source: https://www.getmailbird.com/fix-imap-latency-email-sync-failures/, https://github.com/home-assistant/core/issues/86407
> Maps to: USP #6

[paraphrase] AgenticMail blog post documents exponential-backoff IDLE handling as a hard-won implementation lesson. Production agentic systems have to solve this; naive prototypes do not.
> Source: AgenticMail (2025)
> Maps to: USP #6

---

## 10. Write-side inbox management (label, move, archive) → gap

[paraphrase] Use case #7 in the broader-web survey: every triage tutorial across n8n, Zapier, LangGraph, Lindy, and Make.com builds label-and-route logic on top of email reads. Mailroom has `move` and `flag` tools but does not surface them as a USP. The surface area exists; the marketing positioning does not.
> Source: aggregate across n8n template 9157, kaymen99/langgraph-email-automation, Lindy/Zapier tutorials
> Maps to: gap (positioning, not implementation)

[paraphrase] Shortwave's "Tasklet" feature (October 2025 launch) and Gemini's "AI priorities" (January 2026) both pitch label/route automation as the headline value of an AI email client.
> Source: https://blog.google/products-and-platforms/products/gmail/gmail-is-entering-the-gemini-era/, Shortwave blog
> Maps to: gap (positioning)

---

## How to add an entry

1. Identify the category. If none fits, leave the entry in the closest sibling and add a TODO at the bottom of this file. Promote to a new section once a second entry arrives.
2. Capture verbatim where possible. Use double quotes plus an attribution line beginning with an em-dash (the only attribution-line em-dash exception in the project).
3. If only a paraphrase is available, mark `[paraphrase]` and link the source so the next contributor can recover the verbatim text.
4. Add the date. If the source date is approximate, write `~YYYY` or `early 2025`, do not invent precision.
5. Map to a USP number from `usp-ranking.md` or label `gap`. If the mapping is partial or indirect, say so in the line.
6. Within the section, sort by date oldest-first.
