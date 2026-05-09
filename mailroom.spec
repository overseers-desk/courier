Name:           mailroom
Version:        1.1.8
Release:        1%{?dist}
Summary:        Email toolkit for AI assistants and command-line scripting
License:        MIT
URL:            https://github.com/SmartLayer/mailroom
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
Mailroom provides CLI commands for searching, reading, moving, flagging,
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
" dist/mailroom-*.whl "${SITE_DIR}"

# Create entry-point script
cat > %{buildroot}/usr/bin/mailroom << 'ENTRY'
#!/usr/bin/python3
from mailroom.__main__ import main
main()
ENTRY
chmod 755 %{buildroot}/usr/bin/mailroom

# Install man page
install -Dpm 644 debian/mailroom.1 %{buildroot}%{_mandir}/man1/mailroom.1

%files
%license LICENSE
%doc README.md
/usr/bin/mailroom
/usr/lib/python*/site-packages/mailroom/
/usr/lib/python*/site-packages/mailroom-*.dist-info/
%{_mandir}/man1/mailroom.1*

%changelog
* Sun May 10 2026 Weiwu Zhang <a@colourful.land> - 1.1.8-1
- `-i` is no longer a shorthand for `--imap`. Use `--imap NAME` at the
  top level. `-i` is now exclusively `save --identifier` (closes #36):
  previously `mailroom -i acct save -f INBOX -u 100 -i "Billete.pdf"
  -o out.pdf` failed because the argv preprocessor consumed
  `save -i "Billete.pdf"` as a second `--imap`. Removing the shorthand
  removes the collision at the source. Migration:
  `mailroom -i NAME ...` → `mailroom --imap NAME ...`.
- `-n/--limit` and `-f/--folder` are now chain-level: a trailing
  `-n N` or `-f FOLDER` after the last verb applies to every chained
  verb that doesn't set its own value (closes #34).
- MCP tool coverage: `search_emails`, `read`, and `folders` are
  consistently exposed; thread matching uses strict-subject semantics
  (closes #11).
- `install-claude-command` is now version-aware. It checks the
  installed `mailroom.md` against the bundled one and prompts before
  overwriting; the `--force` flag is removed. The status nudge
  surfaces globally when registration is missing.

* Wed May 06 2026 Weiwu Zhang <a@colourful.land> - 1.1.7-1
- `search` and `read` repeat at the top level to run several operations
  in one invocation:
      mailroom -A search "sergio" search "panedas" read -f INBOX -u 42
  Each operation gets its own outer key in the result, so per-keyword
  hit attribution is preserved. Output is JSON of shape
  `{op_key: {imap_name: result}}`, the same shape single-op `search`
  and `read` already produced.
- The `batch` subcommand is removed. Anyone driving mailroom from a
  script that ran `mailroom batch "search foo" "search bar"` should
  switch to `mailroom search foo search bar`. The output shape is
  unchanged. Issue #34 tracks promoting `--limit` and `--folder` to
  chain-level so they apply to every chained verb.

* Wed May 06 2026 Weiwu Zhang <a@colourful.land> - 1.1.6-1
- New `install-claude-command` subcommand: copies the bundled Claude
  Code command file into ~/.claude/commands/mailroom.md so Claude Code
  recognises mailroom and routes email-related prompts through the CLI.
  The file ships inside the Python package, so the same one-liner works
  regardless of install method. `mailroom status` notes when ~/.claude
  exists but mailroom is not yet registered.
- Plain-text bodies containing markdown tables or headings now auto-
  render an HTML alternative on send. The wire form goes as
  multipart/alternative with the original plain text first and rendered
  HTML second, so recipients see structure while plain-text-only clients
  still get the intended source. Closes #28.

* Wed May 06 2026 Weiwu Zhang <a@colourful.land> - 1.1.5-1
- `mailroom status` is now a connection-probe table rather than a
  JSON inventory. Each [imap.NAME] runs a full IMAP login; each
  [smtp.NAME] runs EHLO + STARTTLS plus an authenticated login when
  credentials are configured. Output is a short aligned table
  answering "which servers are reachable right now". Template SMTP
  blocks (no own creds) stop at STARTTLS and are marked. Scripts
  that parsed status JSON should switch to `mailroom list`. Closes #29.

* Tue May 06 2026 Weiwu Zhang <a@colourful.land> - 1.1.4-1
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
  surface on `mailroom`, `--help`, `status`, and `list-accounts`

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
