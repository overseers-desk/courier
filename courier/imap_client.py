"""IMAP client implementation."""

import email
import glob
import logging
import os
import re
import shlex
from datetime import date, datetime, timedelta
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
)

import imapclient  # type: ignore[import-untyped]

from courier import world_bound
from courier.config import ImapBlock
from courier.errors import FolderNotFound, WorldBoundRefused, as_courier_error
from courier.models import Email
from courier.oauth2 import get_access_token
from courier.query_parser import parse_query

if TYPE_CHECKING:
    from courier.local_cache import MuBackend


# mbsync encodes flags in the maildir filename suffix after ``:2,``.
# Map each letter to its RFC 3501 IMAP flag so disk-served Email objects
# carry the same ``flags`` list as IMAP-served ones.
_MAILDIR_FLAG_CHARS = {
    "S": "\\Seen",
    "R": "\\Answered",
    "T": "\\Deleted",
    "D": "\\Draft",
    "F": "\\Flagged",
}

logger = logging.getLogger(__name__)


# Fallback tree for the Sent folder when neither the identity nor the
# command line pins one. Dovecot-style INBOX-prefixed names come before
# the bare names because Dovecot servers reject the bare form with
# "Mailbox name should probably be prefixed with: INBOX.". SPECIAL-USE
# (\Sent) is consulted first by ``resolve_sent_folder`` and is not in
# this list.
SENT_FOLDER_CANDIDATES = (
    "INBOX.Sent",
    "INBOX.Sent Items",
    "INBOX.Sent Messages",
    "Sent",
    "Sent Items",
    "Sent Messages",
    "[Gmail]/Sent Mail",
)


class AppendResult(NamedTuple):
    """Outcome of an IMAP APPEND: both halves of the APPENDUID response.

    ``uid``/``uidvalidity`` are ``None`` when the server does not
    advertise UIDPLUS (no APPENDUID in the response).
    """

    uid: Optional[int]
    uidvalidity: Optional[int]


# APPENDUID <uidvalidity> <uid> (RFC 4315). Both groups are kept: the UID
# alone is ambiguous across mailbox re-creations.
_APPENDUID_RE = re.compile(rb"APPENDUID\s+(\d+)\s+(\d+)")


def _as_uid_list(uid: Union[int, Sequence[int]]) -> List[int]:
    """Normalize a single UID or a sequence of UIDs to a list."""
    if isinstance(uid, int):
        return [uid]
    return list(uid)


# Sentinel default for ImapClient's world_as_of parameter: "read the
# WORLD_AS_OF environment variable at construction". Distinct from None,
# which means an explicit unbounded client, so no construction site can
# silently opt out of the bound by omitting the argument.
_ENV_BOUND: Any = object()

# IMAP month abbreviations for SEARCH date formatting. An explicit table
# because %b is locale-dependent and RFC 3501 dates are always English.
_IMAP_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _imap_date(d: date) -> str:
    """Format a date as an RFC 3501 SEARCH date (e.g. ``13-Jul-2026``)."""
    return f"{d.day:02d}-{_IMAP_MONTHS[d.month - 1]}-{d.year}"


def _apply_search_bound(
    criteria: Union[str, List[Any], Tuple[Any, ...]], bound: datetime
) -> Union[str, List[Any]]:
    """AND a ``BEFORE`` prefilter onto search criteria (Layer 1, coarse).

    IMAP ``SEARCH BEFORE`` filters on INTERNALDATE at day granularity in
    the server's idea of the day, so the clause uses the bound's date
    plus one day: over-inclusive by up to a day plus timezone slack,
    never under-inclusive. This keeps result sets small; the exact cut
    is the INTERNALDATE post-filter (Layer 2), never this clause.

    Args:
        criteria: Resolved search criteria, either a raw string or an
            imapclient criteria list/tuple.
        bound: The WORLD_AS_OF instant.

    Returns:
        The criteria with the BEFORE clause ANDed on. String criteria
        gain a textual clause (imapclient passes strings through
        unquoted); list criteria gain two items, with the date object
        formatted by imapclient itself.
    """
    before_day = bound.date() + timedelta(days=1)
    if isinstance(criteria, str):
        return f"{criteria} BEFORE {_imap_date(before_day)}"
    return list(criteria) + ["BEFORE", before_day]


class ImapClient:
    """IMAP client for interacting with email servers."""

    def __init__(
        self,
        block: ImapBlock,
        local_cache: Optional["MuBackend"] = None,
        world_as_of: Union[Optional[datetime], object] = _ENV_BOUND,
    ):
        """Initialize IMAP client.

        Args:
            block: [imap.NAME] block carrying the IMAP connection details
                plus per-block options (allowed_folders, maildir,
                default_smtp).
            local_cache: Optional ``MuBackend`` for serving search calls
                from a local mu index. When ``None`` or when the block's
                ``maildir`` is unset, all searches are served by IMAP.
            world_as_of: The WORLD_AS_OF bound this client enforces on
                every search and fetch. Defaults to reading the
                environment variable once, here at construction (so a
                construction site that does not thread the value still
                gets the bound, and per-call re-reads never happen);
                pass an aware ``datetime`` to bind explicitly, or
                ``None`` for an explicitly unbounded client.

        Raises:
            WorldAsOfInvalid: When the default is used and the
                environment variable is set but unparseable or naive.
        """
        self.block = block
        if world_as_of is _ENV_BOUND:
            self.world_as_of: Optional[datetime] = world_bound.world_as_of()
        else:
            self.world_as_of = cast(Optional[datetime], world_as_of)
        self.allowed_folders = (
            set(block.allowed_folders) if block.allowed_folders else None
        )
        self.local_cache = local_cache
        self.client: Optional[imapclient.IMAPClient] = None
        self.folder_cache: Dict[str, List[str]] = {}
        self.connected = False
        self.count_cache: Dict[str, Dict[str, Tuple[int, datetime]]] = (
            {}
        )  # Cache for message counts
        self.current_folder: Optional[str] = None  # Store the currently selected folder
        self.folder_message_counts: Dict[str, int] = (
            {}
        )  # Cache for folder message counts
        self.last_activity: Optional[datetime] = (
            None  # Track last successful IMAP operation
        )

    def _client_or_raise(self) -> imapclient.IMAPClient:
        """Return the underlying IMAPClient, raising if not connected."""
        if self.client is None:
            raise ConnectionError("Not connected to IMAP server")
        return self.client

    def connect(self) -> None:
        """Connect to IMAP server.

        Raises:
            ConnectionError: If connection fails
        """
        try:
            self.client = imapclient.IMAPClient(
                self.block.host,
                port=self.block.port,
                ssl=self.block.use_ssl,
                timeout=10,  # 10 second connection timeout
            )

            # Use OAuth2 for Gmail if configured
            if self.block.requires_oauth2:
                logger.info(f"Using OAuth2 authentication for {self.block.host}")

                # Get fresh access token
                if not self.block.oauth2:
                    raise ValueError("OAuth2 configuration is required for Gmail")

                access_token, _ = get_access_token(self.block.oauth2)

                # Authenticate with XOAUTH2
                # Use the oauth_login method which properly formats the XOAUTH2 string
                self.client.oauth2_login(self.block.username, access_token)
            else:
                # Standard password authentication
                if not self.block.password:
                    raise ValueError("Password is required for authentication")

                self.client.login(self.block.username, self.block.password)

            self.connected = True
            self.last_activity = datetime.now()  # Track connection time
            logger.info(f"Connected to IMAP server {self.block.host}")
        except Exception as e:
            self.connected = False
            logger.error(f"Failed to connect to IMAP server: {e}")
            raise ConnectionError(f"Failed to connect to IMAP server: {e}")

    def disconnect(self) -> None:
        """Disconnect from IMAP server."""
        if self.client:
            try:
                self.client.logout()
            except Exception as e:
                logger.warning(f"Error during IMAP logout: {e}")
            finally:
                self.client = None
                self.connected = False
                self.last_activity = None  # Reset activity tracking
                logger.info("Disconnected from IMAP server")

    def _is_connection_stale(self) -> bool:
        """Check if connection is likely stale based on idle timeout.

        Returns:
            True if connection should be considered stale
        """
        idle_timeout = self.block.idle_timeout

        # -1 means never consider stale (legacy behaviour)
        if idle_timeout < 0:
            return False

        # 0 means always stale (close after each operation)
        if idle_timeout == 0:
            return True

        # Check actual idle time
        if self.last_activity is None:
            return True

        idle_seconds = (datetime.now() - self.last_activity).total_seconds()
        return idle_seconds > idle_timeout

    def _verify_connection(self) -> bool:
        """Verify connection is alive using NOOP command.

        Returns:
            True if connection is alive, False otherwise
        """
        if not self.client or not self.connected:
            return False

        try:
            self.client.noop()
            return True
        except Exception as e:
            logger.warning(f"Connection verification failed: {e}")
            return False

    def _update_activity(self) -> None:
        """Update last activity timestamp after successful operation."""
        self.last_activity = datetime.now()

    def ensure_connected(self) -> None:
        """Ensure connection is available and healthy.

        This method implements the connection lifecycle strategy:
        - idle_timeout = 0: Reconnect before every operation (stateless mode)
        - idle_timeout > 0: Reconnect if idle longer than timeout
        - idle_timeout = -1: Never proactively reconnect (legacy mode)

        Raises:
            ConnectionError: If connection cannot be established
        """
        idle_timeout = self.block.idle_timeout

        # Case 1: Not connected at all - must connect
        if not self.connected or not self.client:
            self.connect()
            return

        # Case 2: Stateless mode (idle_timeout = 0) - always reconnect
        if idle_timeout == 0:
            logger.debug("Stateless mode: reconnecting for operation")
            self.disconnect()
            self.connect()
            return

        # Case 3: Legacy mode (idle_timeout = -1) - never proactively reconnect
        if idle_timeout < 0:
            return

        # Case 4: Connection might be stale - check and reconnect if needed
        if self._is_connection_stale():
            logger.info(f"Connection idle for >{idle_timeout}s, reconnecting...")
            self.disconnect()
            self.connect()
            return

        # Case 5: Connection within timeout - optionally verify with NOOP
        if self.block.verify_with_noop:
            if not self._verify_connection():
                logger.warning("Connection verification failed, reconnecting...")
                self.disconnect()
                self.connect()

    def get_capabilities(self) -> List[str]:
        """Get IMAP server capabilities.

        Returns:
            List of server capabilities

        Raises:
            ConnectionError: If not connected and connection fails
        """
        self.ensure_connected()
        raw_capabilities = self._client_or_raise().capabilities()

        # Convert byte strings to regular strings and normalize case
        capabilities = []
        for cap in raw_capabilities:
            if isinstance(cap, bytes):
                cap = cap.decode("utf-8")
            capabilities.append(cap.upper())

        self._update_activity()
        return capabilities

    def list_folders(self, refresh: bool = False) -> List[str]:
        """List available folders.

        Args:
            refresh: Force refresh folder list cache

        Returns:
            List of folder names

        Raises:
            ConnectionError: If not connected and connection fails
        """
        self.ensure_connected()

        # Check cache first
        if not refresh and self.folder_cache:
            return list(self.folder_cache.keys())

        # Get folders from server
        folders = []
        for flags, delimiter, name in self._client_or_raise().list_folders():
            if isinstance(name, bytes):
                # Convert bytes to string if necessary
                name = name.decode("utf-8")

            # Skip non-selectable folders (e.g. Gmail's '[Gmail]' parent has
            # \Noselect; SELECTing it returns NONEXISTENT).
            if b"\\Noselect" in flags or b"\\NonExistent" in flags:
                continue

            # Filter folders if allowed_folders is set
            if self.allowed_folders is not None and name not in self.allowed_folders:
                continue

            folders.append(name)
            self.folder_cache[name] = flags

        self._update_activity()
        logger.debug(f"Listed {len(folders)} folders")
        return folders

    def find_special_use_folder(self, role: bytes) -> Optional[str]:
        """Return the folder marked with the given SPECIAL-USE flag.

        IMAP SPECIAL-USE (RFC 6154) advertises folders by role:
        ``\\All``, ``\\Sent``, ``\\Drafts``, ``\\Trash``, ``\\Junk``,
        ``\\Flagged``, ``\\Important``. Gmail tags ``[Gmail]/All Mail`` with
        ``\\All``; Fastmail uses ``Archive``; etc.

        Args:
            role: The SPECIAL-USE flag as bytes, e.g. ``b'\\\\All'``.

        Returns:
            The folder name, or ``None`` if no folder advertises that role.
        """
        if not self.folder_cache:
            self.list_folders()
        for name, flags in self.folder_cache.items():
            if role in flags:
                return name
        return None

    def _is_folder_allowed(self, folder: str) -> bool:
        """Check if a folder is allowed.

        Args:
            folder: Folder to check

        Returns:
            True if folder is allowed, False otherwise
        """
        # If no allowed_folders specified, all folders are allowed
        if self.allowed_folders is None:
            return True

        # If allowed_folders is specified, check if folder is in it
        return folder in self.allowed_folders

    def select_folder(self, folder: str, readonly: bool = False) -> Dict:
        """Select folder on IMAP server.

        Args:
            folder: Folder to select
            readonly: If True, select folder in read-only mode

        Returns:
            Dictionary with folder information

        Raises:
            ValueError: If folder is not allowed
            ConnectionError: If connection error occurs
        """
        # Make sure the folder is allowed
        if not self._is_folder_allowed(folder):
            raise ValueError(f"Folder '{folder}' is not allowed")

        self.ensure_connected()

        try:
            result: Dict[Any, Any] = self._client_or_raise().select_folder(
                folder, readonly=readonly
            )
            self.current_folder = folder
            self._update_activity()
            logger.debug(f"Selected folder '{folder}'")
            return result
        except imapclient.IMAPClient.Error as e:
            logger.error(f"Error selecting folder {folder}: {e}")
            raise ConnectionError(f"Failed to select folder {folder}: {e}")

    def search(
        self,
        criteria: Union[str, List[Any], Tuple[Any, ...]],
        folder: str = "INBOX",
        charset: Optional[str] = None,
    ) -> List[int]:
        """Search for messages.

        Args:
            criteria: Search criteria
            folder: Folder to search in
            charset: Character set for search criteria

        Returns:
            List of message UIDs

        Raises:
            ConnectionError: If not connected and connection fails
        """
        self.ensure_connected()
        self.select_folder(folder, readonly=True)

        resolved_criteria: Union[str, List[Any], Tuple[Any, ...]] = criteria
        if isinstance(criteria, str):
            # Relative presets resolve against the bound when set, so a
            # replayed "today" is the bound's day, not the wall clock's.
            now = self.world_as_of if self.world_as_of is not None else datetime.now()
            # Predefined criteria strings
            criteria_map: Dict[str, Union[str, List[Any]]] = {
                "all": "ALL",
                "unseen": "UNSEEN",
                "seen": "SEEN",
                "answered": "ANSWERED",
                "unanswered": "UNANSWERED",
                "deleted": "DELETED",
                "undeleted": "UNDELETED",
                "flagged": "FLAGGED",
                "unflagged": "UNFLAGGED",
                "recent": "RECENT",
                "today": ["SINCE", now.date()],
                "yesterday": [
                    "SINCE",
                    (now - timedelta(days=1)).date(),
                    "BEFORE",
                    now.date(),
                ],
                "week": ["SINCE", (now - timedelta(days=7)).date()],
                "month": ["SINCE", (now - timedelta(days=30)).date()],
            }

            if criteria.lower() in criteria_map:
                resolved_criteria = criteria_map[criteria.lower()]

        if self.world_as_of is not None:
            # Layer 1: every search the client issues gains the coarse
            # server-side BEFORE prefilter; Layer 2 post-filters exactly.
            resolved_criteria = _apply_search_bound(resolved_criteria, self.world_as_of)

        results = self._client_or_raise().search(resolved_criteria, charset=charset)
        self._update_activity()
        logger.debug(f"Search returned {len(results)} results")
        return list(results)

    @staticmethod
    def _email_from_bytes(raw: bytes, uid: int, folder: str, flags: List[str]) -> Email:
        """Parse RFC 822 bytes into an :class:`Email` with the given flags.

        Used by every fetch path (IMAP single, IMAP batch, disk-first):
        each path produces its own ``flags`` list from a different
        source (IMAP server response or maildir filename suffix) but
        the message-bytes-to-Email pipeline is the same.
        """
        message = email.message_from_bytes(raw)
        email_obj = Email.from_message(message, uid=uid, folder=folder)
        email_obj.flags = flags
        return email_obj

    def _after_bound(self, dt: Optional[datetime]) -> bool:
        """Whether a message date falls after the WORLD_AS_OF bound.

        The shared Layer 2 predicate: result-assembly paths drop
        messages for which this is ``True``. Always ``False`` when the
        client is unbounded, when the date is unavailable (an undated
        message is not "dated after the bound"), or when the value is
        not a ``datetime`` (defensive against server-library quirks).

        Args:
            dt: The message's INTERNALDATE, or its Date-header date on
                paths without one (disk cache, mu index).

        Returns:
            ``True`` when the message must not leave the tool.
        """
        if self.world_as_of is None or not isinstance(dt, datetime):
            return False
        return world_bound.after_bound(dt, self.world_as_of)

    def _refuse_read_after_bound(self, dt: Optional[datetime]) -> None:
        """Refuse a direct read of a message dated after the bound (Layer 2).

        Args:
            dt: The message's INTERNALDATE, or its Date-header date on
                paths without one (disk cache).

        Raises:
            WorldBoundRefused: When the bound is set and *dt* is after
                it, with a message naming both instants.
        """
        bound = self.world_as_of
        if bound is None or dt is None or not self._after_bound(dt):
            return
        raise WorldBoundRefused(world_bound.refusal_message(dt, bound))

    def _bound_fetch_items(self, items: List[str]) -> List[str]:
        """Add INTERNALDATE to fetch items when the bound is set.

        Layer 2 needs the INTERNALDATE to judge each message; it is
        fetched only under a bound so unbounded operation stays
        byte-identical on the wire.
        """
        if self.world_as_of is not None:
            return items + ["INTERNALDATE"]
        return items

    def _disk_cache_eligible(self, no_cache: bool = False) -> bool:
        """Whether a read-shaped call may be served from the local maildir.

        The single gate shared by :meth:`fetch_email` and
        :meth:`fetch_emails`, mirroring the search policy: the block is
        opted into the local cache (a ``local_cache`` backend and a
        ``maildir``), ``no_cache`` is not set, and the index passes
        :meth:`MuBackend.is_eligible` (mu present, index present and
        within the staleness window).  A stale index sends reads to
        IMAP so flags reflect the server rather than the last sync.

        Args:
            no_cache: When ``True``, the cache is declined unconditionally.

        Returns:
            ``True`` when the maildir may serve this call.
        """
        if no_cache or self.local_cache is None or not self.block.maildir:
            return False
        return self.local_cache.is_eligible(self.block).eligible

    def fetch_email(
        self, uid: int, folder: str = "INBOX", no_cache: bool = False
    ) -> Optional[Email]:
        """Fetch a single email by UID.

        When the block is opted into the local cache and the index is
        eligible (see :meth:`_disk_cache_eligible`), the call is served
        from the local synced file at
        ``<maildir>/<folder>/{cur,new}/*,U=<uid>,*`` and IMAP is not
        contacted; on disk miss (file not yet synced) the call falls
        back to IMAP.  When the index is stale, ``no_cache`` is set, or
        the block is not opted in, the call goes to live IMAP.  Redact
        policy is applied to the resulting ``Email`` regardless of source.

        Args:
            uid: Email UID
            folder: Folder to fetch from
            no_cache: When ``True``, bypass the local cache and read from
                live IMAP.

        Returns:
            Email object or None if not found. When this block has a
            ``redact_policy`` and the policy matches, returns a
            placeholder ``Email`` (``redacted_by`` set, sensitive fields
            blanked) rather than ``None``: the agent must know the
            message exists in order for the privacy posture to be
            honest.

        Raises:
            ConnectionError: If not connected and connection fails
        """
        if self._disk_cache_eligible(no_cache):
            disk_email = self._fetch_email_disk(uid, folder)
            if disk_email is not None:
                # Disk files carry no INTERNALDATE; the Date-header date
                # judges the bound, as on the mu-index path.
                self._refuse_read_after_bound(disk_email.date)
                return self._apply_redact(disk_email)

        self.ensure_connected()
        self.select_folder(folder, readonly=True)

        # Fetch message data with BODY.PEEK[] to get all parts including headers
        # Using BODY.PEEK[] instead of RFC822 to avoid setting the \Seen flag
        result = self._client_or_raise().fetch(
            [uid], self._bound_fetch_items(["BODY.PEEK[]", "FLAGS"])
        )

        if not result or uid not in result:
            logger.warning(f"Message with UID {uid} not found in folder {folder}")
            return None

        # Parse message
        message_data = result[uid]
        raw_message = message_data[b"BODY[]"]
        flags = message_data[b"FLAGS"]
        internal_date = message_data.get(b"INTERNALDATE")

        str_flags = [f.decode("utf-8") if isinstance(f, bytes) else f for f in flags]
        email_obj = self._email_from_bytes(raw_message, uid, folder, str_flags)

        self._refuse_read_after_bound(
            internal_date if internal_date is not None else email_obj.date
        )

        self._update_activity()
        return self._apply_redact(email_obj)

    def _fetch_email_disk(self, uid: int, folder: str) -> Optional[Email]:
        """Read a message from the mbsync-synced maildir, if present.

        Searches ``<block.maildir>/<folder>/{cur,new}/`` for a file
        whose name encodes the IMAP UID via the mbsync ``,U=<uid>,``
        segment.  Returns ``None`` when the file is absent (the caller
        falls back to IMAP).

        Args:
            uid: IMAP UID to resolve.
            folder: IMAP folder, used as the maildir subdirectory name.

        Returns:
            An :class:`Email` built from the on-disk bytes, with
            ``flags`` derived from the maildir suffix; ``None`` when no
            matching file is found.
        """
        if not self.block.maildir:
            return None
        # Escape the folder segment: maildir names carry glob
        # metacharacters (e.g. ``[Gmail]/Sent Mail``) that would
        # otherwise be read as character classes and never match.
        folder_glob = glob.escape(folder)
        for subdir in ("cur", "new"):
            pattern = os.path.join(
                self.block.maildir, folder_glob, subdir, f"*,U={uid},*"
            )
            matches = glob.glob(pattern)
            if not matches:
                continue
            path = matches[0]
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
            except OSError as e:
                logger.warning(
                    f"Could not read maildir file {path!r}: {e}; falling back to IMAP"
                )
                return None
            return self._email_from_bytes(
                raw, uid, folder, self._parse_maildir_flags(path)
            )
        return None

    @staticmethod
    def _parse_maildir_flags(path: str) -> List[str]:
        """Decode the ``:2,XYZ`` flag suffix of a maildir filename."""
        name = os.path.basename(path)
        marker = name.find(":2,")
        if marker == -1:
            return []
        return [
            _MAILDIR_FLAG_CHARS[ch]
            for ch in name[marker + 3 :]
            if ch in _MAILDIR_FLAG_CHARS
        ]

    def _apply_redact(self, email_obj: Email) -> Email:
        """Run the per-block redact policy and replace if matched."""
        policy = self.block.redact_policy
        if policy is not None and policy(email_obj):
            return email_obj.redact("redacted")
        return email_obj

    def fetch_emails(
        self,
        uids: List[int],
        folder: str = "INBOX",
        limit: Optional[int] = None,
        no_cache: bool = False,
    ) -> Dict[int, Email]:
        """Fetch multiple emails by UIDs.

        When the block is opted into the local cache and the index is
        eligible (see :meth:`_disk_cache_eligible`), each UID is resolved
        from the local synced file first; UIDs whose file is not yet on
        disk are fetched in a single IMAP batch.  When the index is
        stale, ``no_cache`` is set, or the block is not opted in, every
        UID is fetched from live IMAP.

        Args:
            uids: List of email UIDs
            folder: Folder to fetch from
            limit: Maximum number of emails to fetch
            no_cache: When ``True``, bypass the local cache and read from
                live IMAP.

        Returns:
            Dictionary mapping UIDs to Email objects

        Raises:
            ConnectionError: If not connected and connection fails
        """
        if limit is not None and limit > 0:
            uids = uids[:limit]
        if not uids:
            return {}

        emails: Dict[int, Email] = {}
        missing: List[int] = []
        if self._disk_cache_eligible(no_cache):
            for uid in uids:
                disk_email = self._fetch_email_disk(uid, folder)
                if disk_email is not None:
                    if self._after_bound(disk_email.date):
                        # Dated after WORLD_AS_OF: dropped from batch
                        # assembly (direct reads refuse instead).
                        continue
                    emails[uid] = self._apply_redact(disk_email)
                else:
                    missing.append(uid)
        else:
            missing = list(uids)

        if not missing:
            return emails

        self.ensure_connected()
        self.select_folder(folder, readonly=True)
        result = self._client_or_raise().fetch(
            missing, self._bound_fetch_items(["BODY.PEEK[]", "FLAGS"])
        )

        for uid, message_data in result.items():
            raw_message = message_data[b"BODY[]"]
            flags = message_data[b"FLAGS"]
            internal_date = message_data.get(b"INTERNALDATE")
            str_flags = [
                f.decode("utf-8") if isinstance(f, bytes) else f for f in flags
            ]
            email_obj = self._email_from_bytes(raw_message, uid, folder, str_flags)
            if self._after_bound(
                internal_date if internal_date is not None else email_obj.date
            ):
                continue
            emails[uid] = self._apply_redact(email_obj)

        self._update_activity()
        return emails

    def fetch_thread(self, uid: int, folder: str = "INBOX") -> List[Email]:
        """Fetch all emails in a thread.

        This method retrieves the initial email identified by the UID, and then
        searches for all related emails that belong to the same thread using
        Message-ID, In-Reply-To, References headers, and Subject matching as a fallback.

        Args:
            uid: UID of any email in the thread
            folder: Folder to fetch from

        Returns:
            List of Email objects in the thread, sorted chronologically

        Raises:
            ConnectionError: If not connected and connection fails
            ValueError: If the initial email cannot be found
        """
        self.ensure_connected()
        self.select_folder(folder, readonly=True)

        # Fetch the initial email
        initial_email = self.fetch_email(uid, folder)
        if not initial_email:
            raise ValueError(
                f"Initial email with UID {uid} not found in folder {folder}"
            )

        # Get thread identifiers from the initial email
        message_id = initial_email.headers.get("Message-ID", "")
        subject = initial_email.subject

        # Strip "Re:", "Fwd:", etc. from the subject for better matching
        clean_subject = re.sub(
            r"^(?:Re|Fwd|Fw|FWD|RE|FW):\s*", "", subject, flags=re.IGNORECASE
        )

        # Set to store all UIDs that belong to the thread
        thread_uids = {uid}

        # Search for emails with this Message-ID in the References or In-Reply-To headers
        if message_id:
            # Look for emails that reference this message ID
            references_query = f'HEADER References "{message_id}"'
            try:
                references_results = self.search(references_query, folder)
                thread_uids.update(references_results)
            except Exception as e:
                logger.warning(f"Error searching for References: {e}")

            # Look for direct replies to this message
            inreplyto_query = f'HEADER In-Reply-To "{message_id}"'
            try:
                inreplyto_results = self.search(inreplyto_query, folder)
                thread_uids.update(inreplyto_results)
            except Exception as e:
                logger.warning(f"Error searching for In-Reply-To: {e}")

            # If the initial email has References or In-Reply-To, fetch those messages too
            initial_references = initial_email.headers.get("References", "")
            initial_inreplyto = initial_email.headers.get("In-Reply-To", "")

            # Extract all message IDs from the References header
            if initial_references:
                for ref_id in re.findall(r"<[^>]+>", initial_references):
                    query = f'HEADER Message-ID "{ref_id}"'
                    try:
                        results = self.search(query, folder)
                        thread_uids.update(results)
                    except Exception as e:
                        logger.warning(
                            f"Error searching for Referenced message {ref_id}: {e}"
                        )

            # Look for the message that this is a reply to
            if initial_inreplyto:
                query = f'HEADER Message-ID "{initial_inreplyto}"'
                try:
                    results = self.search(query, folder)
                    thread_uids.update(results)
                except Exception as e:
                    logger.warning(f"Error searching for In-Reply-To message: {e}")

        # If we still have only the initial email or a small thread, try subject-based matching
        if len(thread_uids) <= 2 and clean_subject:
            # Look for emails with the same or related subject (Re: Subject)
            # This is a fallback for email clients that don't properly use References/In-Reply-To
            subject_query = f'SUBJECT "{clean_subject}"'
            try:
                subject_results = self.search(subject_query, folder)

                # Filter out emails that are unlikely to be part of the thread
                # For example, avoid including all emails with a common subject like "Hello"
                if len(subject_results) < 20:  # Set a reasonable limit
                    thread_uids.update(subject_results)
                else:
                    # If there are too many results, try a more strict approach
                    # Look for exact subject match or common Re: pattern
                    strict_matches = []
                    strict_subjects = [
                        clean_subject,
                        f"Re: {clean_subject}",
                        f"RE: {clean_subject}",
                        f"Fwd: {clean_subject}",
                        f"FWD: {clean_subject}",
                        f"Fw: {clean_subject}",
                        f"FW: {clean_subject}",
                    ]

                    # Fetch subjects for all candidate emails
                    candidate_emails = self.fetch_emails(subject_results, folder)
                    for candidate_uid, candidate_email in candidate_emails.items():
                        if candidate_email.subject in strict_subjects:
                            strict_matches.append(candidate_uid)

                    thread_uids.update(strict_matches)
            except Exception as e:
                logger.warning(f"Error searching by subject: {e}")

        # Fetch all discovered thread emails
        thread_emails = self.fetch_emails(list(thread_uids), folder)

        # Sort emails by date (chronologically)
        sorted_emails = sorted(
            thread_emails.values(), key=lambda e: e.date if e.date else datetime.min
        )

        self._update_activity()
        return sorted_emails

    def has_capability(self, cap: str) -> bool:
        """Whether the server advertises the given capability (e.g. "MOVE")."""
        self.ensure_connected()
        return bool(self._client_or_raise().has_capability(cap))

    def _expunge_uids(self, uids: List[int]) -> None:
        """Expunge only *uids* when the server allows it, else folder-wide.

        With UIDPLUS (RFC 4315) this issues UID EXPUNGE for exactly the
        given messages. imapclient's ``expunge(messages)`` sends UID
        EXPUNGE only under ``use_uid=True`` — courier never changes that
        constructor default, so the UIDs here are message UIDs as required.
        """
        client = self._client_or_raise()
        if self.has_capability("UIDPLUS"):
            client.expunge(uids)
            return
        # ponytail: bare EXPUNGE purges every \Deleted message in the
        # folder, not just ours. Mainstream servers all advertise UIDPLUS,
        # so this leg only fires on legacy/appliance servers.
        logger.warning(
            "Server lacks UIDPLUS; falling back to folder-wide EXPUNGE, "
            "which purges every \\Deleted message in the folder"
        )
        client.expunge()

    def mark_email(
        self,
        uid: Union[int, Sequence[int]],
        folder: str,
        flag: str,
        value: bool = True,
    ) -> None:
        """Mark one or more emails with a flag.

        Args:
            uid: Email UID or sequence of UIDs
            folder: Folder containing the email(s)
            flag: Flag to set or remove
            value: True to set, False to remove

        Raises:
            ConnectionError: If not connected and connection fails
            TransientError: On connection-layer failure (retryable)
            PermanentError: When the server answers NO/BAD
        """
        uids = _as_uid_list(uid)
        self.ensure_connected()
        self.select_folder(folder)

        try:
            client = self._client_or_raise()
            if value:
                client.add_flags(uids, flag)
                logger.debug(f"Added flag {flag} to messages {uids}")
            else:
                client.remove_flags(uids, flag)
                logger.debug(f"Removed flag {flag} from messages {uids}")
        except Exception as e:
            logger.error(f"Failed to mark email: {e}")
            raise as_courier_error(e) from e
        self._update_activity()

    def move_email(
        self,
        uid: Union[int, Sequence[int]],
        source_folder: str,
        target_folder: str,
    ) -> None:
        """Move one or more emails to another folder.

        Uses the server's MOVE capability (RFC 6851) when advertised;
        otherwise falls back to copy + \\Deleted + expunge (UID EXPUNGE
        under UIDPLUS, folder-wide as a last resort).

        Args:
            uid: Email UID or sequence of UIDs
            source_folder: Source folder
            target_folder: Target folder

        Raises:
            ConnectionError: If not connected and connection fails
            ValueError: If folder is not allowed
            TransientError: On connection-layer failure (retryable)
            PermanentError: When the server answers NO/BAD
        """
        uids = _as_uid_list(uid)
        self.ensure_connected()

        # Check if folders are allowed
        if self.allowed_folders is not None:
            if source_folder not in self.allowed_folders:
                raise ValueError(f"Source folder '{source_folder}' is not allowed")
            if target_folder not in self.allowed_folders:
                raise ValueError(f"Target folder '{target_folder}' is not allowed")

        # Select source folder
        self.select_folder(source_folder)

        try:
            client = self._client_or_raise()
            if self.has_capability("MOVE"):
                client.move(uids, target_folder)
            else:
                client.copy(uids, target_folder)
                client.add_flags(uids, r"\Deleted")
                self._expunge_uids(uids)
            logger.debug(
                f"Moved messages {uids} from {source_folder} to {target_folder}"
            )
        except Exception as e:
            logger.error(f"Failed to move email: {e}")
            raise as_courier_error(e) from e
        self._update_activity()

    # Trash/Bin folder names to try when the server does not advertise the
    # \Trash SPECIAL-USE role. Gmail localises the Bin: en-GB/en-AU accounts
    # expose [Gmail]/Bin, en-US accounts [Gmail]/Trash.
    _TRASH_FALLBACK_NAMES = ("[Gmail]/Bin", "[Gmail]/Trash", "Trash")

    def resolve_trash_folder(self) -> Optional[str]:
        """Return the server's Trash/Bin folder, or None if none is found.

        Prefers the RFC 6154 SPECIAL-USE ``\\Trash`` role; falls back to
        common Bin/Trash folder names present in the folder list.

        Returns:
            The Trash folder name, or None when neither the SPECIAL-USE role
            nor a known fallback name is present.
        """
        special = self.find_special_use_folder(b"\\Trash")
        if special:
            return special
        if not self.folder_cache:
            self.list_folders()
        for name in self._TRASH_FALLBACK_NAMES:
            if name in self.folder_cache:
                return name
        return None

    def trash_email(self, uid: Union[int, Sequence[int]], folder: str) -> str:
        """Move one or more emails to the server's Trash/Bin.

        The recommended removal path. A plain EXPUNGE in the source folder
        does not delete the message on Gmail (it only removes the folder
        label, leaving the message in All Mail); moving it to the Trash/Bin
        is what a mail client's "delete" actually does, and the server purges
        the Bin after its retention window. A message already in the Trash is
        expunged in place.

        Args:
            uid: Email UID or sequence of UIDs
            folder: Folder containing the email(s)

        Returns:
            The resolved Trash/Bin folder name.

        Raises:
            ConnectionError: If not connected and connection fails
            FolderNotFound: If no Trash/Bin folder can be resolved
            TransientError: On connection-layer failure (retryable)
            PermanentError: When the server answers NO/BAD
        """
        self.ensure_connected()
        trash = self.resolve_trash_folder()
        if trash is None:
            raise FolderNotFound(
                "No Trash/Bin folder found on the server (no \\Trash "
                "SPECIAL-USE and no [Gmail]/Bin, [Gmail]/Trash, or Trash "
                "folder). Use `move -t <folder>` to a known folder, or "
                "`delete` to expunge in place."
            )
        if trash == folder:
            self.delete_email(uid, folder)
        else:
            self.move_email(uid, folder, trash)
        return trash

    def delete_email(self, uid: Union[int, Sequence[int]], folder: str) -> None:
        """Delete one or more emails (\\Deleted + expunge, in place).

        Args:
            uid: Email UID or sequence of UIDs
            folder: Folder containing the email(s)

        Raises:
            ConnectionError: If not connected and connection fails
            TransientError: On connection-layer failure (retryable)
            PermanentError: When the server answers NO/BAD
        """
        uids = _as_uid_list(uid)
        self.ensure_connected()
        self.select_folder(folder)

        try:
            client = self._client_or_raise()
            client.add_flags(uids, r"\Deleted")
            self._expunge_uids(uids)
            logger.debug(f"Deleted messages {uids} from {folder}")
        except Exception as e:
            logger.error(f"Failed to delete email: {e}")
            raise as_courier_error(e) from e
        self._update_activity()

    def process_email_action(
        self,
        uid: int,
        folder: str,
        action: str,
        target_folder: Optional[str] = None,
    ) -> str:
        """Execute a high-level email action by name.

        Args:
            uid: Email UID
            folder: Folder containing the email
            action: One of move, read, unread, flag, unflag, trash, delete
            target_folder: Required when *action* is ``move``

        Returns:
            Human-readable result message

        Raises:
            ValueError: If *action* is unknown or *target_folder* missing for move
        """
        action_l = action.lower()
        if action_l == "move":
            if not target_folder:
                raise ValueError("target_folder is required for move action")
            self.move_email(uid, folder, target_folder)
            return f"Email moved from {folder} to {target_folder}"
        elif action_l == "read":
            self.mark_email(uid, folder, r"\Seen", True)
            return "Email marked as read"
        elif action_l == "unread":
            self.mark_email(uid, folder, r"\Seen", False)
            return "Email marked as unread"
        elif action_l == "flag":
            self.mark_email(uid, folder, r"\Flagged", True)
            return "Email flagged"
        elif action_l == "unflag":
            self.mark_email(uid, folder, r"\Flagged", False)
            return "Email unflagged"
        elif action_l == "trash":
            self.trash_email(uid, folder)
            return "Email trashed"
        elif action_l == "delete":
            self.delete_email(uid, folder)
            return "Email deleted"
        else:
            raise ValueError(
                f"Unknown action '{action}'. "
                "Valid: move, read, unread, flag, unflag, trash, delete"
            )

    def resolve_sent_folder(self, configured: Optional[str] = None) -> Optional[str]:
        """Resolve the FCC target folder, verifying it exists on the server.

        Used pre-send so the caller can refuse to open SMTP when the FCC
        target is bogus, instead of sending and then losing the local
        copy.

        When ``configured`` is given (from ``identity.fcc`` or
        ``--sent-folder``), require that exact folder to exist; do not
        fall back. Otherwise prefer SPECIAL-USE ``\\Sent`` (RFC 6154);
        failing that, walk ``SENT_FOLDER_CANDIDATES`` (Dovecot-prefixed
        names first because bare ``Sent`` is rejected by Dovecot's default
        namespace).

        Args:
            configured: A user-pinned folder name. ``None`` means
                auto-discover.

        Returns:
            The folder name to APPEND to (with the case the server
            reports, so a configured "sent" matches a server "Sent"), or
            ``None`` when no candidate matches. The caller distinguishes
            the two failure modes via whether ``configured`` was set.
        """
        self.ensure_connected()
        folders = self.list_folders(refresh=True)
        folders_by_lower = {f.lower(): f for f in folders}

        if configured is not None:
            return folders_by_lower.get(configured.lower())

        special = self.find_special_use_folder(b"\\Sent")
        if special is not None:
            return special

        for candidate in SENT_FOLDER_CANDIDATES:
            match = folders_by_lower.get(candidate.lower())
            if match is not None:
                return match

        return None

    # Standard drafts folder names, checked case-insensitively when the
    # server does not advertise the \Drafts SPECIAL-USE role.
    _DRAFTS_FALLBACK_NAMES = (
        "Drafts",
        "Draft",
        "Brouillons",
        "Borradores",
        "Entwürfe",
    )

    def resolve_drafts_folder(self) -> str:
        """Resolve the drafts folder for the current server.

        Prefers the RFC 6154 SPECIAL-USE ``\\Drafts`` role (the same
        machinery as sent/trash resolution); falls back to Gmail's
        ``*/Drafts`` naming, then common localized names, then INBOX.

        Returns:
            The name of the drafts folder, or "INBOX" as a last resort.
        """
        self.ensure_connected()
        folders = self.list_folders(refresh=True)

        special = self.find_special_use_folder(b"\\Drafts")
        if special is not None:
            logger.debug(f"Using SPECIAL-USE drafts folder: {special}")
            return special

        # Check for Gmail's special folders structure
        if self.block.host and "gmail" in self.block.host.lower():
            gmail_drafts = [f for f in folders if f.lower().endswith("/drafts")]
            if gmail_drafts:
                logger.debug(f"Using Gmail drafts folder: {gmail_drafts[0]}")
                return gmail_drafts[0]

        # Look for standard drafts folder names (case-insensitive)
        for folder in folders:
            if folder.lower() in [n.lower() for n in self._DRAFTS_FALLBACK_NAMES]:
                logger.debug(f"Using drafts folder: {folder}")
                return folder

        # Fallback to INBOX if no drafts folder found
        logger.warning("No drafts folder found, using INBOX as fallback")
        return "INBOX"

    @staticmethod
    def _parse_append_response(response: Any, folder: str) -> AppendResult:
        """Extract APPENDUID (uidvalidity, uid) from an APPEND response."""
        if isinstance(response, bytes):
            match = _APPENDUID_RE.search(response)
            if match:
                result = AppendResult(
                    uid=int(match.group(2)), uidvalidity=int(match.group(1))
                )
                logger.debug(
                    f"Message appended to {folder} with UID {result.uid} "
                    f"(UIDVALIDITY {result.uidvalidity})"
                )
                return result
        logger.warning(f"Could not extract UID from append response: {response}")
        return AppendResult(uid=None, uidvalidity=None)

    def folder_status(self, folder: str) -> Dict[str, int]:
        """UIDVALIDITY, UIDNEXT, and MESSAGES for a folder (no SELECT).

        Args:
            folder: Folder to query.

        Returns:
            Dict with str keys "UIDVALIDITY", "UIDNEXT", "MESSAGES".

        Raises:
            ConnectionError: If not connected and connection fails
            TransientError: On connection-layer failure (retryable)
            PermanentError: When the server answers NO/BAD
        """
        self.ensure_connected()
        try:
            raw = self._client_or_raise().folder_status(
                folder, ["UIDVALIDITY", "UIDNEXT", "MESSAGES"]
            )
        except Exception as e:
            logger.error(f"folder_status failed for {folder}: {e}")
            raise as_courier_error(e) from e
        self._update_activity()
        return {
            (k.decode("ascii") if isinstance(k, bytes) else str(k)): int(v)
            for k, v in raw.items()
        }

    def save_draft_mime(self, message: Any) -> AppendResult:
        """Save a MIME message as a draft.

        Args:
            message: email.message.Message object to save as draft

        Returns:
            AppendResult with the draft's UID/UIDVALIDITY (fields are None
            when the server response carries no APPENDUID).

        Raises:
            ConnectionError: If not connected and connection fails
            TransientError: On connection-layer failure (retryable)
            PermanentError: When the server answers NO/BAD
        """
        self.ensure_connected()

        # Get the drafts folder
        drafts_folder = self.resolve_drafts_folder()

        try:
            # Convert message to bytes if it's not already
            if hasattr(message, "as_bytes"):
                message_bytes = message.as_bytes()
            else:
                message_bytes = message.as_string().encode("utf-8")

            # Save the draft with Draft flag
            response = self._client_or_raise().append(
                drafts_folder, message_bytes, flags=(r"\Draft",)
            )
        except Exception as e:
            logger.error(f"Failed to save draft: {e}")
            raise as_courier_error(e) from e

        result = self._parse_append_response(response, drafts_folder)
        self._update_activity()
        return result

    def fetch_raw(
        self,
        uid: int,
        folder: str = "INBOX",
    ) -> Optional[Dict[str, Any]]:
        """Fetch raw RFC 822 bytes, flags, and INTERNALDATE for a message.

        Args:
            uid: Email UID
            folder: Folder containing the email

        Returns:
            Dict with keys 'raw' (bytes), 'flags' (tuple), 'date' (datetime),
            'subject' (str) or None if not found.
        """
        self.ensure_connected()
        self.select_folder(folder, readonly=True)

        result = self._client_or_raise().fetch(
            [uid], ["BODY.PEEK[]", "FLAGS", "INTERNALDATE"]
        )

        if not result or uid not in result:
            logger.warning(f"Message with UID {uid} not found in folder {folder}")
            return None

        data = result[uid]
        raw_message = data[b"BODY[]"]
        flags = data[b"FLAGS"]
        internal_date = data.get(b"INTERNALDATE")

        self._refuse_read_after_bound(
            internal_date if isinstance(internal_date, datetime) else None
        )

        # Extract subject for logging/display
        msg = email.message_from_bytes(raw_message)
        subject = msg.get("Subject", "(no subject)")

        self._update_activity()

        policy = self.block.redact_policy
        if policy is not None:
            email_obj = Email.from_message(msg, uid=uid, folder=folder)
            if policy(email_obj):
                redacted = email_obj.redact("redacted")
                return {
                    "raw": b"",
                    "flags": flags,
                    "date": internal_date,
                    "subject": redacted.subject,
                    "redacted_by": redacted.redacted_by,
                }
        return {
            "raw": raw_message,
            "flags": flags,
            "date": internal_date,
            "subject": subject,
        }

    def append_raw(
        self,
        folder: str,
        raw_message: bytes,
        flags: tuple = (),
        msg_time: Optional[datetime] = None,
    ) -> AppendResult:
        """Append raw RFC 822 bytes to a folder.

        Args:
            folder: Target folder.
            raw_message: Complete RFC 822 message as bytes.
            flags: IMAP flags to set (e.g. (r'\\Seen', r'\\Flagged')).
            msg_time: INTERNALDATE for the message. If None, server uses
                current time.

        Returns:
            AppendResult with the new message's UID/UIDVALIDITY (fields
            are None when the server response carries no APPENDUID).

        Raises:
            ConnectionError: If not connected and connection fails
            TransientError: On connection-layer failure (retryable)
            PermanentError: When the server answers NO/BAD
        """
        self.ensure_connected()

        try:
            response = self._client_or_raise().append(
                folder, raw_message, flags=flags, msg_time=msg_time
            )
        except Exception as e:
            logger.error(f"Failed to append message to {folder}: {e}")
            raise as_courier_error(e) from e

        result = self._parse_append_response(response, folder)
        self._update_activity()
        return result

    # Prefixes whose presence triggers the Gmail X-GM-RAW dispatch.  Standard
    # IMAP SEARCH FROM/TO/CC/BCC against Gmail's All Mail empirically does not
    # filter by header content for values that contain "@"/"."; X-GM-RAW
    # evaluates the query the way Gmail's web UI does and produces the expected
    # filter (issue #17).  ``has:`` is here so has:attachment gets Gmail's
    # native exact attachment search instead of the parse-time error the
    # operator raises on backends with no server-side attachment predicate.
    _GMAIL_RAW_TRIGGER_PREFIXES = ("from:", "to:", "cc:", "bcc:", "has:")

    # Matches a msgid:/rfc822msgid: token in an X-GM-RAW query: the leading
    # boundary (start or whitespace) and an optional negation dash are
    # captured so both can be preserved when rewriting to Gmail's spelling.
    _GMAIL_RAW_MSGID_RE = re.compile(
        r"(?P<lead>^|\s)(?P<neg>-?)(?:rfc822msgid|msgid):(?P<val>\S+)",
        re.IGNORECASE,
    )

    def _build_search_spec(self, query: str) -> Union[str, List[Any]]:
        """Translate a user query into IMAP search criteria.

        For Gmail accounts the function returns ``[b"X-GM-RAW", query]`` when
        the query contains a header search prefix, so Gmail evaluates the
        query with web-UI semantics.  All other queries (and the ``imap:``
        raw escape) go through the standard ``parse_query`` emitter.

        Args:
            query: Raw user query string.

        Returns:
            A criteria value suitable for ``imapclient.IMAPClient.search``.

        Raises:
            ValueError: Propagated from ``parse_query`` on malformed queries.
        """
        if self._should_use_gmail_raw(query):
            raw_query = self._canonicalize_gmail_raw(query.strip())
            if self.world_as_of is not None:
                # Gmail's before: accepts epoch seconds — a second-precision
                # Layer 1, tighter than the day-granular SEARCH BEFORE that
                # is also ANDed on. Layer 2 still decides.
                raw_query = f"{raw_query} before:{int(self.world_as_of.timestamp())}"
            return [b"X-GM-RAW", raw_query]
        # Relative terms resolve against the bound when set (wall clock
        # otherwise), so a replayed newer:7d is anchored to the bound.
        return parse_query(query, now=self.world_as_of)

    def _canonicalize_gmail_raw(self, query: str) -> str:
        """Rewrite any msgid token in an ``X-GM-RAW`` query to Gmail syntax.

        Gmail's web query language spells the Message-ID operator
        ``rfc822msgid:`` and expects a bare id (no angle brackets).  A mixed
        query such as ``from:alice msgid:<x@h>`` routes through ``X-GM-RAW``
        because of the ``from:`` prefix, so its ``msgid:``/``rfc822msgid:``
        token must be rewritten to ``rfc822msgid:`` plus the id stripped of
        its surrounding ``<>``.  A leading negation dash is preserved.  A
        query with no such token is returned unchanged, so the raw branch
        stays byte-for-byte verbatim for every other query.

        Args:
            query: The stripped query string destined for ``X-GM-RAW``.

        Returns:
            The query with each msgid token canonicalised to Gmail's
            ``rfc822msgid:`` form; identical to the input when none is present.
        """

        def _rewrite(match: re.Match[str]) -> str:
            val = match.group("val").strip("<>")
            return f"{match.group('lead')}{match.group('neg')}rfc822msgid:{val}"

        return self._GMAIL_RAW_MSGID_RE.sub(_rewrite, query)

    def _should_use_gmail_raw(self, query: str) -> bool:
        """Decide whether a query should be sent via ``X-GM-RAW``.

        Returns ``True`` only when the server is Gmail, the query is not a
        raw IMAP escape, and at least one whitespace-separated token starts
        with a header search prefix (``from:``/``to:``/``cc:``/``bcc:``).
        Pure flag/date queries continue to use standard IMAP search so
        non-Gmail capability assumptions (no ``X-GM-EXT-1`` requirement)
        and existing tests remain unchanged.

        Args:
            query: Raw user query string.

        Returns:
            ``True`` when the Gmail X-GM-RAW dispatch should be used.
        """
        host = (self.block.host or "").lower()
        if "gmail" not in host:
            return False
        stripped = query.strip()
        if stripped.lower().startswith("imap:"):
            return False
        try:
            tokens = shlex.split(stripped)
        except ValueError:
            tokens = stripped.split()
        for tok in tokens:
            tok_lower = tok.lower()
            if any(tok_lower.startswith(p) for p in self._GMAIL_RAW_TRIGGER_PREFIXES):
                return True
            # Also catch negated prefixes like -to:foo.
            if tok_lower.startswith("-") and any(
                tok_lower[1:].startswith(p) for p in self._GMAIL_RAW_TRIGGER_PREFIXES
            ):
                return True
        return False

    def search_emails(
        self,
        query: str,
        folder: Optional[str] = None,
        limit: int = 10,
        no_cache: bool = False,
    ) -> Dict[str, Any]:
        """High-level email search across one or all folders.

        Uses Gmail-style query syntax::

            from:alice subject:invoice is:unread after:2025-03-01
            meeting notes                     # bare words → TEXT search
            imap:OR TEXT foo SUBJECT bar       # raw IMAP passthrough

        On Gmail accounts (host contains ``gmail``), the query is dispatched
        through Gmail's ``X-GM-RAW`` extension whenever it contains a header
        search prefix (``from:``/``to:``/``cc:``/``bcc:``).  Standard IMAP
        ``SEARCH TO foo@example.com`` against Gmail's All Mail folder
        empirically matches every recent message rather than filtering by
        the To header (issue #17); ``X-GM-RAW`` evaluates the query with
        the same semantics as Gmail's web UI and filters correctly.
        Queries without header prefixes (pure flag/date searches) and the
        ``imap:`` raw escape continue to use standard IMAP search.

        When the client was constructed with a ``local_cache`` backend
        and an opted-in ``account_cfg``, eligible calls are served from
        the local mu index instead of IMAP, whether or not a ``folder``
        is given.  ``no_cache`` forces the call to live IMAP.

        Args:
            query: Gmail-style search query string.
            folder: Folder to search (``None`` searches all folders).
            limit: Maximum number of results.
            no_cache: When ``True``, bypass the local cache and query
                live IMAP regardless of eligibility.

        Returns:
            A dict ``{"results": [...], "provenance": {...}}``.  Each
            result carries either an ``uid`` (IMAP) or ``message_id``
            and ``path`` (local cache); both shapes share ``folder``,
            ``from``, ``to``, ``subject``, ``date``, ``flags``, and
            ``has_attachments``.  ``provenance`` carries ``source``
            (``"local"`` or ``"remote"``), ``indexed_at`` (ISO 8601 or
            ``None``), and ``fell_back_reason`` (``None`` or one of
            ``"no_cache"``, ``"mu_missing"``, ``"db_missing"``,
            ``"stale"``, ``"untranslatable"``, ``"exception"``).  When
            the client is bounded, ``provenance`` additionally carries a
            ``world_as_of`` block (see :meth:`_world_as_of_provenance`).

        Raises:
            ValueError: On malformed queries.
        """
        local_results, fell_back_reason = self._try_local_cache_search(
            query, folder, limit, no_cache
        )
        if local_results is not None:
            dropped_after_bound = 0
            if self.world_as_of is not None:
                local_results, dropped_after_bound = self._drop_results_after_bound(
                    local_results
                )
            provenance: Dict[str, Any] = {
                "source": "local",
                "indexed_at": (
                    self.local_cache.index_mtime_iso()
                    if self.local_cache is not None
                    else None
                ),
                "fell_back_reason": None,
            }
            if self.world_as_of is not None:
                provenance["world_as_of"] = self._world_as_of_provenance(
                    dropped_after_bound, date_source="mu_index"
                )
            return {"results": local_results, "provenance": provenance}
        imap_results, dropped_after_bound = self._search_emails_imap(
            query, folder, limit, no_cache
        )
        provenance = {
            "source": "remote",
            "indexed_at": None,
            "fell_back_reason": fell_back_reason,
        }
        if self.world_as_of is not None:
            provenance["world_as_of"] = self._world_as_of_provenance(
                dropped_after_bound, date_source="internaldate"
            )
        return {"results": imap_results, "provenance": provenance}

    def _world_as_of_provenance(
        self, dropped_after_bound: int, date_source: str
    ) -> Dict[str, Any]:
        """The provenance block recording that this result was bounded.

        ``dropped_after_bound`` makes the filtering auditable rather
        than invisible (a replay harness can assert it);
        ``current_state_fields`` names the fields IMAP keeps no history
        for, served as they now stand; ``date_source`` names the date
        the bound compared against (``"internaldate"`` on the IMAP
        path, ``"mu_index"`` when the local cache served the call, whose
        indexed date derives from the Date header).

        Args:
            dropped_after_bound: How many hits Layer 2 dropped.
            date_source: ``"internaldate"`` or ``"mu_index"``.

        Returns:
            The ``world_as_of`` provenance dict.

        Raises:
            ValueError: If called on an unbounded client.
        """
        if self.world_as_of is None:
            raise ValueError("client is not bounded by WORLD_AS_OF")
        return {
            "bound": self.world_as_of.isoformat(),
            "dropped_after_bound": dropped_after_bound,
            "current_state_fields": ["flags", "folder"],
            "date_source": date_source,
        }

    def _drop_results_after_bound(
        self, results: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Exact post-filter over assembled result dicts (Layer 2).

        Used on the local-cache path, whose date field is the indexed
        (Date-header) date rendered as ISO 8601. Undated or unparseable
        dates are kept: an undated message is not "dated after the
        bound".

        Args:
            results: Search result dicts carrying a ``date`` ISO string.

        Returns:
            ``(kept_results, dropped_count)``.
        """
        kept: List[Dict[str, Any]] = []
        dropped = 0
        for result in results:
            iso = result.get("date")
            dt: Optional[datetime] = None
            if isinstance(iso, str):
                try:
                    dt = datetime.fromisoformat(iso)
                except ValueError:
                    dt = None
            if self._after_bound(dt):
                dropped += 1
                continue
            kept.append(result)
        return kept, dropped

    def _try_local_cache_search(
        self,
        query: str,
        folder: Optional[str],
        limit: int,
        no_cache: bool = False,
    ) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """Attempt to serve a search from the local cache.

        Args:
            query: Gmail-style search query string.
            folder: Folder to search (``None`` searches all folders).
            limit: Maximum number of results.
            no_cache: When ``True``, decline the cache and report
                ``"no_cache"`` so the caller goes to live IMAP.

        Returns:
            ``(results, None)`` on a successful local-cache hit, or
            ``(None, reason)`` when the local cache cannot serve the
            call.  ``reason`` is ``None`` when the account is not opted
            into the local cache (the wrapped shape still applies, but
            no fallback is reported); otherwise it is one of the tags
            from the ``provenance.fell_back_reason`` vocabulary.
        """
        # Late import to avoid a circular dependency.
        from courier.local_cache import MuFailure
        from courier.query_parser import UntranslatableQuery

        if self.local_cache is None or not self.block.maildir:
            return None, None
        if no_cache:
            return None, "no_cache"
        eligibility = self.local_cache.is_eligible(self.block)
        if not eligibility.eligible:
            return None, eligibility.reason
        try:
            results = self.local_cache.search(
                self.block, query, limit, folder, world_as_of=self.world_as_of
            )
        except UntranslatableQuery:
            return None, "untranslatable"
        except (MuFailure, ValueError) as e:
            logger.warning(f"Local cache search failed, falling back to IMAP: {e}")
            return None, "exception"
        return results, None

    def _search_emails_imap(
        self,
        query: str,
        folder: Optional[str] = None,
        limit: int = 10,
        no_cache: bool = False,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Run a search against the IMAP server (no local-cache attempt).

        Args:
            query: Gmail-style search query string.
            folder: Folder to search (``None`` searches all folders).
            limit: Maximum number of results.
            no_cache: Forwarded to :meth:`fetch_emails` so message
                bodies are read from live IMAP rather than the maildir
                when the caller forced ``--no-cache``.

        Returns:
            ``(results, dropped_after_bound)``.  ``results`` is a list
            of result dicts sorted by date descending, each with keys:
            ``uid``, ``folder``, ``from``, ``to``, ``subject``,
            ``date``, ``flags``, ``has_attachments``, ``message_id``.
            ``message_id`` matches the field already emitted by the
            local-cache path in ``local_cache.py``.
            ``dropped_after_bound`` counts hits whose INTERNALDATE fell
            after the WORLD_AS_OF bound; the drop happens before the
            limit cut so a limit-truncated page cannot come back
            artificially empty.

        Raises:
            ValueError: On malformed queries.
        """
        search_spec = self._build_search_spec(query)

        if folder:
            folders_to_search = [folder]
        else:
            # Prefer the SPECIAL-USE \All folder when the server advertises one
            # (Gmail's [Gmail]/All Mail, Fastmail's Archive, etc.): one SELECT
            # instead of iterating every folder. Falls back to all selectable.
            all_mail = self.find_special_use_folder(b"\\All")
            if all_mail:
                folders_to_search = [all_mail]
            else:
                # Diagnostic for issue #38: record why the SPECIAL-USE
                # optimization did not fire so the cause can be attributed
                # from journald without needing a live reproduction. The
                # flag universe across the cached LIST response tells us
                # whether the server returned SPECIAL-USE attributes at
                # all, or only on folders we are not interested in.
                flags_seen = sorted(
                    {
                        (
                            f.decode("ascii", "replace")
                            if isinstance(f, bytes)
                            else str(f)
                        )
                        for flags in self.folder_cache.values()
                        for f in flags
                    }
                )
                logger.warning(
                    "iterate-all fallback for search: SPECIAL-USE \\All not "
                    "detected (host=%s, cached_folders=%d, flags_seen=%s)",
                    self.block.host,
                    len(self.folder_cache),
                    flags_seen,
                )
                folders_to_search = self.list_folders()

        # Pass 1: collect (uid, folder, date) using a lightweight fetch.
        # The exact WORLD_AS_OF cut happens here, on the fetched
        # INTERNALDATE and before the limit cut: SEARCH BEFORE (Layer 1)
        # is day-granular and may leak same-day post-bound messages.
        candidates: List[tuple] = []
        dropped_after_bound = 0
        for current_folder in folders_to_search:
            try:
                uids = self.search(search_spec, folder=current_folder)
                if not uids:
                    continue
                self.select_folder(current_folder, readonly=True)
                date_data = self._client_or_raise().fetch(uids, ["INTERNALDATE"])
                for uid, data in date_data.items():
                    dt = data.get(b"INTERNALDATE")
                    if self._after_bound(dt):
                        dropped_after_bound += 1
                        continue
                    iso = dt.isoformat() if dt else "0"
                    candidates.append((iso, uid, current_folder))
            except Exception as e:
                logger.warning(
                    f"{self.block.label} Error searching folder {current_folder}: {e}"
                )

        # Sort globally by date and keep only the top `limit`
        candidates.sort(key=lambda x: x[0], reverse=True)
        top = candidates[:limit]

        # Pass 2: full-fetch only the messages we will return
        # Group by folder to minimise SELECT commands
        by_folder: Dict[str, List[int]] = {}
        for _date, uid, fldr in top:
            by_folder.setdefault(fldr, []).append(uid)

        results: List[Dict[str, Any]] = []
        for current_folder, uid_list in by_folder.items():
            try:
                emails = self.fetch_emails(
                    uid_list, folder=current_folder, no_cache=no_cache
                )
                for email_obj in emails.values():
                    results.append(
                        email_obj.as_search_result(
                            folder=current_folder,
                            flags=email_obj.flags,
                            date_iso=(
                                email_obj.date.astimezone().isoformat()
                                if email_obj.date
                                else None
                            ),
                            has_attachments=len(email_obj.attachments) > 0,
                        )
                    )
            except Exception as e:
                logger.warning(f"Error fetching from folder {current_folder}: {e}")

        results.sort(key=lambda x: x.get("date") or "0", reverse=True)
        return results, dropped_after_bound


def copy_email_between_imap_blocks(
    source: "ImapClient",
    dest: "ImapClient",
    uid: int,
    from_folder: str,
    to_folder: str = "INBOX",
    move: bool = False,
    preserve_flags: bool = False,
) -> Dict[str, Any]:
    """Copy (or move) an email from one IMAP account to another.

    Fetches the raw RFC 822 message from *source*, applies optional flag
    filtering, and APPENDs it to *dest*.  The original INTERNALDATE is
    always preserved.  If *move* is True the source message is deleted
    after a successful append; a failed source-delete after a successful
    append is reported in the result (``moved`` False, ``error`` set)
    rather than propagated, so the caller knows the copy itself landed.

    Args:
        source: IMAP client connected to the source account.
        dest: IMAP client connected to the destination account.
        uid: UID of the email in the source folder.
        from_folder: Folder in the source account containing the email.
        to_folder: Destination folder (default: INBOX).
        move: If True, delete the email from the source after copy.
        preserve_flags: If True, copy original flags (excluding \\Recent)
            to the destination.  If False, no flags are set.

    Returns:
        Dict with keys: success (bool), subject (str), new_uid (int | None),
        moved (bool), error (str | None).
    """
    raw_data = source.fetch_raw(uid, from_folder)
    if raw_data is None:
        return {
            "success": False,
            "subject": "",
            "new_uid": None,
            "moved": False,
            "error": f"UID {uid} not found in {from_folder}",
        }

    flags: tuple = ()
    if preserve_flags:
        raw_flags = raw_data["flags"]
        flags = tuple(
            f.decode("utf-8") if isinstance(f, bytes) else f
            for f in raw_flags
            if f not in (b"\\Recent", "\\Recent")
        )

    append_result = dest.append_raw(
        to_folder,
        raw_data["raw"],
        flags=flags,
        msg_time=raw_data["date"],
    )

    moved = False
    error: Optional[str] = None
    if move:
        try:
            source.delete_email(uid, from_folder)
            moved = True
        except Exception as e:
            logger.warning(f"Copied but failed to delete source message: {e}")
            error = f"copied, but failed to delete source message: {e}"

    return {
        "success": True,
        "subject": raw_data["subject"],
        "new_uid": append_result.uid,
        "moved": moved,
        "error": error,
    }
