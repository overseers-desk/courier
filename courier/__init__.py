"""Email toolkit for AI assistants and command-line scripting.

The names in ``__all__`` are courier's supported library surface: config
(``CourierConfig.from_dict`` for a dict the caller builds, ``load_config``
for the file and environment the ``courier`` command reads), the error
taxonomy, identity/SMTP resolution, the IMAP client and its Message-ID
lookup, ``search`` across [imap.NAME] blocks, ``watch``, message models
with ``Email.to_dict``, the query grammar, MIME building, and
send-with-FCC.
Anything not listed here is internal and may reshape without notice.

The library reads only what it is handed. Importing it loads no ``.env``
file and configures no logging, and ``search``, ``ImapClient`` and
``watch`` take the WORLD_AS_OF bound as their ``world_as_of`` argument;
``None`` leaves them unbounded whatever the environment holds.
"""

from courier.config import (
    CourierConfig,
    Identity,
    ImapBlock,
    SmtpConfig,
    load_config,
)
from courier.errors import (
    AmbiguousMessageId,
    CapabilityMissing,
    CourierError,
    FccUnresolved,
    FolderNotFound,
    MessageNotFound,
    PermanentError,
    TransientError,
    WorldAsOfInvalid,
    WorldBoundRefused,
)
from courier.identity import (
    IdentityNotFound,
    SendDisabled,
    SmtpUnresolved,
    resolve_identity_for_reply,
    resolve_identity_for_send,
    resolve_smtp_for_identity,
)
from courier.imap_client import AppendResult, ImapClient
from courier.local_cache import MuBackend
from courier.models import Email, EmailAddress, EmailAttachment, decode_mime_header
from courier.query import (
    ParseResult,
    QuerySyntaxError,
    UntranslatableForBackend,
    parse,
)
from courier.searching import search
from courier.sending import send_with_fcc
from courier.smtp_client import create_mime
from courier.smtp_transport import send as smtp_send
from courier.watch import WatchEvent, watch

# Backwards-compatible name for the exception external callers caught
# from the old translator; the cache-fallback semantics are unchanged.
UntranslatableQuery = UntranslatableForBackend

__version__ = "1.2.1"

__all__ = [
    # config
    "load_config",
    "CourierConfig",
    "ImapBlock",
    "SmtpConfig",
    "Identity",
    # errors
    "CourierError",
    "TransientError",
    "PermanentError",
    "MessageNotFound",
    "FolderNotFound",
    "CapabilityMissing",
    "FccUnresolved",
    "WorldAsOfInvalid",
    "WorldBoundRefused",
    "AmbiguousMessageId",
    # identity
    "resolve_identity_for_send",
    "resolve_identity_for_reply",
    "resolve_smtp_for_identity",
    "SendDisabled",
    "IdentityNotFound",
    "SmtpUnresolved",
    # imap
    "ImapClient",
    "AppendResult",
    # watch
    "watch",
    "WatchEvent",
    # models
    "Email",
    "EmailAddress",
    "EmailAttachment",
    "decode_mime_header",
    # query
    "parse",
    "ParseResult",
    "QuerySyntaxError",
    "UntranslatableForBackend",
    "UntranslatableQuery",
    # searching
    "search",
    # sending
    "create_mime",
    "smtp_send",
    "send_with_fcc",
    # cache
    "MuBackend",
]
