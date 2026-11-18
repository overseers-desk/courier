"""Optional local-cache search backend via mu (Xapian).

Subprocess-driven: shells out to ``mu find --format=json`` against a
user-indexed maildir.  This module provides eligibility checks and
search execution; integration into ``ImapClient.search_emails`` and the
CLI is handled by the caller.

The contract is "a maildir exists and mu indexes it".  This module does
not invoke ``mu index``; it does not read external sync-tool state (e.g. offlineimap's);
and it does not model any sync stack.  It reports the index mtime and
judges nothing by it: the caller reads the age off
``provenance.indexed_at`` and decides what its own question tolerates.
When a check fails, eligibility returns ``False`` and the caller is
expected to fall back to IMAP.
"""

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from . import world_bound
from .config import ImapBlock, LocalCacheConfig
from .imap_client import ImapClient
from .query.ast import TranslationReport, UntranslatableForBackend
from .query.emit_mu import emit as emit_mu
from .query.grammar import ParseResult

# Backwards-compatible name: the cache-fallback catch and external
# callers knew the old translator's exception by this name.
UntranslatableQuery = UntranslatableForBackend

_UID_FROM_FILENAME = re.compile(r",U=(\d+)[,:]")

logger = logging.getLogger(__name__)


def _files_gone_notes(from_index: int, left_out: int) -> List[str]:
    """The notes for hits whose file was not on disk when the row was built.

    Args:
        from_index: Rows built from mu's own fields instead of the file.
        left_out: Hits dropped because a redact policy needed the file.

    Returns:
        One note per nonzero count, each naming how the answer differs
        from the server's and the ``--no-cache`` retry.
    """
    notes: List[str] = []
    if from_index == 1:
        notes.append(
            "1 row comes from the index alone because its file was moved or "
            "removed after indexing, so its sender, recipients, subject and "
            "date may be written differently from the server's; --no-cache "
            "asks the server"
        )
    elif from_index > 1:
        notes.append(
            f"{from_index} rows come from the index alone because their files "
            "were moved or removed after indexing, so their senders, "
            "recipients, subjects and dates may be written differently from "
            "the server's; --no-cache asks the server"
        )
    if left_out == 1:
        notes.append(
            "1 matching message is left out because its file was moved or "
            "removed after indexing and the redact policy cannot be checked "
            "without it; --no-cache asks the server"
        )
    elif left_out > 1:
        notes.append(
            f"{left_out} matching messages are left out because their files "
            "were moved or removed after indexing and the redact policy cannot "
            "be checked without them; --no-cache asks the server"
        )
    return notes


class MuFailure(Exception):
    """Raised when invoking mu fails for a non-eligibility reason.

    Eligibility failures (mu missing, db missing) are
    reported via :class:`EligibilityResult`; this exception is reserved
    for runtime failures (timeout, non-zero exit, malformed output, an
    unservable maildir scope) that should trigger an IMAP fallback at
    the caller.

    Attributes:
        fell_back_reason: Optional tag from the
            ``provenance.fell_back_reason`` vocabulary naming the
            fallback condition more precisely than the caller's generic
            ``"exception"``; ``None`` when no specific tag applies.
    """

    def __init__(self, message: str, *, fell_back_reason: Optional[str] = None):
        super().__init__(message)
        self.fell_back_reason = fell_back_reason


@dataclass
class EligibilityResult:
    """Outcome of a backend eligibility check.

    Attributes:
        eligible: Whether the local cache should be used for this call.
        reason: When ``eligible`` is ``False``, a short tag matching the
            ``provenance.fell_back_reason`` vocabulary used in search
            responses (``"mu_missing"``, ``"db_missing"``).
            A block carrying a redact policy stays eligible: the policy
            is applied against the on-disk maildir file at format time,
            not by forcing an IMAP round-trip.
    """

    eligible: bool
    reason: Optional[str] = None


class MuSearch(NamedTuple):
    """One local-cache search, before envelope assembly.

    Named fields only, no behaviour, the same standard as the remote
    search's result in ``imap_client``: a flag and counts cross this
    boundary, and a bare tuple would transpose them silently.

    Attributes:
        results: Result rows, newest first, at most ``limit`` of them.
        report: The mu emission's translation report, with the notes
            this search added.
        truncated: Whether the index holds more matches than ``limit``.
        total_count: The match count before the limit cut and after
            the WORLD_AS_OF cut; ``None`` only when the page was cut and
            counting failed.
        dropped_after_bound: Hits the WORLD_AS_OF cut took out, counted
            before the limit cut.
    """

    results: List[Dict[str, Any]]
    report: TranslationReport
    truncated: bool
    total_count: Optional[int]
    dropped_after_bound: int = 0


class MuBackend:
    """Local-cache search backend driven by ``mu find`` subprocess calls.

    A single instance is shared across [imap.*] blocks; per-block
    scoping is applied at search time via the maildir predicate, not at
    initialisation.  The resolved muhome is cached in process memory.
    """

    def __init__(self, cfg: LocalCacheConfig) -> None:
        """Initialise with the global local-cache configuration.

        Args:
            cfg: Configuration block carrying ``indexer`` and an
                optional ``mu_index`` override.  When ``mu_index`` is
                unset, the muhome is discovered lazily from ``mu info
                store`` on first use.
        """
        self.cfg = cfg
        self._muhome: Optional[str] = cfg.mu_index
        self._muhome_resolved: bool = cfg.mu_index is not None
        self._mu_root: Optional[str] = None
        self._mu_root_resolved: bool = False

    @property
    def muhome(self) -> Optional[str]:
        """Return the resolved muhome path, discovering it if necessary.

        Returns:
            The muhome (the directory passed to ``mu --muhome=…``), or
            ``None`` if mu is missing or the path could not be parsed.
        """
        if self._muhome_resolved:
            return self._muhome
        try:
            self._muhome = self._discover_muhome()
        except MuFailure as e:
            logger.warning(f"Could not discover mu muhome: {e}")
            self._muhome = None
        self._muhome_resolved = True
        return self._muhome

    def _discover_muhome(self) -> str:
        """Run ``mu info store`` to learn the muhome.

        Also records the store's maildir root when the output carries
        it, saving :meth:`_store_maildir_root` a second invocation.

        Returns:
            The muhome (the parent directory of the xapian database).

        Raises:
            MuFailure: When mu is missing or the output cannot be parsed.
        """
        if shutil.which("mu") is None:
            raise MuFailure("mu binary not on PATH")
        try:
            proc = subprocess.run(
                ["mu", "info", "store"],
                capture_output=True,
                text=True,
                errors="surrogateescape",
                timeout=10,
                check=True,
            )
        except OSError as e:
            raise MuFailure(f"mu info store could not run: {e}") from e
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise MuFailure(f"mu info store failed: {e}") from e
        root_match = re.search(r"\bmaildir\s*\|\s*(\S+)", proc.stdout)
        if root_match:
            self._mu_root = root_match.group(1)
            self._mu_root_resolved = True
        match = re.search(r"database-path\s*\|\s*(\S+)", proc.stdout)
        if not match:
            raise MuFailure("could not parse database-path from mu info store output")
        db_path = match.group(1)
        return os.path.dirname(db_path)

    def _store_maildir_root(self) -> Optional[str]:
        """Return the maildir root of the mu store, discovering it lazily.

        Reads the ``maildir`` row from ``mu info store`` (the same
        output muhome discovery parses). The result — including a
        miss — is cached for the backend's lifetime.

        Returns:
            The store's root maildir path, or ``None`` when it cannot
            be determined (mu invocation failed, or the output carries
            no ``maildir`` row). Callers fall back to the historical
            assumption that the block's maildir sits directly under
            the store root.
        """
        if self._mu_root_resolved:
            return self._mu_root
        home = self.muhome  # may resolve the root as a side effect
        if self._mu_root_resolved:
            return self._mu_root
        self._mu_root_resolved = True
        argv = ["mu", "info", "store"]
        if home:
            argv.append(f"--muhome={home}")
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                errors="surrogateescape",
                timeout=10,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.warning(f"Could not read the mu store's maildir root: {e}")
            return None
        match = re.search(r"\bmaildir\s*\|\s*(\S+)", proc.stdout)
        if not match:
            logger.warning("could not parse the maildir row from mu info store output")
            return None
        self._mu_root = match.group(1)
        return self._mu_root

    def _scope_prefix(self, imap_block: ImapBlock) -> str:
        """Derive the block's maildir scope prefix from the store root.

        mu reports each message's ``:maildir`` relative to the store
        root, so the scope prefix depends on where the block's maildir
        sits inside that root — not on its basename. ``mu init
        --maildir=<block maildir>`` (the layout the docs' own setup
        instructions produce) makes the two equal, in which case the
        prefix is empty and folders sit directly under ``/``.

        Args:
            imap_block: [imap.NAME] block; ``maildir`` must be set.

        Returns:
            ``""`` when the store root and the block maildir are the
            same directory; otherwise ``"/<relative path>"`` of the
            block maildir inside the root. When the root cannot be
            determined, falls back to ``"/<basename>"`` (the historical
            assumption).

        Raises:
            MuFailure: The block's maildir lies outside the store root,
                so mu cannot serve it; carries ``fell_back_reason``
                ``"maildir_not_indexed"``.
        """
        configured = (imap_block.maildir or "").rstrip("/")
        root = self._store_maildir_root()
        if root is None:
            return "/" + os.path.basename(configured)
        block_real = os.path.realpath(os.path.expanduser(configured))
        root_real = os.path.realpath(os.path.expanduser(root.rstrip("/")))
        if block_real == root_real:
            return ""
        if block_real.startswith(root_real + os.sep):
            rel = os.path.relpath(block_real, root_real)
            return "/" + rel.replace(os.sep, "/")
        raise MuFailure(
            f"block maildir {imap_block.maildir!r} is not under the mu "
            f"store root {root!r}; mu does not index it",
            fell_back_reason="maildir_not_indexed",
        )

    def _xapian_dir(self) -> Optional[str]:
        """Return the path to the xapian database directory, if known."""
        home = self.muhome
        if not home:
            return None
        return os.path.join(home, "xapian")

    def is_eligible(self, imap_block: ImapBlock) -> EligibilityResult:
        """Check whether the local cache can serve a call for the block.

        Args:
            imap_block: [imap.NAME] block; ``maildir`` must already be
                set by the caller (callers that have not opted the block
                in should bypass this method).

        Returns:
            ``EligibilityResult(eligible=True)`` on success, otherwise
            ``EligibilityResult(eligible=False, reason="…")`` with a
            tag from the ``provenance.fell_back_reason`` vocabulary.
        """
        if shutil.which("mu") is None:
            return EligibilityResult(False, "mu_missing")
        xapian = self._xapian_dir()
        if not xapian or not os.path.isdir(xapian):
            return EligibilityResult(False, "db_missing")
        return EligibilityResult(True)

    def index_mtime_iso(self) -> Optional[str]:
        """Return the xapian database mtime as an ISO 8601 string.

        Returns:
            ISO 8601 timestamp in UTC, or ``None`` when the index is
            unavailable.  Used to populate ``provenance.indexed_at``.
        """
        xapian = self._xapian_dir()
        if not xapian or not os.path.isdir(xapian):
            return None
        try:
            mtime = os.path.getmtime(xapian)
        except OSError:
            return None
        return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(
            timespec="seconds"
        )

    def search(
        self,
        imap_block: ImapBlock,
        parsed: ParseResult,
        limit: int,
        folder: Optional[str] = None,
        world_as_of: Optional[datetime] = None,
        allowed_folders: Optional[List[str]] = None,
    ) -> MuSearch:
        """Run a search against the local mu store, scoped to the block.

        Args:
            imap_block: [imap.NAME] block whose ``maildir`` defines the
                search scope.  Must have ``maildir`` configured.
            parsed: The parsed query with any top-level ``in:`` scope
                already stripped by the dispatcher (the mu emitter
                refuses ``in:`` unconditionally).
            limit: Maximum number of results to return.
            folder: When given, narrow the search to that one IMAP folder
                (exact, non-recursive); when ``None``, search the whole
                block, showing each Message-ID once on a Gmail maildir
                (see :meth:`_gmail_layout`), as the server's All Mail
                does.
            world_as_of: When set, the WORLD_AS_OF bound: relative terms
                resolve against it, and the mu query gains a
                ``date:..<bound>`` clause (Layer 1).  Note the semantic
                caveat: mu indexes the Date header, not INTERNALDATE, so
                the caller flags results with ``date_source: "mu_index"``.
                Rows dated after the bound are then dropped on their
                date field (Layer 2) before the limit cut, as on the IMAP
                path, and counted in ``dropped_after_bound``.
            allowed_folders: When set (and *folder* is ``None``), the
                block's ``allowed_folders`` whitelist: the whole-block
                scope is replaced by the union of these folders' exact
                scopes so the index never answers for walled-off
                folders (issue #60).  The caller gates an explicit
                *folder* against the whitelist itself.

        Returns:
            The :class:`MuSearch`.  Its ``results`` are result dicts
            built from each hit's file as a live search builds its rows
            (see :meth:`_format_result`), plus ``path``; on a redact
            block, a hit whose on-disk file has vanished since the last
            index is skipped with a warning rather than aborting the
            whole result set.  ``report`` is the mu emission's
            translation report for ``provenance.query``, with a note
            added when some hit's file was gone.  ``truncated`` is
            ``True`` when the index holds more matches than ``limit``
            (probed by asking mu for one extra record, reading further
            when the bound or a vanished file took rows out), and a cut
            page's ``total_count`` comes from a second run counting
            every match.

        Raises:
            UntranslatableForBackend: When the query cannot be expressed
                in mu (raised by the mu emitter; also importable under
                its old name ``UntranslatableQuery``).
            MuFailure: When mu invocation fails (timeout, an exit code
                other than 0 or 2, or malformed output), the block's
                maildir is not under the mu store root, or a
                folder-scoped search names a folder absent from the
                synced maildir (tagged ``"folder_not_synced"``; issue
                #64). Exit 2 is mu's no-match answer and returns an
                empty result rather than raising.
            ValueError: When ``imap_block.maildir`` is not configured.
        """
        if not imap_block.maildir:
            raise ValueError(
                "[imap.NAME] block has no maildir configured for local cache."
            )
        if folder is not None and not self._folder_synced(imap_block.maildir, folder):
            # A selectively-synced maildir simply lacks the folder's
            # directory; mu would answer with the same exit it uses
            # for a genuine empty, so the absence must be detected
            # here and handed back as a fallback signal, never served
            # as authoritative emptiness (issue #64).
            raise MuFailure(
                f"folder {folder!r} is not present in the synced maildir "
                f"{imap_block.maildir!r}",
                fell_back_reason="folder_not_synced",
            )
        home = self.muhome
        if not home:
            raise MuFailure("muhome could not be resolved")

        now = world_as_of if world_as_of is not None else datetime.now()
        emission = emit_mu(parsed, now=now)
        translated = emission.query
        if world_as_of is not None:
            # Layer 1: bound the indexed date at second precision, in
            # local time (mu evaluates date: terms in the local zone).
            clause = f"date:..{world_as_of.astimezone().strftime('%Y%m%dT%H%M%S')}"
            translated = f"({translated}) AND {clause}" if translated else clause
        prefix = self._scope_prefix(imap_block)
        scoped = self._scope_query(prefix, translated, folder, allowed_folders)
        # A Gmail maildir holds one copy of a message per label, while
        # the server answers a whole-account search from All Mail with
        # one row per message; within one folder, and on other servers,
        # whose whole-account search covers every folder, copies are
        # rows live as well.
        options = (
            ["--skip-dups"]
            if folder is None and self._gmail_layout(imap_block.maildir)
            else []
        )

        # One extra record is requested so a full page can be told apart
        # from a truncated one.  Rows the bound or a vanished file takes
        # out are not answers: the page is read again, that many records
        # further, until it holds the extra record or the index runs out,
        # so the cut falls where the server's would (issue #115).
        want = limit + 1
        while True:
            raw = self._find_page(home, scoped, want, options)
            records: List[Dict[str, Any]] = []
            from_index = left_out = dropped_after_bound = 0
            for rec in raw:
                row, file_gone = self._format_result(imap_block, rec, prefix)
                if row is None:
                    left_out += 1
                    continue
                if world_as_of is not None and self._dated_after(row, world_as_of):
                    # Layer 2: exact, and ahead of the limit cut as on
                    # the IMAP path.
                    dropped_after_bound += 1
                    continue
                records.append(row)
                if len(records) > limit:
                    break
                if file_gone:
                    from_index += 1
            if len(records) > limit or len(raw) < want:
                break
            want += left_out + dropped_after_bound
        report = emission.report
        report.approximations.extend(_files_gone_notes(from_index, left_out))
        truncated = len(records) > limit
        total_count: Optional[int] = len(records)
        if truncated:
            counted = self._count_matches(home, scoped, options)
            # The count cannot fall below the rows already seen, and the
            # hits taken out above are not answers.
            total_count = (
                None
                if counted is None
                else max(counted - left_out - dropped_after_bound, len(records))
            )
        return MuSearch(
            records[:limit], report, truncated, total_count, dropped_after_bound
        )

    def _find_page(
        self, home: str, scoped: str, maxnum: int, options: List[str]
    ) -> List[Dict[str, Any]]:
        """Read the newest *maxnum* matches of a scoped query from mu.

        Args:
            home: The muhome.
            scoped: The scoped mu query.
            maxnum: How many records to read, newest first by date.
            options: Further ``mu find`` options (``--skip-dups``).

        Returns:
            mu's JSON records; empty when nothing matches.

        Raises:
            MuFailure: When mu times out, cannot run, exits other than
                0 or 2, or prints output that is not a JSON list.
        """
        # ``--muhome`` is parsed by ``mu find`` (the subcommand), not the
        # outer ``mu`` driver, so it must follow ``find`` in the argv.
        argv = [
            "mu",
            "find",
            f"--muhome={home}",
            *options,
            "--format=json",
            "--maxnum",
            str(maxnum),
            "--sortfield",
            "date",
            "--reverse",
            scoped,
        ]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise MuFailure(f"mu find timed out: {e}") from e
        except OSError as e:
            raise MuFailure(f"mu find could not run: {e}") from e
        if proc.returncode == 2:
            # mu exits 2 for a no-match result. It rejects no field:
            # an unknown one is reinterpreted as free text (visible
            # via ``mu find --analyze``), and the queries mu receives
            # come only from courier's emitter, whose field vocabulary
            # is closed and whose user text travels as quoted values.
            # Exit 2 on an emitter-produced query is therefore a
            # genuine miss, served as one; the caller's envelope
            # disclosure names the --no-cache retry (issue #95).
            return []
        if proc.returncode != 0:
            raise MuFailure(f"mu find exited {proc.returncode}: {proc.stderr.strip()}")
        if not proc.stdout.strip():
            return []
        try:
            # mu copies a header's raw tab or other control character
            # into its JSON strings unescaped.
            raw = json.loads(proc.stdout, strict=False)
        except json.JSONDecodeError as e:
            raise MuFailure(f"could not decode mu json output: {e}") from e
        if not isinstance(raw, list):
            raise MuFailure("mu json output was not a list")
        return raw

    @staticmethod
    def _dated_after(row: Dict[str, Any], bound: datetime) -> bool:
        """Whether a result row is dated after the WORLD_AS_OF bound.

        An undated or unparseable date is kept: an undated message is
        not "dated after the bound".

        Args:
            row: A result row carrying a ``date`` ISO 8601 string.
            bound: The WORLD_AS_OF instant.

        Returns:
            ``True`` when the row must not leave the tool.
        """
        iso = row.get("date")
        if not isinstance(iso, str):
            return False
        try:
            return world_bound.after_bound(datetime.fromisoformat(iso), bound)
        except ValueError:
            return False

    @staticmethod
    def _count_matches(home: str, scoped: str, options: List[str]) -> Optional[int]:
        """Count every match of a scoped query, with no limit.

        ``mu find --fields=i`` prints one Message-ID line per match,
        which is cheap to produce even for tens of thousands of
        matches.

        Args:
            home: The muhome.
            scoped: The scoped mu query the page was read with.
            options: The ``mu find`` options the page was read with.

        Returns:
            The number of matches, or ``None`` (with a warning) when mu
            could not run or failed.
        """
        argv = ["mu", "find", f"--muhome={home}", *options, "--fields=i", scoped]
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=30, check=False)
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"could not count the index's matches: {e}")
            return None
        if proc.returncode != 0:
            logger.warning(
                f"could not count the index's matches: mu find exited "
                f"{proc.returncode}"
            )
            return None
        return proc.stdout.count(b"\n")

    @staticmethod
    def _gmail_layout(maildir: str) -> bool:
        """Whether the block's maildir is a synced Gmail account.

        A Gmail sync writes the server's ``[Gmail]`` folders (Sent
        Mail, Trash and so on) under the block maildir, and each label
        as a folder holding its own copy of the message.

        Args:
            maildir: The block's maildir root path.

        Returns:
            ``True`` when a ``[Gmail]`` folder sits under the block root.
        """
        return os.path.isdir(os.path.join(os.path.expanduser(maildir), "[Gmail]"))

    @staticmethod
    def _folder_synced(maildir: str, folder: str) -> bool:
        """Whether *folder* is present in the block's synced maildir.

        ``INBOX`` counts as present when the block root is itself a
        maildir (``cur`` or ``new`` present) or an ``INBOX``
        subdirectory exists, the two layouts :meth:`_derive_folder`
        collapses to ``"INBOX"``.  Any other folder must exist as a
        subdirectory of the block maildir.

        Args:
            maildir: The block's maildir root path.
            folder: IMAP folder name, used as the maildir subdirectory.

        Returns:
            ``True`` when the folder's directory exists on disk.
        """
        base = os.path.expanduser(maildir)
        if folder == "INBOX":
            return (
                os.path.isdir(os.path.join(base, "cur"))
                or os.path.isdir(os.path.join(base, "new"))
                or os.path.isdir(os.path.join(base, "INBOX"))
            )
        return os.path.isdir(os.path.join(base, folder))

    @staticmethod
    def _folder_scope(prefix: str, folder: str) -> str:
        """The exact maildir predicate for one IMAP folder.

        mu's ``maildir:`` term is an exact match when the trailing
        slash is omitted, so subfolders are not swept in.  ``"INBOX"``
        is matched both at the block root and at an ``INBOX`` subdir,
        mirroring the two cases :meth:`_derive_folder` collapses to
        ``"INBOX"``.

        Args:
            prefix: The block's scope prefix from :meth:`_scope_prefix`.
            folder: IMAP folder name.

        Returns:
            A quoted mu ``maildir:`` predicate for the folder.
        """
        if folder == "INBOX":
            root_term = prefix or "/"
            return f'(maildir:"{root_term}" OR maildir:"{prefix}/INBOX")'
        return f'maildir:"{prefix}/{folder}"'

    @staticmethod
    def _scope_query(
        prefix: str,
        translated: str,
        folder: Optional[str] = None,
        allowed_folders: Optional[List[str]] = None,
    ) -> str:
        """Wrap a translated query with a maildir predicate scoping the search.

        With ``folder`` unset the predicate matches the whole block
        recursively (``maildir:"<prefix>/"`` — mu treats the quoted
        trailing-slash form as the folder plus everything below it;
        verified on mu 1.12.14), unless ``allowed_folders`` narrows
        it to the union of that whitelist's exact folder scopes
        (issue #60).  With ``folder`` set it matches that one IMAP
        folder exactly (see :meth:`_folder_scope`).  Every scope is
        quoted so spaces and Xapian metacharacters (``[``, ``&``,
        ``+``) survive query parsing — the recursive form included: an
        unquoted ``maildir:/Work Account/`` matches nothing.

        An empty *prefix* is the mu-root-equals-block-maildir layout:
        the recursive scope becomes the wildcard ``maildir:/*``
        (``maildir:"/"`` is an exact match on root-level messages
        only), and folders sit directly under ``/``.

        Args:
            prefix: The block's scope prefix from :meth:`_scope_prefix`
                (``""`` or ``"/<relative path>"``).
            translated: The query already translated to mu syntax.
            folder: IMAP folder name to scope to, or ``None`` for the
                whole block.
            allowed_folders: The block's whitelist, applied only when
                *folder* is ``None``.

        Returns:
            A mu query string combining the translated query with the
            maildir scope predicate.
        """
        if folder is not None:
            scope = MuBackend._folder_scope(prefix, folder)
        elif allowed_folders:
            scope = (
                "("
                + " OR ".join(
                    MuBackend._folder_scope(prefix, name) for name in allowed_folders
                )
                + ")"
            )
        else:
            scope = "maildir:/*" if not prefix else f'maildir:"{prefix}/"'
        if translated:
            return f"({translated}) AND {scope}"
        return scope

    def _format_result(
        self, imap_block: ImapBlock, rec: Dict[str, Any], scope_prefix: str
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Build the result row for one mu record from its file on disk.

        The row is built the way a live search builds its rows: the
        file is parsed into an :class:`~courier.models.Email` as every
        disk read parses it, and projected with
        :meth:`~courier.models.Email.as_search_result`, so names,
        subject, date and the ``<...>`` Message-ID read as the server's
        would.  Flags come from the ``:2,`` suffix of the file name,
        mapped to IMAP flags as disk reads map them.  The UID is parsed
        from the mbsync-style ``,U=N`` segment of the file name so
        search→read piping works uniformly with IMAP-served hits; a
        row whose file name has none carries no ``uid`` key.  ``path``
        is added so a later read can open the file.

        When ``imap_block.redact_policy`` is set and matches, the row
        takes the redacted shape (blank from/to/subject, ``redacted_by``
        set, no ``path``).

        A file moved or removed since the last index (a routine state
        while the syncer renames files on flag changes) cannot be
        parsed, so the row is built from mu's own fields instead.  On a
        block with a redact policy, which cannot be judged without the
        file, the hit is dropped with a warning instead, so one stale
        path degrades that record alone rather than the block's whole
        result set.

        Args:
            imap_block: The [imap.NAME] block the hit belongs to.
            rec: One record of ``mu find --format=json``.
            scope_prefix: The block's scope prefix from
                :meth:`_scope_prefix`.

        Returns:
            ``(row, file_gone)``: the result row, or ``None`` when the
            hit is dropped; ``file_gone`` is ``True`` when the file
            could not be read, so the caller can count the rows that
            affected.
        """
        path = rec.get(":path", "")
        folder = self._derive_folder(scope_prefix, rec.get(":maildir"))
        uid = self._parse_uid_from_path(path)
        flags = ImapClient._parse_maildir_flags(path)
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as e:
            if imap_block.redact_policy is not None:
                logger.warning(
                    f"skipping search hit whose maildir file is gone "
                    f"(stale index path {path!r}): {e}"
                )
                return None, True
            return self._index_row(rec, path, folder, uid, flags), True
        email_obj = ImapClient._email_from_bytes(raw, uid, folder, flags)
        policy = imap_block.redact_policy
        if policy is not None and policy(email_obj):
            email_obj = email_obj.redact("redacted")
        row = email_obj.as_search_result(
            folder=folder,
            flags=email_obj.flags,
            date_iso=(
                email_obj.date.astimezone().isoformat() if email_obj.date else None
            ),
            has_attachments=len(email_obj.attachments) > 0,
        )
        if uid is None:
            del row["uid"]
        if email_obj.redacted_by is None:
            row["path"] = path
        return row, False

    def _index_row(
        self,
        rec: Dict[str, Any],
        path: str,
        folder: str,
        uid: Optional[int],
        flags: List[str],
    ) -> Dict[str, Any]:
        """Build a result row from mu's fields, for a hit whose file is gone.

        The keys and the Message-ID's angle brackets match the rows
        built from files; names and subject are mu's renderings.

        Args:
            rec: One record of ``mu find --format=json``.
            path: The file path mu indexed.
            folder: The folder derived from the record.
            uid: The UID from the file name, or ``None``.
            flags: The IMAP flags from the file name.

        Returns:
            The result row.
        """
        message_id = rec.get(":message-id") or ""
        if message_id and not message_id.startswith("<"):
            message_id = f"<{message_id}>"
        row: Dict[str, Any] = {} if uid is None else {"uid": uid}
        row.update(
            {
                "folder": folder,
                "from": self._format_address_first(rec.get(":from")),
                "to": self._format_address_list(rec.get(":to")),
                "subject": rec.get(":subject", ""),
                "date": self._format_date(rec.get(":date-unix")),
                "flags": flags,
                "has_attachments": "attach" in (rec.get(":flags") or []),
                "message_id": message_id,
                "path": path,
            }
        )
        return row

    @staticmethod
    def _parse_uid_from_path(path: str) -> Optional[int]:
        """Extract the IMAP UID from an mbsync-style maildir filename.

        Returns ``None`` when the filename does not carry the
        ``,U=N,`` segment (non-mbsync layouts).
        """
        if not path:
            return None
        match = _UID_FROM_FILENAME.search(path)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _format_address_first(value: Any) -> str:
        """Format mu's address list value as a single ``Name <email>`` string.

        mu's :from is a list of address plists; we collapse to the first
        entry to mirror the IMAP path's single-string ``from`` field.
        """
        if not isinstance(value, list) or not value:
            return ""
        first = value[0]
        if not isinstance(first, dict):
            return ""
        name = first.get(":name") or ""
        email_addr = first.get(":email") or ""
        if name and email_addr:
            return f"{name} <{email_addr}>"
        return email_addr or name

    @staticmethod
    def _format_address_list(value: Any) -> List[str]:
        """Format mu's address list as a list of ``Name <email>`` strings."""
        if not isinstance(value, list):
            return []
        out: List[str] = []
        for entry in value:
            if not isinstance(entry, dict):
                continue
            name = entry.get(":name") or ""
            email_addr = entry.get(":email") or ""
            if name and email_addr:
                out.append(f"{name} <{email_addr}>")
            elif email_addr:
                out.append(email_addr)
            elif name:
                out.append(name)
        return out

    @staticmethod
    def _format_date(unix_ts: Any) -> Optional[str]:
        """Convert a unix timestamp to a local-time ISO 8601 string.

        Returns ``None`` on error. The instant is rendered in the host's
        local timezone so the displayed wall-clock matches what the user
        sees in their own client, rather than UTC.
        """
        if not isinstance(unix_ts, (int, float)):
            return None
        try:
            return (
                datetime.fromtimestamp(unix_ts, tz=timezone.utc)
                .astimezone()
                .isoformat()
            )
        except (OSError, OverflowError, ValueError):
            return None

    @staticmethod
    def _derive_folder(scope_prefix: str, mu_maildir: Any) -> str:
        """Derive the relative folder name from mu's ``:maildir`` field.

        mu reports ``:maildir`` as a path relative to its store root
        (e.g. ``/work/Deleted Messages``).  Strip the block's scope
        prefix (from :meth:`_scope_prefix`) to get the folder relative
        to the block; report ``"INBOX"`` for messages sitting at the
        block root.  With an empty prefix (mu root == block maildir)
        the block root is reported as ``/`` and folders sit directly
        under it.
        """
        if not isinstance(mu_maildir, str):
            return ""
        if mu_maildir == (scope_prefix or "/"):
            return "INBOX"
        if mu_maildir.startswith(scope_prefix + "/"):
            return mu_maildir[len(scope_prefix) + 1 :]
        return mu_maildir.lstrip("/")


__all__ = [
    "EligibilityResult",
    "MuBackend",
    "MuFailure",
    "MuSearch",
    "UntranslatableQuery",
]
