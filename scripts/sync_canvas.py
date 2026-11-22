"""Bring the active courses in step with Quercus now (the dashboard also does this at launch and every 15 minutes).

    uv run python -m scripts.sync_canvas

Needs the Canvas integration from `scripts.setup_canvas`. Read-only on the Canvas side: assignments and quizzes become
course tasks (submitted ones are checked off), announcements are mirrored into their course.
"""

import asyncio

from src.dashboard.canvas import Quercus
from src.dashboard.store import Store

REASONS = {"not_set_up": "Quercus isn't set up yet: run `uv run python -m scripts.setup_canvas` first.",
           "token_setup_required": "There's no Quercus token: run `uv run python -m scripts.setup_canvas`.",
           "token_rejected": "Quercus rejected the token: make a new one and run `uv run python -m scripts.setup_canvas`."}


async def main():
    store = Store()
    try:
        status = await Quercus(store).sync()
    finally:
        store.close()
    if not status["ok"]:
        raise SystemExit(REASONS.get(status["reason"], f"Couldn't read Quercus ({status['reason']})."))
    counts = {k: v for k, v in status.items() if k not in ("ok", "at") and v}
    print(", ".join(f"{n} {k}" for k, n in counts.items()) or "Already in step with Quercus.")


if __name__ == "__main__":
    asyncio.run(main())
