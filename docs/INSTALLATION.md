# Mailroom Installation Guide

## Prerequisites

- Python 3.11 or higher
- An IMAP-enabled email account

## Installation Methods

### uv (any platform, no install step)

```bash
uvx mailroom search "subject:invoice"
```

To install permanently: `uv tool install mailroom`

### pipx from GitHub (all features, all platforms)

```bash
pipx install "mailroom[mcp] @ git+https://github.com/SmartLayer/mailroom"
```

This installs everything including MCP server mode. For CLI-only (no MCP):

```bash
pipx install "git+https://github.com/SmartLayer/mailroom"
```

### Homebrew (all features, macOS and Linux)

```bash
brew tap overseers-desk/ot
brew install mailroom
```

### Debian / Ubuntu (CLI-only)

Download the latest `.deb` from the [Releases](https://github.com/SmartLayer/mailroom/releases) page and install:

```bash
sudo apt install ./mailroom_*_all.deb
```

The .deb provides all CLI commands. The MCP server subcommand (`mailroom mcp`) is not supported in the .deb — it requires the `mcp` Python package which is not yet available as a Debian package. Users who need MCP mode should install via pipx or Homebrew.

To build the .deb from source:

```bash
sudo apt install debhelper dh-python pybuild-plugin-pyproject
dpkg-buildpackage -us -uc -b
# produces ../mailroom_<version>_all.deb
```

### RPM / Fedora (CLI-only)

Build from the included spec file:

```bash
rpmbuild -ba mailroom.spec
```

Or use `fpm` for a quick build:

```bash
fpm -s python -t rpm --python-bin python3 --python-pip pip3 \
    --depends python3-imapclient --depends python3-typer \
    --depends python3-requests --depends python3-dotenv \
    .
```

## Upgrading

### Homebrew (macOS / Linux)

```bash
brew update
brew upgrade mailroom
```

`brew upgrade mailroom` alone reports "already installed and up-to-date" because Homebrew's auto-update does not pull third-party taps. Run `brew update` first so the `overseers-desk/ot` tap fetches the new formula.

### Debian / Ubuntu

Download the new `.deb` from the [Releases](https://github.com/SmartLayer/mailroom/releases) page and re-run the install command. apt handles the upgrade transparently when the version is higher:

```bash
sudo apt install ./mailroom_<version>_all.deb
```

### RPM / Fedora

Same shape as install: dnf upgrades when the file's version is higher than the installed one:

```bash
sudo dnf install ./mailroom-<version>-1.noarch.rpm
```

### pipx / uv

```bash
pipx upgrade mailroom        # if installed via pipx
uv tool upgrade mailroom     # if installed via uv tool install
```

## Configuration

Copy the sample configuration and edit with your credentials:

```bash
mkdir -p ~/.config/mailroom
cp examples/config.sample.toml ~/.config/mailroom/config.toml
```

Example configuration:

```toml
default_imap = "personal"

[imap.personal]
host = "imap.gmail.com"
username = "you@gmail.com"
client_id = "YOUR_CLIENT_ID"
client_secret = "YOUR_CLIENT_SECRET"
refresh_token = "YOUR_REFRESH_TOKEN"

[identity.personal]
imap = "personal"
address = "you@gmail.com"
```

For password-based authentication, set the password in the config or via environment variable:

```bash
export IMAP_PASSWORD="your_secure_password"
```

## Usage

### CLI commands

```bash
mailroom search "from:alice" search 'subject:"hotel booking"' search "is:unread"
mailroom search "from:alice subject:invoice"
mailroom list
mailroom move -f INBOX -u 123 -t Archive
mailroom reply -f INBOX -u 123 -b "Thanks for the update."
```

Run `mailroom --help` for the full list of commands, or `man mailroom` on systems installed via .deb.

### MCP server (pipx / Homebrew installs only)

```bash
mailroom mcp --config /path/to/config.toml
```

### Integrating with Claude Desktop

Add to your Claude Desktop MCP configuration:

```json
{
  "mcpServers": {
    "mailroom": {
      "command": "mailroom",
      "args": ["mcp", "--config", "/path/to/config.toml"],
      "env": {
        "IMAP_PASSWORD": "your_secure_password"
      }
    }
  }
}
```

## Troubleshooting

1. Verify your IMAP server settings are correct
2. Check that your email provider allows IMAP access
3. For Gmail, use OAuth2 credentials (app passwords work but are less reliable)
4. Enable debug mode (`--verbose`) for detailed logs
5. For authentication errors with OAuth2, refresh your token: `mailroom auth refresh-token --config config.toml`
