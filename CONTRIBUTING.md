# Contributing

1. `pip install -r requirements.txt`
2. Run the simulated test suite (no keys or network needed): `./tests/run_all.sh`
3. Keep changes small and add a test in `tests/` that drives the orchestrator with a fake runner (see
   `tests/test_escalation.py` for the pattern).

Good first areas: Slack/Telegram notifiers (subclass `harness/notify.py:Notifier`), per-task git worktrees,
more preflight checks, Neon as an alternative database provider.
