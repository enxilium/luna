"""Import a Google web client into Keychain and configure Workspace MCP services (see src/install/google.py).

    uv run python -m scripts.setup_google ~/Downloads/client_secret_….json   # import (the file moves into Luna's folder)
    uv run python -m scripts.setup_google --backend local|hosted             # switch the Workspace transport
"""

import argparse
import asyncio
from pathlib import Path

from src.install.google import AuthFailure, setup, use_backend


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "credentials",
        type=Path,
        nargs="?",
        help="Downloaded Google OAuth web-client JSON",
    )
    parser.add_argument("--backend", choices=("local", "hosted"), default="local")
    try:
        args = parser.parse_args()
        if args.credentials:
            asyncio.run(setup(args.credentials, backend=args.backend))
        else:
            backup = use_backend(args.backend)
            print(
                f"Workspace uses the {args.backend} backend."
                + (f" Previous config: {backup}" if backup else "")
            )
    except (AuthFailure, OSError):
        parser.exit(
            1,
            "Google setup failed. Check the credential file, redirect, Keychain, and existing configuration.\n",
        )
