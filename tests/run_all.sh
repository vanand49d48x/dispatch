#!/usr/bin/env bash
# Simulated end-to-end tests: fake agents drive the real orchestrator, DB, tools and Discord-free notifier.
# No API keys, network or Docker needed.  Usage: ./tests/run_all.sh
cd "$(dirname "$0")/.."
fail=0
for t in tests/test_*.py; do
  printf "%-40s" "$t"
  if out=$(timeout 90 python "$t" 2>&1) && echo "$out" | grep -q " OK"; then echo "✔"; else echo "✘"; echo "$out" | tail -15; fail=1; fi
done
exit $fail
