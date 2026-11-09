"""Serve a Workspace toolset over standard MCP stdio."""

import argparse
import asyncio
import logging

from mcp.server.stdio import stdio_server

from . import SERVICES
from .server import build_server


async def run(service):
    server = build_server(service)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", choices=SERVICES, required=True)
    asyncio.run(run(parser.parse_args().service))
