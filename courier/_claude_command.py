"""Slash-command source for Claude Code, plus its calibration record.

This module carries the body of the file emitted by
``courier install-claude-command`` into the user's Claude Code commands
directory, alongside the failure-modes registry the slash-command source
is calibrated against. The two sit together so that any editor of the
slash-command body has the calibration record in line of sight.

Before editing ``SLASH_COMMAND``, walk ``RATIONALE`` and verify the
load-bearing element of the source that prevents each listed failure is
still intact.
"""

RATIONALE = r"""# Failure modes the AI hint document is calibrated against

Each entry pairs a concrete failure case with the load-bearing element of the source that prevents it. The hint states facts and shows shapes; this block holds the problem record.

## Solutions are framing and examples, not rules

Most preventions in the slash-command source operate through framing and example choice rather than imperative rules. Two reasons.

First, imperative phrasing ("you must X", "always Y", "never Z") sometimes gets folded into the user's prompt frame by the reading AI, with the doc's rule overriding the user's actual instruction in edge cases. The effect is a form of accidental prompt injection. Framing acts on the doc's shape rather than on the reader's judgement: a section titled "Searches" and an example that chains two `search` verbs makes the plural operation the shape the reader sees, leaving their judgement to choose when the shape applies.

Second, established prompt-engineering practice holds that positive framing and fewer constraints produce better instruction adherence than negation and long rule lists. Negation is processed less reliably than affirmation by current LLMs; dense constraint stacks dilute per-rule attention. The slash-command source biases toward shape and example over rule for this reason, and this registry mirrors the same convention in its own prose: every solution that can be expressed as a framing decision is.

When a solution genuinely requires a rule (e.g. "avoid `2>&1` in examples"), the rule appears once, at the position the AI is most likely to be reading when it applies.

---

## A. Calling pattern

### A1. Singular-verb bias

**Failure case.** In a case where the user asked "find emails from Alice about hotel booking and from Bob about the contract", the AI failed to use the chain shape because it ran the two questions as separate `courier` invocations, paying two IMAP logins per IMAP block (Gmail caps simultaneous IMAP connections per account). The same fan-out pattern surfaces with `read`, `attachments`, and serial `search`+`read`.

**Solution.** The Searches and Reads examples chain at least two verbs, so batching is the only shape the reader meets for the verbs that batch. The Attachments and Sending blocks list their verbs on separate lines because those are alternatives to choose between, not a fan-out to collapse. Section titles are plural where the operation is ("Searches", "Reads", "Attachments").

### A2. Fan-out across processes for N items

**Failure case.** In a case where the user asked the AI to read three messages from the same folder, the AI failed to share one IMAP login because it ran `courier read -u 1`, `courier read -u 2`, `courier read -u 3` as three separate processes (or a bash loop), each paying its own login. On Gmail this trips the per-account simultaneous-connection cap.

**Solution.** The `read` example chains the three UIDs inside one invocation: `read -u 100 read -u 200 read -u 300 -f INBOX`. The prose states "Login dominates the per-fetch cost; Gmail caps simultaneous IMAP connections per account, so N parallel `read` processes hit that cap" as a fact, not as an instruction. The reader infers the chain shape from the cost statement plus the example.

### A3. Expecting dynamic UID substitution within a chain

**Failure case.** In a case where the AI had absorbed the chain shape, it tried `courier search "from:alice" read -u $UID -f INBOX` expecting the search to feed UIDs into `read`. The chain ran both verbs but the UID in `read` was static; no substitution happened.

**Solution.** The `read` example uses literal UIDs (`-u 100 -u 200 -u 300`). The prose says "UIDs from a prior search go into one chain." The "from a prior search" phrasing implies the AI ran search, looked at the JSON, then pasted UIDs into the second invocation. Examples in the source stay clear of any shape that suggests dynamic substitution.

### A4. Preflight probing

**Failure case.** In a case where the user asked "what's in my inbox?", the AI failed to answer directly because it first ran `courier list` and `courier config-check` to "see what's configured" before getting to the actual question, doubling the latency.

**Solution.** Examples open directly on the user's question. Configuration discovery appears exactly once, scoped to the case where it is genuinely needed (`courier list returns the configured identity names under its identity key`). The shape of every example does the work; the source leaves probing to the AI's judgement.

---

## B. Entity disambiguation in queries

### B1. Guessing addresses from names

**Failure case.** In a case where the user asked "find Alice's email about the contract", the AI failed to retrieve any results because it synthesised `from:alicedoe@gmail.com` from the name. The address was wrong, the search returned nothing, and the AI reported "no results found" until the user pushed back with the real address.

**Solution.** The frontmatter description routes address lookup to the tool in trigger form ("look up a contact's address") rather than asserting a rule; the body carries the explicit steer once: "Use the words from the user's request (a name they mentioned, a domain, a subject phrase); an AI-constructed address often misses." The example query in the Searches section uses a bare name (`from:alice`) rather than a synthesised full address; the pattern in the example does the work before the prose rule is reached.

### B2. Surface-disjoint identifiers for one entity

**Failure case.** An entity files its mail under surfaces the user does not speak, and the AI searches the spoken one. Four observed shapes:

- Corporate domain against operating name. Asked for "China Southern Airlines", the AI composed `search "from:china southern"`, while the airline files under `@csair.com` (the Chinese-derived domain shares no letters with the English operating name) and refers to itself by the IATA code `CZ` in older subjects.
- Language variants of an operating name. Asked for the Spanish airline Iberia, the AI queried `Iberia Airlines`, while `Iberia Líneas Aéreas` appears on most messages and `@iberia.com` catches the rest.
- Nickname against filed name. Asked what Tony said about the lease, the AI queried `from:Tony`, while the person signs and sends as `Antonio`; the colloquial form never reaches the mail surface.
- Known synonyms split apart. Holding a domain, a trading name and an English variant, the AI issued three `search` repetitions, fragmenting one entity's mail across three outer keys at three server queries for no benefit.

**Solution.** The Searches section shows OR-clustering inside one `search` for the variants of a single entity, and the prose contrasts the two shapes: synonyms of one entity go inside one `search` with `OR`, returning a flat union under one outer key; separate questions go in separate `search` repetitions. The OR paragraph notes that an entity's surfaces (name, code, corporate domain, language variants) often share no letters, so enumeration draws on what the user already knows. The prose adds that the form filed in headers may differ from the spoken form, and that reading a hit recovers the actual surface.

### B3. Cartesian N×M instead of one composed query

**Failure case.** In a case where the user asked for messages where a specific address appeared in any of several recipient roles AND the body mentioned any of several topic words, the AI failed to compose one query because it issued N×M chained `search` repetitions (one per role × topic pair), multiplying server queries by the cartesian product without changing the result set.

**Solution.** The OR-cluster example combined with the operator-stacking note ("tokens combine with implicit AND, `OR` clusters alternatives") implies the form: two independent disjunctions form one query `(A OR B OR C) (X OR Y OR Z)`. The current source carries one OR-cluster example; the two-disjunction lesson lands on that simpler case. A second example would risk the prior fabricated-scenario problem (E8) without a clear use-case shape.

### B4. Sort-order surprise on "recent" / "last few" requests

**Failure case.** In a case where the user asked "what are the last 5 emails from Alice", the AI failed to bound the result to the recent five because it ran a search without `-n`, getting up to 10 hits in unspecified order, then either reported too many or picked the wrong ones because it expected ascending date.

**Solution.** The Searches prose states "Results sort newest-first; trailing `-n` peels off as a chain default (default 50) and applies to every `search` that doesn't set its own", so the order and the bound arrive together without a trip to `--help` on a routine request. No example sets `-n`: the examples bound the *reported* set at the `jq` step instead, and the prose says the tempfile still holds all 50.

---

## C. Output handling

### C1. Mixing stderr into stdout when piping to jq

**Failure case.** In a case where the AI wrote `courier search "..." --format json 2>&1 | jq '.'`, the JSON parse broke because stderr nudges (e.g. "courier command installed at version X, current is Y") concatenated into stdout, and `jq` choked on the trailing text. The AI saw the parse error and sometimes retried with the same `2>&1`, looping.

**Solution.** Examples redirect stdout to a tempfile (`> "$RESULTS"`) and read it with `jq` separately, leaving stderr to fall on the user's terminal where nudges are addressed to a human reader. The stream-merging anti-pattern is absent from every model in the source. Shape alone left the reason unstated, so the output paragraph now also carries it as a fact: stderr holds the nudges, and `2>&1` puts non-JSON text in front of `jq`.

### C2. Truncating JSON via head / tail

**Failure case.** In a case where the AI ran a search and piped the JSON through `head -50` to "preview" it, the cut landed mid-record and downstream parsing failed.

**Solution.** The Searches prose carries "Slice the JSON with `jq` against a tempfile; `head`/`tail` cut mid-structure." Stated once, naming the wrong tool and the consequence. This is one of the few preventions expressed as a rule rather than a framing, because example shape cannot demonstrate the alternative; the AI reaches for `head`/`tail` by default and benefits from the explicit nudge.

### C3. jq filter omits the fields a follow-up verb needs

**Failure case.** In a case where the doc's search example showed `jq '... | {subject, from, date}'` and the AI copied the filter, it ran the search, returned `{subject, from, date}` to itself, then realised it had no `uid` or `folder` to feed into `read`. The AI then either ran a second search, guessed fields, or fell back to `--help`.

**Solution.** Both examples filter through `map_values(map_values(...))`, which keeps the `{op_key: {imap_name: ...}}` envelope, so one idiom is taught rather than two. The search step trims each block's `results` in place without selecting fields, so every field a follow-up verb might need survives. The `read` step selects `{uid, folder, subject, from, date, body}`, all of which a read result carries.

---

## D. Sending

### D1. Mode B (relay-style sends) inflates the send section

**Failure case.** In a case where the user asked the AI to send a routine reply from their main identity, the AI failed to act quickly because the doc carried both Mode A (`--identity NAME`) and Mode B (`--smtp NAME --from EMAIL`) up front; the AI read both, weighed which applied, and produced a slower response. Mode B serves a small minority of installations.

**Solution.** The source covers only `-i NAME` (= `--identity NAME`). Relay-style and other less-common send paths route to `courier <verb> --help`. The pointer to `--help` is what the doc does instead of enumerating modes.

### D2. Transliterating non-ASCII content to ASCII

**Failure case.** In a case where the user's body carried "Skål" (a club name), the AI sent "Skal", dropping the ring, and justified the change to the user as keeping the send "clean". No encoding constraint existed: the same backend had already carried Spanish (ñ, á) and Chinese in the user's other mail, with valid message-ids. The AI had applied a training-era prior that ASCII is the safe default and manufactured a deliverability rationale to fit a change it had no reason to make.

**Solution.** The Sending prose states that subjects and bodies pass through as UTF-8 in whatever script the user writes, and that the user's correspondence runs in Spanish and Chinese as well as English. The fact plus the base rate of non-English mail frames character fidelity as the default, leaving no opening for an English-only assumption or an ASCII-normalisation step. Phrased as framing rather than a prohibition, per the convention above (a "never transliterate" rule reads as infantilising and processes less reliably than the positive fact).

### D3. Auto-selecting the nearest identity when the prior one is unavailable

**Failure case.** In a case where the user asked the AI to reply to a thread and the identity that thread was previously sent from no longer appeared among the configured `[identity.NAME]` blocks, the AI silently picked the closest-matching configured identity and sent under it. The picked identity was a personal address, not the business address the thread needed, and the user only caught it after the message had gone out.

**Solution.** The Sending prose states directly that a missing prior identity is a stop-and-ask case, not a nearest-match guess. This is one of the few instructions phrased as a behaviour script rather than a framing fact (per E4), because the failure is an unauthorised send under the wrong identity, not a stylistic choice the AI's judgement can absorb from an example.

---

## E. Doc mechanics

### E1. Writer-centric content

**Failure case.** In a case where an AI editor wrote down every fact found while exploring the codebase (operators list, `X-GM-RAW` dispatch detail, `imap:` raw escape, `[local_cache]` provenance fields, FCC mechanics), the resulting doc duplicated `--help`. The reading AI used the doc as authoritative reference and skipped `--help` even when `--help` was more accurate.

**Solution.** Every line in the source must reduce a recurring AI mistake the runtime does not already correct. Lines that restate `--help` content are cut. The doc points the AI at the right shape so the AI consults `--help` on the cases that need it.

### E2. Doc opens with framing before any use case

**Failure case.** In a case where the doc opened with "An invocation is a chain of verbs. One question maps to one invocation..." before showing what a search actually looked like, the reading AI skimmed the abstraction (which means nothing until they have run one search) and reached the first example without internalising the principle.

**Solution.** The source opens with one short framing sentence, then immediately shows a chained `courier search` example. The chain principle is implicit in the example shape and named in passing afterward.

### E3. Repeating the same rule in multiple places

**Failure case.** In a case where the doc stated "don't guess email address" in the frontmatter description, in a "Looking up a person by name" section, and again as commentary on a search example, the AI absorbed three slight variants and treated them as separate rules with overlapping but non-identical scopes.

**Solution.** Each rule appears once, at the position the AI is most likely to be reading when the rule applies. The "don't guess address" rule lives once in the Searches body, where the AI reads it as it composes a query; the frontmatter routes address lookup in trigger form rather than restating the rule.

### E4. Imperative tone where peer phrasing fits

**Failure case.** In a case where the doc used "must", "always", "ensure", "never" for rules that have edge cases, the AI applied the rules rigidly and refused sensible exceptions. In a related case where the doc wrote behaviour scripts ("show the user before transmitting", "wait for approval") for actions the system already enforces structurally (`compose --send` requires `--identity` or `--smtp/--from`), the AI mimicked the script as performative supervisor narration even when no approval flow was in play.

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

### F1. Reproducing an inventory `--help` already owns

**Failure case.** The source copies a list the runtime carries accurately, and the reading AI treats the copy as complete. Five observed:

- Operators. The doc enumerated `from:`, `to:`, `subject:`, `after:`, `before:`, `is:unread`, `is:read`; the AI memorised the partial list, missed an operator that exists but was not in it (`larger:`, `has:attachment`), and either invented one or ran a second search without it.
- Send flags. The doc listed `--bcc`, `--cc`, `--attach`, `--body-html`, `--no-thread`, `--allow-no-copy`, `--keep-draft`, `--dry-run`, `--fcc IMAP:FOLDER`; for a routine reply the AI scanned the list and guessed a wrong combination instead of consulting `courier reply --help`.
- Dispatch internals. The doc explained `X-GM-RAW` dispatch on Gmail and the `imap:` raw escape for parens grouping elsewhere; Gmail-only users absorbed reference material they never needed, and non-Gmail users met the runtime error anyway, which is more specific than the pre-explanation.
- Niche send configuration. The doc pre-documented `--allow-no-copy`, `[identity.NAME].bcc`, `fcc` folder auto-detection and `save_sent = false`; the AI memorised configuration paths it almost never needs, while the error at the moment one matters names the exact corrective flag.
- Superseded syntax. The doc carried "Migration: `-a <account>` → `--imap <name>`" for users coming from the pre-1.x CLI; a fresh AI session has never seen `-a` and reads the line as noise that wastes attention.

**Solution.** Inventory lives in `--help`; the source demonstrates shape. Operator syntax comes from the example queries rather than a list. The Sending section carries the three verbs (`compose`, `reply`, `send-draft`), the flags a minimal send cannot omit (`--to`, `--subject`, `--body`, `--send`), and the identity flag (`-i NAME`). Relay-style sends, dispatch internals and niche configuration route to `courier <verb> --help`, where a runtime error at the point of failure beats a pre-explanation. Only current syntax is described.
"""


SLASH_COMMAND = r"""---
name: courier
description: Find, read, or recall anything in the user's email: search emails, check replies, look up a contact's address, pull attachments, show which sender email addresses the user has, or send mail as one of them.
---

# courier

courier searches, reads, and pulls attachments from the user's mailboxes via the courier CLI, and sends mail when asked, although most of the time it is not for sending. Each request is one invocation; chained verbs share one connection per IMAP block.

## Searches

```bash
RESULTS=$(mktemp /tmp/courier.XXXXXXXX)
courier -A --format json \
  search "from:alice hotel booking" \
  search "from:bob contract after:2026-01-01" \
  > "$RESULTS"
jq 'map_values(map_values(.results |= .[0:10]))' "$RESULTS"
```

Pack questions into one invocation: each `search` becomes one outer key in the result, sharing one connection per IMAP block. `search` accepts Gmail-style queries; tokens combine with implicit AND, `OR` clusters alternatives. Results sort newest-first; trailing `-n` peels off as a chain default (default 50) and applies to every `search` that doesn't set its own. The `jq` step trims each block's `results` to 10 in place, keeping the `{op_key: {imap_name: ...}}` shape; the underlying tempfile holds all 50. To see more, widen the slice (e.g. `.[0:25]` or drop the `|= .[0:10]` step) rather than re-run `courier`. Use the words from the user's request (a name they mentioned, a domain, a subject phrase); an AI-constructed address often misses, and a spoken nickname or short form (Tony for Antonio) may not match the form filed in headers. Read a hit to recover the actual surface from `from` or the body, then re-search if needed.

`OR` inside one `search` returns a flat union under that one outer key, so the same entity's different surfaces (name, code, corporate domain, language variants) stay together rather than scatter across separate keys. Surfaces often share no letters; enumerate from what the user knows:

```bash
courier -A --format json \
  search "from:@example.com OR 'Example Trading' OR 'Example Inc'" \
  search "subject:invoice after:2026-01-01" \
  > "$RESULTS"
```

Output shape: `{op_key: {imap_name: {results: [...], provenance: {...}}}}`. `courier -A` queries every IMAP block; `--imap NAME` (repeats across the chain) selects specific blocks. Slice the JSON with `jq` against a tempfile; `head`/`tail` cut mid-structure. stderr carries nudges addressed to the user's terminal (a version reminder, a config warning), so folding it into stdout with `2>&1` puts non-JSON text in front of `jq`.

## Reads

```bash
courier --imap <imap> --format json \
  read -u 100 \
  read -u 200 \
  read -u 300 \
  -f INBOX > "$RESULTS"
jq 'map_values(map_values({uid, folder, subject, from, date, body}))' "$RESULTS"
```

UIDs from a prior search go into one chain over a single IMAP session; trailing `-f FOLDER` peels off as the chain default. Login dominates the per-fetch cost; Gmail caps simultaneous IMAP connections per account, so N parallel `read` processes hit that cap.

## Attachments

```bash
courier --imap <imap> attachments -f <folder> -u <uid>
courier --imap <imap> save -f <folder> -u <uid> --attachment <name> -o <path>
courier --imap <imap> export -f <folder> -u <uid> --raw -o /tmp/msg.eml
```

`--attachment` accepts a filename or the numeric index reported by `attachments`.

## Sending

```bash
courier compose --to recipient@example.com --subject "..." --body "..." --send -i NAME
courier --imap <imap> reply -f <folder> -u <uid> --body "..." --send -i NAME
courier --imap <imap> send-draft -f Drafts -u <uid>
```

Subjects and bodies pass through as UTF-8 in whatever script the user writes; their correspondence runs in Spanish and Chinese as well as English.

`-i NAME` (= `--identity NAME`) picks a configured `[identity.NAME]` block; reply inherits the parent's threading headers. Drop `--send` to save as a draft. `courier list` returns the configured identity names under its `identity` key, as JSON already and with no format flag of its own; `courier <verb> --help` carries flags for relay-style sends and other less-common paths. For a reply, if the identity the thread was previously sent from isn't among those names, ask the user which identity to use rather than picking the closest match: the nearest name is often a personal address on a thread that needs a business one.
"""


def render(version: str) -> str:
    """Return the slash-command body with ``version:`` stamped into the frontmatter.

    Args:
        version: The courier version string to record in the installed copy.

    Returns:
        The text written to the installed command file. The
        ``version:`` line is inserted as the first field inside the YAML
        frontmatter block when the body opens with ``---\\n``; otherwise
        the body is returned unchanged.
    """
    if SLASH_COMMAND.startswith("---\n"):
        return "---\nversion: " + version + "\n" + SLASH_COMMAND[4:]
    return SLASH_COMMAND
