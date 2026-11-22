"""Install the pinned Canvas MCP (canvas-mcp), keep a Quercus access token in Keychain, and register it read-only.

    uv run python -m scripts.setup_canvas            # install, ask for the token (hidden), register, test
    uv run python -m scripts.setup_canvas --test     # check the stored token against Canvas
    uv run python -m scripts.setup_canvas --update   # refresh the registered tool list after an upgrade

The token never touches disk: it goes to Keychain (service "Luna MCP", or "Luna MCP (dev)" from a checkout) and into the server process's environment
only. The server runs in the student profile with every Canvas write tool disabled, and Luna publishes only the
read tools listed below.
"""

import argparse
import getpass

from src.install.canvas import TOOLS, adapter, install_server, register, test  # noqa: F401  (TOOLS: --update's list)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--test", action="store_true", help="Only check the stored token")
    parser.add_argument("--update", action="store_true", help="Only refresh the registered entry's tool list")
    args = parser.parse_args()
    if args.test:
        works = test()
        print("Canvas connection works." if works else "Canvas didn't accept the token (or Quercus is unreachable).")
        raise SystemExit(0 if works else 1)
    if args.update:
        print({"updated": "Updated the Canvas integration.", "unchanged": "The Canvas integration is up to date.",
               "registered": "Registered the Canvas integration."}[register()])
        return
    install_server()
    print("Create a token in Quercus: Account → Settings → Approved Integrations → + New Access Token.")
    token = getpass.getpass("Paste the Quercus access token (hidden): ")
    adapter().save(token)
    print("Token saved to Keychain.")
    print({"updated": "Updated the Canvas integration.", "unchanged": "The Canvas integration was already registered.",
           "registered": "Registered the Canvas integration."}[register()])
    print("Canvas connection works." if test() else "Canvas didn't accept the token (or Quercus is unreachable).")


if __name__ == "__main__":
    main()
