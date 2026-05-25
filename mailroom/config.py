"""Configuration handling for Mailroom.

The on-disk schema has three named-entity tables at the top level:

    [imap.NAME]       one IMAP mailbox
    [smtp.NAME]       one SMTP endpoint
    [identity.NAME]   one send identity, with `imap = "..."` naming the
                      [imap.NAME] block it routes through

An [imap.NAME] block with no [identity.*] pointing at it is read-only:
drafting and reading work, sending is rejected at send time with
``SendDisabled``. Identities are first-class; their address is the From
header used on transmit, and the SMTP block resolves via the chain
identity.smtp -> imap_block.default_smtp -> lone [smtp.NAME].
"""

import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from dotenv import load_dotenv

# Display-name characters mailroom refuses to accept on configured
# identities. Two groups, both rejected at config-load time:
#
#   1. C0 control characters and DEL. Header-injection vector (LF/CR) and
#      MIME-composition hazards (NUL).
#   2. RFC 5322 specials that require quoting in a display-name:
#      ``, ( ) < > [ ] : ; @ \ "``. ``email.utils.formataddr`` would quote
#      these correctly on emit, but the failure mode when something
#      downstream re-parses the header without honouring the quotes is
#      severe: a comma in a display-name has been observed to reach an
#      MTA as an address-list separator, splitting one address into two
#      and triggering ``User name is missing`` at MAIL FROM. The cost of
#      using a quote-free name is small; the risk of a quoted name
#      surviving end to end across an unfamiliar mail path is not.
#
# Atext characters (alphanumeric, plus ``! # $ % & ' * + - / = ? ^ _ ` {
# | } ~``), spaces, and dots remain acceptable.
_INVALID_DISPLAY_NAME_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f,()<>\[\]:;@\\\"]")


def validate_display_name(name: str, where: str) -> None:
    """Reject display names containing characters mailroom won't carry.

    The same check is applied at config-load time on ``[identity.NAME]``
    blocks and at CLI-parse time on ``compose --send --smtp ... --name``.
    Centralising it here keeps the rejection set and the error message
    in one place; see the comment near ``_INVALID_DISPLAY_NAME_RE`` for
    the rationale.

    Args:
        name: Candidate display name.
        where: Caller-supplied prefix (e.g. ``[identity.alice]`` or
            ``--name``) used to anchor the error message.

    Raises:
        ValueError: If the name contains a forbidden character.
    """
    bad = _INVALID_DISPLAY_NAME_RE.search(name)
    if bad is not None:
        raise ValueError(
            f"{where}: 'name' contains a character that requires RFC "
            f"5322 quoting or that breaks MIME composition: "
            f"{bad.group(0)!r}. Use a quote-free display name (no "
            f"commas, parens, angle brackets, square brackets, "
            f"colons, semicolons, at-signs, backslashes, or double "
            f"quotes), or omit the 'name' field to send From the "
            f"bare address."
        )


def smtp_has_own_creds(smtp: "SmtpConfig") -> bool:
    """Return True when the SMTP block carries its own username and password.

    Used to gate operations that have no IMAP block in scope (the
    ``--send --smtp NAME --from EMAIL`` form), where credential
    inheritance has nothing to inherit from.
    """
    return bool(smtp.username) and bool(smtp.password)


logger = logging.getLogger(__name__)

# Load environment variables from .env file if it exists
load_dotenv()

# Hosts where a credential-less SMTP block can safely inherit per-block creds.
# (smtp.gmail.com accepts each Gmail mailbox's own app password, so sharing one
# credential-less SMTP across multiple [imap.*] blocks is the intended pattern.)
_INHERITANCE_SAFE_SMTP_HOSTS = ("smtp.gmail.com", "smtp.googlemail.com")


@dataclass
class OAuth2Config:
    """OAuth2 configuration for IMAP authentication."""

    client_id: str
    client_secret: str
    refresh_token: Optional[str] = None
    access_token: Optional[str] = None
    token_expiry: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["OAuth2Config"]:
        """Create OAuth2 configuration from a flat [imap.NAME] dictionary.

        Looks for client_id/client_secret directly in the dict, falling
        back to environment variables.
        """
        client_id = data.get("client_id") or os.environ.get("GMAIL_CLIENT_ID")
        client_secret = data.get("client_secret") or os.environ.get(
            "GMAIL_CLIENT_SECRET"
        )
        refresh_token = data.get("refresh_token") or os.environ.get(
            "GMAIL_REFRESH_TOKEN"
        )

        if not client_id or not client_secret:
            return None

        return cls(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            access_token=data.get("access_token"),
            token_expiry=data.get("token_expiry"),
        )


@dataclass
class LocalCacheConfig:
    """Configuration for the optional local-cache search backend.

    The presence of this block plus a per-block ``maildir`` opts an
    [imap.NAME] block into local-cache search.  Currently only mu is
    supported.

    Attributes:
        indexer: Backend identifier; only ``"mu"`` is accepted in v1.
        max_staleness_seconds: Maximum age of the index before the
            backend declines and the call falls back to IMAP.  Default
            4000 (~67 minutes), comfortably above an hourly index cron.
        mu_index: Optional explicit muhome path (the value passed to
            ``mu --muhome=...``).  If unset, the backend discovers it
            from ``mu info store`` on first use.
    """

    indexer: str = "mu"
    max_staleness_seconds: int = 4000
    mu_index: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LocalCacheConfig":
        """Create local-cache configuration from a flat dictionary."""
        return cls(
            indexer=data.get("indexer", "mu"),
            max_staleness_seconds=int(data.get("max_staleness_seconds", 4000)),
            mu_index=data.get("mu_index"),
        )


@dataclass
class SmtpConfig:
    """A named SMTP endpoint, referenced by [imap.*] and [identity.*] by name.

    Two patterns are supported:
        Template (host/port only): credentials inherit from the [imap.NAME]
            block in scope at send time. Right for Gmail/Fastmail where SMTP
            creds equal IMAP creds.
        Concrete (host/port/username/password): credentials live here. Right
            for SES, where one IAM SMTP user serves many From addresses.

    Attributes:
        host: SMTP server hostname.
        port: TCP port. Default 587 (STARTTLS); 465 implies SMTPS.
        username: Optional. When absent, inherits from the [imap.NAME] block in scope.
        password: Optional. When absent, inherits from the [imap.NAME] block in scope.
        save_sent: ``"auto"`` (default; resolves to false on gmail.com hosts
            where the server auto-files outgoing, true elsewhere), or an
            explicit bool override.
        rewrite_msgid_from_response: When true, the SMTP transport rewrites
            the local copy's ``Message-ID:`` header to match the recipient's
            view based on the post-DATA server response (used for SES, which
            rewrites Message-ID in transit). Defaults true when host matches
            an SES SMTP endpoint (``email-smtp.<region>.amazonaws.com``).
    """

    host: str
    port: int = 587
    username: Optional[str] = None
    password: Optional[str] = None
    save_sent: Any = "auto"  # "auto" | True | False
    rewrite_msgid_from_response: bool = False

    @classmethod
    def from_dict(cls, name: str, data: Dict[str, Any]) -> "SmtpConfig":
        """Build an SmtpConfig from a TOML table.

        Args:
            name: SMTP block name (used in error messages).
            data: Raw table from ``[smtp.NAME]``.

        Returns:
            Parsed SmtpConfig with auto-defaulted fields filled in.

        Raises:
            ValueError: When required fields are missing or types mismatch.
        """
        where = f"[smtp.{name}]"
        if not isinstance(data, dict):
            raise ValueError(f"{where}: must be a table")
        host = data.get("host")
        if not isinstance(host, str) or not host:
            raise ValueError(f"{where}: missing required string field 'host'")
        port = data.get("port", 587)
        if not isinstance(port, int):
            raise ValueError(f"{where}: 'port' must be an integer")
        for credfield in ("username", "password"):
            v = data.get(credfield)
            if v is not None and not isinstance(v, str):
                raise ValueError(
                    f"{where}: '{credfield}' must be a string when present"
                )
        save_sent = data.get("save_sent", "auto")
        if save_sent not in ("auto", True, False):
            raise ValueError(
                f"{where}: 'save_sent' must be one of \"auto\", true, false"
            )
        rewrite = data.get("rewrite_msgid_from_response")
        if rewrite is not None and not isinstance(rewrite, bool):
            raise ValueError(f"{where}: 'rewrite_msgid_from_response' must be boolean")
        if rewrite is None:
            # SES SMTP endpoints look like "email-smtp.<region>.amazonaws.com".
            # The Message-ID rewrite target is the unrelated email.amazonses.com,
            # so we match on the SMTP host shape, not the rewrite target.
            rewrite = "email-smtp." in host and host.endswith("amazonaws.com")
        return cls(
            host=host,
            port=port,
            username=data.get("username"),
            password=data.get("password"),
            save_sent=save_sent,
            rewrite_msgid_from_response=rewrite,
        )

    def resolve_save_sent(self) -> bool:
        """Resolve ``"auto"`` save_sent to a concrete bool based on host.

        Returns false for Gmail hosts (server auto-files outgoing into Sent),
        true for everything else (mailroom must FCC manually).
        """
        if self.save_sent == "auto":
            return not (
                self.host.endswith("gmail.com") or self.host.endswith("googlemail.com")
            )
        return bool(self.save_sent)


@dataclass
class Identity:
    """A send identity: one From address served by one [imap.NAME] block.

    Each [identity.NAME] block normally names the [imap.NAME] block it
    routes through via its ``imap`` field. The address is the From
    header used on transmit. A block with no identities pointing at it
    is read-only; sending is rejected at send time with ``SendDisabled``.

    FCC (filing a Sent copy via IMAP APPEND) and BCC are independent: an
    identity may keep a Sent copy and also BCC a list. ``from_dict``
    enforces the copy-retention rule and explains it in the raised error.

    Attributes:
        imap: Name of the [imap.NAME] block this identity belongs to.
            When absent the identity is send-only (no fetch, drafts, or
            reply-to-parent).
        address: The bare email address used in the ``From`` header.
        name: Display name. Empty string for bare-address From.
        smtp: Name of an ``[smtp.NAME]`` block. When None, falls back to
            ``imap_block.default_smtp`` then to the lone SMTP block if
            exactly one is defined.
        fcc: ``None`` files into the block's resolved Sent folder per the
            SMTP host convention; a folder-name string files there
            explicitly; ``False`` disables FCC.
        bcc: Addresses BCC'd on every send (string or list of strings).
    """

    address: str
    imap: Optional[str] = None
    name: str = ""
    smtp: Optional[str] = None
    fcc: Union[bool, str, None] = None
    bcc: Optional[List[str]] = None

    @classmethod
    def from_dict(cls, ident_name: str, data: Dict[str, Any]) -> "Identity":
        """Build an Identity from a TOML table.

        Args:
            ident_name: Identity TOML key (used in error messages).
            data: Raw table from ``[identity.NAME]``.

        Raises:
            ValueError: On missing/invalid fields.
        """
        where = f"[identity.{ident_name}]"
        if not isinstance(data, dict):
            raise ValueError(f"{where}: must be a table")
        bcc_raw = data.get("bcc")
        bcc: Optional[List[str]] = None
        if bcc_raw is not None:
            if isinstance(bcc_raw, str):
                bcc_list = [bcc_raw]
            elif isinstance(bcc_raw, list) and all(isinstance(x, str) for x in bcc_raw):
                bcc_list = list(bcc_raw)
            else:
                raise ValueError(
                    f"{where}: 'bcc' must be a string or a list of strings"
                )
            for entry in bcc_list:
                if "@" not in entry:
                    raise ValueError(
                        f"{where}: 'bcc' entry {entry!r} is not an email address"
                    )
            if not bcc_list:
                raise ValueError(f"{where}: 'bcc' must not be empty if set")
            bcc = bcc_list
        imap_raw = data.get("imap")
        if imap_raw is None:
            imap = None
        else:
            if not isinstance(imap_raw, str) or not imap_raw:
                raise ValueError(
                    f"{where}: 'imap' must be a non-empty string referencing "
                    f"an [imap.NAME] block"
                )
            imap = imap_raw
        address = data.get("address")
        if not isinstance(address, str) or "@" not in address:
            raise ValueError(f"{where}: missing or invalid 'address'")
        name = data.get("name", "")
        if not isinstance(name, str):
            raise ValueError(f"{where}: 'name' must be a string when present")
        validate_display_name(name, where)
        smtp = data.get("smtp")
        if smtp is not None and not isinstance(smtp, str):
            raise ValueError(
                f"{where}: 'smtp' must be a string referencing an [smtp.NAME]"
            )
        fcc_raw = data.get("fcc")
        fcc: Union[bool, str, None]
        if fcc_raw is None or fcc_raw is False:
            fcc = fcc_raw
        elif isinstance(fcc_raw, str):
            if not fcc_raw:
                raise ValueError(f"{where}: 'fcc' folder name must not be empty")
            fcc = fcc_raw
        else:
            raise ValueError(
                f"{where}: 'fcc' must be a folder name (string) or false; "
                f"omit it for the default Sent folder"
            )
        if isinstance(fcc, str) and imap is None:
            raise ValueError(
                f"{where}: 'fcc' selects an IMAP Sent folder but no 'imap' "
                f"block is set to APPEND into; add 'imap', or drop 'fcc'"
            )
        # Copy-retention guarantee: every identity must keep a record of what
        # it sends. FCC provides it (imap set, fcc not switched off); otherwise
        # a 'bcc' that includes this identity's own address must.
        fcc_keeps_copy = imap is not None and fcc is not False
        bcc_keeps_copy = bcc is not None and address.lower() in {b.lower() for b in bcc}
        if not fcc_keeps_copy and not bcc_keeps_copy:
            raise ValueError(
                f"{where}: retains no copy of sent mail. Set 'imap' (FCC to "
                f"the Sent folder), or add a 'bcc' that includes this "
                f"identity's own address {address!r}. With 'fcc = false' a "
                f"self-inclusive 'bcc' is required."
            )
        return cls(
            imap=imap,
            address=address,
            name=name,
            smtp=smtp,
            fcc=fcc,
            bcc=bcc,
        )


@dataclass
class ImapBlock:
    """One [imap.NAME] block: IMAP connection details plus per-block options.

    Attributes:
        host: IMAP server hostname.
        port: TCP port (993 for SSL, 143 otherwise).
        username: IMAP login.
        password: Optional password (env: IMAP_PASSWORD).
        oauth2: Optional OAuth2 credentials (Gmail).
        use_ssl: Use IMAPS (SSL) or plaintext IMAP.
        idle_timeout: Seconds to keep the IMAP connection open. 0 closes
            after each call; -1 keeps it forever; >0 closes on idle.
        verify_with_noop: Send NOOP before reusing a cached connection.
        allowed_folders: Whitelist of folder names this block exposes.
            None means all folders.
        maildir: Optional local maildir path; opts the block into
            local-cache search when [local_cache] is configured.
        default_smtp: Optional name of an [smtp.NAME] block; identities
            in this block inherit it when their own ``smtp`` is unset.
        redact_policy: Compiled callable from a ``redact = "rules.sieve"``
            field, taking an ``Email`` and returning ``True`` when that
            message should be replaced with a placeholder before reaching
            the agent. ``None`` when no policy is configured.
        name: TOML block name (the ``work`` in ``[imap.work]``). Empty
            string when the block was constructed outside a named-block
            context. Surfaced in runtime warnings so log readers can tell
            which account a failure came from.
    """

    host: str
    port: int
    username: str
    password: Optional[str] = None
    oauth2: Optional[OAuth2Config] = None
    use_ssl: bool = True
    idle_timeout: int = 300
    verify_with_noop: bool = True
    allowed_folders: Optional[List[str]] = None
    maildir: Optional[str] = None
    default_smtp: Optional[str] = None
    redact_policy: Optional[Any] = None
    name: str = ""

    @property
    def label(self) -> str:
        """Bracketed block label for log lines (``[imap.work]`` or ``[imap]``)."""
        return f"[imap.{self.name}]" if self.name else "[imap]"

    @property
    def is_gmail(self) -> bool:
        """Check if this is a Gmail configuration."""
        return self.host.endswith("gmail.com") or self.host.endswith("googlemail.com")

    @property
    def requires_oauth2(self) -> bool:
        """Check if this configuration requires OAuth2."""
        return self.is_gmail and self.oauth2 is not None

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        defaults: Dict[str, Any] | None = None,
        name: str = "",
        config_dir: Optional[Path] = None,
    ) -> "ImapBlock":
        """Create [imap.NAME] block configuration from a flat dictionary.

        Args:
            data: Flat block dictionary (host, username, password or
                client_id/client_secret/refresh_token, plus
                allowed_folders, maildir, default_smtp, redact).
            defaults: Global defaults inherited from top level.
            name: Block name for error-message context. Optional; when
                empty the error prefix uses ``[imap]`` rather than the
                named form.
            config_dir: Directory containing the loaded ``config.toml``,
                used to resolve a relative ``redact`` path. Absolute
                ``redact`` paths are taken as written and ignore this.

        Raises:
            ValueError: On structural problems (bad types, missing required
                fields, invalid or missing redact script).
        """
        prefix = f"[imap.{name}]" if name else "[imap]"
        defaults = defaults or {}

        oauth2_config = OAuth2Config.from_dict(data)
        password = data.get("password") or os.environ.get("IMAP_PASSWORD")

        host = data.get("host", "")
        is_gmail = host.endswith("gmail.com") or host.endswith("googlemail.com")

        if is_gmail and not oauth2_config and not password:
            raise ValueError(
                "Gmail requires either an app-specific password or OAuth2 credentials"
            )
        elif not is_gmail and not password and not oauth2_config:
            raise ValueError(
                "IMAP password must be specified in config or "
                "IMAP_PASSWORD environment variable"
            )

        default_smtp = data.get("default_smtp")
        if default_smtp is not None and not isinstance(default_smtp, str):
            raise ValueError(f"{prefix}: 'default_smtp' must be a string")

        redact_policy: Optional[Any] = None
        redact_raw = data.get("redact")
        if redact_raw is not None:
            if not isinstance(redact_raw, str) or not redact_raw.strip():
                raise ValueError(f"{prefix}: 'redact' must be a non-empty string path")
            resolved = Path(redact_raw).expanduser()
            if not resolved.is_absolute():
                base = config_dir or Path.cwd()
                resolved = (base / resolved).resolve()
            # Import locally to keep the public config import surface small
            # and to avoid a hard dependency on sievelib for callers that
            # never touch the redact field.
            from mailroom.sieve_filter import compile_policy

            try:
                redact_policy = compile_policy(str(resolved))
            except ValueError as e:
                raise ValueError(f"{prefix}: 'redact' invalid: {e}") from e

        use_ssl = data.get("use_ssl", True)

        return cls(
            host=data["host"],
            port=data.get("port", 993 if use_ssl else 143),
            username=data["username"],
            password=password,
            oauth2=oauth2_config,
            use_ssl=use_ssl,
            idle_timeout=data.get("idle_timeout", defaults.get("idle_timeout", 300)),
            verify_with_noop=data.get(
                "verify_with_noop", defaults.get("verify_with_noop", True)
            ),
            allowed_folders=data.get("allowed_folders"),
            maildir=data.get("maildir"),
            default_smtp=default_smtp,
            redact_policy=redact_policy,
            name=name,
        )


@dataclass
class MailroomConfig:
    """Top-level mailroom configuration.

    Attributes:
        imap_blocks: All [imap.NAME] blocks, keyed by name.
        smtp_blocks: All [smtp.NAME] blocks, keyed by name.
        identities: All [identity.NAME] blocks, keyed by name.
        local_cache: Optional [local_cache] table when configured.
        warnings: Non-fatal advisories collected at load time.
    """

    imap_blocks: Dict[str, ImapBlock]
    _default_imap: Optional[str] = None
    local_cache: Optional[LocalCacheConfig] = None
    smtp_blocks: Dict[str, SmtpConfig] = field(default_factory=dict)
    identities: Dict[str, Identity] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def default_imap(self) -> str:
        """Explicit default_imap, or first [imap.NAME] block."""
        if self._default_imap and self._default_imap in self.imap_blocks:
            return self._default_imap
        return next(iter(self.imap_blocks))

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        config_dir: Optional[Path] = None,
    ) -> "MailroomConfig":
        """Create top-level configuration from a parsed-TOML dictionary.

        Parses ``[smtp.*]``, ``[imap.*]`` and ``[identity.*]`` blocks,
        validates cross-references (default_smtp, identity.smtp must name
        a defined SMTP block; identity.imap must name a defined IMAP
        block), and collects non-fatal warnings on
        ``MailroomConfig.warnings``.

        Args:
            data: Parsed top-level TOML dictionary.
            config_dir: Directory the config file was loaded from, passed
                through to ``ImapBlock.from_dict`` so per-block ``redact``
                paths can resolve against it.

        Raises:
            ValueError: On structural errors that prevent loading.
        """
        smtp_data = data.get("smtp", {})
        if smtp_data and not isinstance(smtp_data, dict):
            raise ValueError("top-level 'smtp' must be a table of named blocks")
        smtp_blocks: Dict[str, SmtpConfig] = {}
        for n, b in smtp_data.items():
            if not isinstance(b, dict):
                raise ValueError(f"[smtp.{n}]: must be a table")
            smtp_blocks[n] = SmtpConfig.from_dict(n, b)
        smtp_names = sorted(smtp_blocks)

        defaults = {
            k: data[k] for k in ("idle_timeout", "verify_with_noop") if k in data
        }

        imap_data = data.get("imap", {})
        if imap_data and not isinstance(imap_data, dict):
            raise ValueError("top-level 'imap' must be a table of named blocks")
        imap_blocks: Dict[str, ImapBlock] = {}
        for name, block_data in imap_data.items():
            if not isinstance(block_data, dict):
                raise ValueError(f"[imap.{name}]: must be a table")
            block = ImapBlock.from_dict(
                block_data, defaults, name=name, config_dir=config_dir
            )
            if block.default_smtp is not None and block.default_smtp not in smtp_blocks:
                raise ValueError(
                    f"[imap.{name}]: 'default_smtp' references undefined "
                    f"[smtp.{block.default_smtp}]; defined: {smtp_names}"
                )
            imap_blocks[name] = block

        if not imap_blocks:
            raise ValueError("No [imap.NAME] blocks defined in configuration")

        identity_data = data.get("identity", {})
        if identity_data and not isinstance(identity_data, dict):
            raise ValueError("top-level 'identity' must be a table of named blocks")
        identities: Dict[str, Identity] = {}
        # Track per-imap-block address uniqueness. The same address may appear
        # in identities pointing at different [imap.*] blocks (the shared-alias
        # case); within one block, addresses must be unique.
        per_imap_addresses: Dict[str, set] = {}
        for ident_name, ident_data in identity_data.items():
            ident = Identity.from_dict(ident_name, ident_data)
            if ident.imap is not None and ident.imap not in imap_blocks:
                raise ValueError(
                    f"[identity.{ident_name}]: 'imap' references undefined "
                    f"[imap.{ident.imap}]; defined: {sorted(imap_blocks)}"
                )
            if ident.smtp is not None and ident.smtp not in smtp_blocks:
                raise ValueError(
                    f"[identity.{ident_name}]: references undefined "
                    f"[smtp.{ident.smtp}]; defined: {smtp_names}"
                )
            addr_lc = ident.address.lower()
            if ident.imap is not None:
                seen = per_imap_addresses.setdefault(ident.imap, set())
                if addr_lc in seen:
                    raise ValueError(
                        f"[identity.{ident_name}]: address '{addr_lc}' "
                        f"already declared by another identity pointing at "
                        f"[imap.{ident.imap}]"
                    )
                seen.add(addr_lc)
            identities[ident_name] = ident

        default_imap = data.get("default_imap")
        if default_imap is not None and default_imap not in imap_blocks:
            raise ValueError(
                f"top-level: default_imap '{default_imap}' is not a "
                f"defined [imap.NAME] block"
            )

        local_cache_data = data.get("local_cache")
        local_cache = (
            LocalCacheConfig.from_dict(local_cache_data) if local_cache_data else None
        )

        cfg = cls(
            imap_blocks=imap_blocks,
            _default_imap=default_imap,
            local_cache=local_cache,
            smtp_blocks=smtp_blocks,
            identities=identities,
        )
        cfg.warnings = _collect_warnings(cfg)
        return cfg


def _collect_warnings(cfg: MailroomConfig) -> List[str]:
    """Walk the config and emit non-fatal advisories.

    Warnings are surfaced to the user on no-args/--help and after
    status/list JSON. They never abort loading. Categories:
        - No SMTP blocks at all (drafting works, sending blocked globally).
        - [imap.NAME] block has no identities pointing at it (sending from
          this block is disabled).
        - Identity has no SMTP resolution path.
        - Shared credential-less SMTP block on a non-Gmail host (each send
          would inherit whichever block's IMAP creds are currently in
          scope, which is rarely intentional outside Gmail-style hosts).
    """
    warnings: List[str] = []

    if not cfg.smtp_blocks:
        warnings.append(
            "no [smtp.*] blocks defined: drafting works, sending will be blocked"
        )

    smtp_count = len(cfg.smtp_blocks)
    has_lone_smtp = smtp_count == 1

    by_imap: Dict[str, List[str]] = {}
    for ident_name, ident in cfg.identities.items():
        if ident.imap is not None:
            by_imap.setdefault(ident.imap, []).append(ident_name)

    for ident_name, ident in cfg.identities.items():
        if ident.smtp is not None:
            continue
        if ident.imap is None:
            # bcc-only identity: no [imap.*] block to inherit default_smtp
            # from. Sending requires identity.smtp or exactly one SMTP block.
            if not has_lone_smtp:
                warnings.append(
                    f"[identity.{ident_name}]: no 'smtp' set and no "
                    f"[imap.*] block to inherit default_smtp from; "
                    f"sending requires either 'smtp = ...' on this "
                    f"identity or exactly one [smtp.*] block."
                )
            continue
        block = cfg.imap_blocks[ident.imap]
        has_block_default = (
            block.default_smtp is not None and block.default_smtp in cfg.smtp_blocks
        )
        if not has_block_default and not has_lone_smtp:
            warnings.append(
                f"[identity.{ident_name}]: no smtp specified; "
                f"sending from this identity is disabled. Set 'smtp' here "
                f"or 'default_smtp' on [imap.{ident.imap}]."
            )

    for name in cfg.imap_blocks:
        if name not in by_imap:
            warnings.append(
                f"[imap.{name}]: no [identity.*] block points here; "
                f"sending from this block is disabled. Add an "
                f'[identity.NAME] block with imap = "{name}" to enable sends.'
            )

    refs: Dict[str, set] = {n: set() for n in cfg.smtp_blocks}
    for imap_name, block in cfg.imap_blocks.items():
        if block.default_smtp and block.default_smtp in refs:
            refs[block.default_smtp].add(imap_name)
    for ident in cfg.identities.values():
        if ident.smtp and ident.smtp in refs and ident.imap is not None:
            refs[ident.smtp].add(ident.imap)
    for smtp_name, blocks in refs.items():
        smtp = cfg.smtp_blocks[smtp_name]
        if smtp_has_own_creds(smtp) or len(blocks) <= 1:
            continue
        if smtp.host in _INHERITANCE_SAFE_SMTP_HOSTS:
            continue
        warnings.append(
            f"[smtp.{smtp_name}]: no creds and shared by [imap.*] blocks "
            f"{sorted(blocks)}; each send will use the selected block's "
            f"IMAP credentials. This is correct only when host "
            f"'{smtp.host}' accepts each block's own login. Add "
            f"username/password to the [smtp.{smtp_name}] block if it "
            f"should not inherit."
        )

    return warnings


def _load_config_data(
    config_path: Optional[str] = None,
) -> Tuple[Dict[str, Any], Optional[Path]]:
    """Load raw configuration data from file or environment variables.

    Args:
        config_path: Path to configuration file

    Returns:
        Tuple of (raw configuration dictionary, directory the file was
        loaded from). The directory is ``None`` when configuration came
        from environment variables rather than a file; callers needing
        to resolve relative paths fall back to the current working
        directory in that case.

    Raises:
        ValueError: If no configuration source is available
    """
    default_locations = [
        Path("~/.config/mailroom/config.toml"),
    ]

    config_data: Dict[str, Any] = {}
    config_dir: Optional[Path] = None
    if config_path:
        try:
            with open(config_path, "rb") as f:
                config_data = tomllib.load(f)
            config_dir = Path(config_path).resolve().parent
            logger.info(f"Loaded configuration from {config_path}")
        except FileNotFoundError:
            logger.warning(f"Configuration file not found: {config_path}")
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"Invalid TOML in {config_path}: {e}") from e
    else:
        for path in default_locations:
            expanded_path = path.expanduser()
            if expanded_path.exists():
                try:
                    with open(expanded_path, "rb") as f:
                        config_data = tomllib.load(f)
                except tomllib.TOMLDecodeError as e:
                    raise ValueError(f"Invalid TOML in {expanded_path}: {e}") from e
                config_dir = expanded_path.resolve().parent
                logger.info(f"Loaded configuration from {expanded_path}")
                break

    if not config_data:
        logger.info("No configuration file found, using environment variables")
        if not os.environ.get("IMAP_HOST"):
            raise ValueError(
                "No configuration file found and IMAP_HOST environment variable not set"
            )

        config_data = {
            "imap": {
                "default": {
                    "host": os.environ.get("IMAP_HOST"),
                    "port": int(os.environ.get("IMAP_PORT", "993")),
                    "username": os.environ.get("IMAP_USERNAME"),
                    "password": os.environ.get("IMAP_PASSWORD"),
                    "use_ssl": os.environ.get("IMAP_USE_SSL", "true").lower() == "true",
                    "idle_timeout": int(os.environ.get("IMAP_IDLE_TIMEOUT", "300")),
                    "verify_with_noop": os.environ.get(
                        "IMAP_VERIFY_WITH_NOOP", "true"
                    ).lower()
                    == "true",
                }
            }
        }

        allowed_folders_env = os.environ.get("IMAP_ALLOWED_FOLDERS")
        if allowed_folders_env:
            config_data["imap"]["default"]["allowed_folders"] = (
                allowed_folders_env.split(",")
            )

    return config_data, config_dir


def load_config(config_path: Optional[str] = None) -> MailroomConfig:
    """Load configuration from file or environment variables.

    Args:
        config_path: Path to configuration file

    Returns:
        Top-level mailroom configuration with ``warnings`` populated.

    Raises:
        ValueError: If configuration is invalid
    """
    config_data, config_dir = _load_config_data(config_path)

    try:
        return MailroomConfig.from_dict(config_data, config_dir=config_dir)
    except KeyError as e:
        raise ValueError(f"Missing required configuration: {e}")


def load_config_with_warnings(
    config_path: Optional[str] = None,
) -> Tuple[MailroomConfig, List[str]]:
    """Load configuration and return ``(cfg, warnings)`` explicitly.

    Sugar over ``load_config`` for callers (CLI, validator script) that want
    the warnings list as a separate return value rather than reading
    ``cfg.warnings``.

    Raises:
        ValueError: If configuration is invalid (same as ``load_config``).
    """
    cfg = load_config(config_path)
    return cfg, cfg.warnings
