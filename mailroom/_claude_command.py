"""Slash-command source for Claude Code, plus its calibration record.

This module carries the body of the file emitted by
``mailroom install-claude-command`` into the user's Claude Code commands
directory, alongside the failure-modes registry the slash-command source
is calibrated against. The two sit together so that any editor of the
slash-command body has the calibration record in line of sight.

Before editing ``SLASH_COMMAND``, walk ``RATIONALE`` and verify the
load-bearing element of the source that prevents each listed failure is
still intact.
"""

RATIONALE = r"""# Failure modes the AI hint document is calibrated against

This block sits in the same module as the slash-command source so a future editor of that source has the calibration record in line of sight. Each entry pairs a concrete failure case with the load-bearing element of the source that prevents it. Before shipping a rewrite, walk this list and verify the prevention is still intact.

The hint states facts and shows shapes; this block holds the problem record.

## Solutions are framing and examples, not rules

Most preventions in the slash-command source operate through framing and example choice rather than imperative rules. Two reasons.

First, imperative phrasing ("you must X", "always Y", "never Z") sometimes gets folded into the user's prompt frame by the reading AI, with the doc's rule overriding the user's actual instruction in edge cases. The effect is a form of accidental prompt injection. Framing acts on the doc's shape rather than on the reader's judgement: a section titled "Searches" and an example that chains two `search` verbs makes the plural operation the shape the reader sees, leaving their judgement to choose when the shape applies.

Second, established prompt-engineering practice holds that positive framing and fewer constraints produce better instruction adherence than negation and long rule lists. Negation is processed less reliably than affirmation by current LLMs; dense constraint stacks dilute per-rule attention. The slash-command source biases toward shape and example over rule for this reason, and this registry mirrors the same convention in its own prose: every solution that can be expressed as a framing decision is.

When a solution genuinely requires a rule (e.g. "avoid `2>&1` in examples"), the rule appears once, at the position the AI is most likely to be reading when it applies.

---

## A. Calling pattern

### A1. Singular-verb bias

**Failure case.** In a case where the user asked "find emails from Alice about hotel booking and from Bob about the contract", the AI failed to use the chain shape because it ran the two questions as separate `mailroom` invocations, paying two IMAP logins per IMAP block (Gmail caps simultaneous IMAP connections per account). The same fan-out pattern surfaces with `read`, `attachments`, and serial `search`+`read`.

**Solution.** Every code example in the source chains at least two verbs. Section titles are plural ("Searches", "Reads") so the operation reads as naturally plural. The plural shape is the only one the reader encounters; the AI absorbs the chained form from the only pattern present.

### A2. Fan-out across processes for N items

**Failure case.** In a case where the user asked the AI to read three messages from the same folder, the AI failed to share one IMAP login because it ran `mailroom read -u 1`, `mailroom read -u 2`, `mailroom read -u 3` as three separate processes (or a bash loop), each paying its own login. On Gmail this trips the per-account simultaneous-connection cap.

**Solution.** The `read` example chains the three UIDs inside one invocation: `read -u 100 read -u 200 read -u 300 -f INBOX`. The prose states "Login dominates the per-fetch cost; Gmail caps simultaneous IMAP connections per account, so N parallel `read` processes hit that cap" as a fact, not as an instruction. The reader infers the chain shape from the cost statement plus the example.

### A3. Expecting dynamic UID substitution within a chain

**Failure case.** In a case where the AI had absorbed the chain shape, it tried `mailroom search "from:alice" read -u $UID -f INBOX` expecting the search to feed UIDs into `read`. The chain ran both verbs but the UID in `read` was static; no substitution happened.

**Solution.** The `read` example uses literal UIDs (`-u 100 -u 200 -u 300`). The prose says "UIDs from a prior search go into one chain." The "from a prior search" phrasing implies the AI ran search, looked at the JSON, then pasted UIDs into the second invocation. Examples in the source stay clear of any shape that suggests dynamic substitution.

### A4. Preflight probing

**Failure case.** In a case where the user asked "what's in my inbox?", the AI failed to answer directly because it first ran `mailroom list` and `mailroom config-check` to "see what's configured" before getting to the actual question, doubling the latency.

**Solution.** Examples open directly on the user's question. Configuration discovery appears exactly once, scoped to the case where it is genuinely needed (`mailroom list returns the configured identity names under its identity key`). The shape of every example does the work; the source leaves probing to the AI's judgement.

---

## B. Entity disambiguation in queries

### B1. Guessing addresses from names

**Failure case.** In a case where the user asked "find Alice's email about the contract", the AI failed to retrieve any results because it synthesised `from:alicedoe@gmail.com` from the name. The address was wrong, the search returned nothing, and the AI reported "no results found" until the user pushed back with the real address.

**Solution.** The frontmatter description carries: "Use this rather than guessing an email address from a name." The body reinforces once: "Use the words from the user's request (a name they mentioned, a domain, a subject phrase); an AI-constructed address often does not match the real one, which sits in each hit's `from`." The example query in the Searches section uses a bare name (`from:alice`) rather than a synthesised full address; the pattern in the example does the work before the prose rule is reached.

### B2. Surface-disjoint identifiers for one entity

**Failure case.** In a case where the user asked "find emails from China Southern Airlines", the AI failed to retrieve most of the mail because it composed `search "from:china southern"`, while the company files its mail under `@csair.com` (the Chinese-derived corporate domain shares no letters with the English operating name) and refers to itself by the IATA code `CZ` in older subjects. The AI took the user's spoken English name as the search key and did not enumerate the surface-disjoint identifiers (code, domain, name) that the same entity files mail under.

**Solution.** The Searches section shows OR-clustering inside one `search` for variants of a single entity. The prose contrasts repeated `search` verbs (separate questions, separate outer keys) with `OR` inside one search (synonyms of one entity, one outer key, flat union). The OR paragraph mentions that the entity's surfaces (name, code, corporate domain) often share no letters, so enumeration draws on what the user already knows.

### B3. Language variants of operating names

**Failure case.** In a case where the user asked for emails from the Spanish airline Iberia, the AI failed to retrieve the bulk of the correspondence because it queried only `Iberia Airlines`, while the airline's Spanish operating name `Iberia Líneas Aéreas` appears on most messages and `@iberia.com` catches the rest. The AI did not consider that an entity's operating name has language variants and that any of them may be the form recorded in a given message.

**Solution.** Same OR-cluster example as B2: the AI sees that name variants of one entity belong inside one `search` connected by `OR`. The reader infers that language variation is one source of disjointness (others: trading-name variation, code abbreviations, domain derivations).

### B4. Name shortening and preferred-name divergence

**Failure case.** In a case where the user asked "find what Tony said about the lease", the AI failed to retrieve the relevant correspondence because it queried `from:Tony`, while the person is filed as `Antonio` in his email signature and From header (the colloquial nickname "Tony" never reaches the mail surface; his account uses his full name). The AI took the user's spoken form as the search key and did not consider that nicknames and formal names diverge between conversation and mail headers.

**Solution.** The Searches prose notes that the form filed in headers may differ from the spoken form (nicknames, abbreviations, formal names) and that reading a hit recovers the actual surface used in mail. The OR mechanism is available once the AI knows multiple forms.

### B5. Splitting known synonyms across separate searches

**Failure case.** In a case where the AI knew three forms of one entity (a domain, a trading name, an English variant), it failed to keep them as one entity in the result because it issued three `search` repetitions, fragmenting the entity's mail across three outer keys. Cost three server queries, no benefit.

**Solution.** The OR example sits as the primary illustration of "synonyms of one entity go inside one `search` with `OR`; separate questions go in separate `search` repetitions." The contrast is stated once, alongside the example that demonstrates it. The reader sees the OR shape and the chain shape side by side and infers which goes where.

### B6. Cartesian N×M instead of one composed query

**Failure case.** In a case where the user asked for messages where a specific address appeared in any of several recipient roles AND the body mentioned any of several topic words, the AI failed to compose one query because it issued N×M chained `search` repetitions (one per role × topic pair), multiplying server queries by the cartesian product without changing the result set.

**Solution.** The OR-cluster example combined with the operator-stacking note ("tokens combine with implicit AND, `OR` clusters alternatives") implies the form: two independent disjunctions form one query `(A OR B OR C) (X OR Y OR Z)`. The current source carries one OR-cluster example; the two-disjunction lesson lands on that simpler case. A second example would risk the prior fabricated-scenario problem (E8) without a clear use-case shape.

### B7. Sort-order surprise on "recent" / "last few" requests

**Failure case.** In a case where the user asked "what are the last 5 emails from Alice", the AI failed to bound the result to the recent five because it ran a search without `-n`, getting up to 10 hits in unspecified order, then either reported too many or picked the wrong ones because it expected ascending date.

**Solution.** The Searches prose states "Results sort newest-first; `-n` limits per IMAP block (default 10)" in one sentence so the AI has both facts together without needing `--help` for a routine request. The first example uses `-n 5` to ground the flag in a typical request.

---

## C. Output handling

### C1. Mixing stderr into stdout when piping to jq

**Failure case.** In a case where the AI wrote `mailroom search "..." --format json 2>&1 | jq '.'`, the JSON parse broke because stderr nudges (e.g. "mailroom command installed at version X, current is Y") concatenated into stdout, and `jq` choked on the trailing text. The AI saw the parse error and sometimes retried with the same `2>&1`, looping.

**Solution.** Examples redirect stdout to a tempfile (`> "$RESULTS"`) and read it with `jq` separately, leaving stderr to fall on the user's terminal where nudges are addressed to a human reader. The example shape is what the AI copies; the stream-merging anti-pattern is absent from every model in the source.

### C2. Truncating JSON via head / tail

**Failure case.** In a case where the AI ran a search and piped the JSON through `head -50` to "preview" it, the cut landed mid-record and downstream parsing failed.

**Solution.** The Searches prose ends with "Slice the JSON with `jq` against a tempfile; `head`/`tail` cut mid-structure." Stated once, naming the wrong tool and the consequence. This is one of the few preventions expressed as a rule rather than a framing, because example shape cannot demonstrate the alternative; the AI reaches for `head`/`tail` by default and benefits from the explicit nudge.

### C3. jq filter omits the fields a follow-up verb needs

**Failure case.** In a case where the doc's search example showed `jq '... | {subject, from, date}'` and the AI copied the filter, it ran the search, returned `{subject, from, date}` to itself, then realised it had no `uid` or `folder` to feed into `read`. The AI then either ran a second search, guessed fields, or fell back to `--help`.

**Solution.** The search example's jq filter returns at minimum `{uid, folder, subject, from, date}`. The `read` example's filter additionally surfaces `has_attachments`. The fields visible in each example are exactly the fields the next verb in a plausible workflow needs.

---

## D. Sending: niche apparatus

### D1. Mode B (relay-style sends) inflates the send section

**Failure case.** In a case where the user asked the AI to send a routine reply from their main identity, the AI failed to act quickly because the doc carried both Mode A (`--identity NAME`) and Mode B (`--smtp NAME --from EMAIL`) up front; the AI read both, weighed which applied, and produced a slower response. Mode B serves a small minority of installations.

**Solution.** The source covers only `-i NAME` (= `--identity NAME`). Relay-style and other less-common send paths route to `mailroom <verb> --help`. The pointer to `--help` is what the doc does instead of enumerating modes.

### D2. Pre-explained niche behaviours

**Failure case.** In a case where the doc preemptively documented `--allow-no-copy`, `[identity.NAME].bcc`, `sent_folder` auto-detection, and `save_sent = false`, the AI memorised niche configuration paths it almost never needed, while the runtime error message at the moment it actually matters is more specific than the doc's pre-explanation anyway.

**Solution.** The source omits these mechanics; the runtime surfaces the relevant error with the exact corrective flag at the point of failure.

### D3. Migration footnote for old syntax

**Failure case.** In a case where the doc carried "Migration: `-a <account>` → `--imap <name>`" for users coming from the pre-1.x CLI, a fresh AI session has never seen `-a` and reads the line as noise that wastes attention.

**Solution.** The source describes only the current syntax.

---

## E. Doc mechanics

### E1. Writer-centric content

**Failure case.** In a case where an AI editor wrote down every fact found while exploring the codebase (operators list, `X-GM-RAW` dispatch detail, `imap:` raw escape, `[local_cache]` provenance fields, FCC mechanics), the resulting doc duplicated `--help`. The reading AI used the doc as authoritative reference and skipped `--help` even when `--help` was more accurate.

**Solution.** Every line in the source must reduce a recurring AI mistake the runtime does not already correct. Lines that restate `--help` content are cut. The doc points the AI at the right shape so the AI consults `--help` on the cases that need it.

### E2. Doc opens with framing before any use case

**Failure case.** In a case where the doc opened with "An invocation is a chain of verbs. One question maps to one invocation..." before showing what a search actually looked like, the reading AI skimmed the abstraction (which means nothing until they have run one search) and reached the first example without internalising the principle.

**Solution.** The source opens with one short framing sentence, then immediately shows a chained `mailroom search` example. The chain principle is implicit in the example shape and named in passing afterward.

### E3. Repeating the same rule in multiple places

**Failure case.** In a case where the doc stated "don't guess email address" in the frontmatter description, in a "Looking up a person by name" section, and again as commentary on a search example, the AI absorbed three slight variants and treated them as separate rules with overlapping but non-identical scopes.

**Solution.** Each rule appears once, at the position the AI is most likely to be reading when the rule applies. The "don't guess address" rule is in the frontmatter (where call routing happens) and reinforced once in the Searches body.

### E4. Imperative tone where peer phrasing fits

**Failure case.** In a case where the doc used "must", "always", "ensure", "never" for rules that have edge cases, the AI applied the rules rigidly and refused sensible exceptions. In a related case where the doc wrote behaviour scripts ("show the user before transmitting", "wait for approval") for actions the system already enforces structurally (`compose --send` requires `--to`), the AI mimicked the script as performative supervisor narration even when no approval flow was in play.

**Solution.** Peer-AI tone throughout the source. Indicative phrasing by default; "avoid" or "prefer" when a leaning is needed; "never" reserved for true invariants. Behaviour scripts ("show before X / wait for Y") appear only when an observed failure shows the AI doing X without Y.

### E5. Editor narrates the problems being solved in the doc itself

**Failure case.** In a case where an AI editor added prose like "this prevents the AI from guessing addresses" or "this section addresses the connection-cap problem" to the slash-command source, the doc became a problem-statement document instead of a hint. The next reader had to infer behaviour from a mix of facts and meta-commentary, lengthening the doc without sharpening it.

**Solution.** The source states facts and shows shapes; the prevention is implicit in the choice of fact and shape. The problem record lives in this companion block above.

### E6. Em-dashes

**Failure case.** In a case where AI generation produced em-dashes by reflex (half-sentence, attach a thought, bridge with an em-dash), the doc accumulated them; downstream readers (AI and human) registered the AI authorship cue.

**Solution.** The source uses comma, colon, parenthetical, full stop, or dependency-grammar recasting in place of em-dash.

### E7. Self-referential filenames or paths

**Failure case.** In a case where doc body text referenced its own filename or storage location (e.g. "see hint.md", "located at packagename/somedir/..."), a later rename or move required editing not just the file but every self-mention.

**Solution.** Body text refers to "the slash-command source", "this hint", or "the doc". Literal filenames and storage paths stay outside the body.

### E8. AI-fabricated example scenarios

**Failure case.** In a case where an AI editor invented a plausible-but-artificial use case (surveillance / threat-detection queries to demonstrate two-disjunction OR; a legal-evidence search shape) to flex a syntax feature, the next AI session read the example as canonical and inferred the tool was for that purpose, accreting tone and framing the tool does not actually have. The pattern compounded across rewrites.

**Solution.** Examples use generic placeholders (`alice`, `Bob`, `example.com`, neutral entity names) and the smallest shape that demonstrates the lesson. Domain-specific scenarios (legal, medical, surveillance, finance) stay outside the source.

### E9. Real user identities leaking into placeholders

**Failure case.** In a case where an AI editor used a real configured identity name from this user's environment (an actual `[identity.NAME]` block name like `partnerships`) as a placeholder in an example, the doc shipped with a name that looked generic but in fact identified the user's setup.

**Solution.** Identity placeholders stay generic (`NAME`, neutral words); configured identity names stay outside the source.

---

## F. Boundary with `--help` and `docs/`

### F1. Reproducing the operators list

**Failure case.** In a case where the doc enumerated `from:`, `to:`, `subject:`, `after:`, `before:`, `is:unread`, `is:read`, the AI memorised the partial list, missed an operator that exists but wasn't in the doc (e.g. `larger:`, `has:attachment`), and either invented one or ran a second search without it. `mailroom search --help` and the Gmail-syntax docs already enumerate these accurately.

**Solution.** Operator inventory lives in `--help`; the example queries in the source demonstrate the syntax shape only.

### F2. Reproducing send-flag inventory

**Failure case.** In a case where the doc listed `--bcc`, `--cc`, `--attach`, `--body-html`, `--reply-all`, `--allow-no-copy`, `--keep-draft`, `--dry-run`, `--fcc IMAP:FOLDER` in the Sending section, the AI for a routine reply scanned the list and guessed a wrong flag combination instead of consulting `mailroom reply --help` for the specific case.

**Solution.** The Sending section names only the verbs (`compose`, `reply`, `send-draft`) and the load-bearing flag (`-i NAME`). Other flags live in `mailroom <verb> --help`.

### F3. Reproducing exotic-backend dispatch internals

**Failure case.** In a case where the doc explained `X-GM-RAW` dispatch on Gmail accounts and the `imap:` raw escape for parens grouping on non-Gmail backends, the AI for Gmail-only users absorbed reference material it never needed, and non-Gmail users hit a runtime error at the point it actually mattered with a more specific message than the doc's pre-explanation anyway.

**Solution.** Dispatch internals live in `--help`. The Searches example uses parens; on Gmail it works through the dispatch; on non-Gmail backends the AI sees the runtime error and consults `--help` for the escape syntax.
"""


SLASH_COMMAND = r"""---
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

Pack questions into one invocation: each `search` becomes one outer key in the result, sharing one connection per IMAP block. `search` accepts Gmail-style queries; tokens combine with implicit AND, `OR` clusters alternatives. Results sort newest-first; trailing `-n` peels off as a chain default (default 10) and applies to every `search` that doesn't set its own. Use the words from the user's request (a name they mentioned, a domain, a subject phrase); an AI-constructed address often misses, and a spoken nickname or short form (Tony for Antonio) may not match the form filed in headers. Read a hit to recover the actual surface from `from` or the body, then re-search if needed.

`OR` inside one `search` returns a flat union under that one outer key, so the same entity's different surfaces (name, code, corporate domain, language variants) stay together rather than scatter across separate keys. Surfaces often share no letters; enumerate from what the user knows:

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
"""


def render(version: str) -> str:
    """Return the slash-command body with ``version:`` stamped into the frontmatter.

    Args:
        version: The mailroom version string to record in the installed copy.

    Returns:
        The text written to ``~/.claude/commands/mailroom.md``. The
        ``version:`` line is inserted as the first field inside the YAML
        frontmatter block when the body opens with ``---\\n``; otherwise
        the body is returned unchanged.
    """
    if SLASH_COMMAND.startswith("---\n"):
        return "---\nversion: " + version + "\n" + SLASH_COMMAND[4:]
    return SLASH_COMMAND
