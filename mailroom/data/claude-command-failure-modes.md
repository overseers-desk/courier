# Failure modes the AI hint document is calibrated against

This file lives next to the slash-command source so a future editor of that source has the calibration record in line of sight. Each entry pairs a concrete AI failure with the load-bearing element of the source that prevents it. Before shipping a rewrite, walk this list and verify the prevention is still intact.

The hint itself contains no prose announcing the problems it solves; this file is where that record lives.

---

## A. Calling pattern

### A1. Singular-verb bias: AI calls one verb per invocation

**Failure mode.** A user asks "find emails from Alice about hotel booking and from Bob about the contract." The AI runs:

```
mailroom search "from:alice hotel booking"
mailroom search "from:bob contract"
```

as two separate processes. Each pays its own IMAP login (Gmail caps simultaneous IMAP connections per account) and the AI never learns the chain shape. The same fan-out pattern surfaces with `read`, `attachments`, and even `search` + `read` written as serial invocations rather than one chain.

**Prevention.** Every code example in the slash-command source shows a chain — at minimum two repeated verbs in one invocation, more often a chain that mixes verbs (e.g. `search ... search ... read -u N` or `read -u 1 read -u 2 read -u 3 -f INBOX`). Prose refers to "searches" and "reads" in the plural. There is no example anywhere in the doc that shows a lone verb followed by a `>` redirect; the singular form is invisible.

### A2. Fan-out across processes for N items

**Failure mode.** AI receives a list of UIDs to read and runs `mailroom read -u 1`, `mailroom read -u 2`, `mailroom read -u 3` in parallel (or a bash loop). Each invocation is a fresh IMAP login. On Gmail, this trips the per-account simultaneous-connection cap; on any backend, login dominates the per-fetch cost.

**Prevention.** The `read` example chains multiple `-u` arguments inside one invocation. The prose immediately following names "Login dominates the per-fetch cost; Gmail caps simultaneous IMAP connections per account, so N parallel `read` processes hit that cap." This states the cost as a fact, not as an instruction, so the reader infers the chain shape is the right shape.

### A3. Expecting dynamic UID substitution within a chain

**Failure mode.** Having absorbed the chain shape, the AI tries `mailroom search "from:alice" read -u $UID -f INBOX` thinking the search result feeds into `read`. The chain runs both verbs, but UIDs in `read` are static; nothing is substituted from search output.

**Prevention.** The `read` example uses literal UIDs (`-u 100 -u 200 -u 300`). The prose says "UIDs from a prior search go into one chain." "From a prior search" is the load-bearing phrase: it implies the AI ran search, looked at the JSON, and now pastes UIDs into a second invocation. No example in the doc tries to chain search and read in a way that suggests dynamic substitution.

### A4. Preflight probing

**Failure mode.** AI runs `mailroom list` or `mailroom config-check` before answering a question that doesn't need them, just to "see what's configured."

**Prevention.** No example in the doc opens with such a probe. Configuration discovery is mentioned exactly once, scoped to the case where it is genuinely needed: `mailroom list returns the configured identity names under its identity key`. There is no general "first run X to check" instruction.

---

## B. Query construction

### B1. Guessing email addresses from names

**Failure mode.** User says "find Alice's email about the contract." AI synthesises `from:alicedoe@gmail.com` based on the name. The address is wrong; the search returns nothing; the AI reports "no results found" and the user has to push back.

**Prevention.** The frontmatter description says: "Use this rather than guessing an email address from a name." The body reinforces in one line: "Use the words from the user's request (a name they mentioned, a domain, a subject phrase); an AI-constructed address often does not match the real one, which sits in each hit's `from`." The example query uses a bare name (`from:alice`) rather than a synthesised full address. The reader sees: search by what the user gave you; pull the real address from a hit if you need it later.

### B2. Splitting one entity's synonyms across separate searches

**Failure mode.** AI looking for an airline that trades under multiple names (a domain, a Spanish trading name, an English variant) issues three `search` repetitions, splitting one entity's mail across three outer keys in the JSON. Costs three server queries, fragments the results.

**Prevention.** A code example shows `OR` clustering inside one `search` for synonyms of a single entity (`search "from:@example.com OR 'Example Trading' OR 'Example Inc'"`). The prose distinguishes: "Repeated `search` verbs run separate questions, each under its own outer key" vs. "`OR` inside one `search` returns a flat union under one outer key, so synonyms or trading-name variants of the same entity stay together."

### B3. Sort-order surprise on "recent" / "last few" requests

**Failure mode.** User asks "what are the last 5 emails from Alice." AI runs a search without `-n`, gets up to 10 hits in unspecified order, and either reports too many or picks the wrong ones because it expected ascending date.

**Prevention.** The Searching prose states: "Results sort newest-first; `-n` limits per IMAP block (default 10)." Both facts in one sentence so the AI doesn't have to derive ordering from `--help` for a routine request.

### B4. AI-fabricated example scenarios

**Failure mode.** A previous AI editor invents a plausible but artificial use case to demonstrate a syntax feature (e.g. surveillance / threat-detection queries to demonstrate two-disjunction OR). The next AI session reads the example as canonical and infers the tool is for that purpose, accreting tone and framing the tool does not actually have. This pattern compounds across rewrites.

**Prevention.** Examples use generic placeholders only (`alice`, `Bob`, `example.com`, `Example Trading`, `Example Inc`). No example carries a domain-specific scenario (legal, medical, surveillance, finance). When a structural lesson needs an example, the example uses the smallest shape that demonstrates the lesson, with no invented backstory.

### B5. Real user identities leaking into placeholders

**Failure mode.** AI uses a real configured identity name from this user's environment (e.g. an actual `[identity.NAME]` block name) as a placeholder in an example. The doc ships with a name that looks generic but in fact identifies the user's setup.

**Prevention.** Identity placeholders are uppercase generic tokens (`NAME`) or neutral words (`team`, `partner`). Specific identity names are never written into the source.

---

## C. Output handling

### C1. Mixing stderr into stdout when piping to `jq`

**Failure mode.** AI writes `mailroom search "..." --format json 2>&1 | jq '.'`. The CLI writes JSON to stdout and writes nudges (e.g. "mailroom command installed at version X, current is Y") to stderr. With `2>&1`, the nudge concatenates into the stdout stream and `jq` chokes on the trailing text. AI sees a parse error and may retry with the same flag, looping.

**Prevention.** The recommended pattern in every example is `mailroom ... > "$RESULTS"` followed by `jq ... "$RESULTS"`. There is no `2>&1` anywhere in the doc. Stderr is left to fall on the user's terminal where nudges are addressed to a human reader, not a parser.

### C2. Truncating JSON via `head`/`tail`

**Failure mode.** AI sees a long JSON output and pipes it through `head -50` or `tail -100` to "preview." The cut lands mid-record; downstream parsing fails.

**Prevention.** The Searching prose ends with: "Slice the JSON with `jq` against a tempfile; `head`/`tail` cut mid-structure." Direct mention of the wrong tool with the consequence stated.

### C3. jq filter omits the fields a follow-up needs

**Failure mode.** The doc's search example shows a `jq` filter that returns only `{subject, from, date}`. AI copies the filter, runs the search, then realises it has no UID or folder to feed `read`. AI then either guesses fields, runs a second search to extract the missing fields, or falls back to `--help`.

**Prevention.** The search example's `jq` filter returns at minimum `{uid, folder, subject, from, date}`. The follow-up `read` example's filter additionally surfaces `has_attachments`. The fields visible in the example match the fields the next verb needs.

---

## D. Sending: the niche apparatus

### D1. Mode B (relay-style sends) inflates the send section

**Failure mode.** Doc explains both `--identity NAME` (Mode A) and `--smtp NAME --from EMAIL` (Mode B) up front. AI for the common case (user has identities configured) wades through Mode B prose to find Mode A. Mode B serves a small minority of installations.

**Prevention.** The doc covers only `-i NAME` (= `--identity NAME`). Relay-style and other less-common send paths are routed to `mailroom <verb> --help`. The pointer to `--help` is the load-bearing element.

### D2. Pre-explained niche behaviours: cowardly refusal, identity-level BCC, FCC plumbing

**Failure mode.** Doc preemptively documents `--allow-no-copy`, `[identity.NAME].bcc`, the `Sent`-folder auto-detection, the `save_sent = false` behaviour, etc. AI memorises niche configuration paths it almost never needs. When it does encounter them, the runtime error message is more specific than the doc's pre-explanation anyway.

**Prevention.** None of these mechanics appear in the doc. The runtime surfaces the relevant error with the exact corrective flag at the moment it matters.

### D3. Migration footnote for old syntax

**Failure mode.** Doc carries a line like "Migration: `-a <account>` → `--imap <name>`" for users who once used the pre-1.x syntax. A fresh AI session in a fresh project has never seen `-a`; the line is noise.

**Prevention.** No migration footnote in the doc. The current syntax is the only syntax described.

---

## E. Doc mechanics

### E1. Writer-centric content (writing what the writer knows)

**Failure mode.** AI editor adds every fact they found while exploring the codebase: the operators list, the `X-GM-RAW` dispatch detail, the `imap:` raw escape, the `[local_cache]` provenance fields, the FCC mechanics. Result is a manual that duplicates `--help`. Each line costs reader attention; cumulative effect is the AI uses the doc as if it were authoritative reference and skips `--help` even when `--help` is more accurate.

**Prevention.** Every line in the doc must reduce a recurring AI mistake the runtime does not already correct. Lines that restate `--help` content are cut. The doc's job is not to make `--help` redundant; it is to point the AI at the right shape so it consults `--help` only on the cases that need it.

### E2. The doc opens with framing/philosophy before any use case

**Failure mode.** Doc starts with "An invocation is a chain of verbs. One question maps to one invocation..." Reader has nothing concrete yet; the abstraction means nothing until they have seen one search. AI reading top-to-bottom skims the abstract opening and lands on the first example without internalising the principle.

**Prevention.** After a one-paragraph framing sentence, the next thing the reader sees is a concrete `mailroom search` chain. The chain principle is implicit in the example shape, then named in passing.

### E3. Repeating the same rule in multiple places

**Failure mode.** Doc states "don't guess email address" in the frontmatter description, then again in a "Looking up a person by name" section, then again as commentary on a search example. AI absorbs three slight variants and may treat them as separate rules.

**Prevention.** Each rule appears once, in the spot the AI is most likely to be reading at the moment it applies. The "don't guess address" rule is in the frontmatter (which routes the call) and reinforced once in the Searching section.

### E4. Imperative / supervisor tone where peer phrasing fits (NNN)

**Failure mode.** Doc uses "must", "always", "ensure", "never" for rules that actually have edge cases. AI reads them as invariants, applies them rigidly, refuses sensible exceptions. Or: doc writes behaviour scripts ("show the user before transmitting", "wait for approval") for actions the system already enforces structurally — `compose --send` requires `--to`, the AI cannot fire a send without inputs the user gave. The instruction has nothing to do; the AI then mimics it as performative supervisor narration ("I'll show you first and wait for approval") even when no approval flow is in play.

**Prevention.** Peer-AI tone throughout. "Avoid"/"prefer"/declarative phrasing by default; "never" reserved for true invariants. No "show before X / wait for Y" lines at all unless an observed failure mode shows the AI actually doing X without Y.

### E5. Editor narrates the problems being solved in the doc itself

**Failure mode.** AI editor of the doc adds prose like "this prevents the AI from guessing addresses" or "this section addresses the connection-cap problem." The doc becomes a problem-statement document instead of a hint document. Reader (the next AI agent) is expected to infer behaviour from a mix of facts and meta-commentary.

**Prevention.** The doc contains zero meta-narration about its own design. It states facts and shows shapes; the prevention is implicit in the choice of fact and shape. The problem record lives in this companion file, separate from the hint itself.

### E6. Em-dashes and AI-default punctuation

**Failure mode.** AI generation reflex: emit an em-dash to bridge half-finished sentences. Doc accumulates em-dashes; reader (human or AI) registers the AI authorship cue.

**Prevention.** Zero em-dashes in the source. Comma, colon, parenthetical, full stop, or recasting the sentence under dependency-grammar.

### E7. Self-referential filenames or paths

**Failure mode.** Doc references its own filename or full path in body text. Refactoring or renaming requires editing not just the file but every self-mention.

**Prevention.** Body text refers to "the slash-command source", "this hint", "the doc"; never `claude-command.md`, never an absolute path.

---

## F. Boundary with `--help` and `docs/`

### F1. Reproducing the operators list

**Failure mode.** Doc enumerates `from:`, `to:`, `subject:`, `after:`, `before:`, `is:unread`, `is:read`, etc. AI memorises the partial list, then misses an operator that exists but wasn't in the doc, and either invents one or runs a second search without it. `mailroom search --help` and the Gmail-syntax docs already enumerate these accurately.

**Prevention.** No operator list in the doc. The example queries demonstrate the syntax shape; `--help` carries the inventory.

### F2. Reproducing send-flag inventory

**Failure mode.** Doc lists `--bcc`, `--cc`, `--attach`, `--body-html`, `--reply-all`, `--allow-no-copy`, `--keep-draft`, `--dry-run`, `--fcc IMAP:FOLDER`, etc. AI for a routine reply scans the list and guesses the wrong flag combination.

**Prevention.** The Sending section names only the verbs (`compose`, `reply`, `send-draft`) and the load-bearing flag (`-i NAME`). Other flags are in `mailroom <verb> --help`.

### F3. Reproducing exotic-backend dispatch internals

**Failure mode.** Doc explains `X-GM-RAW` dispatch on Gmail accounts and the `imap:` raw escape for parens grouping on non-Gmail backends. Most users hit only the Gmail path; non-Gmail users hit a runtime error that names the corrective flag.

**Prevention.** Neither dispatch detail is in the doc. The Searching example uses parens; on Gmail it works; on non-Gmail backends the AI sees the runtime error and consults `--help` for the escape syntax.
