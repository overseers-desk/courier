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
    for runtime failures (timeout, non-zero exit, malformed output)
    that should trigger an IMAP fallback at the caller.
    """


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
        match = re.search(r"database-path\s*\|\s*(\S+)", proc.stdout)
        if not match:
            raise MuFailure("could not parse database-path from mu info store output")
        db_path = match.group(1)
        return os.path.dirname(db_path)

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
    ) -> List[Dict[str, Any]]:
        """Run a search against the local mu store, scoped to the block.

        Args:
            imap_block: [imap.NAME] block whose ``maildir`` defines the
                search scope.  Must have ``maildir`` configured.
            query: User query string in mailroom syntax.
            limit: Maximum number of results to return.

        Returns:
            A list of result dicts mirroring the IMAP search shape minus
            ``uid``, plus ``message_id`` and ``path``.

        Raises:
            UntranslatableQuery: When the query cannot be expressed in
                mu (re-raised from the query translator).
            MuFailure: When mu invocation fails (timeout, non-zero
                exit, malformed output).
            ValueError: When ``imap_block.maildir`` is not configured.
        """
        if not imap_block.maildir:
            raise ValueError(
                "[imap.NAME] block has no maildir configured for local cache."
            )
        home = self.muhome
        if not home:
            raise MuFailure("muhome could not be resolved")

        translated = parse_query_to_mu(query)
        scoped = self._scope_query(imap_block.maildir, translated)

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
        # mu returns exit 2 when no matches; treat as empty result, not
        # an error.  Other non-zero codes are real failures.
        if proc.returncode not in (0, 2):
            raise MuFailure(f"mu find exited {proc.returncode}: {proc.stderr.strip()}")
        if proc.returncode == 2 or not proc.stdout.strip():
            return []
        try:
            raw = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise MuFailure(f"could not decode mu json output: {e}") from e
        if not isinstance(raw, list):
            raise MuFailure("mu json output was not a list")
        return [self._format_result(imap_block, rec) for rec in raw]

    @staticmethod
    def _scope_query(maildir: str, translated: str) -> str:
        """Wrap a translated query with a per-block maildir predicate."""
        basename = os.path.basename(maildir.rstrip("/"))
        scope = f"maildir:/{basename}/"
        if translated:
            return f"({translated}) AND {scope}"
        return scope

    def _format_result(
        self, imap_block: ImapBlock, rec: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Translate a single mu json record into mailroom result shape.

        UID is parsed from the mbsync-style ``,U=N,`` segment of the
        filename when present so search→read piping works uniformly with
        IMAP-served hits; records from non-mbsync layouts omit the
        ``uid`` key.

        When ``imap_block.redact_policy`` is set the on-disk maildir
        file at ``:path`` is read and parsed and the policy is evaluated
        against the resulting :class:`Email`.  Matching records get the
        redacted shape (blank from/to/subject, ``redacted_by`` set, no
        ``path``); non-matching records pass through untouched.  When
        no policy is set the file is not opened.
        """
        flags = rec.get(":flags") or []
        path = rec.get(":path", "")
        folder = self._derive_folder(imap_block.maildir, rec.get(":maildir"))

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
            raise MuFailure(f"could not read maildir file {path!r}: {e}") from e
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
        """Convert a unix timestamp to ISO 8601, returning ``None`` on error."""
        if not isinstance(unix_ts, (int, float)):
            return None
        try:
            return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            return None

    @staticmethod
    def _derive_folder(block_maildir: Optional[str], mu_maildir: Any) -> str:
        """Derive the relative folder name from mu's ``:maildir`` field.

        mu reports ``:maildir`` as a path relative to its store root
        (e.g. ``/work/Deleted Messages``).  Strip the leading
        ``/<basename>/`` of the block's maildir to get the folder
        relative to the block; report ``"INBOX"`` for messages sitting
        at the block root.
        """
        if not isinstance(mu_maildir, str):
            return ""
        if not block_maildir:
            return mu_maildir.lstrip("/")
        basename = os.path.basename(block_maildir.rstrip("/"))
        prefix = f"/{basename}"
        if mu_maildir == prefix:
            return "INBOX"
        if mu_maildir.startswith(prefix + "/"):
            return mu_maildir[len(prefix) + 1 :]
        return mu_maildir.lstrip("/")


__all__ = [
    "EligibilityResult",
    "MuBackend",
    "MuFailure",
    "UntranslatableQuery",
]
