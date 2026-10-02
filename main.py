import asyncio
import sys

from loguru import logger

from src.agent.conversation import run_conversation


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    asyncio.run(run_conversation())
