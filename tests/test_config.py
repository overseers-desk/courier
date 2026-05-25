"""Tests for the config module."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mailroom.config import (
    Identity,
    ImapBlock,
    MailroomConfig,
    SmtpConfig,
    load_config,
    load_config_with_warnings,
)


class TestImapBlock:
    """Test cases for the ImapBlock class."""

    def test_init(self):
        """Test ImapBlock initialization."""
        block = ImapBlock(
            host="imap.example.com",
            port=993,
            username="test@example.com",
            password="password",
        )

        assert block.host == "imap.example.com"
        assert block.port == 993
        assert block.username == "test@example.com"
        assert block.password == "password"
        assert block.use_ssl is True

        block = ImapBlock(
            host="imap.example.com",
            port=143,
            username="test@example.com",
            password="password",
            use_ssl=False,
        )
        assert block.use_ssl is False

    def test_from_dict(self):
        """Test creating ImapBlock from a flat dictionary."""
        data = {
            "host": "imap.example.com",
            "port": 993,
            "username": "test@example.com",
            "password": "password",
            "use_ssl": True,
        }

        block = ImapBlock.from_dict(data)
        assert block.host == "imap.example.com"
        assert block.port == 993
        assert block.password == "password"
        assert block.use_ssl is True
        assert block.oauth2 is None

    def test_from_dict_oauth2(self):
        """Test creating ImapBlock with OAuth2 from a flat dictionary."""
        data = {
            "host": "imap.gmail.com",
            "username": "test@gmail.com",
            "client_id": "my_id",
            "client_secret": "my_secret",
            "refresh_token": "my_token",
        }

        block = ImapBlock.from_dict(data)
        assert block.host == "imap.gmail.com"
        assert block.oauth2 is not None
        assert block.oauth2.client_id == "my_id"
        assert block.password is None

    def test_from_dict_defaults(self):
        """Test that port defaults to 993 for SSL, 143 for non-SSL."""
        ssl_data = {"host": "imap.example.com", "username": "u", "password": "p"}
        assert ImapBlock.from_dict(ssl_data).port == 993

        non_ssl_data = {
            "host": "imap.example.com",
            "username": "u",
            "password": "p",
            "use_ssl": False,
        }
        assert ImapBlock.from_dict(non_ssl_data).port == 143

    def test_from_dict_global_defaults(self):
        """Test that global defaults are inherited."""
        data = {"host": "imap.example.com", "username": "u", "password": "p"}
        defaults = {"idle_timeout": 600, "verify_with_noop": False}

        block = ImapBlock.from_dict(data, defaults)
        assert block.idle_timeout == 600
        assert block.verify_with_noop is False

    def test_from_dict_block_overrides_global(self):
        """Test that per-block values override global defaults."""
        data = {
            "host": "imap.example.com",
            "username": "u",
            "password": "p",
            "idle_timeout": 60,
        }
        defaults = {"idle_timeout": 600}

        block = ImapBlock.from_dict(data, defaults)
        assert block.idle_timeout == 60

    def test_from_dict_with_env_password(self, monkeypatch):
        """Test creating ImapBlock with password from environment variable."""
        monkeypatch.setenv("IMAP_PASSWORD", "env_password")

        data = {"host": "imap.example.com", "username": "test@example.com"}
        block = ImapBlock.from_dict(data)
        assert block.password == "env_password"

        data_with_password = {
            "host": "imap.example.com",
            "username": "test@example.com",
            "password": "dict_password",
        }
        block = ImapBlock.from_dict(data_with_password)
        assert block.password == "dict_password"

    def test_from_dict_missing_password(self, monkeypatch):
        """Test error when password is missing from both dict and environment."""
        monkeypatch.delenv("IMAP_PASSWORD", raising=False)

        data = {"host": "imap.example.com", "username": "test@example.com"}

        with pytest.raises(ValueError, match="IMAP password must be specified"):
            ImapBlock.from_dict(data)

    def test_from_dict_missing_required_fields(self):
        """Test error when required fields are missing."""
        with pytest.raises(KeyError):
            ImapBlock.from_dict(
                {"username": "test@example.com", "password": "password"}
            )

        with pytest.raises(KeyError):
            ImapBlock.from_dict({"host": "imap.example.com", "password": "password"})

    def test_default_smtp_string(self):
        d = {
            "host": "imap.example.com",
            "username": "u@example.com",
            "password": "p",
            "default_smtp": "gmail",
        }
        block = ImapBlock.from_dict(d)
        assert block.default_smtp == "gmail"

    def test_default_smtp_must_be_string(self):
        d = {
            "host": "imap.example.com",
            "username": "u@example.com",
            "password": "p",
            "default_smtp": 42,
        }
        with pytest.raises(ValueError, match="'default_smtp' must be a string"):
            ImapBlock.from_dict(d, name="acc")

    def test_allowed_folders(self):
        d = {
            "host": "imap.example.com",
            "username": "u@example.com",
            "password": "p",
            "allowed_folders": ["INBOX", "Sent"],
        }
        block = ImapBlock.from_dict(d)
        assert block.allowed_folders == ["INBOX", "Sent"]

    def test_redact_compiles_policy(self, tmp_path):
        """A valid sieve file is parsed at config-load and stashed as a callable."""
        sieve_path = tmp_path / "rules.sieve"
        sieve_path.write_text(
            'require ["mailroom-policy"];\n' 'if address :is "from" "x@y" { redact; }\n'
        )
        d = {
            "host": "imap.example.com",
            "username": "u@example.com",
            "password": "p",
            "redact": str(sieve_path),
        }
        block = ImapBlock.from_dict(d)
        assert callable(block.redact_policy)

    def test_redact_relative_resolves_against_config_dir(self, tmp_path):
        """Relative redact paths resolve against the config directory."""
        (tmp_path / "rules.sieve").write_text(
            'require ["mailroom-policy"];\n' 'if address :is "from" "x@y" { redact; }\n'
        )
        d = {
            "host": "imap.example.com",
            "username": "u@example.com",
            "password": "p",
            "redact": "rules.sieve",
        }
        block = ImapBlock.from_dict(d, config_dir=tmp_path)
        assert callable(block.redact_policy)

    def test_redact_must_be_string(self):
        d = {
            "host": "imap.example.com",
            "username": "u@example.com",
            "password": "p",
            "redact": 42,
        }
        with pytest.raises(ValueError, match="'redact' must be a non-empty string"):
            ImapBlock.from_dict(d, name="acc")

    def test_redact_empty_fails_closed(self):
        d = {
            "host": "imap.example.com",
            "username": "u@example.com",
            "password": "p",
            "redact": "  ",
        }
        with pytest.raises(ValueError, match="'redact' must be a non-empty string"):
            ImapBlock.from_dict(d, name="acc")

    def test_redact_invalid_script_fails_closed(self, tmp_path):
        """A sieve file outside the supported subset fails the block at load."""
        sieve_path = tmp_path / "bad.sieve"
        sieve_path.write_text(
            'require ["body"];\n' 'if body :contains "secret" { redact; }\n'
        )
        d = {
            "host": "imap.example.com",
            "username": "u@example.com",
            "password": "p",
            "redact": str(sieve_path),
        }
        with pytest.raises(ValueError, match=r"\[imap\.acc\]: 'redact' invalid"):
            ImapBlock.from_dict(d, name="acc")


class TestMailroomConfig:
    """Test cases for MailroomConfig."""

    def test_default_imap_explicit(self):
        """Test explicit default_imap."""
        block_a = ImapBlock(host="h", port=993, username="u", password="p")
        block_b = ImapBlock(host="h", port=993, username="u", password="p")
        cfg = MailroomConfig(
            imap_blocks={"a": block_a, "b": block_b},
            _default_imap="b",
        )
        assert cfg.default_imap == "b"

    def test_default_imap_fallback(self):
        """Test default_imap falls back to first block."""
        block = ImapBlock(host="h", port=993, username="u", password="p")
        cfg = MailroomConfig(imap_blocks={"first": block})
        assert cfg.default_imap == "first"


class TestLoadConfig:
    """Test cases for the load_config function."""

    def test_load_imap_blocks(self):
        """Test loading [imap.NAME] blocks."""
        toml_content = """\
default_imap = "work"

[imap.personal]
host = "imap.gmail.com"
username = "me@gmail.com"
client_id = "cid"
client_secret = "csec"
refresh_token = "rtok"

[imap.work]
host = "imap.fastmail.com"
username = "me@company.com"
password = "secret"
"""
        with tempfile.NamedTemporaryFile(suffix=".toml", mode="wb") as f:
            f.write(toml_content.encode())
            f.flush()

            config = load_config(f.name)

            assert isinstance(config, MailroomConfig)
            assert config.default_imap == "work"
            assert "personal" in config.imap_blocks
            assert "work" in config.imap_blocks

            personal = config.imap_blocks["personal"]
            assert personal.host == "imap.gmail.com"
            assert personal.oauth2 is not None
            assert personal.oauth2.client_id == "cid"

            work = config.imap_blocks["work"]
            assert work.host == "imap.fastmail.com"
            assert work.password == "secret"
            assert work.oauth2 is None

    def test_load_global_defaults(self):
        """Test that global idle_timeout is inherited by blocks."""
        toml_content = """\
idle_timeout = 600

[imap.test]
host = "imap.example.com"
username = "u"
password = "p"
"""
        with tempfile.NamedTemporaryFile(suffix=".toml", mode="wb") as f:
            f.write(toml_content.encode())
            f.flush()

            config = load_config(f.name)
            assert config.imap_blocks["test"].idle_timeout == 600

    def test_load_from_default_locations(self, monkeypatch, tmp_path):
        """Test loading configuration from default locations."""
        for env_var in [
            "IMAP_HOST",
            "IMAP_PORT",
            "IMAP_USERNAME",
            "IMAP_PASSWORD",
            "IMAP_USE_SSL",
            "IMAP_ALLOWED_FOLDERS",
        ]:
            monkeypatch.delenv(env_var, raising=False)

        toml_content = """\
[imap.test]
host = "imap.example.com"
username = "test@example.com"
password = "password"
"""
        temp_dir = tmp_path / ".config" / "mailroom"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_file = temp_dir / "config.toml"
        temp_file.write_bytes(toml_content.encode())

        original_expanduser = Path.expanduser

        def mock_expanduser(self):
            if str(self) == "~/.config/mailroom/config.toml":
                return temp_file
            return original_expanduser(self)

        monkeypatch.setattr(Path, "expanduser", mock_expanduser)

        def mock_exists(path):
            return path == temp_file

        monkeypatch.setattr(Path, "exists", mock_exists)

        config = load_config()
        assert config.imap_blocks["test"].host == "imap.example.com"

    def test_load_from_env_variables(self, monkeypatch):
        """Test loading configuration from environment variables."""
        monkeypatch.setenv("IMAP_HOST", "imap.example.com")
        monkeypatch.setenv("IMAP_PORT", "993")
        monkeypatch.setenv("IMAP_USERNAME", "test@example.com")
        monkeypatch.setenv("IMAP_PASSWORD", "env_password")
        monkeypatch.setenv("IMAP_USE_SSL", "true")
        monkeypatch.setenv("IMAP_ALLOWED_FOLDERS", "INBOX,Sent,Archive")

        original_open = open

        def mock_open(*args, **kwargs):
            if args[0] == "nonexistent_file.toml":
                raise FileNotFoundError(f"No such file: {args[0]}")
            return original_open(*args, **kwargs)

        with patch("builtins.open", side_effect=mock_open):
            config = load_config("nonexistent_file.toml")

            block = config.imap_blocks["default"]
            assert block.host == "imap.example.com"
            assert block.password == "env_password"
            assert block.allowed_folders == ["INBOX", "Sent", "Archive"]

    def test_load_missing_required_env(self, monkeypatch):
        """Test error when required environment variables are missing."""
        monkeypatch.delenv("IMAP_HOST", raising=False)

        original_open = open

        def mock_open(*args, **kwargs):
            if args[0] == "nonexistent_file.toml":
                raise FileNotFoundError(f"No such file: {args[0]}")
            return original_open(*args, **kwargs)

        with patch("builtins.open", side_effect=mock_open):
            with pytest.raises(ValueError, match="IMAP_HOST"):
                load_config("nonexistent_file.toml")

    def test_invalid_config_missing_host(self):
        """Test error when config is missing required host."""
        toml_content = """\
[imap.test]
username = "test@example.com"
password = "password"
"""
        with tempfile.NamedTemporaryFile(suffix=".toml", mode="wb") as f:
            f.write(toml_content.encode())
            f.flush()

            with pytest.raises(ValueError, match="Missing required configuration"):
                load_config(f.name)

    def test_no_imap_blocks(self):
        """Test error when no [imap.*] blocks are defined."""
        toml_content = """\
idle_timeout = 300
"""
        with tempfile.NamedTemporaryFile(suffix=".toml", mode="wb") as f:
            f.write(toml_content.encode())
            f.flush()

            with pytest.raises(ValueError, match="No \\[imap.NAME\\] blocks defined"):
                load_config(f.name)


class TestSmtpConfig:
    """SmtpConfig parsing and defaulting."""

    def test_minimal_block(self):
        smtp = SmtpConfig.from_dict("gmail", {"host": "smtp.gmail.com"})
        assert smtp.host == "smtp.gmail.com"
        assert smtp.port == 587
        assert smtp.username is None and smtp.password is None
        assert smtp.save_sent == "auto"
        assert smtp.rewrite_msgid_from_response is False

    def test_ses_auto_rewrite(self):
        smtp = SmtpConfig.from_dict(
            "ses",
            {
                "host": "email-smtp.us-east-1.amazonaws.com",
                "username": "AKIA",
                "password": "x",
            },
        )
        assert smtp.rewrite_msgid_from_response is True

    def test_explicit_save_sent_true(self):
        smtp = SmtpConfig.from_dict(
            "fast", {"host": "smtp.fastmail.com", "save_sent": True}
        )
        assert smtp.save_sent is True

    def test_invalid_save_sent_value(self):
        with pytest.raises(ValueError, match="save_sent.*must be one of"):
            SmtpConfig.from_dict("x", {"host": "h", "save_sent": "yes"})

    def test_invalid_port_type(self):
        with pytest.raises(ValueError, match="'port' must be an integer"):
            SmtpConfig.from_dict("x", {"host": "h", "port": "587"})

    def test_missing_host(self):
        with pytest.raises(ValueError, match="missing required string field 'host'"):
            SmtpConfig.from_dict("x", {})

    def test_resolve_save_sent_auto_gmail(self):
        smtp = SmtpConfig(host="smtp.gmail.com")
        assert smtp.resolve_save_sent() is False

    def test_resolve_save_sent_auto_other(self):
        smtp = SmtpConfig(host="smtp.fastmail.com")
        assert smtp.resolve_save_sent() is True

    def test_resolve_save_sent_explicit_overrides_auto(self):
        smtp = SmtpConfig(host="smtp.gmail.com", save_sent=True)
        assert smtp.resolve_save_sent() is True


class TestIdentity:
    """Identity parsing."""

    def test_minimal(self):
        ident = Identity.from_dict("alice", {"imap": "work", "address": "x@y.com"})
        assert ident.imap == "work"
        assert ident.address == "x@y.com"
        assert ident.name == ""
        assert ident.smtp is None
        assert ident.fcc is None

    def test_full(self):
        ident = Identity.from_dict(
            "alice",
            {
                "imap": "work",
                "address": "x@y.com",
                "name": "X",
                "smtp": "ses",
                "fcc": "Sent",
            },
        )
        assert ident.imap == "work"
        assert ident.address == "x@y.com"
        assert ident.name == "X"
        assert ident.smtp == "ses"
        assert ident.fcc == "Sent"

    def test_missing_imap(self):
        with pytest.raises(ValueError, match="retains no copy of sent mail"):
            Identity.from_dict("alice", {"address": "x@y.com"})

    def test_missing_address(self):
        with pytest.raises(ValueError, match="missing or invalid 'address'"):
            Identity.from_dict("alice", {"imap": "work", "name": "X"})

    def test_invalid_address_no_at(self):
        with pytest.raises(ValueError, match="missing or invalid 'address'"):
            Identity.from_dict("alice", {"imap": "work", "address": "not-an-email"})

    def test_name_with_safe_chars_accepted(self):
        for name in ["Smith Jane", "Dr. Smith", "O'Brien", "Anne-Marie"]:
            ident = Identity.from_dict(
                "alice",
                {"imap": "work", "address": "x@y.com", "name": name},
            )
            assert ident.name == name

    def test_name_with_quoting_specials_rejected(self):
        for bad in [
            "Smith, Jane",
            "Smith (work)",
            "Sender; tag",
            "Smith <fake@x.com>",
            "user@host",
            'Bob "Bobby" Smith',
        ]:
            with pytest.raises(ValueError, match="requires RFC 5322 quoting"):
                Identity.from_dict(
                    "alice",
                    {"imap": "work", "address": "x@y.com", "name": bad},
                )

    def test_name_with_control_chars_rejected(self):
        for bad in ["foo\nbar", "foo\rbar", "foo\x00bar", "foo\x01bar", "foo\x7fbar"]:
            with pytest.raises(
                ValueError, match="requires RFC 5322 quoting|breaks MIME"
            ):
                Identity.from_dict(
                    "alice",
                    {"imap": "work", "address": "x@y.com", "name": bad},
                )

    def test_bcc_string_normalised_to_list(self):
        ident = Identity.from_dict(
            "alice",
            {"imap": "work", "address": "x@y.com", "bcc": "x@y.com"},
        )
        assert ident.bcc == ["x@y.com"]

    def test_bcc_list_kept(self):
        ident = Identity.from_dict(
            "alice",
            {"imap": "work", "address": "x@y.com", "bcc": ["x@y.com", "audit@y.com"]},
        )
        assert ident.bcc == ["x@y.com", "audit@y.com"]

    def test_bcc_without_imap_allowed(self):
        ident = Identity.from_dict(
            "alice",
            {"address": "x@y.com", "bcc": "x@y.com"},
        )
        assert ident.imap is None
        assert ident.bcc == ["x@y.com"]

    def test_neither_bcc_nor_imap_rejected(self):
        with pytest.raises(ValueError, match="retains no copy of sent mail"):
            Identity.from_dict("alice", {"address": "x@y.com"})

    def test_send_only_bcc_must_include_self(self):
        """A send-only identity (no imap) whose bcc omits its own address
        keeps no self-copy and is rejected."""
        with pytest.raises(ValueError, match="retains no copy of sent mail"):
            Identity.from_dict(
                "alice",
                {"address": "x@y.com", "bcc": "audit@y.com"},
            )

    def test_fcc_folder_string(self):
        ident = Identity.from_dict(
            "alice",
            {"imap": "work", "address": "x@y.com", "fcc": "Archive/Sent"},
        )
        assert ident.fcc == "Archive/Sent"

    def test_fcc_false_with_self_bcc_ok(self):
        ident = Identity.from_dict(
            "alice",
            {"imap": "work", "address": "x@y.com", "bcc": "x@y.com", "fcc": False},
        )
        assert ident.fcc is False
        assert ident.bcc == ["x@y.com"]

    def test_fcc_false_without_self_bcc_rejected(self):
        with pytest.raises(ValueError, match="self-inclusive 'bcc' is required"):
            Identity.from_dict(
                "alice",
                {"imap": "work", "address": "x@y.com", "fcc": False},
            )

    def test_fcc_false_with_third_party_bcc_rejected(self):
        """A bcc that does not include the identity's own address is not a
        self-copy, so fcc = false alongside it is still rejected."""
        with pytest.raises(ValueError, match="retains no copy of sent mail"):
            Identity.from_dict(
                "alice",
                {
                    "imap": "work",
                    "address": "x@y.com",
                    "bcc": "audit@y.com",
                    "fcc": False,
                },
            )

    def test_fcc_folder_without_imap_rejected(self):
        with pytest.raises(ValueError, match="no 'imap' block is set to APPEND"):
            Identity.from_dict(
                "alice",
                {"address": "x@y.com", "bcc": "x@y.com", "fcc": "Sent"},
            )

    def test_fcc_empty_string_rejected(self):
        with pytest.raises(ValueError, match="'fcc' folder name must not be empty"):
            Identity.from_dict(
                "alice",
                {"imap": "work", "address": "x@y.com", "fcc": ""},
            )

    def test_fcc_invalid_type_rejected(self):
        with pytest.raises(ValueError, match="'fcc' must be a folder name"):
            Identity.from_dict(
                "alice",
                {"imap": "work", "address": "x@y.com", "fcc": 42},
            )

    def test_bcc_invalid_type_rejected(self):
        with pytest.raises(ValueError, match="'bcc' must be a string or a list"):
            Identity.from_dict(
                "alice",
                {"imap": "work", "address": "x@y.com", "bcc": 42},
            )

    def test_bcc_empty_list_rejected(self):
        with pytest.raises(ValueError, match="'bcc' must not be empty"):
            Identity.from_dict(
                "alice",
                {"imap": "work", "address": "x@y.com", "bcc": []},
            )

    def test_bcc_non_email_rejected(self):
        with pytest.raises(ValueError, match="not an email address"):
            Identity.from_dict(
                "alice",
                {"imap": "work", "address": "x@y.com", "bcc": "no-at-sign"},
            )


class TestValidateDisplayName:
    """Direct tests for the standalone display-name validator.

    The same function is exercised end-to-end through ``Identity.from_dict``
    in TestIdentity above; these cases pin the contract of the helper as
    used by the CLI's ``--name`` flag in mode B.
    """

    def test_accepts_quote_free_name(self):
        from mailroom.config import validate_display_name

        for name in ["Smith Jane", "O'Brien", "Anne-Marie", "Dr. Smith", ""]:
            validate_display_name(name, "--name")

    def test_rejects_each_forbidden_class(self):
        from mailroom.config import validate_display_name

        for bad in ["a, b", "a (b)", "a@b", 'a "b"', "a\nb", "a\rb", "a\x00b"]:
            with pytest.raises(
                ValueError, match="requires RFC 5322 quoting|breaks MIME"
            ):
                validate_display_name(bad, "--name")

    def test_error_message_carries_caller_prefix(self):
        from mailroom.config import validate_display_name

        with pytest.raises(ValueError, match=r"\[identity\.alice\]"):
            validate_display_name("Bad, Name", "[identity.alice]")
        with pytest.raises(ValueError, match=r"--name"):
            validate_display_name("Bad, Name", "--name")


class TestSmtpHasOwnCreds:
    def test_true_when_both_set(self):
        from mailroom.config import smtp_has_own_creds

        assert smtp_has_own_creds(SmtpConfig(host="x", username="u", password="p"))

    def test_false_when_either_missing(self):
        from mailroom.config import smtp_has_own_creds

        assert not smtp_has_own_creds(SmtpConfig(host="x"))
        assert not smtp_has_own_creds(SmtpConfig(host="x", username="u"))
        assert not smtp_has_own_creds(SmtpConfig(host="x", password="p"))


class TestMailroomCrossRefs:
    """Cross-reference checks across [imap.*], [smtp.*], [identity.*]."""

    def _toml_with(self, content: str) -> "MailroomConfig":
        with tempfile.NamedTemporaryFile(suffix=".toml", mode="wb", delete=False) as f:
            f.write(content.encode())
            f.flush()
            return load_config(f.name)

    def test_default_smtp_undefined(self):
        with pytest.raises(
            ValueError, match="'default_smtp' references undefined \\[smtp.gmial\\]"
        ):
            self._toml_with("""\
[smtp.gmail]
host = "smtp.gmail.com"

[imap.a]
host = "imap.example.com"
username = "u@example.com"
password = "p"
default_smtp = "gmial"
""")

    def test_identity_smtp_undefined(self):
        with pytest.raises(ValueError, match="references undefined \\[smtp.nope\\]"):
            self._toml_with("""\
[smtp.gmail]
host = "smtp.gmail.com"

[imap.a]
host = "imap.example.com"
username = "u@example.com"
password = "p"

[identity.alice]
imap = "a"
address = "u@example.com"
smtp = "nope"
""")

    def test_identity_imap_undefined(self):
        with pytest.raises(
            ValueError, match="'imap' references undefined \\[imap.nope\\]"
        ):
            self._toml_with("""\
[imap.a]
host = "imap.example.com"
username = "u@example.com"
password = "p"

[identity.alice]
imap = "nope"
address = "u@example.com"
""")

    def test_default_imap_undefined(self):
        with pytest.raises(ValueError, match="default_imap 'nope' is not a defined"):
            self._toml_with("""\
default_imap = "nope"

[imap.a]
host = "imap.example.com"
username = "u@example.com"
password = "p"
""")

    def test_duplicate_address_same_imap(self):
        with pytest.raises(ValueError, match="address 'a@x.com' already declared"):
            self._toml_with("""\
[imap.a]
host = "imap.example.com"
username = "u@example.com"
password = "p"

[identity.first]
imap = "a"
address = "a@x.com"

[identity.second]
imap = "a"
address = "A@X.COM"
""")

    def test_same_address_different_imap_allowed(self):
        cfg = self._toml_with("""\
[imap.a]
host = "imap.example.com"
username = "u1@example.com"
password = "p1"

[imap.b]
host = "imap.example.com"
username = "u2@example.com"
password = "p2"

[identity.shared_a]
imap = "a"
address = "support@example.com"

[identity.shared_b]
imap = "b"
address = "support@example.com"
""")
        assert "shared_a" in cfg.identities
        assert "shared_b" in cfg.identities
        assert cfg.identities["shared_a"].imap == "a"
        assert cfg.identities["shared_b"].imap == "b"

    def test_smtp_blocks_parsed(self):
        cfg = self._toml_with("""\
[smtp.gmail]
host = "smtp.gmail.com"

[smtp.ses]
host = "email-smtp.example.com"
username = "AKIA"
password = "x"

[imap.a]
host = "imap.example.com"
username = "u@example.com"
password = "p"
default_smtp = "gmail"
""")
        assert sorted(cfg.smtp_blocks) == ["gmail", "ses"]
        assert cfg.smtp_blocks["ses"].rewrite_msgid_from_response is False


class TestConfigWarnings:
    """Non-fatal warnings collected on the config object."""

    def _toml_with(self, content: str) -> "MailroomConfig":
        with tempfile.NamedTemporaryFile(suffix=".toml", mode="wb", delete=False) as f:
            f.write(content.encode())
            f.flush()
            return load_config(f.name)

    def test_no_smtp_blocks_warns(self):
        cfg = self._toml_with("""\
[imap.a]
host = "imap.example.com"
username = "u@example.com"
password = "p"
""")
        assert any("no [smtp.*] blocks defined" in w for w in cfg.warnings)

    def test_imap_block_with_no_identities_warns(self):
        cfg = self._toml_with("""\
[smtp.gmail]
host = "smtp.gmail.com"

[imap.read_only]
host = "imap.example.com"
username = "u@example.com"
password = "p"
""")
        assert any("sending from this block is disabled" in w for w in cfg.warnings)

    def test_block_with_identity_no_send_warning(self):
        cfg = self._toml_with("""\
[smtp.gmail]
host = "smtp.gmail.com"

[imap.a]
host = "imap.example.com"
username = "u@example.com"
password = "p"

[identity.alice]
imap = "a"
address = "u@example.com"
""")
        assert not any("sending from this block is disabled" in w for w in cfg.warnings)

    def test_shared_credless_non_gmail_warns(self):
        cfg = self._toml_with("""\
[smtp.fast]
host = "smtp.fastmail.com"

[imap.a]
host = "imap.fastmail.com"
username = "a@x.com"
password = "p1"
default_smtp = "fast"

[imap.b]
host = "imap.fastmail.com"
username = "b@x.com"
password = "p2"
default_smtp = "fast"

[identity.alice]
imap = "a"
address = "a@x.com"

[identity.bob]
imap = "b"
address = "b@x.com"
""")
        assert any("no creds and shared by [imap.*] blocks" in w for w in cfg.warnings)

    def test_bcc_only_identity_loads_cleanly_with_lone_smtp(self):
        """Regression: a bcc-only identity (no [imap.*] reference) must
        not crash _collect_warnings. With exactly one SMTP block, the
        identity resolves smtp via the lone-fallback rule and emits no
        warning."""
        cfg = self._toml_with("""\
[smtp.relay]
host = "relay.example.com"
username = "u"
password = "p"

[imap.a]
host = "imap.example.com"
username = "u@example.com"
password = "p"

[identity.alice]
imap = "a"
address = "alice@example.com"

[identity.bccself]
address = "bccself@example.com"
bcc = "bccself@example.com"
""")
        assert "bccself" in cfg.identities
        assert cfg.identities["bccself"].imap is None
        assert not any("bccself" in w for w in cfg.warnings)

    def test_bcc_only_identity_with_explicit_smtp_loads_cleanly(self):
        """A bcc-only identity that names its own smtp resolves cleanly
        even when multiple SMTP blocks exist."""
        cfg = self._toml_with("""\
[smtp.relay1]
host = "r1.example.com"
username = "u1"
password = "p1"

[smtp.relay2]
host = "r2.example.com"
username = "u2"
password = "p2"

[imap.a]
host = "imap.example.com"
username = "u@example.com"
password = "p"

[identity.alice]
imap = "a"
address = "alice@example.com"

[identity.bccself]
address = "bccself@example.com"
smtp = "relay1"
bcc = "bccself@example.com"
""")
        assert not any("bccself" in w for w in cfg.warnings)

    def test_bcc_only_identity_without_smtp_path_warns(self):
        """A bcc-only identity with multiple SMTP blocks and no explicit
        smtp can't resolve a route. _collect_warnings must surface this
        rather than crash."""
        cfg = self._toml_with("""\
[smtp.relay1]
host = "r1.example.com"
username = "u1"
password = "p1"

[smtp.relay2]
host = "r2.example.com"
username = "u2"
password = "p2"

[imap.a]
host = "imap.example.com"
username = "u@example.com"
password = "p"

[identity.alice]
imap = "a"
address = "alice@example.com"
smtp = "relay1"

[identity.bccself]
address = "bccself@example.com"
bcc = "bccself@example.com"
""")
        assert any(
            "bccself" in w and "no [imap.*] block to inherit" in w for w in cfg.warnings
        )

    def test_shared_credless_gmail_does_not_warn(self):
        cfg = self._toml_with("""\
[smtp.gmail]
host = "smtp.gmail.com"

[imap.a]
host = "imap.gmail.com"
username = "a@gmail.com"
password = "p1"
default_smtp = "gmail"

[imap.b]
host = "imap.gmail.com"
username = "b@gmail.com"
password = "p2"
default_smtp = "gmail"

[identity.alice]
imap = "a"
address = "a@gmail.com"

[identity.bob]
imap = "b"
address = "b@gmail.com"
""")
        assert cfg.warnings == []


class TestLoadConfigWithWarnings:
    """The (cfg, warnings) tuple wrapper."""

    def test_returns_tuple(self):
        toml_content = """\
[smtp.gmail]
host = "smtp.gmail.com"

[imap.a]
host = "imap.example.com"
username = "u@example.com"
password = "p"
default_smtp = "gmail"

[identity.alice]
imap = "a"
address = "u@example.com"
"""
        with tempfile.NamedTemporaryFile(suffix=".toml", mode="wb") as f:
            f.write(toml_content.encode())
            f.flush()
            cfg, warnings = load_config_with_warnings(f.name)
        assert isinstance(cfg, MailroomConfig)
        assert warnings is cfg.warnings
