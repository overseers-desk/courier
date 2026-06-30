"""Identity resolution: which From identity to use, and which SMTP route it takes.

Resolution rules are documented in courier/config.sample.toml. This module
turns a parsed ``CourierConfig`` plus context (an explicit From, a parent
email being replied to, etc.) into the concrete ``Identity`` and
``SmtpConfig`` that the SMTP transport will use.

Three failure modes are exposed as exceptions so the CLI can convert them
into clean exit-1 errors rather than tracebacks:

    SendDisabled: the selected [imap.NAME] block has no [identity.*] block
        pointing at it. The block is read-only for sending; an
        [identity.NAME] block must be added to enable sends.
    IdentityNotFound: the explicit From address does not match any
        identity configured for the selected [imap.NAME] block.
    SmtpUnresolved: the resolved identity has no SMTP route (no
        identity.smtp, no imap_block.default_smtp, and not a single
        ``[smtp.*]`` block to fall back to).
"""

from dataclasses import replace
from typing import Any, Dict, List, Optional

from courier.config import CourierConfig, Identity, ImapBlock, SmtpConfig


class SendDisabled(LookupError):
    """The selected [imap.NAME] block has no identities; sending is disabled.

    Raised when ``resolve_identity_for_send`` or
    ``resolve_identity_for_reply`` is called for an [imap.NAME] block with
    no [identity.*] entries pointing at it. The CLI converts this into a
    clean exit-1 error explaining how to enable sends.
    """

    def __init__(self, imap_name: str):
        self.imap_name = imap_name
        super().__init__(
            f"[imap.{imap_name}] has no identities; sending is disabled. "
            f'Add an [identity.NAME] block with imap = "{imap_name}" '
            f"to enable sends."
        )


class IdentityNotFound(LookupError):
    """An explicit From address does not match any configured identity.

    Raised when ``--from EMAIL`` (compose/reply) or the From header of a
    draft being sent (send-draft) does not match any of the [imap.NAME]
    block's identities. The unmatched address and the list of available
    addresses are stored on the exception so the CLI can render them.
    """

    def __init__(self, from_addr: str, available: List[str]):
        self.from_addr = from_addr
        self.available = available
        super().__init__(
            f"From address '{from_addr}' is not configured for this "
            f"[imap.NAME] block. Configured identities: {available}"
        )


class SmtpUnresolved(LookupError):
    """An identity could not be paired with an SMTP block.

    Raised when none of the resolution rules (identity.smtp,
    imap_block.default_smtp, lone-smtp fallback) produces an SMTP block
    for the identity at hand.
    """

    def __init__(self, imap_name: str, identity_addr: str, available: List[str]):
        self.imap_name = imap_name
        self.identity_addr = identity_addr
        self.available = available
        super().__init__(
            f"Cannot resolve SMTP for identity '{identity_addr}' on "
            f"[imap.{imap_name}]. Set 'smtp' on the identity, set "
            f"'default_smtp' on the [imap.NAME] block, or define a single "
            f"[smtp.*] block. Available SMTP blocks: {available}"
        )


def identities_for_imap(cfg: CourierConfig, imap_name: str) -> List[Identity]:
    """Return identities pointing at the given [imap.NAME] block.

    Order matches the order in which identities appear in the config file.
    An empty list means the block is read-only for sending.
    """
    return [ident for ident in cfg.identities.values() if ident.imap == imap_name]


def resolve_identity_for_send(
    cfg: CourierConfig, imap_name: str, from_addr: Optional[str] = None
) -> Identity:
    """Pick the identity to send from.

    Args:
        cfg: Parsed courier configuration.
        imap_name: Name of the selected [imap.NAME] block.
        from_addr: Optional explicit From address (e.g. from ``--from``).

    Returns:
        The chosen ``Identity``. With *from_addr* None, returns the first
        identity pointing at the block.

    Raises:
        SendDisabled: When no [identity.*] points at the block.
        IdentityNotFound: When *from_addr* is set but matches no identity
            on the block.
    """
    identities = identities_for_imap(cfg, imap_name)
    if not identities:
        raise SendDisabled(imap_name=imap_name)
    if from_addr is None:
        return identities[0]
    target = from_addr.strip().lower()
    for ident in identities:
        if ident.address.lower() == target:
            return ident
    raise IdentityNotFound(
        from_addr=from_addr,
        available=[i.address for i in identities],
    )


def resolve_identity_for_reply(
    cfg: CourierConfig, imap_name: str, email_obj: Any
) -> Identity:
    """Pick the reply-from identity by matching the parent email's recipients.

    Walks ``email_obj.to`` then ``email_obj.cc``, returning the identity
    whose address matches any of those recipients. This is what tells
    courier "the user received this on alias X, so reply as X".

    Args:
        cfg: Parsed courier configuration.
        imap_name: Name of the selected [imap.NAME] block.
        email_obj: An ``Email`` model with ``.to`` and ``.cc`` lists of
            objects exposing ``.address``.

    Returns:
        The matching ``Identity``, or the first identity pointing at the
        block if no recipient matches (the safe fallback so we never fail
        to compose a reply).

    Raises:
        SendDisabled: When no [identity.*] points at the block.
    """
    identities = identities_for_imap(cfg, imap_name)
    if not identities:
        raise SendDisabled(imap_name=imap_name)
    addr_to_identity = {i.address.lower(): i for i in identities}
    for recipient in (email_obj.to or []) + (email_obj.cc or []):
        addr = (getattr(recipient, "address", "") or "").lower()
        if addr and addr in addr_to_identity:
            return addr_to_identity[addr]
    return identities[0]


def resolve_smtp_for_identity(
    identity: Identity,
    imap_block: Optional[ImapBlock],
    imap_name: str,
    smtp_blocks: Dict[str, SmtpConfig],
) -> SmtpConfig:
    """Resolve the SMTP block for an identity, applying credential inheritance.

    Resolution order:
        1. ``identity.smtp`` if set.
        2. ``imap_block.default_smtp`` if set (skipped if no imap block).
        3. The lone ``[smtp.*]`` block when exactly one is defined.

    When the resolved SMTP block is a template (no username/password) and
    an imap block is available, this function returns a copy with
    credentials filled from the [imap.NAME] block's IMAP login. Without
    an imap block (a bcc-only identity), a credential-less SMTP block
    raises ``SmtpUnresolved``.

    Raises:
        SmtpUnresolved: When none of the rules match, or when the
            resolved SMTP needs credentials and no IMAP block is
            available to inherit from.
    """
    name: Optional[str]
    if identity.smtp:
        name = identity.smtp
    elif imap_block is not None and imap_block.default_smtp:
        name = imap_block.default_smtp
    elif len(smtp_blocks) == 1:
        name = next(iter(smtp_blocks))
    else:
        raise SmtpUnresolved(
            imap_name=imap_name,
            identity_addr=identity.address,
            available=sorted(smtp_blocks),
        )
    smtp = smtp_blocks[name]
    if smtp.username and smtp.password:
        return smtp
    if imap_block is None:
        raise SmtpUnresolved(
            imap_name=imap_name,
            identity_addr=identity.address,
            available=sorted(smtp_blocks),
        )
    return replace(
        smtp,
        username=smtp.username or imap_block.username,
        password=smtp.password or imap_block.password,
    )
