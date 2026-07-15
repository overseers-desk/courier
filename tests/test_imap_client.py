"""Tests for the IMAP client."""

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from courier.config import ImapBlock
from courier.errors import (
    CourierError,
    FolderNotFound,
    PermanentError,
    TransientError,
)
from courier.imap_client import ImapClient
from courier.local_cache import EligibilityResult, MuFailure, UntranslatableQuery
from courier.models import Email
from courier.query import parse
from courier.query.ast import TranslationReport
from courier.query.dispatch import extract_scope


def _mu_hit(results, truncated: bool = False):
    """Wrap canned cache results in MuBackend.search's return shape."""
    return (results, TranslationReport(dialect="mu"), truncated)


def _make_maildir_root(tmp_path, folder: str = "INBOX") -> str:
    """Create ``<tmp>/<folder>/{cur,new}`` and return the block-root path."""
    root = tmp_path / "maildir"
    (root / folder / "cur").mkdir(parents=True)
    (root / folder / "new").mkdir(parents=True)
    return str(root)


def _write_maildir_message(
    maildir_root: str,
    folder: str,
    uid: int,
    *,
    subdir: str = "cur",
    from_addr: str = "alice@example.com",
    subject: str = "Test Disk Email",
    flag_suffix: str = "S",
) -> str:
    """Write an RFC 822 message at the mbsync-style filename; return path."""
    name = f"1700000000_0.hostname,U={uid},FMD5=abc:2,{flag_suffix}"
    path = Path(maildir_root) / folder / subdir / name
    path.write_bytes(
        f"From: {from_addr}\r\n"
        f"To: bob@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: Thu, 01 Jan 2023 12:00:00 +0000\r\n"
        f"Message-ID: <disk-{uid}@example.com>\r\n"
        f"\r\n"
        f"disk body\r\n".encode("utf-8")
    )
    return str(path)


def _make_block_with_maildir(maildir: str, redact_policy=None) -> ImapBlock:
    return ImapBlock(
        host="imap.example.com",
        port=993,
        username="test@example.com",
        password="password",
        use_ssl=True,
        maildir=maildir,
        redact_policy=redact_policy,
    )


def _eligible_mu() -> MagicMock:
    """A local-cache backend that reports the block as eligible.

    Disk-served reads now require the block to be opted into the local
    cache and the index to pass ``is_eligible`` (the one-policy gate), so
    disk tests must supply a backend rather than rely on ``maildir``
    alone.
    """
    mu = MagicMock()
    mu.is_eligible.return_value = EligibilityResult(True)
    return mu


class TestImapClient:
    """Test the IMAP client."""

    def test_init(self):
        """Test initializing the client."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        assert client.block == config
        assert client.allowed_folders is None
        assert client.client is None
        assert client.folder_cache == {}
        assert client.connected is False

        # Test with allowed folders
        allowed_folders = ["INBOX", "Sent"]
        client = ImapClient(replace(config, allowed_folders=allowed_folders))
        assert client.allowed_folders == set(allowed_folders)

    def test_connect_success(self, mock_imap_client):
        """Test successful connection."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client
            client.connect()

            # Verify connection was established with correct parameters
            mock_client_class.assert_called_once_with(
                "imap.example.com", port=993, ssl=True, timeout=10
            )

            # Verify login was called with correct credentials
            mock_imap_client.login.assert_called_once_with(
                "test@example.com", "password"
            )

            # Verify client is connected
            assert client.connected is True
            assert client.client is mock_imap_client

    def test_connect_failure(self):
        """Test connection failure."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.side_effect = ConnectionError("Connection failed")

            # Verify that the correct exception is raised
            with pytest.raises(ConnectionError) as excinfo:
                client.connect()

            # Verify error message
            assert "Failed to connect to IMAP server" in str(excinfo.value)

            # Verify client is not connected
            assert client.connected is False
            assert client.client is None

    def test_disconnect(self, mock_imap_client):
        """Test disconnection."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        # Simulate connected state
        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client
            client.connect()

            # Now disconnect
            client.disconnect()

            # Verify logout was called
            mock_imap_client.logout.assert_called_once()

            # Verify client is disconnected
            assert client.connected is False
            assert client.client is None

    def test_disconnect_with_exception(self, mock_imap_client):
        """Test disconnection with exception."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        # Simulate connected state
        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client
            client.connect()

            # Make logout raise an exception
            mock_imap_client.logout.side_effect = Exception("Logout failed")

            # Disconnect should handle the exception
            client.disconnect()

            # Verify logout was called
            mock_imap_client.logout.assert_called_once()

            # Verify client is still disconnected despite the exception
            assert client.connected is False
            assert client.client is None

    def test_ensure_connected_when_not_connected(self, mock_imap_client):
        """Test ensuring connection when not connected."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Client starts not connected
            assert client.connected is False

            # Ensure connected should call connect
            client.ensure_connected()

            # Verify connect was called
            mock_client_class.assert_called_once()
            mock_imap_client.login.assert_called_once()

            # Verify client is now connected
            assert client.connected is True

    def test_ensure_connected_when_already_connected(self, mock_imap_client):
        """Test ensuring connection when already connected."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Connect first
            client.connect()
            mock_client_class.reset_mock()
            mock_imap_client.login.reset_mock()

            # Now ensure_connected should do nothing
            client.ensure_connected()

            # Verify connect was not called again
            mock_client_class.assert_not_called()
            mock_imap_client.login.assert_not_called()

            # Verify client is still connected
            assert client.connected is True

    def test_list_folders_from_cache(self, mock_imap_client):
        """Test listing folders from cache."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        # Manually populate folder cache
        client.folder_cache = {
            "INBOX": [b"\\HasNoChildren"],
            "Sent": [b"\\HasNoChildren"],
            "Trash": [b"\\HasNoChildren"],
        }

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Connect first
            client.connect()
            mock_imap_client.list_folders.reset_mock()

            # List folders should use cache
            folders = client.list_folders(refresh=False)

            # Verify list_folders was not called
            mock_imap_client.list_folders.assert_not_called()

            # Verify correct folders were returned
            assert set(folders) == {"INBOX", "Sent", "Trash"}

    def test_list_folders_refresh(self, mock_imap_client):
        """Test listing folders with refresh."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        # Manually populate folder cache with old data
        client.folder_cache = {
            "INBOX": [b"\\HasNoChildren"],
            "OldFolder": [b"\\HasNoChildren"],
        }

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Set up mock response for list_folders
            mock_imap_client.list_folders.return_value = [
                ((b"\\HasNoChildren",), b"/", "INBOX"),
                ((b"\\HasNoChildren",), b"/", "Sent"),
                ((b"\\HasNoChildren",), b"/", "Drafts"),
            ]

            # Connect first
            client.connect()

            # Clear the folder cache to force fresh data
            client.folder_cache = {}

            # List folders with refresh
            folders = client.list_folders(refresh=True)

            # Verify list_folders was called
            mock_imap_client.list_folders.assert_called_once()

            # Verify correct folders were returned
            assert set(folders) == {"INBOX", "Sent", "Drafts"}

            # Verify cache was updated
            assert set(client.folder_cache.keys()) == {"INBOX", "Sent", "Drafts"}

    def test_list_folders_with_allowed_folders(self, mock_imap_client):
        """Test listing folders with allowed folders filter."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        allowed_folders = ["INBOX", "Sent"]
        client = ImapClient(replace(config, allowed_folders=allowed_folders))

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Set up mock response for list_folders
            mock_imap_client.list_folders.return_value = [
                ((b"\\HasNoChildren",), b"/", "INBOX"),
                ((b"\\HasNoChildren",), b"/", "Sent"),
                ((b"\\HasNoChildren",), b"/", "Drafts"),
                ((b"\\HasNoChildren",), b"/", "Trash"),
            ]

            # Connect first
            client.connect()

            # List folders
            folders = client.list_folders()

            # Verify list_folders was called
            mock_imap_client.list_folders.assert_called_once()

            # Verify only allowed folders were returned
            assert set(folders) == {"INBOX", "Sent"}

            # Verify only allowed folders were cached
            assert set(client.folder_cache.keys()) == {"INBOX", "Sent"}

    def test_select_folder(self, mock_imap_client):
        """Test selecting a folder."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Set up mock response for select_folder
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}

            # Connect first
            client.connect()

            # Select folder
            result = client.select_folder("INBOX")

            # Verify select_folder was called with correct folder and default readonly=False
            mock_imap_client.select_folder.assert_called_once_with(
                "INBOX", readonly=False
            )

            # Verify result is correct
            assert result == {b"EXISTS": 10}

            # Also test with readonly=True
            mock_imap_client.select_folder.reset_mock()
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}

            result = client.select_folder("INBOX", readonly=True)

            # Verify select_folder was called with readonly=True
            mock_imap_client.select_folder.assert_called_once_with(
                "INBOX", readonly=True
            )

    def test_select_folder_not_allowed(self, mock_imap_client):
        """Test selecting a folder that's not allowed."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        allowed_folders = ["INBOX", "Sent"]
        client = ImapClient(replace(config, allowed_folders=allowed_folders))

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Connect first
            client.connect()

            # Attempt to select a non-allowed folder
            with pytest.raises(ValueError) as excinfo:
                client.select_folder("Trash")

            # Verify error message
            assert "Folder 'Trash' is not allowed" in str(excinfo.value)

            # Verify select_folder was not called
            mock_imap_client.select_folder.assert_not_called()

    def test_select_folder_server_no_raises_folder_not_found(self, mock_imap_client):
        """A server NO/BAD on SELECT is a judgement about the folder
        (missing, ACL-denied, \\Noselect), not about the connection: it
        maps to FolderNotFound, a PermanentError, so callers stop
        retrying a command the server will keep refusing (issue #65)."""
        from imapclient.exceptions import IMAPClientError

        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client
            mock_imap_client.select_folder.side_effect = IMAPClientError(
                "select failed: NO No such mailbox Inbxo"
            )
            client.connect()

            with pytest.raises(FolderNotFound, match="Inbxo"):
                client.select_folder("Inbxo")

    def test_select_folder_not_found_is_permanent(self, mock_imap_client):
        """FolderNotFound from SELECT is caught by PermanentError
        handlers: the CLI's typed exit codes and as_courier_error both
        classify it as not-retryable."""
        from imapclient.exceptions import IMAPClientError

        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client
            mock_imap_client.select_folder.side_effect = IMAPClientError(
                "SELECT failed: NO"
            )
            client.connect()

            with pytest.raises(PermanentError):
                client.select_folder("Nope")

    def test_select_folder_abort_raises_connection_error(self, mock_imap_client):
        """A dropped socket mid-SELECT is connection-layer trouble, not
        a judgement about the folder: it stays the builtin
        ConnectionError so retry loops keep treating it as transient."""
        from imapclient.exceptions import IMAPClientAbortError

        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client
            mock_imap_client.select_folder.side_effect = IMAPClientAbortError(
                "socket error: EOF during SELECT"
            )
            client.connect()

            with pytest.raises(ConnectionError):
                client.select_folder("INBOX")

    def test_search_with_string_criteria(self, mock_imap_client):
        """Test searching with string criteria."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Set up mock responses
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.search.return_value = [1, 2, 3]

            # Connect first
            client.connect()

            # String criteria pass through verbatim; presets like
            # "today" now resolve in the query translator, not here.
            result = client.search("UNSEEN", folder="INBOX")

            # Verify select_folder was called with readonly=True (safe for search)
            mock_imap_client.select_folder.assert_called_once_with(
                "INBOX", readonly=True
            )

            # Verify search was called with correct criteria
            mock_imap_client.search.assert_called_once_with("UNSEEN", charset=None)

            # Verify result is correct
            assert result == [1, 2, 3]

            # Reset mocks
            mock_imap_client.select_folder.reset_mock()
            mock_imap_client.search.reset_mock()

            # List criteria also pass through untouched.
            result = client.search(["SINCE", "01-Jul-2026"], folder="INBOX")

            # Verify select_folder was called with readonly=True
            mock_imap_client.select_folder.assert_called_once_with(
                "INBOX", readonly=True
            )
            mock_imap_client.search.assert_called_once_with(
                ["SINCE", "01-Jul-2026"], charset=None
            )

    def test_search_with_complex_criteria(self, mock_imap_client):
        """Test searching with complex criteria."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Set up mock responses
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.search.return_value = [4, 5, 6]

            # Connect first
            client.connect()

            # Search with complex criteria
            complex_criteria = ["FROM", "test@example.com", "SUBJECT", "test"]
            result = client.search(complex_criteria, folder="Sent")

            # Verify select_folder was called with readonly=True
            mock_imap_client.select_folder.assert_called_once_with(
                "Sent", readonly=True
            )

            # Verify search was called with correct criteria
            mock_imap_client.search.assert_called_once_with(
                complex_criteria, charset=None
            )

            # Verify result is correct
            assert result == [4, 5, 6]

    def test_fetch_email(self, mock_imap_client, test_email_response_data):
        """Test fetching a single email."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Set up mock responses
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.fetch.return_value = {12345: test_email_response_data}

            # Connect first
            client.connect()

            # Fetch email
            email_obj = client.fetch_email(12345, folder="INBOX")

            # Verify select_folder was called with readonly=True
            mock_imap_client.select_folder.assert_called_once_with(
                "INBOX", readonly=True
            )

            # Verify fetch was called with correct parameters
            mock_imap_client.fetch.assert_called_once_with(
                [12345], ["BODY.PEEK[]", "FLAGS"]
            )

            # Verify result is a valid Email object
            assert isinstance(email_obj, Email)
            assert email_obj.uid == 12345
            assert email_obj.folder == "INBOX"
            assert "Test Email" in email_obj.subject
            assert "Test Sender" in email_obj.from_.name
            assert "sender@example.com" in email_obj.from_.address

    def test_fetch_email_not_found(self, mock_imap_client):
        """Test fetching an email that doesn't exist."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Set up mock responses
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.fetch.return_value = {}  # Empty result

            # Connect first
            client.connect()

            # Fetch non-existent email
            email_obj = client.fetch_email(99999, folder="INBOX")

            # Verify select_folder was called with readonly=True
            mock_imap_client.select_folder.assert_called_once_with(
                "INBOX", readonly=True
            )

            # Verify fetch was called with correct parameters
            mock_imap_client.fetch.assert_called_once_with(
                [99999], ["BODY.PEEK[]", "FLAGS"]
            )

            # Verify result is None
            assert email_obj is None

    def test_fetch_emails(self, mock_imap_client, make_test_email_response_data):
        """Test fetching multiple emails."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Set up mock responses
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}

            # Create response data for multiple emails
            response_data = {
                101: make_test_email_response_data(
                    uid=101,
                    headers={
                        "Subject": "Email 1",
                        "From": "sender@example.com",
                        "To": "recipient@example.com",
                    },
                ),
                102: make_test_email_response_data(
                    uid=102,
                    headers={
                        "Subject": "Email 2",
                        "From": "sender@example.com",
                        "To": "recipient@example.com",
                    },
                ),
                103: make_test_email_response_data(
                    uid=103,
                    headers={
                        "Subject": "Email 3",
                        "From": "sender@example.com",
                        "To": "recipient@example.com",
                    },
                ),
            }
            mock_imap_client.fetch.return_value = response_data

            # Connect first
            client.connect()

            # Fetch emails
            emails = client.fetch_emails([101, 102, 103], folder="INBOX")

            # Verify select_folder was called with readonly=True
            mock_imap_client.select_folder.assert_called_once_with(
                "INBOX", readonly=True
            )

            # Verify fetch was called with correct parameters
            mock_imap_client.fetch.assert_called_once_with(
                [101, 102, 103], ["BODY.PEEK[]", "FLAGS"]
            )

            # Verify result contains all emails
            assert len(emails) == 3
            assert isinstance(emails, dict)
            assert all(isinstance(email, Email) for email in emails.values())
            assert 101 in emails
            assert 102 in emails
            assert 103 in emails
            assert emails[101].subject == "Email 1"
            assert emails[102].subject == "Email 2"
            assert emails[103].subject == "Email 3"

    def test_fetch_emails_with_limit(
        self, mock_imap_client, make_test_email_response_data
    ):
        """Test fetching emails with a limit."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Set up mock responses
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}

            # Create response data for multiple emails
            response_data = {
                101: make_test_email_response_data(
                    uid=101,
                    headers={
                        "Subject": "Email 1",
                        "From": "sender@example.com",
                        "To": "recipient@example.com",
                    },
                ),
                102: make_test_email_response_data(
                    uid=102,
                    headers={
                        "Subject": "Email 2",
                        "From": "sender@example.com",
                        "To": "recipient@example.com",
                    },
                ),
            }
            mock_imap_client.fetch.return_value = response_data

            # Connect first
            client.connect()

            # Fetch emails with limit
            emails = client.fetch_emails(
                [101, 102, 103, 104, 105], folder="INBOX", limit=2
            )

            # Verify select_folder was called with readonly=True
            mock_imap_client.select_folder.assert_called_once_with(
                "INBOX", readonly=True
            )

            # Verify fetch was called with correct parameters (only first 2 UIDs)
            mock_imap_client.fetch.assert_called_once_with(
                [101, 102], ["BODY.PEEK[]", "FLAGS"]
            )

            # Verify result contains only limited emails
            assert len(emails) == 2
            assert 101 in emails
            assert 102 in emails

    def test_fetch_email_disk_hit_skips_imap(self, tmp_path):
        """When ``block.maildir`` is set and the file exists, the disk
        path serves the fetch and the IMAP client is never touched."""
        root = _make_maildir_root(tmp_path)
        _write_maildir_message(root, "INBOX", uid=691, subject="From disk")
        client = ImapClient(_make_block_with_maildir(root), local_cache=_eligible_mu())

        with patch("imapclient.IMAPClient") as mock_cls:
            mock_imap = MagicMock()
            mock_cls.return_value = mock_imap
            email_obj = client.fetch_email(691, folder="INBOX")

        assert email_obj is not None
        assert email_obj.uid == 691
        assert email_obj.folder == "INBOX"
        assert email_obj.subject == "From disk"
        assert email_obj.from_.address == "alice@example.com"
        mock_imap.fetch.assert_not_called()
        mock_imap.select_folder.assert_not_called()

    def test_fetch_email_disk_hit_finds_message_in_new_subdir(self, tmp_path):
        """``new/`` is searched alongside ``cur/`` so just-delivered mail
        is still disk-served."""
        root = _make_maildir_root(tmp_path)
        _write_maildir_message(
            root, "INBOX", uid=42, subdir="new", subject="Fresh", flag_suffix=""
        )
        client = ImapClient(_make_block_with_maildir(root), local_cache=_eligible_mu())

        with patch("imapclient.IMAPClient") as mock_cls:
            mock_imap = MagicMock()
            mock_cls.return_value = mock_imap
            email_obj = client.fetch_email(42, folder="INBOX")

        assert email_obj is not None
        assert email_obj.uid == 42
        mock_imap.fetch.assert_not_called()

    def test_fetch_email_disk_hit_mbsync_colon_filename(self, tmp_path):
        """mbsync's native scheme writes ``,U=<uid>:2,<flags>`` (colon
        before the maildir info suffix, no trailing comma); the glob
        must match it or every read silently round-trips to IMAP
        (issue #64)."""
        root = _make_maildir_root(tmp_path)
        name = "1700000000_0.hostname,U=77:2,S"
        (Path(root) / "INBOX" / "cur" / name).write_bytes(
            b"From: alice@example.com\r\n"
            b"Subject: colon form\r\n"
            b"Message-ID: <colon@example.com>\r\n"
            b"\r\n"
            b"body\r\n"
        )
        client = ImapClient(_make_block_with_maildir(root), local_cache=_eligible_mu())

        with patch("imapclient.IMAPClient") as mock_cls:
            mock_imap = MagicMock()
            mock_cls.return_value = mock_imap
            email_obj = client.fetch_email(77, folder="INBOX")

        assert email_obj is not None
        assert email_obj.subject == "colon form"
        mock_imap.fetch.assert_not_called()

    def test_fetch_email_disk_hit_bare_terminal_uid_in_new(self, tmp_path):
        """A ``new/`` message has no flags suffix at all, so the
        filename ends at ``,U=<uid>``; the terminal form must match."""
        root = _make_maildir_root(tmp_path)
        name = "1700000000_0.hostname,U=88"
        (Path(root) / "INBOX" / "new" / name).write_bytes(
            b"From: alice@example.com\r\n"
            b"Subject: fresh\r\n"
            b"Message-ID: <bare@example.com>\r\n"
            b"\r\n"
            b"body\r\n"
        )
        client = ImapClient(_make_block_with_maildir(root), local_cache=_eligible_mu())

        with patch("imapclient.IMAPClient") as mock_cls:
            mock_imap = MagicMock()
            mock_cls.return_value = mock_imap
            email_obj = client.fetch_email(88, folder="INBOX")

        assert email_obj is not None
        assert email_obj.subject == "fresh"
        mock_imap.fetch.assert_not_called()

    def test_fetch_email_disk_uid_prefix_does_not_match(self, tmp_path):
        """UID 7 must not match a file carrying UID 77: the character
        after the UID digits must be ``,`` or ``:`` (or end of name)."""
        root = _make_maildir_root(tmp_path)
        name = "1700000000_0.hostname,U=77:2,S"
        (Path(root) / "INBOX" / "cur" / name).write_bytes(
            b"From: alice@example.com\r\n"
            b"Subject: colon form\r\n"
            b"Message-ID: <colon@example.com>\r\n"
            b"\r\n"
            b"body\r\n"
        )
        client = ImapClient(_make_block_with_maildir(root), local_cache=_eligible_mu())

        with patch("imapclient.IMAPClient") as mock_cls:
            mock_imap = MagicMock()
            mock_cls.return_value = mock_imap
            mock_imap.select_folder.return_value = {b"EXISTS": 1}
            mock_imap.fetch.return_value = {}
            client.connect()
            email_obj = client.fetch_email(7, folder="INBOX")

        # Disk miss -> IMAP fallback (which also has nothing).
        assert email_obj is None
        mock_imap.fetch.assert_called_once()

    def test_fetch_email_disk_miss_falls_back_to_imap(
        self, tmp_path, mock_imap_client, test_email_response_data
    ):
        """No matching file on disk → IMAP fallback (e.g. mail newer than
        the last mbsync sync)."""
        root = _make_maildir_root(tmp_path)
        # No file written for uid 12345.
        client = ImapClient(_make_block_with_maildir(root), local_cache=_eligible_mu())

        with patch("imapclient.IMAPClient") as mock_cls:
            mock_cls.return_value = mock_imap_client
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.fetch.return_value = {12345: test_email_response_data}
            client.connect()
            email_obj = client.fetch_email(12345, folder="INBOX")

        assert email_obj is not None
        assert email_obj.uid == 12345
        mock_imap_client.fetch.assert_called_once_with(
            [12345], ["BODY.PEEK[]", "FLAGS"]
        )

    def test_fetch_email_no_maildir_uses_imap(
        self, mock_imap_client, test_email_response_data
    ):
        """A block without ``maildir`` skips the disk path entirely."""
        block = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
            maildir=None,
        )
        client = ImapClient(block)

        with patch("imapclient.IMAPClient") as mock_cls:
            mock_cls.return_value = mock_imap_client
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.fetch.return_value = {12345: test_email_response_data}
            client.connect()
            email_obj = client.fetch_email(12345, folder="INBOX")

        assert email_obj is not None
        assert email_obj.uid == 12345
        mock_imap_client.fetch.assert_called_once()

    def test_fetch_emails_serves_each_uid_from_disk(self, tmp_path):
        """``fetch_emails`` resolves each UID via the disk path when files
        exist; zero IMAP traffic."""
        root = _make_maildir_root(tmp_path)
        _write_maildir_message(root, "INBOX", uid=1, subject="one")
        _write_maildir_message(root, "INBOX", uid=2, subject="two")
        client = ImapClient(_make_block_with_maildir(root), local_cache=_eligible_mu())

        with patch("imapclient.IMAPClient") as mock_cls:
            mock_imap = MagicMock()
            mock_cls.return_value = mock_imap
            emails = client.fetch_emails([1, 2], folder="INBOX")

        assert set(emails.keys()) == {1, 2}
        assert emails[1].subject == "one"
        assert emails[2].subject == "two"
        mock_imap.fetch.assert_not_called()

    def test_fetch_emails_mixes_disk_and_imap_on_partial_miss(
        self, tmp_path, mock_imap_client, make_test_email_response_data
    ):
        """When some UIDs hit disk and others miss, the misses fall back
        to IMAP; the merged result is keyed by UID."""
        root = _make_maildir_root(tmp_path)
        _write_maildir_message(root, "INBOX", uid=1, subject="from disk")
        client = ImapClient(_make_block_with_maildir(root), local_cache=_eligible_mu())

        with patch("imapclient.IMAPClient") as mock_cls:
            mock_cls.return_value = mock_imap_client
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.fetch.return_value = {
                2: make_test_email_response_data(uid=2)
            }
            client.connect()
            emails = client.fetch_emails([1, 2], folder="INBOX")

        assert set(emails.keys()) == {1, 2}
        assert emails[1].subject == "from disk"
        fetched_uids = mock_imap_client.fetch.call_args.args[0]
        assert 2 in fetched_uids
        assert 1 not in fetched_uids

    def test_fetch_email_stale_index_uses_imap(
        self, tmp_path, mock_imap_client, test_email_response_data
    ):
        """A stale index sends a read to IMAP even though the file is on
        disk: under one policy, read obeys the same staleness gate as
        search so flags reflect the server."""
        root = _make_maildir_root(tmp_path)
        _write_maildir_message(root, "INBOX", uid=12345, subject="On disk")
        mu = MagicMock()
        mu.is_eligible.return_value = EligibilityResult(False, "stale")
        client = ImapClient(_make_block_with_maildir(root), local_cache=mu)

        with patch("imapclient.IMAPClient") as mock_cls:
            mock_cls.return_value = mock_imap_client
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.fetch.return_value = {12345: test_email_response_data}
            client.connect()
            email_obj = client.fetch_email(12345, folder="INBOX")

        assert email_obj is not None
        mock_imap_client.fetch.assert_called_once_with(
            [12345], ["BODY.PEEK[]", "FLAGS"]
        )

    def test_fetch_email_no_cache_skips_disk(
        self, tmp_path, mock_imap_client, test_email_response_data
    ):
        """``no_cache`` reads from live IMAP even when the file is on disk
        and the index is eligible."""
        root = _make_maildir_root(tmp_path)
        _write_maildir_message(root, "INBOX", uid=12345, subject="On disk")
        client = ImapClient(_make_block_with_maildir(root), local_cache=_eligible_mu())

        with patch("imapclient.IMAPClient") as mock_cls:
            mock_cls.return_value = mock_imap_client
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.fetch.return_value = {12345: test_email_response_data}
            client.connect()
            email_obj = client.fetch_email(12345, folder="INBOX", no_cache=True)

        assert email_obj is not None
        mock_imap_client.fetch.assert_called_once_with(
            [12345], ["BODY.PEEK[]", "FLAGS"]
        )

    def test_fetch_emails_no_cache_skips_disk(
        self, tmp_path, mock_imap_client, make_test_email_response_data
    ):
        """The batch path honours ``no_cache`` too: every UID is fetched
        from IMAP even when files are on disk."""
        root = _make_maildir_root(tmp_path)
        _write_maildir_message(root, "INBOX", uid=1, subject="from disk")
        client = ImapClient(_make_block_with_maildir(root), local_cache=_eligible_mu())

        with patch("imapclient.IMAPClient") as mock_cls:
            mock_cls.return_value = mock_imap_client
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.fetch.return_value = {
                1: make_test_email_response_data(uid=1)
            }
            client.connect()
            emails = client.fetch_emails([1], folder="INBOX", no_cache=True)

        assert set(emails.keys()) == {1}
        mock_imap_client.fetch.assert_called_once_with([1], ["BODY.PEEK[]", "FLAGS"])

    def test_fetch_email_disk_serves_glob_metachar_folder(self, tmp_path):
        """A folder whose name carries glob metacharacters
        (``[Gmail]/Sent Mail``) is escaped so the literal directory matches
        and the read is disk-served rather than silently falling to IMAP."""
        root = str(tmp_path / "maildir")
        folder = "[Gmail]/Sent Mail"
        (Path(root) / folder / "cur").mkdir(parents=True)
        (Path(root) / folder / "new").mkdir(parents=True)
        _write_maildir_message(root, folder, uid=55, subject="Sent from disk")
        client = ImapClient(_make_block_with_maildir(root), local_cache=_eligible_mu())

        with patch("imapclient.IMAPClient") as mock_cls:
            mock_imap = MagicMock()
            mock_cls.return_value = mock_imap
            email_obj = client.fetch_email(55, folder=folder)

        assert email_obj is not None
        assert email_obj.subject == "Sent from disk"
        mock_imap.fetch.assert_not_called()

    def test_mark_email(self, mock_imap_client):
        """Test marking an email with a flag."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Set up mock responses
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}

            # Connect first
            client.connect()

            # Mark email as seen (returns None; failure raises)
            client.mark_email(12345, folder="INBOX", flag=r"\Seen", value=True)

            # Verify select_folder was called with readonly=False for modifying flags
            mock_imap_client.select_folder.assert_called_once_with(
                "INBOX", readonly=False
            )

            # Verify add_flags was called with correct parameters
            mock_imap_client.add_flags.assert_called_once_with([12345], r"\Seen")

            # Reset mocks
            mock_imap_client.select_folder.reset_mock()
            mock_imap_client.add_flags.reset_mock()

            # Mark email as not seen
            client.mark_email(12345, folder="INBOX", flag=r"\Seen", value=False)

            # Verify select_folder was called with readonly=False
            mock_imap_client.select_folder.assert_called_once_with(
                "INBOX", readonly=False
            )

            # Verify remove_flags was called with correct parameters
            mock_imap_client.remove_flags.assert_called_once_with([12345], r"\Seen")

    def test_mark_email_failure(self, mock_imap_client):
        """Test marking an email with a flag when operation fails."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Set up mock responses
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.add_flags.side_effect = Exception("Failed to add flag")

            # Connect first
            client.connect()

            # Mark email failure raises a typed error
            with pytest.raises(CourierError):
                client.mark_email(12345, folder="INBOX", flag=r"\Seen", value=True)

            # Verify select_folder was called with readonly=False
            mock_imap_client.select_folder.assert_called_once_with(
                "INBOX", readonly=False
            )

            # Verify add_flags was called with correct parameters
            mock_imap_client.add_flags.assert_called_once_with([12345], r"\Seen")

    def test_move_email(self, mock_imap_client):
        """Test moving an email to another folder."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Set up mock responses
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            # Legacy server: no MOVE, no UIDPLUS (the copy+expunge path)
            mock_imap_client.has_capability.return_value = False

            # Connect first
            client.connect()

            # Move email (returns None; failure raises)
            client.move_email(12345, source_folder="INBOX", target_folder="Archive")

            # Verify select_folder was called with readonly=False for modifying emails
            mock_imap_client.select_folder.assert_called_once_with(
                "INBOX", readonly=False
            )

            # Verify copy was called with correct parameters
            mock_imap_client.copy.assert_called_once_with([12345], "Archive")

            # Verify add_flags was called to mark as deleted
            mock_imap_client.add_flags.assert_called_once_with([12345], r"\Deleted")

            # Verify expunge was called
            mock_imap_client.expunge.assert_called_once()

    def test_move_email_with_allowed_folders(self, mock_imap_client):
        """Test moving an email with allowed folders restriction."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        allowed_folders = ["INBOX", "Archive"]
        client = ImapClient(replace(config, allowed_folders=allowed_folders))

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Set up mock responses
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.has_capability.return_value = False

            # Connect first
            client.connect()

            # Move email between allowed folders should succeed
            client.move_email(12345, source_folder="INBOX", target_folder="Archive")

            # Verify operations were called
            mock_imap_client.select_folder.assert_called_once_with(
                "INBOX", readonly=False
            )
            mock_imap_client.copy.assert_called_once()

            # Reset mocks
            mock_imap_client.select_folder.reset_mock()
            mock_imap_client.copy.reset_mock()

            # Move email to non-allowed folder should fail
            with pytest.raises(ValueError) as excinfo:
                client.move_email(12345, source_folder="INBOX", target_folder="Trash")

            # Verify error message
            assert "Target folder 'Trash' is not allowed" in str(excinfo.value)

            # Verify no operations were called
            mock_imap_client.select_folder.assert_not_called()
            mock_imap_client.copy.assert_not_called()

    def test_move_email_failure(self, mock_imap_client):
        """Test moving an email when operation fails."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Set up mock responses
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.has_capability.return_value = False
            mock_imap_client.copy.side_effect = Exception("Failed to copy email")

            # Connect first
            client.connect()

            # Move email failure raises a typed error
            with pytest.raises(CourierError):
                client.move_email(12345, source_folder="INBOX", target_folder="Archive")

            # Verify select_folder was called with readonly=False
            mock_imap_client.select_folder.assert_called_once_with(
                "INBOX", readonly=False
            )

            # Verify copy was called with correct parameters
            mock_imap_client.copy.assert_called_once_with([12345], "Archive")

    def test_delete_email(self, mock_imap_client):
        """Test deleting an email."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Set up mock responses
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.has_capability.return_value = False

            # Connect first
            client.connect()

            # Delete email (returns None; failure raises)
            client.delete_email(12345, folder="INBOX")

            # Verify select_folder was called with readonly=False
            mock_imap_client.select_folder.assert_called_once_with(
                "INBOX", readonly=False
            )

            # Verify add_flags was called to mark as deleted
            mock_imap_client.add_flags.assert_called_once_with([12345], r"\Deleted")

            # Verify expunge was called
            mock_imap_client.expunge.assert_called_once()

    def test_delete_email_failure(self, mock_imap_client):
        """Test deleting an email when operation fails."""
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)

        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client

            # Set up mock responses
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.add_flags.side_effect = Exception("Failed to add flag")

            # Connect first
            client.connect()

            # Delete email failure raises a typed error
            with pytest.raises(CourierError):
                client.delete_email(12345, folder="INBOX")

            # Verify select_folder was called with readonly=False
            mock_imap_client.select_folder.assert_called_once_with(
                "INBOX", readonly=False
            )

            # Verify add_flags was called
            mock_imap_client.add_flags.assert_called_once_with([12345], r"\Deleted")


class TestMutationTypedErrors:
    """Typed-error mapping, capability ladder, and multi-UID batches."""

    def _connected_client(self, mock_imap_client) -> ImapClient:
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config)
        with patch("imapclient.IMAPClient") as mock_client_class:
            mock_client_class.return_value = mock_imap_client
            client.connect()
        return client

    def test_abort_error_maps_to_transient(self, mock_imap_client):
        from imapclient.exceptions import IMAPClientAbortError

        mock_imap_client.add_flags.side_effect = IMAPClientAbortError(
            "socket error: EOF"
        )
        client = self._connected_client(mock_imap_client)
        with pytest.raises(TransientError):
            client.mark_email(1, "INBOX", r"\Seen")

    def test_no_bad_error_maps_to_permanent(self, mock_imap_client):
        from imapclient.exceptions import IMAPClientError

        mock_imap_client.add_flags.side_effect = IMAPClientError(
            "STORE command error: BAD"
        )
        client = self._connected_client(mock_imap_client)
        with pytest.raises(PermanentError):
            client.mark_email(1, "INBOX", r"\Seen")

    @pytest.mark.parametrize(
        "caps",
        [
            {"MOVE": True, "UIDPLUS": True},
            {"MOVE": False, "UIDPLUS": True},
            {"MOVE": False, "UIDPLUS": False},
        ],
        ids=["move", "uidplus-only", "neither"],
    )
    def test_move_capability_ladder(self, mock_imap_client, caps):
        mock_imap_client.has_capability.side_effect = lambda cap: caps[cap]
        client = self._connected_client(mock_imap_client)

        client.move_email([7, 9], source_folder="INBOX", target_folder="Archive")

        if caps["MOVE"]:
            mock_imap_client.move.assert_called_once_with([7, 9], "Archive")
            mock_imap_client.copy.assert_not_called()
            mock_imap_client.expunge.assert_not_called()
        else:
            mock_imap_client.move.assert_not_called()
            mock_imap_client.copy.assert_called_once_with([7, 9], "Archive")
            mock_imap_client.add_flags.assert_called_once_with([7, 9], r"\Deleted")
            if caps["UIDPLUS"]:
                # UID EXPUNGE: only our UIDs, not the whole folder
                mock_imap_client.expunge.assert_called_once_with([7, 9])
            else:
                mock_imap_client.expunge.assert_called_once_with()

    @pytest.mark.parametrize("uidplus", [True, False], ids=["uidplus", "legacy"])
    def test_delete_expunge_ladder(self, mock_imap_client, uidplus):
        mock_imap_client.has_capability.return_value = uidplus
        client = self._connected_client(mock_imap_client)

        client.delete_email([4, 5], folder="INBOX")

        mock_imap_client.add_flags.assert_called_once_with([4, 5], r"\Deleted")
        if uidplus:
            mock_imap_client.expunge.assert_called_once_with([4, 5])
        else:
            mock_imap_client.expunge.assert_called_once_with()

    def test_mark_email_batch(self, mock_imap_client):
        client = self._connected_client(mock_imap_client)
        client.mark_email([1, 2, 3], "INBOX", r"\Seen", value=True)
        mock_imap_client.add_flags.assert_called_once_with([1, 2, 3], r"\Seen")

        client.mark_email([1, 2, 3], "INBOX", r"\Seen", value=False)
        mock_imap_client.remove_flags.assert_called_once_with([1, 2, 3], r"\Seen")

    def test_trash_email_batch_returns_resolved_folder(self, mock_imap_client):
        client = self._connected_client(mock_imap_client)
        with (
            patch.object(client, "resolve_trash_folder", return_value="Trash"),
            patch.object(client, "move_email") as mock_move,
        ):
            result = client.trash_email([10, 11], "INBOX")
        assert result == "Trash"
        mock_move.assert_called_once_with([10, 11], "INBOX", "Trash")


class TestRemoteEmitterDispatch:
    """Capability-gated remote dispatch (issues #17 and T3).

    The Gmail emitter is selected when the server advertises
    ``X-GM-EXT-1``, never from hostname substrings, so
    ``imap.googlemail.com`` and any host proxying Gmail dispatch
    correctly, and non-Gmail servers get RFC 3501 criteria.  Standard
    IMAP ``SEARCH TO foo@example.com`` against Gmail's All Mail matches
    every recent message rather than filtering by the To header (issue
    #17), which is why the Gmail emitter must win whenever the
    capability is present.
    """

    def _make_client(self, host: str = "imap.example.com") -> ImapClient:
        config = ImapBlock(
            host=host,
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        return ImapClient(config, world_as_of=None)

    def _connect(self, client, mock_imap_client, gmail: bool):
        caps = [b"IMAP4REV1", b"IDLE"]
        if gmail:
            caps.append(b"X-GM-EXT-1")
        mock_imap_client.capabilities.return_value = caps
        mock_imap_client.list_folders.return_value = [
            ((b"\\HasNoChildren", b"\\All"), b"/", "[Gmail]/All Mail"),
        ]
        mock_imap_client.search.return_value = []
        with patch("imapclient.IMAPClient", return_value=mock_imap_client):
            client.connect()
        return mock_imap_client

    def test_gmail_capability_routes_header_search_via_x_gm_raw(self, mock_imap_client):
        """Wire form for the #17 regression: X-GM-RAW, not bare TO."""
        client = self._make_client()
        wire = self._connect(client, mock_imap_client, gmail=True)
        client.search_emails("to:foo@example.com")
        wire.search.assert_called_once_with(
            [b"X-GM-RAW", b"to:foo@example.com"], charset=None
        )

    def test_dispatch_ignores_hostname(self, mock_imap_client):
        """A gmail hostname without the capability gets RFC 3501 keys
        (and a capable non-gmail hostname got X-GM-RAW above)."""
        client = self._make_client(host="imap.gmail.com")
        wire = self._connect(client, mock_imap_client, gmail=False)
        client.search_emails("to:foo@example.com")
        wire.search.assert_called_once_with([b"TO", b"foo@example.com"], charset=None)

    def test_pure_flag_query_also_uses_gmail_dialect(self, mock_imap_client):
        """The old dispatch sent flag-only queries as standard keys on
        Gmail; the capability gate routes every translatable query."""
        client = self._make_client()
        wire = self._connect(client, mock_imap_client, gmail=True)
        client.search_emails("is:unread")
        wire.search.assert_called_once_with([b"X-GM-RAW", b"is:unread"], charset=None)

    def test_imap_escape_takes_standard_path_on_gmail(self, mock_imap_client):
        client = self._make_client()
        wire = self._connect(client, mock_imap_client, gmail=True)
        client.search_emails("imap:UNSEEN")
        wire.search.assert_called_once_with([b"UNSEEN"], charset=None)

    def test_answered_family_hybrid_on_the_wire(self, mock_imap_client):
        """is:answered has no Gmail spelling; it rides beside X-GM-RAW
        as a standard key (live-verified composition)."""
        client = self._make_client()
        wire = self._connect(client, mock_imap_client, gmail=True)
        client.search_emails("from:alice is:answered")
        wire.search.assert_called_once_with(
            [b"X-GM-RAW", b"from:alice", b"ANSWERED"], charset=None
        )

    def test_msgid_canonicalises_from_the_ast_value(self, mock_imap_client):
        """T12: a quoted msgid loses its quotes and angle brackets."""
        client = self._make_client()
        wire = self._connect(client, mock_imap_client, gmail=True)
        client.search_emails('from:alice msgid:"<abc@host>"')
        wire.search.assert_called_once_with(
            [b"X-GM-RAW", b"from:alice rfc822msgid:abc@host"], charset=None
        )

    def test_non_ascii_value_sends_utf8_charset(self, mock_imap_client):
        """Acceptance row: from:josé produces UTF-8 criteria (T2)."""
        client = self._make_client()
        wire = self._connect(client, mock_imap_client, gmail=False)
        client.search_emails("from:josé")
        wire.search.assert_called_once_with(
            [b"FROM", "josé".encode("utf-8")], charset="UTF-8"
        )

    def test_issue_58_query_emits_nested_criteria(self, mock_imap_client):
        """Acceptance row: the #58 parenthesized OR query reaches the
        wire as nested criteria, not literal paren words."""
        client = self._make_client()
        wire = self._connect(client, mock_imap_client, gmail=False)
        client.search_emails("after:2026-07-13 (ticket OR booking OR itinerary)")
        (criteria,), _ = wire.search.call_args
        assert criteria == [
            b"SINCE",
            date(2026, 7, 13),
            b"OR",
            b"TEXT",
            b"ticket",
            b"OR",
            b"TEXT",
            b"booking",
            b"TEXT",
            b"itinerary",
        ]
        assert b"TEXT" in criteria  # words are terms, never literal parens
        assert not any(
            isinstance(atom, bytes) and atom.startswith(b"(") for atom in criteria
        )

    def test_issue_35_or_chain_right_folds_binary(self, mock_imap_client):
        """Acceptance row: OR is binary, so the n-ary chain right-folds
        (flat splice is unambiguous prefix notation for single keys)."""
        client = self._make_client()
        wire = self._connect(client, mock_imap_client, gmail=False)
        client.search_emails("from:a or from:b or from:c")
        (criteria,), _ = wire.search.call_args
        assert criteria == [
            b"OR",
            b"FROM",
            b"a",
            b"OR",
            b"FROM",
            b"b",
            b"FROM",
            b"c",
        ]

    def test_issue_35_multi_key_or_operand_nests(self, mock_imap_client):
        """The #35 root: a multi-key OR operand becomes one nested
        group instead of silently regrouping as the old flat chain."""
        client = self._make_client()
        wire = self._connect(client, mock_imap_client, gmail=False)
        client.search_emails("(from:a subject:x) or from:b")
        (criteria,), _ = wire.search.call_args
        assert criteria == [
            b"OR",
            [b"FROM", b"a", b"SUBJECT", b"x"],
            b"FROM",
            b"b",
        ]


class TestSearchFolderScope:
    """in: scope and the everywhere-means-everywhere folder set (T9)."""

    def _client_with_folders(self, mock_imap_client, folders, gmail=False):
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config, world_as_of=None)
        caps = [b"IMAP4REV1"] + ([b"X-GM-EXT-1"] if gmail else [])
        mock_imap_client.capabilities.return_value = caps
        mock_imap_client.list_folders.return_value = folders
        mock_imap_client.search.return_value = []
        with patch("imapclient.IMAPClient", return_value=mock_imap_client):
            client.connect()
        return client, mock_imap_client

    GMAIL_FOLDERS = [
        ((b"\\HasNoChildren", b"\\All"), b"/", "[Gmail]/All Mail"),
        ((b"\\HasNoChildren", b"\\Junk"), b"/", "[Gmail]/Spam"),
        ((b"\\HasNoChildren", b"\\Trash"), b"/", "[Gmail]/Bin"),
        ((b"\\HasNoChildren", b"\\Sent"), b"/", "[Gmail]/Sent Mail"),
        ((b"\\HasNoChildren",), b"/", "INBOX"),
    ]

    def test_all_shortcut_adds_junk_and_trash(self, mock_imap_client):
        """T9: the \\All shortcut excluded Spam/Trash silently; a
        folderless search now sweeps them additionally."""
        client, wire = self._client_with_folders(mock_imap_client, self.GMAIL_FOLDERS)
        result = client.search_emails("from:alice")
        assert result["provenance"]["folders_searched"] == [
            "[Gmail]/All Mail",
            "[Gmail]/Spam",
            "[Gmail]/Bin",
        ]
        assert wire.search.call_count == 3

    def test_in_sent_resolves_special_use(self, mock_imap_client):
        client, wire = self._client_with_folders(mock_imap_client, self.GMAIL_FOLDERS)
        result = client.search_emails("in:sent from:alice")
        assert result["provenance"]["folders_searched"] == ["[Gmail]/Sent Mail"]
        (criteria,), _ = wire.search.call_args
        assert criteria == [b"FROM", b"alice"]

    def test_negated_in_trash_subtracts_from_the_sweep(self, mock_imap_client):
        client, _ = self._client_with_folders(mock_imap_client, self.GMAIL_FOLDERS)
        result = client.search_emails("-in:trash from:alice")
        assert result["provenance"]["folders_searched"] == [
            "[Gmail]/All Mail",
            "[Gmail]/Spam",
        ]

    def test_in_sent_without_special_use_raises_folder_not_found(
        self, mock_imap_client
    ):
        from courier.errors import FolderNotFound

        client, _ = self._client_with_folders(
            mock_imap_client, [((b"\\HasNoChildren",), b"/", "INBOX")]
        )
        with pytest.raises(FolderNotFound, match="Sent"):
            client.search_emails("in:sent from:alice")

    def test_gmail_path_keeps_in_scope_inside_the_raw_string(self, mock_imap_client):
        """Gmail speaks in: natively, so the raw string carries the
        scope and the physical sweep stays the default set."""
        client, wire = self._client_with_folders(
            mock_imap_client, self.GMAIL_FOLDERS, gmail=True
        )
        result = client.search_emails("in:sent from:alice")
        (criteria,), _ = wire.search.call_args
        assert criteria == [b"X-GM-RAW", b"in:sent from:alice"]
        assert result["provenance"]["folders_searched"] == [
            "[Gmail]/All Mail",
            "[Gmail]/Spam",
            "[Gmail]/Bin",
        ]

    def test_folder_argument_with_in_scope_refuses(self, mock_imap_client):
        client, _ = self._client_with_folders(mock_imap_client, self.GMAIL_FOLDERS)
        with pytest.raises(ValueError, match="not both"):
            client.search_emails("in:sent from:alice", folder="INBOX")


class TestSearchFailureEnvelope:
    """Per-folder failures land in folders_failed; BADCHARSET aborts."""

    def _client(self, mock_imap_client, folders, search_side_effect):
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config, world_as_of=None)
        mock_imap_client.capabilities.return_value = [b"IMAP4REV1"]
        mock_imap_client.list_folders.return_value = folders
        mock_imap_client.search.side_effect = search_side_effect
        with patch("imapclient.IMAPClient", return_value=mock_imap_client):
            client.connect()
        return client

    TWO_FOLDERS = [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\HasNoChildren",), b"/", "Archive"),
    ]

    def test_folder_failure_enters_the_envelope(self, mock_imap_client):
        """#57: a rejected SEARCH or read timeout is envelope-visible,
        not syslog-only, and absent means no folder failed."""
        calls = iter([Exception("read timeout"), []])

        def side_effect(*args, **kwargs):
            item = next(calls)
            if isinstance(item, Exception):
                raise item
            return item

        client = self._client(mock_imap_client, self.TWO_FOLDERS, side_effect)
        result = client.search_emails("from:alice")
        assert result["folders_failed"] == [
            {"folder": "INBOX", "error": "read timeout"}
        ]
        assert result["results"] == []

    def test_clean_search_has_no_folders_failed_key(self, mock_imap_client):
        client = self._client(mock_imap_client, self.TWO_FOLDERS, lambda *a, **k: [])
        result = client.search_emails("from:alice")
        assert "folders_failed" not in result

    def test_badcharset_raises_a_block_error(self, mock_imap_client):
        """T2 follow-through: a refused CHARSET is a per-account error,
        never a clean-empty envelope."""
        from courier.errors import PermanentError

        def side_effect(*args, **kwargs):
            raise Exception("SEARCH command error: BAD [BADCHARSET (US-ASCII)]")

        client = self._client(mock_imap_client, self.TWO_FOLDERS, side_effect)
        with pytest.raises(PermanentError, match="non-ASCII"):
            client.search_emails("from:josé")

    def test_total_count_and_truncated_report_the_limit_cut(self, mock_imap_client):
        client = self._client(
            mock_imap_client,
            [((b"\\HasNoChildren",), b"/", "INBOX")],
            lambda *a, **k: [1, 2, 3],
        )
        mock_imap_client.fetch.return_value = {
            uid: {b"INTERNALDATE": datetime(2026, 7, uid, 12, 0, 0)}
            for uid in (1, 2, 3)
        }
        with patch.object(client, "fetch_emails", return_value={}):
            result = client.search_emails("from:alice", folder="INBOX", limit=2)
        assert result["total_count"] == 3
        assert result["truncated"] is True


class TestSearchEmailsDispatch:
    """Wrapping and local-cache dispatch behaviour of search_emails.

    ``search_emails`` always returns a wrapped ``{"results", "provenance"}``
    dict and dispatches to the local-cache backend when configured and
    eligible, falling back to IMAP otherwise.  These tests pin the
    wrapping shape and the fallback-reason vocabulary.
    """

    def _make_config(self, host: str = "imap.example.com") -> ImapBlock:
        return ImapBlock(
            host=host,
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )

    def _make_block_with_maildir(
        self, maildir: str = "/var/local/mail/test-block"
    ) -> ImapBlock:
        return ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
            maildir=maildir,
        )

    @staticmethod
    def _remote_outcome(results=None, folders=("INBOX",)):
        from courier.imap_client import _RemoteSearch

        results = results or []
        return _RemoteSearch(
            results,
            0,
            [],
            len(results),
            TranslationReport(dialect="imap"),
            list(folders),
        )

    def test_search_emails_wraps_with_provenance_imap_path(self):
        """No local_cache configured → IMAP path runs and result is wrapped."""
        config = self._make_config()
        client = ImapClient(config)

        with patch.object(
            client, "_search_emails_imap", return_value=self._remote_outcome()
        ) as mock_imap:
            result = client.search_emails("from:alice")

        parsed = parse("from:alice")
        remaining, scope = extract_scope(parsed)
        mock_imap.assert_called_once_with(parsed, remaining, scope, None, 10, False)
        assert result == {
            "results": [],
            "provenance": {
                "source": "remote",
                "indexed_at": None,
                "fell_back_reason": None,
                "query": {
                    "dialect": "imap",
                    "approximations": [],
                    "fallbacks": [],
                    "treated_as_text": [],
                },
                "folders_searched": ["INBOX"],
            },
            "total_count": 0,
            "truncated": False,
        }

    def test_search_emails_dispatches_to_mu_when_eligible(self):
        """Eligible local_cache short-circuits the IMAP path."""
        block = self._make_block_with_maildir()

        canned = [
            {
                "message_id": "<m@x>",
                "path": "/var/local/mail/test-account/cur/123",
                "folder": "INBOX",
                "from": "Alice <a@b.com>",
                "to": ["c@d.com"],
                "subject": "Hi",
                "date": "2025-01-01T00:00:00+00:00",
                "flags": ["seen"],
                "has_attachments": False,
            }
        ]
        mu = MagicMock()
        mu.is_eligible.return_value = EligibilityResult(True)
        mu.search.return_value = _mu_hit(canned)
        mu.index_mtime_iso.return_value = "2025-04-01T12:00:00+00:00"

        client = ImapClient(block, local_cache=mu)

        with patch.object(client, "_search_emails_imap") as mock_imap:
            result = client.search_emails("from:alice")

        mock_imap.assert_not_called()
        remaining, _ = extract_scope(parse("from:alice"))
        mu.search.assert_called_once_with(block, remaining, 10, None, world_as_of=None)
        assert result["results"] == canned
        assert result["provenance"]["source"] == "local"
        assert result["provenance"]["indexed_at"] == "2025-04-01T12:00:00+00:00"
        assert result["provenance"]["fell_back_reason"] is None
        assert result["provenance"]["query"]["dialect"] == "mu"
        assert result["total_count"] == 1
        assert result["truncated"] is False

    def test_search_emails_truncated_cache_page_has_no_total(self):
        """A cache page cut at the limit cannot know the match count."""
        block = self._make_block_with_maildir()

        mu = MagicMock()
        mu.is_eligible.return_value = EligibilityResult(True)
        mu.search.return_value = _mu_hit([{"subject": "x", "flags": []}], True)

        client = ImapClient(block, local_cache=mu)
        result = client.search_emails("from:alice", limit=1)
        assert result["truncated"] is True
        assert result["total_count"] is None

    def test_search_emails_falls_back_on_mu_exception(self):
        """A MuFailure from the backend triggers an IMAP fallback."""
        block = self._make_block_with_maildir()

        mu = MagicMock()
        mu.is_eligible.return_value = EligibilityResult(True)
        mu.search.side_effect = MuFailure("boom")

        client = ImapClient(block, local_cache=mu)

        with patch.object(
            client, "_search_emails_imap", return_value=self._remote_outcome()
        ) as mock_imap:
            result = client.search_emails("from:alice")

        mock_imap.assert_called_once()
        assert result["provenance"]["source"] == "remote"
        assert result["provenance"]["fell_back_reason"] == "exception"
        assert result["provenance"]["query"]["fallbacks"] == [
            {"backend": "cache", "reason": "exception"}
        ]

    def test_search_emails_falls_back_on_untranslatable(self):
        """An UntranslatableQuery from the backend triggers an IMAP fallback."""
        block = self._make_block_with_maildir()

        mu = MagicMock()
        mu.is_eligible.return_value = EligibilityResult(True)
        mu.search.side_effect = UntranslatableQuery(
            "mu", "imap:", "raw IMAP expressions cannot run against the local cache"
        )

        client = ImapClient(block, local_cache=mu)

        with patch.object(
            client, "_search_emails_imap", return_value=self._remote_outcome()
        ) as mock_imap:
            result = client.search_emails("imap:UNSEEN")

        mock_imap.assert_called_once()
        assert result["provenance"]["source"] == "remote"
        assert result["provenance"]["fell_back_reason"] == "untranslatable"

    def test_search_emails_with_folder_uses_cache(self):
        """A folder-scoped search is served from the cache, passing the
        folder through to the backend for an exact maildir scope."""
        block = self._make_block_with_maildir()

        mu = MagicMock()
        mu.is_eligible.return_value = EligibilityResult(True)
        mu.search.return_value = _mu_hit([])
        mu.index_mtime_iso.return_value = "2025-04-01T12:00:00+00:00"

        client = ImapClient(block, local_cache=mu)

        with patch.object(
            client, "_search_emails_imap", return_value=self._remote_outcome()
        ) as mock_imap:
            result = client.search_emails("from:alice", folder="INBOX")

        mock_imap.assert_not_called()
        remaining, _ = extract_scope(parse("from:alice"))
        mu.search.assert_called_once_with(
            block, remaining, 10, "INBOX", world_as_of=None
        )
        assert result["provenance"]["source"] == "local"
        assert result["provenance"]["fell_back_reason"] is None

    def test_search_emails_in_scope_serves_from_cache(self):
        """A cache-expressible in: scope becomes the exact mu folder."""
        block = self._make_block_with_maildir()

        mu = MagicMock()
        mu.is_eligible.return_value = EligibilityResult(True)
        mu.search.return_value = _mu_hit([])
        mu.index_mtime_iso.return_value = "2025-04-01T12:00:00+00:00"

        client = ImapClient(block, local_cache=mu)
        client.search_emails("in:inbox from:alice")

        remaining, _ = extract_scope(parse("in:inbox from:alice"))
        mu.search.assert_called_once_with(
            block, remaining, 10, "INBOX", world_as_of=None
        )

    def test_search_emails_special_use_scope_declines_cache(self):
        """in:sent needs the server's SPECIAL-USE; the cache declines."""
        block = self._make_block_with_maildir()

        mu = MagicMock()
        mu.is_eligible.return_value = EligibilityResult(True)

        client = ImapClient(block, local_cache=mu)
        with patch.object(
            client, "_search_emails_imap", return_value=self._remote_outcome()
        ) as mock_imap:
            result = client.search_emails("in:sent from:alice")

        mu.search.assert_not_called()
        mock_imap.assert_called_once()
        assert result["provenance"]["fell_back_reason"] == "untranslatable"

    def test_search_emails_no_cache_forces_imap(self):
        """``no_cache`` bypasses an eligible cache and reports the reason."""
        block = self._make_block_with_maildir()

        mu = MagicMock()
        mu.is_eligible.return_value = EligibilityResult(True)

        client = ImapClient(block, local_cache=mu)

        with patch.object(
            client, "_search_emails_imap", return_value=self._remote_outcome()
        ) as mock_imap:
            result = client.search_emails("from:alice", no_cache=True)

        parsed = parse("from:alice")
        remaining, scope = extract_scope(parsed)
        mock_imap.assert_called_once_with(parsed, remaining, scope, None, 10, True)
        mu.search.assert_not_called()
        mu.is_eligible.assert_not_called()
        assert result["provenance"]["source"] == "remote"
        assert result["provenance"]["fell_back_reason"] == "no_cache"

    def test_search_emails_falls_back_on_mu_missing(self):
        """is_eligible returning ``mu_missing`` forces an IMAP fallback."""
        block = self._make_block_with_maildir()

        mu = MagicMock()
        mu.is_eligible.return_value = EligibilityResult(False, "mu_missing")

        client = ImapClient(block, local_cache=mu)

        with patch.object(
            client, "_search_emails_imap", return_value=self._remote_outcome()
        ) as mock_imap:
            result = client.search_emails("from:alice")

        mock_imap.assert_called_once()
        mu.search.assert_not_called()
        assert result["provenance"]["fell_back_reason"] == "mu_missing"

    def test_terminal_refusal_names_the_cache_decline(self):
        """Untranslatable everywhere: the error names each backend's
        reason, eligibility declines included."""
        block = self._make_block_with_maildir()

        mu = MagicMock()
        mu.is_eligible.return_value = EligibilityResult(False, "stale")

        client = ImapClient(block, local_cache=mu)
        with patch.object(
            client,
            "_search_emails_imap",
            side_effect=UntranslatableQuery(
                "imap", "label:", "labels are Gmail-only on this backend"
            ),
        ):
            with pytest.raises(PermanentError) as excinfo:
                client.search_emails("label:work")
        message = str(excinfo.value)
        assert "label:" in message
        assert "your local cache exists but its index is stale" in message


class TestProcessEmailAction:
    """Tests for ImapClient.process_email_action dispatcher."""

    def _make_client(self):
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        return ImapClient(config)

    def test_move(self):
        client = self._make_client()
        with patch.object(client, "move_email") as mock_move:
            result = client.process_email_action(
                1, "INBOX", "move", target_folder="Archive"
            )
            mock_move.assert_called_once_with(1, "INBOX", "Archive")
            assert result == "Email moved from INBOX to Archive"

    def test_read(self):
        client = self._make_client()
        with patch.object(client, "mark_email") as mock_mark:
            result = client.process_email_action(1, "INBOX", "read")
            mock_mark.assert_called_once_with(1, "INBOX", r"\Seen", True)
            assert result == "Email marked as read"

    def test_unread(self):
        client = self._make_client()
        with patch.object(client, "mark_email") as mock_mark:
            result = client.process_email_action(1, "INBOX", "unread")
            mock_mark.assert_called_once_with(1, "INBOX", r"\Seen", False)
            assert result == "Email marked as unread"

    def test_flag(self):
        client = self._make_client()
        with patch.object(client, "mark_email") as mock_mark:
            result = client.process_email_action(1, "INBOX", "flag")
            mock_mark.assert_called_once_with(1, "INBOX", r"\Flagged", True)
            assert result == "Email flagged"

    def test_unflag(self):
        client = self._make_client()
        with patch.object(client, "mark_email") as mock_mark:
            result = client.process_email_action(1, "INBOX", "unflag")
            mock_mark.assert_called_once_with(1, "INBOX", r"\Flagged", False)
            assert result == "Email unflagged"

    def test_delete(self):
        client = self._make_client()
        with patch.object(client, "delete_email") as mock_delete:
            result = client.process_email_action(1, "INBOX", "delete")
            mock_delete.assert_called_once_with(1, "INBOX")
            assert result == "Email deleted"

    def test_move_missing_target_folder(self):
        client = self._make_client()
        with pytest.raises(ValueError, match="target_folder is required"):
            client.process_email_action(1, "INBOX", "move")

    def test_unknown_action(self):
        client = self._make_client()
        with pytest.raises(ValueError, match="Unknown action 'archive'"):
            client.process_email_action(1, "INBOX", "archive")


class TestSearchEmailsImapResultShape:
    """The IMAP-remote search path must include `message_id` per hit, matching
    the local-cache path (`local_cache.py` already emits it). aesop SPAR-A
    consumes the dispatcher prefetch text and needs Message-ID to thread a
    reply onto the parent."""

    def _make_client(self) -> ImapClient:
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        return ImapClient(config)

    def _make_email(self, message_id: str = "<m1@example.com>") -> Email:
        from datetime import datetime

        from courier.models import EmailAddress, EmailContent

        return Email(
            message_id=message_id,
            subject="Hi",
            from_=EmailAddress(name="Alice", address="alice@example.com"),
            to=[EmailAddress(name="Bob", address="bob@example.com")],
            cc=[],
            date=datetime(2026, 4, 1, 10, 0, 0),
            content=EmailContent(text="body", html=None),
            attachments=[],
            flags=["\\Seen"],
            headers={},
            folder="INBOX",
            uid=42,
        )

    @staticmethod
    def _run_remote(client, query, folder=None, limit=10):
        """Call _search_emails_imap through the parse/scope front door."""
        parsed = parse(query)
        remaining, scope = extract_scope(parsed)
        return client._search_emails_imap(parsed, remaining, scope, folder, limit)

    def test_imap_search_includes_message_id(self):
        """Every dict returned by `_search_emails_imap` carries `message_id`."""
        client = self._make_client()

        with (
            patch.object(client, "ensure_connected"),
            patch.object(client, "get_capabilities", return_value=["IMAP4REV1"]),
            patch.object(client, "find_special_use_folder", return_value=None),
            patch.object(client, "list_folders", return_value=["INBOX"]),
            patch.object(client, "search", return_value=[42]),
            patch.object(client, "select_folder"),
            patch.object(client, "_client_or_raise") as mock_clientor,
            patch.object(
                client,
                "fetch_emails",
                return_value={42: self._make_email("<m1@example.com>")},
            ),
        ):
            from datetime import datetime

            mock_clientor.return_value.fetch.return_value = {
                42: {b"INTERNALDATE": datetime(2026, 4, 1, 10, 0, 0)}
            }
            results = self._run_remote(client, "from:alice", folder="INBOX").results

        assert len(results) == 1
        assert results[0]["message_id"] == "<m1@example.com>"
        # Existing keys must remain.
        for key in (
            "uid",
            "folder",
            "from",
            "to",
            "subject",
            "date",
            "flags",
            "has_attachments",
        ):
            assert key in results[0]

    def test_imap_search_uses_special_use_all_folder(self):
        """When the server advertises a SPECIAL-USE \\All folder (Gmail's
        ``[Gmail]/All Mail``, Fastmail's ``Archive``), the search runs against
        that one folder rather than iterating every selectable folder."""
        from datetime import datetime

        client = self._make_client()

        with (
            patch.object(client, "ensure_connected"),
            patch.object(client, "get_capabilities", return_value=["IMAP4REV1"]),
            patch.object(
                client, "find_special_use_folder", return_value="[Gmail]/All Mail"
            ),
            patch.object(client, "list_folders") as mock_list,
            patch.object(client, "search", return_value=[1]),
            patch.object(client, "select_folder"),
            patch.object(client, "_client_or_raise") as mock_clientor,
            patch.object(client, "fetch_emails", return_value={1: self._make_email()}),
        ):
            mock_clientor.return_value.fetch.return_value = {
                1: {b"INTERNALDATE": datetime(2026, 4, 1, 10, 0, 0)}
            }
            self._run_remote(client, "from:alice")

        mock_list.assert_not_called()

    def test_imap_search_skips_folder_when_pass1_search_raises(self, caplog):
        """A per-folder error during pass 1 is logged and the loop continues
        with the remaining folders rather than aborting the whole search."""
        from datetime import datetime

        client = self._make_client()

        def search_side_effect(spec, folder=None, charset=None):
            if folder == "Broken":
                raise RuntimeError("server hiccup")
            return [42]

        with (
            patch.object(client, "ensure_connected"),
            patch.object(client, "get_capabilities", return_value=["IMAP4REV1"]),
            patch.object(client, "find_special_use_folder", return_value=None),
            patch.object(client, "list_folders", return_value=["Broken", "INBOX"]),
            patch.object(client, "search", side_effect=search_side_effect),
            patch.object(client, "select_folder"),
            patch.object(client, "_client_or_raise") as mock_clientor,
            patch.object(
                client,
                "fetch_emails",
                return_value={42: self._make_email()},
            ),
            caplog.at_level("WARNING"),
        ):
            mock_clientor.return_value.fetch.return_value = {
                42: {b"INTERNALDATE": datetime(2026, 4, 1, 10, 0, 0)}
            }
            results = self._run_remote(client, "from:alice").results

        assert len(results) == 1
        assert results[0]["folder"] == "INBOX"
        assert any("Broken" in m and "server hiccup" in m for m in caplog.messages)

    def test_imap_search_skips_folder_when_pass2_fetch_raises(self, caplog):
        """A per-folder error during pass 2 (full fetch) is logged and other
        folders' results are still returned."""
        from datetime import datetime

        client = self._make_client()

        def fetch_emails_side_effect(uids, folder="INBOX", no_cache=False):
            if folder == "Broken":
                raise RuntimeError("fetch failed")
            return {uids[0]: self._make_email()}

        with (
            patch.object(client, "ensure_connected"),
            patch.object(client, "get_capabilities", return_value=["IMAP4REV1"]),
            patch.object(client, "find_special_use_folder", return_value=None),
            patch.object(client, "list_folders", return_value=["Broken", "INBOX"]),
            patch.object(client, "search", return_value=[1]),
            patch.object(client, "select_folder"),
            patch.object(client, "_client_or_raise") as mock_clientor,
            patch.object(client, "fetch_emails", side_effect=fetch_emails_side_effect),
            caplog.at_level("WARNING"),
        ):
            mock_clientor.return_value.fetch.return_value = {
                1: {b"INTERNALDATE": datetime(2026, 4, 1, 10, 0, 0)}
            }
            results = self._run_remote(client, "from:alice").results

        # INBOX result returned; Broken folder skipped
        assert len(results) == 1
        assert results[0]["folder"] == "INBOX"
        assert any("Broken" in m and "fetch failed" in m for m in caplog.messages)

    def test_imap_search_includes_redacted_by_when_set(self):
        """``redacted_by`` on the parsed Email carries through to the search
        result so the model sees the redaction attribution alongside the
        envelope."""
        from datetime import datetime

        client = self._make_client()
        email_obj = self._make_email()
        email_obj.redacted_by = "newsletter-rule"

        with (
            patch.object(client, "ensure_connected"),
            patch.object(client, "get_capabilities", return_value=["IMAP4REV1"]),
            patch.object(client, "find_special_use_folder", return_value=None),
            patch.object(client, "list_folders", return_value=["INBOX"]),
            patch.object(client, "search", return_value=[42]),
            patch.object(client, "select_folder"),
            patch.object(client, "_client_or_raise") as mock_clientor,
            patch.object(client, "fetch_emails", return_value={42: email_obj}),
        ):
            mock_clientor.return_value.fetch.return_value = {
                42: {b"INTERNALDATE": datetime(2026, 4, 1, 10, 0, 0)}
            }
            results = self._run_remote(client, "from:alice", folder="INBOX").results

        assert results[0]["redacted_by"] == "newsletter-rule"

    def test_imap_search_global_top_n_across_folders(self):
        """The two-pass pipeline keeps only the top-N candidates after sorting
        across all folders, so a date-newer hit in folder B beats an older hit
        in folder A regardless of folder iteration order."""
        from datetime import datetime

        client = self._make_client()
        email_a = self._make_email("<a@example.com>")
        email_a.uid = 10
        email_b = self._make_email("<b@example.com>")
        email_b.uid = 20

        def search_side_effect(spec, folder=None, charset=None):
            return {"FolderA": [10], "FolderB": [20]}[folder]

        def fetch_emails_side_effect(uids, folder="INBOX", no_cache=False):
            return {10: email_a} if folder == "FolderA" else {20: email_b}

        with (
            patch.object(client, "ensure_connected"),
            patch.object(client, "get_capabilities", return_value=["IMAP4REV1"]),
            patch.object(client, "find_special_use_folder", return_value=None),
            patch.object(client, "list_folders", return_value=["FolderA", "FolderB"]),
            patch.object(client, "search", side_effect=search_side_effect),
            patch.object(client, "select_folder"),
            patch.object(client, "_client_or_raise") as mock_clientor,
            patch.object(client, "fetch_emails", side_effect=fetch_emails_side_effect),
        ):
            # FolderA's hit is older; FolderB's hit is newer. Limit=1 keeps B.
            mock_clientor.return_value.fetch.side_effect = [
                {10: {b"INTERNALDATE": datetime(2026, 1, 1, 10, 0, 0)}},
                {20: {b"INTERNALDATE": datetime(2026, 4, 1, 10, 0, 0)}},
            ]
            results = self._run_remote(client, "from:alice", limit=1).results

        assert len(results) == 1
        assert results[0]["folder"] == "FolderB"


class TestResolveSentFolder:
    """``resolve_sent_folder`` is the pre-send verification entry point.

    It backs the FCC-folder check that the issue (#22) requires before
    SMTP opens, so the failure modes (missing folder, wrong configured
    name) must be deterministic and not silently fall back away from a
    user-pinned value.
    """

    def _make_client(self, host: str = "mail.example.com") -> ImapClient:
        client = ImapClient(
            ImapBlock(
                host=host,
                port=993,
                username="test@example.com",
                password="password",
                use_ssl=True,
            )
        )
        client.connected = True
        return client

    def test_special_use_wins_over_name_fallback(self):
        """RFC 6154 SPECIAL-USE \\Sent is the authoritative answer."""
        client = self._make_client()
        with (
            patch.object(client, "ensure_connected"),
            patch.object(
                client,
                "list_folders",
                return_value=["INBOX", "INBOX.Sent", "Saved"],
            ),
            patch.object(client, "find_special_use_folder", return_value="Saved"),
        ):
            assert client.resolve_sent_folder() == "Saved"

    def test_dovecot_inbox_sent_picked_when_no_special_use(self):
        """Bare ``Sent`` would be rejected by Dovecot's namespace; INBOX.Sent wins."""
        client = self._make_client()
        with (
            patch.object(client, "ensure_connected"),
            patch.object(
                client,
                "list_folders",
                return_value=["INBOX", "INBOX.Sent", "INBOX.Drafts"],
            ),
            patch.object(client, "find_special_use_folder", return_value=None),
        ):
            assert client.resolve_sent_folder() == "INBOX.Sent"

    def test_plain_sent_picked_when_no_inbox_prefix(self):
        client = self._make_client()
        with (
            patch.object(client, "ensure_connected"),
            patch.object(
                client, "list_folders", return_value=["INBOX", "Sent", "Drafts"]
            ),
            patch.object(client, "find_special_use_folder", return_value=None),
        ):
            assert client.resolve_sent_folder() == "Sent"

    def test_returns_none_when_nothing_matches(self):
        """Caller distinguishes 'no folder' from 'configured name not found'
        by whether ``configured`` was passed."""
        client = self._make_client()
        with (
            patch.object(client, "ensure_connected"),
            patch.object(
                client, "list_folders", return_value=["INBOX", "Drafts", "Trash"]
            ),
            patch.object(client, "find_special_use_folder", return_value=None),
        ):
            assert client.resolve_sent_folder() is None

    def test_configured_name_verified_no_silent_fallback(self):
        """Configured ``Sent`` must not silently rewrite to the existing INBOX.Sent.

        The whole point of the pre-send check is to surface the user's
        misconfiguration before SMTP runs; an auto-rewrite would mask it.
        """
        client = self._make_client()
        with (
            patch.object(client, "ensure_connected"),
            patch.object(client, "list_folders", return_value=["INBOX", "INBOX.Sent"]),
            patch.object(client, "find_special_use_folder", return_value=None),
        ):
            assert client.resolve_sent_folder(configured="Sent") is None

    def test_configured_name_returns_server_case(self):
        """A configured ``sent`` matches a server ``Sent`` and returns ``Sent``."""
        client = self._make_client()
        with (
            patch.object(client, "ensure_connected"),
            patch.object(client, "list_folders", return_value=["INBOX", "Sent"]),
            patch.object(client, "find_special_use_folder", return_value=None),
        ):
            assert client.resolve_sent_folder(configured="sent") == "Sent"


class TestRedactOnFetch:
    """Per-block redact policy replaces matched fetches with placeholders.

    The policy is a callable on ``ImapBlock.redact_policy`` that takes
    an ``Email`` and returns ``True`` to mean "replace with placeholder".
    Tests stub the callable directly, since the sievelib parsing path
    is exercised separately in ``tests/test_sieve_filter.py``.
    """

    def _make_block_with_policy(self, predicate) -> ImapBlock:
        return ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
            redact_policy=predicate,
        )

    def test_fetch_email_redacts_match(
        self, mock_imap_client, test_email_response_data
    ):
        """``fetch_email`` returns a placeholder Email when policy matches."""
        block = self._make_block_with_policy(lambda e: True)
        client = ImapClient(block)
        with patch("imapclient.IMAPClient") as mock_cls:
            mock_cls.return_value = mock_imap_client
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.fetch.return_value = {12345: test_email_response_data}
            client.connect()
            result = client.fetch_email(12345, folder="INBOX")
        assert result is not None
        assert result.redacted_by == "redacted"
        assert result.from_.address == "[redacted]"
        assert result.uid == 12345

    def test_fetch_email_passthrough_when_predicate_false(
        self, mock_imap_client, test_email_response_data
    ):
        """A predicate that returns False yields the original email."""
        block = self._make_block_with_policy(lambda e: False)
        client = ImapClient(block)
        with patch("imapclient.IMAPClient") as mock_cls:
            mock_cls.return_value = mock_imap_client
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.fetch.return_value = {12345: test_email_response_data}
            client.connect()
            result = client.fetch_email(12345, folder="INBOX")
        assert result is not None
        assert result.redacted_by is None
        assert result.from_.address != "[redacted]"

    def test_fetch_emails_redacts_only_matching(
        self, mock_imap_client, make_test_email_response_data
    ):
        """Per-message predicate evaluation; some matched, some not."""
        block = self._make_block_with_policy(lambda e: e.uid == 2)
        client = ImapClient(block)
        with patch("imapclient.IMAPClient") as mock_cls:
            mock_cls.return_value = mock_imap_client
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.fetch.return_value = {
                1: make_test_email_response_data(uid=1),
                2: make_test_email_response_data(uid=2),
                3: make_test_email_response_data(uid=3),
            }
            client.connect()
            emails = client.fetch_emails([1, 2, 3], folder="INBOX")
        assert emails[1].redacted_by is None
        assert emails[2].redacted_by == "redacted"
        assert emails[2].from_.address == "[redacted]"
        assert emails[3].redacted_by is None

    def test_fetch_email_disk_hit_applies_redact(self, tmp_path):
        """A redact policy that fires on disk-served mail still redacts —
        the policy is callable-on-Email and indifferent to source."""
        root = _make_maildir_root(tmp_path)
        _write_maildir_message(root, "INBOX", uid=7, subject="confidential")
        block = _make_block_with_maildir(root, redact_policy=lambda e: True)
        client = ImapClient(block, local_cache=_eligible_mu())

        with patch("imapclient.IMAPClient") as mock_cls:
            mock_imap = MagicMock()
            mock_cls.return_value = mock_imap
            email_obj = client.fetch_email(7, folder="INBOX")

        assert email_obj is not None
        assert email_obj.redacted_by == "redacted"
        assert email_obj.from_.address == "[redacted]"
        assert email_obj.uid == 7
        mock_imap.fetch.assert_not_called()

    def test_fetch_raw_returns_blank_bytes_for_redacted(self, mock_imap_client):
        """``fetch_raw`` blanks the bytes and tags the dict for redacted UIDs."""
        block = self._make_block_with_policy(lambda e: True)
        client = ImapClient(block)
        raw_bytes = (
            b"From: alice@example.com\r\n"
            b"To: bob@example.com\r\n"
            b"Subject: confidential\r\n"
            b"\r\n"
            b"body text\r\n"
        )
        with patch("imapclient.IMAPClient") as mock_cls:
            mock_cls.return_value = mock_imap_client
            mock_imap_client.select_folder.return_value = {b"EXISTS": 10}
            mock_imap_client.fetch.return_value = {
                7: {
                    b"BODY[]": raw_bytes,
                    b"FLAGS": (),
                    b"INTERNALDATE": None,
                }
            }
            client.connect()
            result = client.fetch_raw(7, folder="INBOX")
        assert result is not None
        assert result["raw"] == b""
        assert result["redacted_by"] == "redacted"
        assert result["subject"].startswith("[redacted")


class TestFetchSummaries:
    """Summary-level listing: headers, flags, structure — no bodies."""

    def _connected(self, mock_imap_client) -> ImapClient:
        config = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
            use_ssl=True,
        )
        client = ImapClient(config, world_as_of=None)
        with patch("imapclient.IMAPClient", return_value=mock_imap_client):
            client.connect()
        return client

    HEADER = (
        b"From: Alice <alice@example.com>\r\n"
        b"To: bob@example.com\r\n"
        b"Subject: Hello\r\n"
        b"Date: Wed, 01 Apr 2026 10:00:00 +1000\r\n"
        b"Message-ID: <m1@example.com>\r\n\r\n"
    )

    def test_fetch_is_header_level_not_full_body(self, mock_imap_client):
        client = self._connected(mock_imap_client)
        mock_imap_client.fetch.return_value = {
            7: {
                b"BODY[HEADER]": self.HEADER,
                b"FLAGS": (b"\\Seen",),
                b"BODYSTRUCTURE": (b"text", b"plain"),
            }
        }
        summaries = client.fetch_summaries([7], folder="INBOX")
        items = mock_imap_client.fetch.call_args[0][1]
        assert "BODY.PEEK[HEADER]" in items
        assert "BODY.PEEK[]" not in items
        assert summaries == [
            {
                "uid": 7,
                "folder": "INBOX",
                "from": "Alice <alice@example.com>",
                "to": ["bob@example.com"],
                "subject": "Hello",
                "date": summaries[0]["date"],  # zone-dependent rendering
                "flags": ["\\Seen"],
                "has_attachments": False,
            }
        ]
        assert summaries[0]["date"].startswith("2026-04-01")

    def test_attachment_disposition_detected_from_structure(self, mock_imap_client):
        client = self._connected(mock_imap_client)
        structure = (
            (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7bit", 5, 1),
            (
                b"application",
                b"pdf",
                (b"name", b"q3.pdf"),
                None,
                None,
                b"base64",
                1000,
                None,
                (b"attachment", (b"filename", b"q3.pdf")),
                None,
            ),
            b"mixed",
        )
        mock_imap_client.fetch.return_value = {
            8: {
                b"BODY[HEADER]": self.HEADER,
                b"FLAGS": (),
                b"BODYSTRUCTURE": structure,
            }
        }
        summaries = client.fetch_summaries([8], folder="INBOX")
        assert summaries[0]["has_attachments"] is True

    def test_empty_uid_list_short_circuits(self, mock_imap_client):
        client = self._connected(mock_imap_client)
        assert client.fetch_summaries([], folder="INBOX") == []
        mock_imap_client.fetch.assert_not_called()
