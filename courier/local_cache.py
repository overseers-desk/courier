"""Optional local-cache search backend via mu (Xapian).

Subprocess-driven: shells out to ``mu find --format=json`` against a
user-indexed maildir.  This module provides eligibility checks and
search execution; integration into ``ImapClient.search_emails`` and the
CLI is handled by the caller.

The contract is "a maildir exists and mu indexes it".  This module does
not invoke ``mu index``; it does not read external sync-tool state (e.g. offlineimap's);
and it does not model any sync stack.  When the configured staleness
budget is exceeded (or any other check fails), eligibility returns
``False`` and the caller is expected to fall back to IMAP.
"""

import email as email_pkg
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config import ImapBlock, LocalCacheConfig
from .models import Email
from .query_parser import UntranslatableQuery, parse_query_to_mu

_UID_FROM_FILENAME = re.compile(r",U=(\d+)[,:]")

logger = logging.getLogger(__name__)


class MuFailure(Exception):
    """Raised when invoking mu fails for a non-eligibility reason.

    Eligibility failures (mu missing, db missing, stale index) are
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
            responses (``"mu_missing"``, ``"db_missing"``, ``"stale"``).
            A block carrying a redact policy stays eligible: the policy
            is applied against the on-disk maildir file at format time,
            not by forcing an IMAP round-trip.
    """

    eligible: bool
    reason: Optional[str] = None


class MuBackend:
    """Local-cache search backend driven by ``mu find`` subprocess calls.

    A single instance is shared across [imap.*] blocks; per-block
    scoping is applied at search time via the maildir predicate, not at
    initialisation.  The resolved muhome is cached in process memory.
    """

    def __init__(self, cfg: LocalCacheConfig) -> None:
        """Initialise with the global local-cache configuration.

        Args:
            cfg: Configuration block carrying ``indexer``,
                ``max_staleness_seconds``, and an optional ``mu_index``
                override.  When ``mu_index`` is unset, the muhome is
                discovered lazily from ``mu info store`` on first use.
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
                timeout=10,
                check=True,
            )
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
                timeout=10,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.warning(f"Could not read the mu store's maildir root: {e}")
            return None
        match = re.search(r"\bmaildir\s*\|\s*(\S+)", proc.stdout)
        if not match:
            logger.warning(
                "could not parse the maildir row from mu info store output"
            )
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
        try:
            mtime = os.path.getmtime(xapian)
        except OSError:
            return EligibilityResult(False, "db_missing")
        age = datetime.now().timestamp() - mtime
        if age > self.cfg.max_staleness_seconds:
            return EligibilityResult(False, "stale")
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
        query: str,
        limit: int,
        folder: Optional[str] = None,
        world_as_of: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Run a search against the local mu store, scoped to the block.

        Args:
            imap_block: [imap.NAME] block whose ``maildir`` defines the
                search scope.  Must have ``maildir`` configured.
            query: User query string in courier syntax.
            limit: Maximum number of results to return.
            folder: When given, narrow the search to that one IMAP folder
                (exact, non-recursive); when ``None``, search the whole
                block.
            world_as_of: When set, the WORLD_AS_OF bound: relative terms
                resolve against it, and the mu query gains a
                ``date:..<bound>`` clause (Layer 1).  Note the semantic
                caveat: mu indexes the Date header, not INTERNALDATE, so
                the caller flags results with ``date_source: "mu_index"``
                and post-filters on the result's date field (Layer 2).

        Returns:
            A list of result dicts mirroring the IMAP search shape minus
            ``uid``, plus ``message_id`` and ``path``.  On a redact
            block, a hit whose on-disk file has vanished since the last
            index is skipped with a warning rather than aborting the
            whole result set.

        Raises:
            UntranslatableQuery: When the query cannot be expressed in
                mu (re-raised from the query translator).
            MuFailure: When mu invocation fails (timeout, non-zero
                exit — including exit 2, which mu uses both for "no
                matches" and for a query it silently failed to parse —
                or malformed output), or the block's maildir is not
                under the mu store root.
            ValueError: When ``imap_block.maildir`` is not configured.
        """
        if not imap_block.maildir:
            raise ValueError(
                "[imap.NAME] block has no maildir configured for local cache."
            )
        home = self.muhome
        if not home:
            raise MuFailure("muhome could not be resolved")

        translated = parse_query_to_mu(query, now=world_as_of)
        if world_as_of is not None:
            # Layer 1: bound the indexed date at second precision, in
            # local time (mu evaluates date: terms in the local zone).
            clause = f"date:..{world_as_of.astimezone().strftime('%Y%m%dT%H%M%S')}"
            translated = f"({translated}) AND {clause}" if translated else clause
        prefix = self._scope_prefix(imap_block)
        scoped = self._scope_query(prefix, translated, folder)

        # ``--muhome`` is parsed by ``mu find`` (the subcommand), not the
        # outer ``mu`` driver, so it must follow ``find`` in the argv.
        argv = [
            "mu",
            "find",
            f"--muhome={home}",
            "--format=json",
            "--maxnum",
            str(limit),
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
        if proc.returncode == 2:
            # mu exits 2 both for a genuinely empty result and for a
            # query it silently failed to parse (verified on mu
            # 1.12.14: an unknown field like ``filename:x`` gives the
            # same exit and the same "no matches" message as a real
            # miss).  An empty that cannot be told apart from a
            # rejected query must not be served as authoritative
            # absence, so it surfaces here and the caller confirms the
            # empty against IMAP (issue #64).
            raise MuFailure(
                f"mu find exited 2 (no matches, or a query mu could not "
                f"parse): {proc.stderr.strip()}",
                fell_back_reason="mu_no_matches",
            )
        if proc.returncode != 0:
            raise MuFailure(f"mu find exited {proc.returncode}: {proc.stderr.strip()}")
        if not proc.stdout.strip():
            return []
        try:
            raw = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise MuFailure(f"could not decode mu json output: {e}") from e
        if not isinstance(raw, list):
            raise MuFailure("mu json output was not a list")
        formatted = (self._format_result(imap_block, rec, prefix) for rec in raw)
        return [rec for rec in formatted if rec is not None]

    @staticmethod
    def _scope_query(
        prefix: str, translated: str, folder: Optional[str] = None
    ) -> str:
        """Wrap a translated query with a maildir predicate scoping the search.

        With ``folder`` unset the predicate matches the whole block
        recursively (``maildir:"<prefix>/"`` — mu treats the quoted
        trailing-slash form as the folder plus everything below it;
        verified on mu 1.12.14).  With ``folder`` set it matches that
        one IMAP folder exactly: mu's ``maildir:`` term is an exact
        match when the trailing slash is omitted, so subfolders are not
        swept in.  ``"INBOX"`` is matched both at the block root and at
        an ``INBOX`` subdir, mirroring the two cases
        :meth:`_derive_folder` collapses to ``"INBOX"``.  Every scope is
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

        Returns:
            A mu query string combining the translated query with the
            maildir scope predicate.
        """
        if folder is None:
            scope = "maildir:/*" if not prefix else f'maildir:"{prefix}/"'
        elif folder == "INBOX":
            root_term = prefix or "/"
            scope = f'(maildir:"{root_term}" OR maildir:"{prefix}/INBOX")'
        else:
            scope = f'maildir:"{prefix}/{folder}"'
        if translated:
            return f"({translated}) AND {scope}"
        return scope

    def _format_result(
        self, imap_block: ImapBlock, rec: Dict[str, Any], scope_prefix: str
    ) -> Optional[Dict[str, Any]]:
        """Translate a single mu json record into courier result shape.

        UID is parsed from the mbsync-style ``,U=N,`` segment of the
        filename when present so search→read piping works uniformly with
        IMAP-served hits; records from non-mbsync layouts omit the
        ``uid`` key.

        When ``imap_block.redact_policy`` is set the on-disk maildir
        file at ``:path`` is read and parsed and the policy is evaluated
        against the resulting :class:`Email`.  Matching records get the
        redacted shape (blank from/to/subject, ``redacted_by`` set, no
        ``path``); non-matching records pass through untouched.  When
        no policy is set the file is not opened.  Returns ``None``
        (with a warning) when the file has vanished since the last
        index — a routine state while the syncer renames files on flag
        changes — so one stale path degrades that record alone instead
        of discarding the block's whole result set.
        """
        flags = rec.get(":flags") or []
        path = rec.get(":path", "")
        folder = self._derive_folder(scope_prefix, rec.get(":maildir"))

        base: Dict[str, Any] = {
            "message_id": rec.get(":message-id", ""),
            "path": path,
            "folder": folder,
            "from": self._format_address_first(rec.get(":from")),
            "to": self._format_address_list(rec.get(":to")),
            "subject": rec.get(":subject", ""),
            "date": self._format_date(rec.get(":date-unix")),
            "flags": list(flags),
            "has_attachments": "attach" in flags,
        }

        uid = self._parse_uid_from_path(path)
        if uid is not None:
            base["uid"] = uid

        if imap_block.redact_policy is None:
            return base

        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as e:
            logger.warning(
                f"skipping search hit whose maildir file is gone "
                f"(stale index path {path!r}): {e}"
            )
            return None
        message = email_pkg.message_from_bytes(raw)
        email_obj = Email.from_message(message, uid=uid, folder=folder)
        email_obj.flags = list(flags)
        if not imap_block.redact_policy(email_obj):
            return base

        redacted = email_obj.redact("redacted")
        return redacted.as_search_result(
            folder=folder,
            flags=list(flags),
            date_iso=base["date"],
            has_attachments=False,
        )

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
    "UntranslatableQuery",
]
