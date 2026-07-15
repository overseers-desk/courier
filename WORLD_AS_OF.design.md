# WORLD_AS_OF support in courier: implementation design

## Problem

WORLD_AS_OF is an office-wide environment variable carrying an ISO-8601 timestamp with timezone. When set, courier may not let anything dated after that instant leave the tool, so a benchmark session replayed against the mailbox yields the same answers later. Unset means normal operation at zero cost. A set-but-unparseable value is a hard failure at startup, since a silently ignored bound produces a contaminated run that looks valid.

Web tools are out of scope. Courier is in scope, including any data it pre-computes and serves into a model's opening context.

## The bound's semantics for a mailbox

An IMAP mailbox splits cleanly into an append-only part and a mutable part, and the design follows that split.

Append-only: message content. Within one UIDVALIDITY epoch, a UID's RFC 822 bytes never change, and every message carries an INTERNALDATE (server receipt time) and usually a Date header (sender-claimed). A date bound over message existence and content is therefore exact, with one caveat: a message that existed at the bound instant but was expunged since is gone, and no IMAP query can resurrect it.

Mutable: everything else. Flags (\Seen, \Flagged), folder membership (moves relabel), the folder list itself, and the Drafts folder are current-state only; IMAP keeps no history for any of them. These cannot be rewound, only served as they now stand and flagged as such.

The bound compares against INTERNALDATE, not the Date header. INTERNALDATE is when the message entered this mailbox, which is when it entered the world the session could see; Date is sender-supplied, forgeable, and often skewed. Where INTERNALDATE is unavailable (mu cache results), the indexed date field substitutes, flagged in provenance.

## Honest rule (the boundary of exactness)

1. Message existence and content: exact. A message whose INTERNALDATE is after the bound is dropped from search results and refused on direct read, with a message naming WORLD_AS_OF as the reason.
2. Flags, folder membership, folder list: current state accepted, flagged. Provenance gains a `world_as_of` block whose `current_state_fields` names them (`["flags", "folder", "folders"]` as applicable).
3. Expunged-since-cutoff messages: silently absent, undetectable. Documented as a known limit; not pretended away.
4. Live watching (`watch` / IMAP IDLE): inherently future-facing; refused outright when the bound is set.

## Parsing and the single source of truth

One function, one home: `world_as_of() -> Optional[datetime]` in a small module `courier/world_bound.py` (a function and a couple of predicates; no class is warranted, since the state is one Optional[datetime] and the behaviours are pure functions over it). It reads `os.environ["WORLD_AS_OF"]`, parses with `datetime.fromisoformat`, and raises `WorldAsOfInvalid` (a `CourierError` subclass) when the value is unparseable or lacks a timezone offset. Naive timestamps are rejected: accepting one would silently bind against an assumed zone, a cousin of the silent fallback the spec forbids.

Hard-failure points, both at process start so no partial output ever escapes:

- CLI: `_global_options` in `__main__.py` (and `_apply_global_flags` for chain mode) call `world_as_of()` before any verb runs; on `WorldAsOfInvalid`, print the error and exit 1.
- MCP server: `mcp_server.py` calls it during lifespan startup; the server refuses to start.

The parsed value is computed once and threaded to `ImapClient` at construction (a `world_as_of` attribute on the client), not re-read from the environment at each call site.

## Enforcement architecture: one choke point

Every fetch surface, MCP or CLI, funnels through `ImapClient` (`search_emails`, `search`, `fetch_email`, `fetch_emails`, `fetch_raw`, `fetch_thread`, `list_folders`). `resources.py` calls `imap_client.search`/`fetch_emails` directly, bypassing `tools.py`, so enforcement in the tool wrappers would leak; it belongs in `ImapClient` and `local_cache.py`. Enforcement is two-layer:

**Layer 1, server-side prefilter (coarse).** IMAP `SEARCH BEFORE <date>` filters on INTERNALDATE at day granularity in the server's idea of the day. Every search the client issues gains `BEFORE <bound_date + 1 day>`: over-inclusive by up to a day plus timezone slack, never under-inclusive. This keeps result sets small; it is not the correctness layer. On the Gmail `X-GM-RAW` path, `before:<epoch_seconds>` gives second precision and is appended there too.

**Layer 2, post-filter (exact).** Search result assembly and every fetch path FETCH the INTERNALDATE and drop or refuse messages whose INTERNALDATE is after the bound. Direct reads (`read`, `attachments`, `save`, `export`, `links`, `fetch_thread` members, `copy` source, `reply`'s original, `triage`'s fetch) return a refusal naming the bound: "message dated 2026-07-13T09:12:00+10:00 is after WORLD_AS_OF 2026-07-12T17:07:00+10:00; refused". `reply` and `accept-invite` are covered transitively because they fetch the original through the same path.

**Ordering with `limit`.** The bound applies before the limit cut, otherwise a limit-truncated page could be all-future and return an artificially empty result. Search already sorts UIDs descending; the post-filter runs on the fetched envelope dates, then the limit is applied.

## Per-surface inventory

MCP tools (`tools.py`) and their CLI twins (`__main__.py`); enforcement column names which layer does the work.

| Surface | Backing store | Enforcement |
|---|---|---|
| `search` (tool, CLI, resource `email://search/{query}`) | append-only (content) over mutable flags | Layer 1 SEARCH BEFORE + Layer 2 post-filter; flag fields marked current-state |
| `read`, `attachments`, `save`, `export`, `links` | append-only content | Layer 2 refusal on INTERNALDATE > bound |
| `folders` (tool, CLI, resource `email://folders`) | mutable, no history | current state accepted, flagged in output |
| resource `email://{folder}/list` | as search | same as search (it is `search("ALL")` + fetch) |
| resource `email://{folder}/{uid}` | append-only content | Layer 2 refusal |
| `fetch_thread` (reply threading) | append-only | Layer 2: thread members after the bound are dropped |
| `copy` (cross-account import) | reads source message | Layer 2 on the source fetch |
| `triage`, `reply`, `accept-invite` | read-then-act | covered by the fetch they start with |
| `watch` (CLI, IDLE) | future events | refused when bound is set |
| local-cache search (`local_cache.py`, mu/Xapian) | mirror of maildir; index mtime known | mu query gains `date:..<bound>` (Layer 1); Layer 2 post-filter on the result's date field; provenance notes the date source is the index, not INTERNALDATE |
| `compose`, `send-draft`, `move`, `mark-*`, `flag`, `trash`, `delete` | writes | out of the read-bound's scope; they emit no dated data. Whether a replay harness should permit mutations at all is the harness's policy, not this variable's |
| `status`, `config-check`, `list`, `config-sample` | config/introspection, no mail data | unbounded by nature; no change |

## Relative dates and "now"

The query translator (`courier/query/`, historically `query_parser.py`) resolves `today`, `week`, `newer:3d`, etc. against a reference instant. Under a bound, "now" is the bound instant: a replayed session asking `newer:7d` means seven days before WORLD_AS_OF. The emitters take the injected reference instant; `ImapClient` passes the bound when set. Without this, relative queries drift as real time advances, which defeats the reproducibility goal even with the post-filter in place (the filter would keep results correct but the window would shrink to empty).

## The pre-filled-prompt path

Courier's pre-computed-context surfaces are the MCP resources in `resources.py` (`email://folders`, `email://{folder}/list`, `email://search/{query}`, `email://{folder}/{uid}`), which MCP hosts may prefetch into a model's opening context. They call `ImapClient` directly, so the choke-point placement bounds them with no separate mechanism. The generated Claude slash-command doc (`_claude_command.py`, `install-claude-command`) contains instructions only, no live mail data, and needs no bounding. No other opening-prompt injection exists in this repository; if the office's courier skill pre-parses mail outside this repo, that is a separate change in that repo, noted here so the later session checks.

## Provenance

The search result's existing `provenance` dict gains, when the bound is set:

```json
"world_as_of": {
  "bound": "2026-07-12T17:07:00+10:00",
  "dropped_after_bound": 3,
  "current_state_fields": ["flags", "folder"],
  "date_source": "internaldate"   // or "mu_index"
}
```

`dropped_after_bound` makes the filtering auditable rather than invisible; a benchmark harness can assert it. Refusal messages on direct reads carry the same bound string.

## Feasibility verdict

Feasible, and cleanly, because the architecture already funnels every fetch through `ImapClient` and the local-cache backend, and search results already carry a provenance dict to extend. Effort: **M**. Roughly: `world_bound.py` + startup wiring (S), `ImapClient` two-layer enforcement across search/fetch/thread (the bulk), query-translator reference-instant injection (S), mu cache clause (S), watch refusal (XS), tests throughout.

Sharpest risks:

1. **IMAP date coarseness.** SEARCH BEFORE is day-granular and evaluated in the server's timezone; treating it as exact would leak same-day future messages. The design never trusts Layer 1 for correctness; the post-filter on FETCHed INTERNALDATE is the invariant. A test that plants two same-day messages straddling the bound instant guards this.
2. **Bypass surfaces.** `resources.py` calling `imap_client.search` directly is exactly the path a tools-layer implementation would miss. Any future fetch path added to `ImapClient` inherits the bound only if enforcement sits inside the shared fetch/search assembly, not sprinkled at call sites. The staged tests include one against the resource functions specifically.
3. **INTERNALDATE vs Date divergence.** A message sent long ago but imported/copied recently has a recent INTERNALDATE; a message with a forged future Date header has a sane INTERNALDATE. Binding on INTERNALDATE is the defensible choice (it is when the mailbox saw it) but produces occasional surprises against user intuition; the provenance block and refusal message name the date used.
4. **Relative-date drift.** Missing the query translator's "now" injection leaves a silent reproducibility hole that the post-filter masks as shrinking result windows. Its test asserts `newer:7d` under a bound resolves against the bound.
5. **mu cache date field.** `mu find` filters on the indexed date (from the Date header, not INTERNALDATE), a semantic mismatch with the IMAP layers. Either accept it flagged (`date_source: "mu_index"`) or fall back to IMAP when the bound is set; the flagged-accept keeps the cache useful and is the recommended default, recorded as a decision for the user to overturn.

## Staging for the implementing session

Each stage is a coherent commit; tests precede implementation per this repo's TDD convention.

1. **Bound parsing.** `tests/test_world_bound.py`: unset → None; valid ISO with offset → aware datetime; garbage, and naive timestamp → `WorldAsOfInvalid`. Then `courier/world_bound.py` and the `errors.py` subclass.
2. **Startup wiring.** CLI `_global_options`/`_apply_global_flags` and MCP lifespan call the parser; tests spawn the CLI with a bad `WORLD_AS_OF` and assert exit 1 with the message, and a good one proceeding.
3. **Fetch enforcement.** Tests with a mocked IMAP connection: `fetch_email` on a future-dated UID refuses; `search_emails` drops future results before the limit; `fetch_thread` drops future members; provenance block present and counting. Then the `ImapClient` changes (bound attribute, SEARCH BEFORE clause, INTERNALDATE post-filter shared by fetch paths).
4. **Query-parser reference instant.** Tests for `today`/`newer:Nd` under an injected instant; then the injection parameter and its threading from `ImapClient`.
5. **Local cache.** Tests for the generated mu query containing `date:..`; post-filter; `date_source` flag. Then `local_cache.py`.
6. **Watch refusal + resource coverage.** `watch` under a bound raises with the WORLD_AS_OF message; a resource-function test proves the bypass path is covered.
7. **Docs.** A WORLD_AS_OF section in `docs/CONFIGURATION.md` stating the three semantics, the honest rule for mutable state, and the INTERNALDATE choice.

Resume by reading this file and `courier/imap_client.py`'s search/fetch region first; the choke-point claim in "Enforcement architecture" is the load point to re-verify against the then-current code before writing stage 3.
