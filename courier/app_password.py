"""Gmail app password authentication setup."""

import logging
import os
import sys
from typing import Dict

logger = logging.getLogger(__name__)


def setup_app_password(
    username: str,
    password: str,
) -> Dict:
    """Set up Gmail with an app password.

    Args:
        username: Gmail email address
        password: App password generated from Google Account

    Returns:
        Configuration dictionary
    """
    config_data = {
        "imap": {
            "host": "imap.gmail.com",
            "port": 993,
            "username": username,
            "password": password,
            "use_ssl": True,
        }
    }

    print("\nAdd the following to your config.toml:\n")
    print("[imap]")
    print('host = "imap.gmail.com"')
    print("port = 993")
    print(f'username = "{username}"')
    print(f'password = "{password}"')
    print("use_ssl = true")
    print()
    print("Or set the environment variable:")
    print(f"  IMAP_PASSWORD={password}")

    return config_data


def main() -> None:
    """Run the Gmail app password setup tool."""
    import argparse

    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description="Configure Gmail with app password")
    parser.add_argument(
        "--username",
        help="Gmail email address",
        default=os.environ.get("GMAIL_USERNAME"),
    )
    parser.add_argument(
        "--password",
        help="App password from Google Account",
        default=os.environ.get("GMAIL_APP_PASSWORD") or os.environ.get("IMAP_PASSWORD"),
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(level=logging.INFO)

    # Prompt for username if not provided
    username = args.username
    if not username:
        username = input("Enter your Gmail address: ").strip()
        if not username:
            print("Error: Gmail address is required")
            sys.exit(1)

    # Prompt for password if not provided
    password = args.password
    if not password:
        import getpass

        password = getpass.getpass("Enter your Gmail app password: ").strip()
        if not password:
            print("Error: App password is required")
            sys.exit(1)

    try:
        setup_app_password(
            username=username,
            password=password,
        )
        logger.info("Gmail app password setup completed successfully")
    except Exception as e:
        logger.error(f"Gmail app password setup failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
