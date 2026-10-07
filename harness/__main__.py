"""Entry point.

  python -m harness                       # run with Discord (normal mode)
  python -m harness --cli "my SaaS idea"  # local test mode: messages in the terminal, no Discord
  python -m harness --cli "Teachloop" --spec SPEC.md --budget 150
  python -m harness --check               # validate config.yaml and tool access, then exit
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys

from .config import ConfigError, load_config
from .db import DB
from .notify import ConsoleNotifier
from .orchestrator import Orchestrator


def _setup_logging() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("discord").setLevel(logging.WARNING)


async def _check(cfg) -> int:
    from .preflight import run_preflight
    print("Checking config.yaml and every account…")
    report = await run_preflight(cfg)
    print(report.text())
    return 1 if report.failed else 0


async def _run_cli(cfg, idea: str, spec_doc: str = "", budget: float | None = None) -> None:
    db = await DB(cfg.db_path).open()
    notifier = ConsoleNotifier()
    orch = Orchestrator(cfg, db, notifier)
    await orch.start()

    async def on_message(thread_id: int, text: str) -> None:
        if not await orch.handle_owner_message(thread_id, text):
            await orch.new_project(text)

    stdin_task = asyncio.create_task(notifier.stdin_loop(on_message))
    pid = await orch.new_project(idea, spec_doc=spec_doc, budget=budget)
    # Keep running across milestones: type "next" to continue, Ctrl-C to quit.
    while True:
        while orch.loops.get(pid) and not orch.loops[pid].done():
            await asyncio.sleep(2)
        if (await db.project(pid))["status"] != "milestone_done":
            break
        await asyncio.sleep(2)
    print(await orch.status_text(pid))
    stdin_task.cancel()
    await db.close()


def _run_discord(cfg) -> None:
    import time
    from .discord_bot import ForgeBot
    from .preflight import run_preflight

    report = asyncio.run(run_preflight(cfg))
    print(report.text(), flush=True)
    if report.failed:
        # Don't start half-configured. Wait before exiting so Docker's auto-restart doesn't spin.
        print("Not starting: fix config.yaml, then `docker compose restart`.", flush=True)
        time.sleep(300)
        sys.exit(1)

    async def make_orch(notifier):
        db = await DB(cfg.db_path).open()
        return Orchestrator(cfg, db, notifier)

    bot = ForgeBot(cfg, make_orch, warnings=[f"{w.area}: {w.name} - {w.detail}" for w in report.warnings])
    bot.run(cfg.get("discord.bot_token"), log_handler=None)


def main() -> None:
    ap = argparse.ArgumentParser(prog="dispatch")
    ap.add_argument("--config", default=os.environ.get("AGENTFORGE_CONFIG", "config.yaml"))
    ap.add_argument("--cli", metavar="IDEA", help="run one project in the terminal (no Discord)")
    ap.add_argument("--spec", metavar="FILE", help="spec file (markdown) for --cli mode")
    ap.add_argument("--budget", type=float, help="USD cap for the --cli project")
    ap.add_argument("--check", action="store_true", help="validate config and credentials")
    args = ap.parse_args()
    _setup_logging()
    try:
        cfg = load_config(args.config, require_discord=not (args.cli or args.check), strict=not args.check)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        sys.exit(2)
    if args.check:
        sys.exit(asyncio.run(_check(cfg)))
    if args.cli:
        spec = open(args.spec).read() if args.spec else ""
        asyncio.run(_run_cli(cfg, args.cli, spec, args.budget))
    else:
        _run_discord(cfg)


if __name__ == "__main__":
    main()
