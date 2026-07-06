"""Email toolkit for AI assistants and command-line scripting.

The names in ``__all__`` are courier's supported library surface: config
loading, the error taxonomy, identity/SMTP resolution, the IMAP client,
message models, the query parser, MIME building, and send-with-FCC.
Anything not listed here is internal and may reshape without notice.
"""

from courier.config import (
    CourierConfig,
    Identity,
    ImapBlock,
    SmtpConfig,
    load_config,
)
from courier.errors import (
    CapabilityMissing,
    CourierError,
    FccUnresolved,
    FolderNotFound,
    MessageNotFound,
    PermanentError,
    TransientError,
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
from courier.query_parser import UntranslatableQuery, parse_query
from courier.sending import send_with_fcc
from courier.smtp_client import create_mime
from courier.smtp_transport import send as smtp_send

__version__ = "1.1.16"

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
    # models
    "Email",
    "EmailAddress",
    "EmailAttachment",
    "decode_mime_header",
    # query
    "parse_query",
    "UntranslatableQuery",
    # sending
    "create_mime",
    "smtp_send",
    "send_with_fcc",
    # cache
    "MuBackend",
]
