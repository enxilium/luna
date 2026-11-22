import argparse
import os
from pathlib import Path
import sys

from loguru import logger


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    parser = argparse.ArgumentParser(description="Luna for Mac")
    parser.add_argument("--preview", action="store_true", help="The notch alone, without microphone, Gemini or dashboard")
    parser.add_argument("--dashboard", action="store_true", help="The dashboard alone, shown at once (development)")
    parser.add_argument("--demo", action="store_true", help="Give the dashboard seeded sample data, never your own")
    parser.add_argument("--home", metavar="FOLDER", help="Keep all of Luna's data (and its Gemini key) in FOLDER instead of "
                        "~/Library/Application Support/Luna, e.g. to go through setup as a new user")
    args = parser.parse_args()
    if args.home:
        os.environ["LUNA_HOME"] = str(Path(args.home).expanduser().resolve())  # before anything reads src.paths
    if args.preview and (args.dashboard or args.demo):
        parser.error("--preview runs the notch alone")
    if args.dashboard:
        from src.dashboard.window import run_dashboard

        run_dashboard(demo=args.demo)
    else:
        from src.ui.app import run_app

        run_app(preview=args.preview, demo=args.demo)
