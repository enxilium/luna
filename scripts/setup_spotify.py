"""Build the pinned Spotify MCP and configure it from .env. No Spotify actions or login (see src/install/spotify.py)."""

from src.install.spotify import setup


if __name__ == "__main__":
    setup()
    print("Spotify MCP is built and configured. Start Luna to connect and sign in.")
