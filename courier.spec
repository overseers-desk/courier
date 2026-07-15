Name:           courier
Version:        1.1.18
Release:        1%{?dist}
Summary:        Email toolkit for AI assistants and command-line scripting
License:        MIT
URL:            https://github.com/overseers-desk/courier
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz#/%{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  python3-setuptools
BuildRequires:  python3-pip
BuildRequires:  python3-wheel

Requires:       python3 >= 3.11
Requires:       python3-imapclient >= 3.0.0
Requires:       python3-markdown-it-py >= 3.0.0
Requires:       python3-typer >= 0.15.0
Requires:       python3-requests >= 2.32.0
Requires:       python3-dotenv >= 1.0.0
Requires:       python3-sievelib >= 1.5

%description
Courier provides CLI commands for searching, reading, moving, flagging,
and replying to emails over IMAP. It also offers an MCP (Model Context
Protocol) server mode for integration with AI assistants.

The RPM package provides all CLI commands. The MCP server subcommand
requires the python3-mcp package; users who need MCP mode should install
via pipx or Homebrew instead.

%prep
%autosetup -n %{name}-%{version}

%build
python3 -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .

%install
# Unpack wheel directly to work around Debian sysconfig patches.
# On Fedora, replace this block with: %%pyproject_install
PYTHON_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
SITE_DIR=%{buildroot}/usr/lib/python${PYTHON_VER}/site-packages
mkdir -p "${SITE_DIR}" %{buildroot}/usr/bin
python3 -c "
import zipfile, sys
with zipfile.ZipFile(sys.argv[1]) as whl:
    whl.extractall(sys.argv[2])
" dist/courier-*.whl "${SITE_DIR}"

# Create entry-point script
cat > %{buildroot}/usr/bin/courier << 'ENTRY'
#!/usr/bin/python3
from courier.__main__ import main
main()
ENTRY
chmod 755 %{buildroot}/usr/bin/courier

# Install man page
install -Dpm 644 debian/courier.1 %{buildroot}%{_mandir}/man1/courier.1

%files
%license LICENSE
%doc README.md
/usr/bin/courier
/usr/lib/python*/site-packages/courier/
/usr/lib/python*/site-packages/courier-*.dist-info/
%{_mandir}/man1/courier.1*

%changelog
* Wed Jul 15 2026 Weiwu Zhang <a@colourful.land> - 1.1.18-1
- Gmail-style search translates faithfully to plain IMAP and the local cache: parentheses group, multi-term OR nests, and operators a backend cannot honour refuse with a clear message instead of silently searching for the literal text and returning nothing.
- Search failures enter the result: a folder that errors or times out is listed in folders_failed, a dead connection reports an error instead of an empty mailbox, and exit codes separate hits, a genuine empty, and a failure.
- Non-ASCII search terms are sent with a UTF-8 charset.
- allowed_folders is enforced on the local-cache and disk read paths.
- A redact policy is honoured wherever a message is copied, exported, or sent.
- SMTP reports recipients the server refuses, uses CRLF line endings, and sets a connection timeout.
- mark/move/delete/trash verify the UIDs and report which matched and which were not found.
- courier watch keeps folder events across its IDLE reissue and backs off correctly on a flaky connection.

* Sun Jul 12 2026 Weiwu Zhang <a@colourful.land> - 1.1.17-1
- msgid: search operator finds a message by RFC 5322 Message-ID on IMAP, the local cache, and Gmail (alias rfc822msgid:).
- search --help and the MCP tool description now list the full operator inventory, rendered from the parser.
- courier watch: new verb streaming IMAP IDLE folder events as JSON.
- Typed CLI exit codes for data-returning commands.
- Sending and FCC filing available as library API.

* Mon Jun 30 2026 Weiwu Zhang <a@colourful.land> - 1.1.16-1
- Project renamed from mailroom to courier; package name on PyPI is now courier.
- Install via pip: pip install courier or pip install "courier[mcp]".

* Thu Jun 25 2026 Weiwu Zhang <a@colourful.land> - 1.1.15-1
- compose --to is now optional, enabling Bcc-only sends.
- free-form --smtp send refuses when no copy can be filed.
- slash-command: non-ASCII body and subject sent verbatim; failure-mode D4 added.
- slash-command: description routes by trigger; routing logic reformatted.

* Sun May 31 2026 Weiwu Zhang <a@colourful.land> - 1.1.14-1
- reply defaults to reply-all; --no-thread flag replaces --reply-all opt-in.

* Mon May 25 2026 Weiwu Zhang <a@colourful.land> - 1.1.13-1
- install-claude-command no longer emits the unregistered/dated nudge when
  it is the subcommand being run; nudge is suppressed for that subcommand.
- Nudge messages phrased as agent directives for stderr-as-prompt-injection.

* Fri May 22 2026 Weiwu Zhang <a@colourful.land> - 1.1.12-1
- Folder-scoped search is now served from the local mu cache under the same
  eligibility check as whole-mailbox search, instead of always going to IMAP.
- read, links, and attachments are served from the local cache under that
  same gate, so a fresh index answers them without contacting IMAP and a
  stale index sends them to live IMAP.
- New --no-cache flag on search and read forces a live IMAP query for a
  single call.
- Messages in folders whose names contain glob characters such as
  [Gmail]/Sent Mail are now read from the local cache correctly.

* Wed May 20 2026 Weiwu Zhang <a@colourful.land> - 1.1.11-1
- Reply now honours Reply-To over From per RFC 5322 §3.6.2. Reply-all with
  Reply-To moves the diverted From to Cc. Fixes replies looping back into
  the sender's mailbox when messages arrive via forwarders or alias services.
- Folder-search warnings now carry the IMAP block label (e.g.
  "[imap.work] Error searching folder Inbox: ...") for easier multi-account
  diagnosis.
- Default search limit raised from 10 to 50 in the CLI, chain parser, and
  MCP search tool. The /Searches slash-command example preserves the
  {op_key: {imap_name: {results, provenance}}} shape so each UID stays
  bound to the correct account.

* Wed May 13 2026 Weiwu Zhang <a@colourful.land> - 1.1.10-1
- Local cache now serves `read`, `links`, and `attachments` from disk for
  any [imap.NAME] with `maildir` configured; previously only `search`
  consulted the cache and the other paths round-tripped to IMAP
  unconditionally.
- The redact policy no longer disables the local cache. Compound queries
  against a redact-bearing block serve from cache in ~5 s instead of
  ~16 s, and the maildir path is dropped from redacted records to close
  that leakage.
- Warnings from `courier.imap_client` are routed through syslog. On
  systemd hosts the records are queryable via `journalctl -t courier`;
  platforms with no reachable syslog socket fall back to stderr.
  Closes #39.
- When `search` falls through to iterating every selectable folder, a
  WARNING now names the host, the size of the cached folder list, and
  the universe of LIST attribute flags seen, so intermittent
  INBOX-only result sets can be attributed from journald without a
  live reproduction. Refs #38.
- Documentation: sync-tool examples are no longer co-equal "offlineimap
  or mbsync"; offlineimap is named as one example of a maildir-producing
  tool. Fixes #37.

* Sun May 10 2026 Weiwu Zhang <a@colourful.land> - 1.1.9-1
- `-i` is now `--identity`, used by `compose`, `reply`, and `send-draft`.
  The 1.1.8 assignment of `-i` to `save --identifier` did not match the
  user-facing intent and is reverted.
- `save --identifier` is renamed to `save --attachment` and has no short
  form. "identifier" was ambiguous against the project's other
  identifier-shaped fields (identity name, imap block name, smtp block
  name, message-id, uid). `--attachment` says exactly what it picks.
  Migration: `courier save -f INBOX -u 100 -i Billete.pdf -o out.pdf`
  → `courier save -f INBOX -u 100 --attachment Billete.pdf -o out.pdf`.
- `--imap` remains the only spelling for the global IMAP-block selector;
  no short form.

* Sun May 10 2026 Weiwu Zhang <a@colourful.land> - 1.1.8-1
- `-i` is no longer a shorthand for `--imap`. Use `--imap NAME` at the
  top level. `-i` is now exclusively `save --identifier` (closes #36):
  previously `courier -i acct save -f INBOX -u 100 -i "Billete.pdf"
  -o out.pdf` failed because the argv preprocessor consumed
  `save -i "Billete.pdf"` as a second `--imap`. Removing the shorthand
  removes the collision at the source. Migration:
  `courier -i NAME ...` → `courier --imap NAME ...`.
- `-n/--limit` and `-f/--folder` are now chain-level: a trailing
  `-n N` or `-f FOLDER` after the last verb applies to every chained
  verb that doesn't set its own value (closes #34).
- MCP tool coverage: `search_emails`, `read`, and `folders` are
  consistently exposed; thread matching uses strict-subject semantics
  (closes #11).
- `install-claude-command` is now version-aware. It checks the
  installed `courier.md` against the bundled one and prompts before
  overwriting; the `--force` flag is removed. The status nudge
  surfaces globally when registration is missing.

* Wed May 06 2026 Weiwu Zhang <a@colourful.land> - 1.1.7-1
- `search` and `read` repeat at the top level to run several operations
  in one invocation:
      courier -A search "sergio" search "panedas" read -f INBOX -u 42
  Each operation gets its own outer key in the result, so per-keyword
  hit attribution is preserved. Output is JSON of shape
  `{op_key: {imap_name: result}}`, the same shape single-op `search`
  and `read` already produced.
- The `batch` subcommand is removed. Anyone driving courier from a
  script that ran `courier batch "search foo" "search bar"` should
  switch to `courier search foo search bar`. The output shape is
  unchanged. Issue #34 tracks promoting `--limit` and `--folder` to
  chain-level so they apply to every chained verb.

* Wed May 06 2026 Weiwu Zhang <a@colourful.land> - 1.1.6-1
- New `install-claude-command` subcommand: copies the bundled Claude
  Code command file into ~/.claude/commands/courier.md so Claude Code
  recognises courier and routes email-related prompts through the CLI.
  The file ships inside the Python package, so the same one-liner works
  regardless of install method. `courier status` notes when ~/.claude
  exists but courier is not yet registered.
- Plain-text bodies containing markdown tables or headings now auto-
  render an HTML alternative on send. The wire form goes as
  multipart/alternative with the original plain text first and rendered
  HTML second, so recipients see structure while plain-text-only clients
  still get the intended source. Closes #28.

* Wed May 06 2026 Weiwu Zhang <a@colourful.land> - 1.1.5-1
- `courier status` is now a connection-probe table rather than a
  JSON inventory. Each [imap.NAME] runs a full IMAP login; each
  [smtp.NAME] runs EHLO + STARTTLS plus an authenticated login when
  credentials are configured. Output is a short aligned table
  answering "which servers are reachable right now". Template SMTP
  blocks (no own creds) stop at STARTTLS and are marked. Scripts
  that parsed status JSON should switch to `courier list`. Closes #29.

* Wed May 06 2026 Weiwu Zhang <a@colourful.land> - 1.1.4-1
- New per-account redact policy. An optional `redact = "rules.sieve"`
  field on [imap.NAME] points at a Sieve script; messages matching any
  rule are replaced with a placeholder Email before reaching the agent
  or the third-party model provider. UID, date, folder, and threading
  headers survive so the agent knows the message exists; subject, from,
  to, cc, and body are blanked.
- Two motivating cases: (1) keep privileged correspondence (legal
  counsel, HR, medical) out of the agent's view and out of model-
  provider logs while the containing mailbox stays connected; (2)
  restrict the agent to a defined scope inside a shared mailbox, e.g.
  hide personal mail from a work-context agent.
- Sieve subset: address/header tests with :is/:contains/:matches;
  anyof/allof/not; one custom action `redact;`. Out-of-subset
  constructs fail closed at config-load time.
- New runtime dependency on python3-sievelib.

* Mon May 04 2026 Weiwu Zhang <a@colourful.land> - 1.1.3-1
- Refuse to send when no copy of the message will be retained: send
  succeeds only if FCC will run or the BCC list includes the sender's
  own address. Pass --allow-no-copy to override.
- New `bcc` field on [identity.NAME] (string or list); auto-applied on
  every send from this identity. With `bcc` set, the `imap` field
  becomes optional, enabling send-only identities that self-archive
  via BCC.
- Homebrew formula on macOS Tahoe now depends on `expat` and sets
  DYLD_LIBRARY_PATH on the wrapper so pyexpat resolves correctly (#26).

* Sat May 02 2026 Weiwu Zhang <a@colourful.land> - 1.1.1-1
- SMTP send capability: --send flag on compose and reply transmits via SMTP
  instead of saving to drafts; new send-draft subcommand reads a draft from
  IMAP and transmits it
- New [smtp.NAME] config blocks declare named SMTP endpoints; per-account
  default_smtp routes outgoing mail; [[identities]] tables enable send-as
  with one mailbox handling multiple From addresses
- Captures the post-DATA SMTP response and rewrites Message-ID to the
  recipient-visible form when the smarthost issues one (e.g. SES)
- After a successful send, FCCs (IMAP-APPENDs) the wire-form bytes to the
  identity's Sent folder, with Bcc stripped and Message-ID rewritten
- New config-check subcommand validates cross-references and identity
  resolution without performing IMAP or SMTP traffic; the same warnings
  surface on `courier`, `--help`, `status`, and `list-accounts`

* Tue Apr 28 2026 Weiwu Zhang <a@colourful.land> - 1.1.0-1
- Batch-first JSON output: all commands now wrap results under an operation
  key {"search from:x": {"account": {...}}} — breaking change for 1.0.x
  consumers that parsed the account name as the top-level key
- New `batch` subcommand: accepts multiple operation strings (as args,
  --file, or stdin JSON array) and executes all ops per account over a
  single IMAP connection, eliminating per-query reconnect overhead
- `read` output now uses the same batch JSON shape as `search`
- `status` and `mcp --version` now derive version from __version__ instead
  of a hardcoded string

* Sun Apr 26 2026 Weiwu Zhang <a@colourful.land> - 1.0.3-1
- search: optional local-cache backend via mu (Xapian); when a [local_cache]
  block is configured and an account names a maildir, search serves from
  `mu find` over a subprocess instead of IMAP with transparent IMAP fallback
- Search response now wraps {"results", "provenance"}; provenance reports
  source, indexed_at, and any fall-back reason (breaking change for
  consumers that indexed the bare result list)
- Route Gmail header queries (from:/to:/cc:/bcc:) through X-GM-RAW so
  Gmail's All Mail filters correctly for values containing "@"/"."
- search: --format text and --format oneline output; --format json (the
  default) is unchanged
- search: multi-account support via --account/-a (repeatable) and
  --all-accounts/-A; output is nested by account name
- search: skip \\Noselect / \\NonExistent folders; prefer SPECIAL-USE \\All
- search: soft-redirect search-variant subcommand names; --account
  accepted before or after the subcommand; --query/-q alias
- Exit code 1 on zero results for search and attachments
- Top-level --version flag
- search/read: surface message_id, in_reply_to, and references; IMAP remote
  search now emits message_id per result (parity with local-cache path);
  read emits message_id always, in_reply_to/references when non-empty;
  --format text appends an "id:" line; --format oneline appends message_id
  as a trailing tab column

* Mon Apr 06 2026 Weiwu Zhang <a@colourful.land> - 1.0.1-1
- Rename CLI commands to aerc-aligned short verbs (search, move, reply, etc.)
- Rename MCP tools to kebab-case (search, move, mark-read, etc.)
- Add read command to view email content
- Add folders command to list email folders
- Normalize all commands to use --folder/-f and --uid/-u named flags
- Rename import-email to copy (JMAP alignment)
- Rename process-email to triage, download-attachment to save, etc.

* Fri Apr 03 2026 Weiwu Zhang <a@colourful.land> - 1.0.0-1
- Initial package
