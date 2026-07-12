# Courier Installation Guide

## Prerequisites

- Python 3.11 or higher
- An IMAP-enabled email account

## Installation Methods

### PyPI (pip or pipx, any platform)

```bash
pip install courier            # CLI only
pip install "courier[mcp]"     # CLI plus MCP server mode
```

For an isolated install that does not touch your system or project environment, use pipx: `pipx install courier` (or `pipx install "courier[mcp]"` for MCP mode).

### uv (any platform, no install step)

```bash
uvx courier search "subject:invoice"
```

To install permanently: `uv tool install courier`

### pipx from GitHub (latest unreleased build)

To track the development version ahead of the latest PyPI release, install straight from the repository:

```bash
pipx install "courier[mcp] @ git+https://github.com/overseers-desk/courier"
```

This installs everything including MCP server mode. For CLI-only (no MCP):

```bash
pipx install "git+https://github.com/overseers-desk/courier"
```

### Homebrew (all features, macOS and Linux)

```bash
brew tap overseers-desk/od
brew install courier
```

### Debian / Ubuntu (CLI-only)

Download the latest `.deb` from the [Releases](https://github.com/overseers-desk/courier/releases) page and install:

```bash
sudo apt install ./courier_*_all.deb
```

The .deb provides all CLI commands. The MCP server subcommand (`courier mcp`) is not supported in the .deb: it requires the `mcp` Python package, which is not yet available as a Debian package. Users who need MCP mode should install via pipx or Homebrew.

To build the .deb from source:

```bash
sudo apt install debhelper dh-python pybuild-plugin-pyproject
dpkg-buildpackage -us -uc -b
# produces ../courier_<version>_all.deb
```

### RPM / Fedora (CLI-only)

Download the latest `.rpm` from the [Releases](https://github.com/overseers-desk/courier/releases) page and install:

```bash
sudo dnf install ./courier-<version>-1.noarch.rpm
```

Or build from the included spec file:

```bash
rpmbuild -ba courier.spec
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
brew upgrade courier
```

`brew upgrade courier` alone reports "already installed and up-to-date" because Homebrew's auto-update does not pull third-party taps. Run `brew update` first so the `overseers-desk/od` tap fetches the new formula.

### Debian / Ubuntu

Download the new `.deb` from the [Releases](https://github.com/overseers-desk/courier/releases) page and re-run the install command. apt handles the upgrade transparently when the version is higher:

```bash
sudo apt install ./courier_<version>_all.deb
```

### RPM / Fedora

Same shape as install: dnf upgrades when the file's version is higher than the installed one:

```bash
sudo dnf install ./courier-<version>-1.noarch.rpm
```

### pip / pipx / uv

```bash
pip install --upgrade courier   # if installed via pip
pipx upgrade courier            # if installed via pipx
uv tool upgrade courier         # if installed via uv tool install
```

## Configuration

Write the sample configuration and edit with your credentials:

```bash
mkdir -p ~/.config/courier
courier config-sample > ~/.config/courier/config.toml
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
courier search "from:alice" search 'subject:"hotel booking"' search "is:unread"
courier search "from:alice subject:invoice"
courier list
courier move -f INBOX -u 123 -t Archive
courier reply -f INBOX -u 123 -b "Thanks for the update."
```

Run `courier --help` for the full list of commands, or `man courier` on systems installed via .deb.

### MCP server (pipx / Homebrew installs only)

```bash
courier mcp --config /path/to/config.toml
```

### Integrating with Claude Desktop

Add to your Claude Desktop MCP configuration:

```json
{
  "mcpServers": {
    "courier": {
      "command": "courier",
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
5. For authentication errors with OAuth2, refresh your token: `courier auth refresh-token --config config.toml`
