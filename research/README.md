# Research

Verbatim user voices and competitive evidence backing the USP claims in the project README and feature copy. Nothing in this folder is canonical product spec, only the basis for it. Feature copy and landing-page language should use words taken from `voices.md` so the project speaks to users in the language they already use.

## Files

- `voices.md`: quotebook of user pain-points by category. Each entry is a verbatim quote (or a clearly marked paraphrase) with link, date, context, and the USP it maps to. The corpus we use to write feature copy in words users already think in.
- `competitive-landscape.md`: feature matrix of MCP email servers and per-USP differentiation verdict. Dated snapshot.
- `use-case-survey.md`: broader-web survey of what people are trying to build with AI plus email (Anthropic, OpenAI, LangChain cookbooks; agentic-email startups; Microsoft and Google AI announcements). Dated snapshot.
- `usp-ranking.md`: synthesis of the above into a re-ranked USP list and a list of confirmed gaps.

## Conventions

- Verbatim quotes get double quotes and an attribution line. The attribution line is the one place an em-dash is allowed (per the project em-dash rule).
- Paraphrases are marked `[paraphrase]` with a link, so a future contributor can re-read the source and replace with verbatim text. Treat every paraphrase as a TODO for verbatim recovery.
- Each entry maps to a USP number from `usp-ranking.md`, or labels itself as a `gap` (real demand, no current USP).
- Within a category, sort by source date, oldest first, so the corpus reads as a timeline.

## Growing the folder

Add new quotes to `voices.md` under the matching category. A new category is fine when it has at least two entries; until then, keep the singleton in the closest sibling category. Re-snapshot `competitive-landscape.md` and `use-case-survey.md` when the field changes meaningfully (new commercial agent launch, a new top-10 MCP email server appears, a new safety incident is reported).

When a USP changes (added, removed, renumbered), update the cross-reference column in `voices.md` and `usp-ranking.md` together.
