import argparse
import sys

from loguru import logger

from src.ui.app import run_app


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    parser = argparse.ArgumentParser(description="Luna for Mac")
    parser.add_argument("--preview", action="store_true", help="Interactive UI preview without microphone or Gemini")
    run_app(preview=parser.parse_args().preview)
